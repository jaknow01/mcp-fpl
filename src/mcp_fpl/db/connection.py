"""Postgres connection helpers.

A new connection per unit of work (the assistant is low-volume; no pool needed).
The database is selected via the ``DATABASE_URL`` env var, read at call time so it
picks up ``.env`` (loaded after import in ``app/main.py``).
"""
from __future__ import annotations

import contextlib
import logging
import os
from typing import Iterator, Optional

import psycopg

logger = logging.getLogger(__name__)


def database_url() -> Optional[str]:
    url = os.getenv("DATABASE_URL")
    return url or None


def is_configured() -> bool:
    """True when a Postgres DATABASE_URL is set (otherwise callers fall back to disk)."""
    return database_url() is not None


@contextlib.contextmanager
def connection() -> Iterator["psycopg.Connection"]:
    """Yield a Postgres connection, committing on success and rolling back on error."""
    url = database_url()
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    conn = psycopg.connect(url)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
