from __future__ import annotations

from typing import Iterable

from app.db import connect
from app.settings import get_settings

from modules.core.db import (
    ensure_cession_codes_schema,
    ensure_garantie_num_unique_numeric_unique,
    ensure_interventions_traiter_schema,
    ensure_unique_indexes,
)


def apply_database_defaults(db_path: str | None = None) -> None:
    """Ensure minimum schema requirements are satisfied."""
    path = db_path or get_settings().sqlite_path()
    with connect(path) as conn:
        ensure_cession_codes_schema(conn)
        ensure_garantie_num_unique_numeric_unique(conn)
        ensure_interventions_traiter_schema(conn)
        ensure_unique_indexes(conn)
