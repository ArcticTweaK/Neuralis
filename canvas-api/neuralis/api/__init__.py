"""
neuralis.api
============
Canvas API — FastAPI bridge between the Neuralis mesh and the React UI.

Public API
----------
    from neuralis.api import create_app, serve, ConnectionManager
"""

from neuralis.api.app import ConnectionManager, create_app, serve

__all__ = ["create_app", "serve", "ConnectionManager"]
