"""
neuralis.store.pins
===================
Pin manager for Neuralis content-addressed storage.

In IPFS, "pinning" a CID means protecting it from garbage collection.
Unpinned blocks can be deleted by the GC at any time to free space.
Pinned blocks persist until explicitly unpinned.

Pin types
---------
RECURSIVE   The default.  Pins a root CID and (conceptually) all blocks
            reachable from it through the DAG.  In Neuralis's flat block
            store, all stored blocks are treated as roots — there is no
            DAG traversal in Module 3.  DAG-aware pinning arrives in
            Module 5 when the agent protocol builds content graphs.

DIRECT      Pins exactly one block.  Does not follow any links.

Pin storage format (pins/pins.json):
    {
        "pins": {
            "<cid_str>": {
                "type": "recursive" | "direct",
                "name": "<optional label>",
                "pinned_at": <unix float>,
                "size": <bytes | null>,
                "tags": ["tag1", "tag2"]
            }
        },
        "version": 1
    }
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set

from neuralis.store.cid import CID

logger = logging.getLogger(__name__)

PINS_DIR  = "pins"
PINS_FILE = "pins.json"
PINS_VERSION = 1


# ---------------------------------------------------------------------------
# PinType
# ---------------------------------------------------------------------------

class PinType(str, Enum):
    RECURSIVE = "recursive"
    DIRECT    = "direct"


# ---------------------------------------------------------------------------
# PinRecord
# ---------------------------------------------------------------------------

@dataclass
class PinRecord:
    """A single pin entry."""
    cid: str
    pin_type: PinType = PinType.RECURSIVE
    name: Optional[str] = None
    pinned_at: float = field(default_factory=time.time)
    size: Optional[int] = None
    tags: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "type": self.pin_type.value,
            "name": self.name,
            "pinned_at": self.pinned_at,
            "size": self.size,
            "tags": self.tags,
        }

    @classmethod
    def from_dict(cls, cid_str: str, d: dict) -> "PinRecord":
        return cls(
            cid=cid_str,
            pin_type=PinType(d.get("type", "recursive")),
            name=d.get("name"),
            pinned_at=d.get("pinned_at", time.time()),
            size=d.get("size"),
            tags=d.get("tags", []),
        )

    def __repr__(self) -> str:
        name_part = f" name={self.name!r}" if self.name else ""
        return f"<PinRecord {self.cid[:20]}…{name_part} type={self.pin_type.value}>"


# ---------------------------------------------------------------------------
# PinManager
# ---------------------------------------------------------------------------

class PinManager:
    """
    Persistent pin registry for the Neuralis block store.

    Pins are stored as a JSON file alongside the block store.
    All mutations are immediately persisted to disk.

    Usage
    -----
        pm = PinManager(Path("~/.neuralis/ipfs"))
        pm.open()

        pm.pin(cid, name="my-file")
        pm.is_pinned(cid)       → True
        pm.unpin(cid)
        pm.pinned_cids()        → set of CID objects

        pm.close()
    """

    def __init__(self, repo_path: Path):
        self._repo = Path(repo_path)
        self._pins_file = self._repo / PINS_DIR / PINS_FILE
        self._pins: Dict[str, PinRecord] = {}
        self._open = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def open(self) -> None:
        if self._open:
            return
        (self._repo / PINS_DIR).mkdir(parents=True, exist_ok=True)
        self._load()
        self._open = True
        logger.info("PinManager opened (%d pins)", len(self._pins))

    def close(self) -> None:
        if not self._open:
            return
        self._save()
        self._open = False

    def __enter__(self) -> "PinManager":
        self.open()
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Pin operations
    # ------------------------------------------------------------------

    def pin(
        self,
        cid: CID,
        pin_type: PinType = PinType.RECURSIVE,
        name: Optional[str] = None,
        size: Optional[int] = None,
        tags: Optional[List[str]] = None,
    ) -> PinRecord:
        """
        Pin a CID.

        If the CID is already pinned, the existing record is returned
        unchanged (idempotent).

        Parameters
        ----------
        cid      : CID to pin
        pin_type : RECURSIVE (default) or DIRECT
        name     : optional human-readable label
        size     : optional byte count (for display purposes)
        tags     : optional list of string tags for filtering

        Returns
        -------
        PinRecord  — the (new or existing) pin record
        """
        self._check_open()
        cid_str = str(cid)

        if cid_str in self._pins:
            logger.debug("CID already pinned: %s", cid_str[:20])
            return self._pins[cid_str]

        record = PinRecord(
            cid=cid_str,
            pin_type=pin_type,
            name=name,
            size=size,
            tags=tags or [],
        )
        self._pins[cid_str] = record
        self._save()
        logger.info("Pinned: %s (type=%s name=%s)", cid_str[:20], pin_type.value, name)
        return record

    def unpin(self, cid: CID) -> bool:
        """
        Remove a pin.

        Returns True if the pin was removed, False if it wasn't pinned.
        Does NOT delete the underlying block — that's the GC's job.
        """
        self._check_open()
        cid_str = str(cid)
        if cid_str not in self._pins:
            return False
        del self._pins[cid_str]
        self._save()
        logger.info("Unpinned: %s", cid_str[:20])
        return True

    def is_pinned(self, cid: CID) -> bool:
        """Return True if the CID is pinned."""
        self._check_open()
        return str(cid) in self._pins

    def get_pin(self, cid: CID) -> Optional[PinRecord]:
        """Return the PinRecord for a CID, or None if not pinned."""
        self._check_open()
        return self._pins.get(str(cid))

    def update_pin(
        self,
        cid: CID,
        name: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> Optional[PinRecord]:
        """
        Update the name and/or tags of an existing pin.
        Returns the updated record, or None if not pinned.
        """
        self._check_open()
        record = self._pins.get(str(cid))
        if record is None:
            return None
        if name is not None:
            record.name = name
        if tags is not None:
            record.tags = tags
        self._save()
        return record

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def pinned_cids(self) -> Set[CID]:
        """Return the set of all pinned CIDs."""
        self._check_open()
        result = set()
        for cid_str in self._pins:
            try:
                result.add(CID.from_str(cid_str))
            except ValueError:
                logger.warning("Invalid CID in pin store: %s", cid_str[:20])
        return result

    def list_pins(
        self,
        pin_type: Optional[PinType] = None,
        tag: Optional[str] = None,
    ) -> List[PinRecord]:
        """
        List all pins, optionally filtered by type or tag.

        Parameters
        ----------
        pin_type : filter to RECURSIVE or DIRECT only
        tag      : filter to pins that have this tag
        """
        self._check_open()
        records = list(self._pins.values())
        if pin_type is not None:
            records = [r for r in records if r.pin_type == pin_type]
        if tag is not None:
            records = [r for r in records if tag in r.tags]
        return records

    def count(self) -> int:
        """Return the total number of pins."""
        self._check_open()
        return len(self._pins)

    def total_pinned_bytes(self) -> int:
        """Sum of size fields for all pins that have a size recorded."""
        self._check_open()
        return sum(r.size for r in self._pins.values() if r.size is not None)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not self._pins_file.exists():
            self._pins = {}
            return
        try:
            raw = json.loads(self._pins_file.read_text())
            pins_data = raw.get("pins", {})
            self._pins = {
                cid_str: PinRecord.from_dict(cid_str, record)
                for cid_str, record in pins_data.items()
            }
        except Exception as exc:
            logger.error("Failed to load pins: %s — starting fresh", exc)
            self._pins = {}

    def _save(self) -> None:
        try:
            self._pins_file.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "version": PINS_VERSION,
                "pins": {
                    cid_str: record.to_dict()
                    for cid_str, record in self._pins.items()
                },
            }
            # Atomic write
            tmp = self._pins_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(self._pins_file)
        except Exception as exc:
            logger.error("Failed to save pins: %s", exc)

    def _check_open(self) -> None:
        if not self._open:
            raise RuntimeError("PinManager is not open — call open() first")

    def __repr__(self) -> str:
        return f"<PinManager pins={len(self._pins)} open={self._open}>"
