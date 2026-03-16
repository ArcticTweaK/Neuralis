"""
neuralis.agents.loader
======================
Agent plugin discovery and loading.

The ``AgentLoader`` scans a directory for Python files, imports each one,
finds all classes that inherit from ``BaseAgent``, and instantiates them
with the running Node and AgentConfig.

Discovery rules
---------------
- Only .py files in the configured ``agents_dir`` are scanned (no recursion)
- Files starting with ``_`` are skipped
- Each file may contain multiple agent classes — all are loaded
- An agent class must inherit from BaseAgent and must NOT be BaseAgent itself
- Duplicate NAME values are rejected — the first loaded wins

Hot-reload
----------
``AgentLoader.reload()`` rescans the directory, loads any new agents, and
stops + removes any agents whose source file has been deleted.  Running
agents are not restarted unless their file has changed (mtime check).

Usage
-----
    loader = AgentLoader(node)
    await loader.discover()
    agents = loader.all_agents()          # list[BaseAgent]
    agent  = loader.get("echo")           # BaseAgent | None
    await loader.reload()                 # hot-reload from disk
"""

from __future__ import annotations

import importlib.util
import inspect
import logging
import os
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Type

from neuralis.agents.base import BaseAgent, AgentState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# AgentLoadError
# ---------------------------------------------------------------------------

class AgentLoadError(Exception):
    """Raised when an agent file cannot be loaded or validated."""


# ---------------------------------------------------------------------------
# AgentLoader
# ---------------------------------------------------------------------------

class AgentLoader:
    """
    Discovers, loads, and manages the lifecycle of agent plugins.

    Parameters
    ----------
    node   : the running neuralis.node.Node instance
    config : neuralis.config.AgentConfig (node.config.agents)
    """

    def __init__(self, node, config) -> None:
        self._node    = node
        self._config  = config
        self._agents:  Dict[str, BaseAgent]  = {}   # name → instance
        self._mtimes:  Dict[str, float]      = {}   # path → last mtime
        self._sources: Dict[str, str]        = {}   # name → source file path

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    async def discover(self) -> List[BaseAgent]:
        """
        Scan ``agents_dir`` and load all agent plugins found.

        Returns the list of newly loaded agents.  Already-loaded agents
        with unchanged mtimes are skipped.

        Raises
        ------
        Nothing — errors per file are logged and skipped.
        """
        agents_dir = Path(self._config.agents_dir)
        if not agents_dir.exists():
            logger.info("AgentLoader: agents_dir does not exist yet (%s) — no agents loaded", agents_dir)
            return []

        loaded: List[BaseAgent] = []

        for py_file in sorted(agents_dir.glob("*.py")):
            if py_file.name.startswith("_"):
                continue

            mtime = py_file.stat().st_mtime
            if str(py_file) in self._mtimes and self._mtimes[str(py_file)] == mtime:
                continue  # unchanged since last scan

            try:
                new_agents = self._load_file(py_file)
                for agent in new_agents:
                    await self._activate(agent, py_file)
                    loaded.append(agent)
                self._mtimes[str(py_file)] = mtime
            except AgentLoadError as exc:
                logger.warning("AgentLoader: skipping %s — %s", py_file.name, exc)
            except Exception as exc:
                logger.error("AgentLoader: unexpected error loading %s: %s", py_file.name, exc)

        logger.info("AgentLoader: discovery complete — %d agent(s) active", len(self._agents))
        return loaded

    async def reload(self) -> dict:
        """
        Hot-reload agents from disk.

        - New files → load and start
        - Changed files (mtime) → stop old, load new, start new
        - Deleted files → stop and remove

        Returns
        -------
        dict with keys ``added``, ``updated``, ``removed`` (lists of names)
        """
        result: dict = {"added": [], "updated": [], "removed": []}
        agents_dir = Path(self._config.agents_dir)

        if not agents_dir.exists():
            return result

        current_files = {str(f): f for f in agents_dir.glob("*.py") if not f.name.startswith("_")}

        # Find removed files
        for path_str, name in list(self._sources.items()):
            if path_str not in current_files:
                await self._deactivate(name)
                result["removed"].append(name)
                del self._sources[path_str]
                self._mtimes.pop(path_str, None)

        # Find new / changed files
        for path_str, py_file in current_files.items():
            mtime = py_file.stat().st_mtime
            old_mtime = self._mtimes.get(path_str)

            if old_mtime is None:
                # New file
                try:
                    new_agents = self._load_file(py_file)
                    for agent in new_agents:
                        await self._activate(agent, py_file)
                        result["added"].append(agent.NAME)
                    self._mtimes[path_str] = mtime
                except (AgentLoadError, Exception) as exc:
                    logger.warning("AgentLoader reload: skipping %s — %s", py_file.name, exc)

            elif mtime != old_mtime:
                # Changed file — find which agents came from this file
                old_names = [n for n, p in self._sources.items() if p == path_str]
                for name in old_names:
                    await self._deactivate(name)

                try:
                    new_agents = self._load_file(py_file)
                    for agent in new_agents:
                        await self._activate(agent, py_file)
                        result["updated"].append(agent.NAME)
                    self._mtimes[path_str] = mtime
                except (AgentLoadError, Exception) as exc:
                    logger.warning("AgentLoader reload: skipping changed %s — %s", py_file.name, exc)

        return result

    # ------------------------------------------------------------------
    # Access
    # ------------------------------------------------------------------

    def get(self, name: str) -> Optional[BaseAgent]:
        """Return a running agent by name, or None."""
        return self._agents.get(name)

    def all_agents(self) -> List[BaseAgent]:
        """Return all currently loaded agents."""
        return list(self._agents.values())

    def names(self) -> List[str]:
        """Return the names of all loaded agents."""
        return list(self._agents.keys())

    def count(self) -> int:
        return len(self._agents)

    def agents_for_task(self, task: str) -> List[BaseAgent]:
        """Return all agents that declare they can handle ``task``."""
        return [a for a in self._agents.values() if a.can_handle(task)]

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def stop_all(self) -> None:
        """Stop all loaded agents in reverse-load order."""
        for agent in reversed(list(self._agents.values())):
            try:
                await agent.stop()
                logger.debug("AgentLoader: stopped %s", agent.NAME)
            except Exception as exc:
                logger.error("AgentLoader: error stopping %s: %s", agent.NAME, exc)
        self._agents.clear()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load_file(self, py_file: Path) -> List[BaseAgent]:
        """
        Import a .py file and extract all BaseAgent subclasses.

        Returns instantiated (but not yet started) agent objects.

        Raises
        ------
        AgentLoadError — if the file cannot be imported or contains no agents
        """
        module_name = f"neuralis_agent_{py_file.stem}_{int(time.time() * 1000)}"

        try:
            spec = importlib.util.spec_from_file_location(module_name, py_file)
            if spec is None or spec.loader is None:
                raise AgentLoadError(f"Cannot create module spec for {py_file}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        except AgentLoadError:
            raise
        except Exception as exc:
            raise AgentLoadError(f"Import error in {py_file.name}: {exc}") from exc

        classes = [
            obj for _, obj in inspect.getmembers(module, inspect.isclass)
            if issubclass(obj, BaseAgent) and obj is not BaseAgent and obj.__module__ == module_name
        ]

        if not classes:
            raise AgentLoadError(f"No BaseAgent subclasses found in {py_file.name}")

        agents: List[BaseAgent] = []
        for cls in classes:
            if not cls.NAME or cls.NAME == "unnamed":
                logger.warning("AgentLoader: %s.%s has no NAME — skipping", py_file.name, cls.__name__)
                continue
            if cls.NAME in self._agents:
                logger.warning(
                    "AgentLoader: agent name '%s' already loaded — skipping duplicate in %s",
                    cls.NAME, py_file.name,
                )
                continue
            try:
                instance = cls(self._node, self._config)
                agents.append(instance)
            except Exception as exc:
                raise AgentLoadError(
                    f"Failed to instantiate {cls.__name__} from {py_file.name}: {exc}"
                ) from exc

        return agents

    async def _activate(self, agent: BaseAgent, source_file: Path) -> None:
        """Start an agent and register it."""
        try:
            await agent.start()
            self._agents[agent.NAME] = agent
            self._sources[agent.NAME] = str(source_file)
            logger.info(
                "AgentLoader: loaded %s v%s [%s]",
                agent.NAME, agent.VERSION, ", ".join(agent.CAPABILITIES) or "no capabilities",
            )
        except Exception as exc:
            agent._state = AgentState.ERROR
            raise AgentLoadError(f"start() failed for {agent.NAME}: {exc}") from exc

    async def _deactivate(self, name: str) -> None:
        """Stop and unregister a named agent."""
        agent = self._agents.pop(name, None)
        if agent:
            try:
                await agent.stop()
                logger.info("AgentLoader: unloaded %s", name)
            except Exception as exc:
                logger.error("AgentLoader: error stopping %s: %s", name, exc)

    def __repr__(self) -> str:
        return f"<AgentLoader agents={self.names()}>"
