"""
neuralis.agents.bus
===================
In-process pub/sub message bus for agents on the same node.

The ``AgentBus`` lets agents communicate with each other without going
through the mesh.  It is the intra-node counterpart to the inter-node
``MeshHost`` from Module 2.

Architecture
------------
- Agents subscribe to a topic string (e.g. "summarise", "store.put", "ping")
- Publishers post an ``AgentMessage`` to a topic
- The bus delivers the message to all subscribers for that topic
- Delivery is async — each handler is awaited in the order it subscribed
- Dead-letter queue captures messages with no matching subscriber
- One bus instance is shared across all agents on a node

Topic naming convention
-----------------------
  "<capability>"           — e.g. "echo", "summarise"
  "<agent_name>.*"         — direct addressing, e.g. "echo.*"
  "broadcast"              — all agents receive this
  "*"                      — wildcard, matches any topic

Usage
-----
    bus = AgentBus()

    # Subscribe
    async def my_handler(msg: AgentMessage) -> None:
        print(msg.payload)

    bus.subscribe("summarise", my_handler)

    # Publish
    msg = AgentMessage(target="summarise", task="summarise", payload={"text": "..."})
    responses = await bus.publish(msg)

    # Request/reply (publish to one agent, wait for response)
    response = await bus.request("echo", msg, timeout=5.0)
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional, Tuple

from neuralis.agents.base import AgentMessage, AgentResponse

logger = logging.getLogger(__name__)

# Type alias for async message handler
Handler = Callable[[AgentMessage], Awaitable[Optional[AgentResponse]]]

# Maximum dead-letter queue depth
DLQ_MAX = 256


# ---------------------------------------------------------------------------
# AgentBus
# ---------------------------------------------------------------------------

class AgentBus:
    """
    Async in-process pub/sub bus for agent communication.

    All operations are coroutine-safe.  The bus is not thread-safe by itself;
    it is always accessed from the asyncio event loop.
    """

    def __init__(self) -> None:
        self._handlers:    Dict[str, List[Tuple[str, Handler]]] = defaultdict(list)
        # topic → list of (subscriber_id, handler)
        self._dead_letters: Deque[AgentMessage] = deque(maxlen=DLQ_MAX)
        self._published:   int = 0
        self._delivered:   int = 0
        self._dead_count:  int = 0

    # ------------------------------------------------------------------
    # Subscribe / Unsubscribe
    # ------------------------------------------------------------------

    def subscribe(self, topic: str, handler: Handler, subscriber_id: str = "") -> None:
        """
        Register ``handler`` to receive messages posted to ``topic``.

        Parameters
        ----------
        topic         : exact topic string (e.g. "echo", "broadcast")
        handler       : async callable taking AgentMessage, returning
                        AgentResponse | None
        subscriber_id : optional label for this subscription (for unsubscribe)
        """
        sid = subscriber_id or f"sub_{len(self._handlers[topic])}"
        self._handlers[topic].append((sid, handler))
        logger.debug("AgentBus: %s subscribed to '%s'", sid, topic)

    def unsubscribe(self, topic: str, subscriber_id: str) -> bool:
        """
        Remove the subscription identified by ``subscriber_id`` from ``topic``.

        Returns True if the subscription was found and removed.
        """
        before = len(self._handlers[topic])
        self._handlers[topic] = [
            (sid, h) for sid, h in self._handlers[topic]
            if sid != subscriber_id
        ]
        removed = len(self._handlers[topic]) < before
        if removed:
            logger.debug("AgentBus: unsubscribed %s from '%s'", subscriber_id, topic)
        return removed

    def unsubscribe_all(self, subscriber_id: str) -> int:
        """
        Remove all subscriptions for ``subscriber_id`` across all topics.

        Returns the number of subscriptions removed.
        """
        count = 0
        for topic in list(self._handlers.keys()):
            before = len(self._handlers[topic])
            self._handlers[topic] = [
                (sid, h) for sid, h in self._handlers[topic]
                if sid != subscriber_id
            ]
            count += before - len(self._handlers[topic])
        return count

    # ------------------------------------------------------------------
    # Publish
    # ------------------------------------------------------------------

    async def publish(self, message: AgentMessage) -> List[AgentResponse]:
        """
        Deliver ``message`` to all subscribers of ``message.target``.

        Also delivers to "broadcast" subscribers and "*" (wildcard) subscribers.

        Returns the list of non-None AgentResponse objects returned by handlers.
        Dead-letters the message if no handlers are found.
        """
        self._published += 1

        if message.is_expired():
            logger.debug("AgentBus: dropping expired message %s", message.message_id[:8])
            return []

        topics = self._topics_for(message.target)
        all_handlers: List[Tuple[str, Handler]] = []

        for topic in topics:
            all_handlers.extend(self._handlers.get(topic, []))

        if not all_handlers:
            self._dead_letters.append(message)
            self._dead_count += 1
            logger.debug(
                "AgentBus: no handlers for target='%s' — dead-lettered (total=%d)",
                message.target, self._dead_count,
            )
            return []

        responses: List[AgentResponse] = []
        for sid, handler in all_handlers:
            try:
                result = await handler(message)
                self._delivered += 1
                if result is not None:
                    responses.append(result)
            except Exception as exc:
                logger.error(
                    "AgentBus: handler %s raised for message %s: %s",
                    sid, message.message_id[:8], exc,
                )

        return responses

    async def request(
        self,
        target: str,
        message: AgentMessage,
        timeout: float = 10.0,
    ) -> Optional[AgentResponse]:
        """
        Publish a message and wait for exactly one response.

        Delivers to the first handler registered for ``target`` and returns
        its response.  Raises ``asyncio.TimeoutError`` if no response arrives
        within ``timeout`` seconds.

        Parameters
        ----------
        target  : agent name (matches message.target)
        message : AgentMessage to deliver
        timeout : seconds before TimeoutError

        Returns
        -------
        AgentResponse | None
        """
        handlers = self._handlers.get(target, [])
        if not handlers:
            # Try broadcast as fallback
            handlers = self._handlers.get("broadcast", [])

        if not handlers:
            self._dead_letters.append(message)
            self._dead_count += 1
            return None

        _, handler = handlers[0]
        try:
            result = await asyncio.wait_for(handler(message), timeout=timeout)
            self._delivered += 1
            return result
        except asyncio.TimeoutError:
            logger.warning(
                "AgentBus.request(): timeout waiting for %s (%.1fs)", target, timeout
            )
            raise
        except Exception as exc:
            logger.error("AgentBus.request(): handler error for %s: %s", target, exc)
            return None

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    def topics(self) -> List[str]:
        """Return all topics that have at least one subscriber."""
        return [t for t, h in self._handlers.items() if h]

    def subscriber_count(self, topic: str) -> int:
        return len(self._handlers.get(topic, []))

    def dead_letters(self) -> List[AgentMessage]:
        """Return a copy of the dead-letter queue."""
        return list(self._dead_letters)

    def drain_dead_letters(self) -> List[AgentMessage]:
        """Return and clear the dead-letter queue."""
        msgs = list(self._dead_letters)
        self._dead_letters.clear()
        return msgs

    def stats(self) -> dict:
        return {
            "published":    self._published,
            "delivered":    self._delivered,
            "dead_letters": self._dead_count,
            "topics":       self.topics(),
            "dlq_size":     len(self._dead_letters),
        }

    def reset(self) -> None:
        """Clear all subscriptions and counters.  Useful in tests."""
        self._handlers.clear()
        self._dead_letters.clear()
        self._published = 0
        self._delivered = 0
        self._dead_count = 0

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _topics_for(self, target: str) -> List[str]:
        """
        Return the list of internal topic keys to check for a given target.

        Matches: exact target, "broadcast", and "*".
        """
        topics = [target]
        if target != "broadcast":
            topics.append("broadcast")
        if target != "*":
            topics.append("*")
        return topics

    def __repr__(self) -> str:
        return (
            f"<AgentBus topics={len(self.topics())} "
            f"published={self._published} delivered={self._delivered}>"
        )
