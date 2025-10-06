from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

from app.services.etl import process_xml_file, run_folder


def update_from_xml(
    db_path: str,
    xml_path: str,
    table: str = "interventions",
) -> Tuple[Tuple[str, int, int, int], List[Tuple[str, str, str, str]]]:
    """Backward compatible wrapper around the refactored ETL service."""
    report = process_xml_file(db_path, Path(xml_path), table=table)
    row_log = [(log.numero, log.action, log.cession, log.snippet) for log in report.row_logs]
    return report.as_tuple(), row_log


def update_all_xml_in_folder(db_path: str, xml_dir: str, table: str = "interventions") -> str:
    summary = run_folder(db_path, Path(xml_dir), table=table)
    return summary.run_at
