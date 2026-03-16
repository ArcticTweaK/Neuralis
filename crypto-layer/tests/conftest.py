from __future__ import annotations
import sys
from pathlib import Path

_repo  = Path(__file__).resolve().parent.parent.parent
_node  = _repo / "neuralis-node"
_crypt = _repo / "crypto-layer"

for _p in (_crypt, _node):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)