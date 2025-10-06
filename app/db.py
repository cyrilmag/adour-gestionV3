from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Generator
import sqlite3

from .settings import get_settings


def connect(db_path: str | None = None) -> sqlite3.Connection:
    """Open a SQLite connection using project settings."""
    path = db_path or get_settings().sqlite_path()
    conn = sqlite3.connect(path)
    conn.row_factory = None
    return conn


@contextmanager
def connection_scope(db_path: str | None = None) -> Iterator[sqlite3.Connection]:
    """Managed connection context for scripts and services."""
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


def get_db() -> Generator[sqlite3.Connection, None, None]:
    """FastAPI dependency that yields a short-lived connection."""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()
