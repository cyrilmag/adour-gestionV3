from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Sequence
import sqlite3
import unicodedata
import xml.etree.ElementTree as ET
import re

from app.db import connect
from app.settings import get_settings


@dataclass(frozen=True)
class RowLog:
    numero: str
    action: str
    cession: str
    snippet: str


@dataclass
class FileReport:
    table_used: str
    filename: str
    total: int
    updated: int
    inserted: int
    row_logs: List[RowLog] = field(default_factory=list)

    def as_tuple(self) -> tuple[str, int, int, int]:
        return (self.table_used, self.total, self.updated, self.inserted)


@dataclass
class EtlRunSummary:
    run_at: str
    reports: List[FileReport]

    @property
    def total_files(self) -> int:
        return len(self.reports)


def _norm(value: str | None) -> str:
    if value is None:
        return ""
    text = "".join(ch for ch in unicodedata.normalize("NFKD", value) if not unicodedata.combining(ch))
    text = re.sub(r"\s+", " ", text.strip().lower())
    return text


def _normalize_travaux(text: str | None) -> str:
    if not text:
        return ""
    sanitized = text.replace("\r\n", "\n").replace("\r", "\n")
    sanitized = re.sub(r"\n{3,}", "\n\n", sanitized)
    return sanitized.strip()


def _ensure_log_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS etl_updates (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at    TEXT NOT NULL,
            filename  TEXT NOT NULL,
            total     INTEGER NOT NULL,
            updated   INTEGER NOT NULL,
            inserted  INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS etl_updates_rows (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at     TEXT NOT NULL,
            filename   TEXT NOT NULL,
            table_used TEXT NOT NULL,
            numero     TEXT NOT NULL,
            action     TEXT NOT NULL,
            cession    TEXT,
            travaux_snippet TEXT
        )
        """
    )
    conn.commit()


def _detect_columns_interventions(conn: sqlite3.Connection, table: str) -> tuple[str, str, str]:
    rows = conn.execute(f"PRAGMA table_info('{table}')").fetchall()
    if not rows:
        raise RuntimeError(f"Table '{table}' introuvable.")

    by_norm = {_norm(r[1]): r[1] for r in rows}
    existing = [r[1] for r in rows]

    def pick(*aliases: str, contains: Optional[str] = None) -> Optional[str]:
        for alias in aliases:
            alias_norm = _norm(alias)
            if alias_norm in by_norm:
                return by_norm[alias_norm]
        if contains:
            for key, col in by_norm.items():
                if contains in key:
                    return col
        return None

    col_num = pick("Numéro", "numero", "n°", "no", "num_or", contains="num")
    col_trav = pick("Travaux", contains="trav")
    col_cess = pick("Cession", "code cession", contains="cess")

    if not (col_num and col_trav and col_cess):
        raise RuntimeError(f"Colonnes manquantes dans '{table}'. Présentes: {existing}")
    return col_num, col_trav, col_cess


def _table_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    rows = conn.execute(f"PRAGMA table_info('{table}')").fetchall()
    if not rows:
        raise RuntimeError(f"Table '{table}' introuvable.")
    return [r[1] for r in rows]


def _xml_to_partial_row(node: ET.Element, table_cols: Sequence[str]) -> dict[str, object]:
    by_norm = {_norm(c): c for c in table_cols}
    row: dict[str, object] = {}
    for child in node:
        tag = child.tag or ""
        value = (child.text or "").strip()
        key_norm = _norm(tag)
        if key_norm in by_norm:
            column = by_norm[key_norm]
            if key_norm == _norm("Travaux"):
                row[column] = _normalize_travaux(value)
            else:
                row[column] = value
    return row


def _iter_table1_nodes(root: ET.Element) -> Iterator[ET.Element]:
    return root.findall(".//TABLE1")


def _load_xml(path: Path) -> ET.Element:
    with path.open("r", encoding="iso-8859-1") as handle:
        xml_text = handle.read()
    return ET.fromstring(xml_text)


def _gather_minimal_items(root: ET.Element) -> list[tuple[str, str, str]]:
    items: list[tuple[str, str, str]] = []
    for node in _iter_table1_nodes(root):
        numero = (node.findtext("Numéro") or "").strip()
        if not numero:
            continue
        cession = (node.findtext("Cession") or "").strip()
        travaux = _normalize_travaux(node.findtext("Travaux") or "")
        items.append((numero, travaux, cession))
    return items


def _ensure_existing(conn: sqlite3.Connection, table: str, col_num: str, numeros: Sequence[str]) -> set[str]:
    if not numeros:
        return set()
    placeholders = ",".join("?" * len(numeros))
    cursor = conn.execute(
        f'SELECT {col_num} FROM {table} WHERE {col_num} IN ({placeholders})',
        tuple(numeros),
    )
    return {row[0] for row in cursor.fetchall()}


def _row_snippet(text: str, limit: int = 160) -> str:
    return (text or "")[:limit]


def _load_reference_info(conn: sqlite3.Connection, table: str) -> dict[int, bool]:
    try:
        rows = conn.execute(
            f'SELECT "Numéro", "Date Cloture" FROM "{table}"'
        ).fetchall()
    except sqlite3.Error:
        return {}

    info: dict[int, bool] = {}
    for numero, date_cloture in rows:
        if numero is None:
            continue
        try:
            key = int(str(numero).strip())
        except ValueError:
            continue
        has_date = bool(str(date_cloture or '').strip())
        info[key] = has_date
    return info


def _site_and_short(numero: str) -> tuple[Optional[str], Optional[int]]:
    digits = "".join(ch for ch in str(numero) if ch.isdigit())
    if not digits:
        return None, None

    if digits.startswith("301"):
        short = digits[-5:] if len(digits) >= 5 else digits
        try:
            return "narrosse", int(short)
        except ValueError:
            return None, None

    if digits.startswith("302"):
        short = digits[-4:] if len(digits) >= 4 else digits
        try:
            return "peyrehorade", int(short)
        except ValueError:
            return None, None

    return None, None





def _short_for_storage(numero: str) -> tuple[Optional[str], Optional[str]]:
    site, short = _site_and_short(numero)
    if site is None or short is None:
        return None, None
    return site, f"{short}"
def _mark_traiter_if_matched(
    conn: sqlite3.Connection,
    numero: str,
    narrosse_info: dict[int, bool],
    peyre_info: dict[int, bool],
) -> bool:
    site, short = _site_and_short(numero)
    if short is None:
        return False

    if site == "narrosse":
        if not narrosse_info.get(short):
            return False
    elif site == "peyrehorade":
        if not peyre_info.get(short):
            return False
    else:
        return False

    conn.execute(
        'UPDATE "interventions" SET "traiter" = 1 '
        'WHERE "Numéro" = ? AND COALESCE("traiter", 0) <> 1',
        (numero,),
    )
    return True


def process_xml_file(db_path: str, xml_path: Path, table: str = "interventions") -> FileReport:
    xml_root = _load_xml(xml_path)
    items = _gather_minimal_items(xml_root)

    conn = connect(db_path)
    cur = conn.cursor()

    col_num, col_trav, col_cess = _detect_columns_interventions(conn, table)
    table_cols = _table_columns(conn, table)

    existing = _ensure_existing(conn, table, col_num, [numero for numero, _, _ in items])
    narrosse_info = _load_reference_info(conn, "interventions_narrosse")
    peyre_info = _load_reference_info(conn, "interventions_peyrehorade")
    has_num_short = "num_short" in table_cols

    updated = inserted = 0
    row_logs: list[RowLog] = []

    for node in _iter_table1_nodes(xml_root):
        numero = (node.findtext("Numéro") or "").strip()
        if not numero:
            continue
        travaux = _normalize_travaux(node.findtext("Travaux") or "")
        cession = (node.findtext("Cession") or "").strip()
        snippet = _row_snippet(travaux)
        _, num_short_value = _short_for_storage(numero)

        if numero in existing:
            params = [travaux, cession]
            set_clause = f"{col_trav}=?, {col_cess}=?"
            if has_num_short and num_short_value is not None:
                set_clause += ', "num_short"=?'
                params.append(num_short_value)
            params.append(numero)
            cur.execute(
                f'UPDATE {table} SET {set_clause} WHERE {col_num}=?',
                params,
            )
            _mark_traiter_if_matched(conn, numero, narrosse_info, peyre_info)
            updated += 1
            row_logs.append(RowLog(numero, "update", cession, snippet))
            continue

        # build partial insert row
        partial = _xml_to_partial_row(node, table_cols)
        partial[col_num] = numero
        partial[col_trav] = travaux
        partial[col_cess] = cession
        if has_num_short and num_short_value is not None:
            partial["num_short"] = num_short_value

        insert_cols = list(partial.keys())
        insert_vals = [partial[col] for col in insert_cols]
        quoted_cols = '","'.join(insert_cols)
        placeholders = ",".join("?" * len(insert_cols))

        try:
            cur.execute(
                f'INSERT INTO {table} ("{quoted_cols}") VALUES ({placeholders})',
                insert_vals,
            )
            existing.add(numero)
            _mark_traiter_if_matched(conn, numero, narrosse_info, peyre_info)
            inserted += 1
            row_logs.append(RowLog(numero, "insert", cession, snippet))
        except Exception as exc:  # pragma: no cover - logged for diagnostics
            row_logs.append(RowLog(numero, "insert_failed", cession, str(exc)))

    conn.commit()
    conn.close()

    return FileReport(
        table_used=table,
        filename=xml_path.name,
        total=len(items),
        updated=updated,
        inserted=inserted,
        row_logs=row_logs,
    )


def _log_report(db_path: str, run_at: str, report: FileReport) -> None:
    with connect(db_path) as conn:
        _ensure_log_tables(conn)
        conn.execute(
            "INSERT INTO etl_updates (run_at, filename, total, updated, inserted) VALUES (?,?,?,?,?)",
            (run_at, report.filename, report.total, report.updated, report.inserted),
        )
        if report.row_logs:
            conn.executemany(
                """
                INSERT INTO etl_updates_rows (run_at, filename, table_used, numero, action, cession, travaux_snippet)
                VALUES (?,?,?,?,?,?,?)
                """,
                [
                    (
                        run_at,
                        report.filename,
                        report.table_used,
                        row.numero,
                        row.action,
                        row.cession,
                        row.snippet,
                    )
                    for row in report.row_logs
                ],
            )
        conn.commit()


def run_folder(db_path: str, xml_dir: Path, table: str = "interventions") -> EtlRunSummary:
    xml_dir = Path(xml_dir)
    xml_paths = sorted(xml_dir.glob("*.xml"))
    run_at = datetime.utcnow().isoformat(timespec="seconds")

    # ensure log tables exist before the run
    with connect(db_path) as conn:
        _ensure_log_tables(conn)

    reports: list[FileReport] = []

    for xml_file in xml_paths:
        try:
            report = process_xml_file(db_path, xml_file, table)
            reports.append(report)
            _log_report(db_path, run_at, report)
            print(
                f"[ETL] {xml_file.name} → {report.table_used} | total:{report.total} | maj:{report.updated} | ins:{report.inserted}"
            )
        except Exception as exc:
            print(f"[ETL][ERREUR] {xml_file.name}: {exc}")

    return EtlRunSummary(run_at=run_at, reports=reports)


def run_with_settings(table: str = "interventions") -> EtlRunSummary:
    settings = get_settings()
    return run_folder(settings.sqlite_path(), settings.xml_dir, table=table)
