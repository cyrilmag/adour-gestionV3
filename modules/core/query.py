
import csv, io
from typing import List, Tuple, Optional, Dict, Sequence
from .utils import is_numero_short_header, fmt_last4_no_dot
from .db import table_exists
TABLE_NAME = "garantie"

def build_where_and_params(columns: List[str], q: Optional[str], hide_empty_client: bool, col_filters: Dict[str, str]):
    clauses = []
    params: List[str] = []
    if q:
        like = f"%{q}%"
        ors = []
        for c in columns:
            ors.append(f'COALESCE("{c}", "") LIKE ?')
            params.append(like)
        clauses.append("(" + " OR ".join(ors) + ")")
    for col, val in col_filters.items():
        if col in columns and str(val).strip() != "":
            clauses.append(f'COALESCE("{col}", "") LIKE ?')
            params.append(f"%{val}%")
    if hide_empty_client and "Client" in columns:
        clauses.append('("Client" IS NOT NULL AND TRIM("Client") <> "")')
    where = " AND ".join(clauses)
    return where, tuple(params)

def fetch_rows(
    conn,
    table: str,
    columns: List[str],
    page: int,
    page_size: int,
    sort_col: Optional[str],
    sort_dir: str,
    q: Optional[str],
    col_filters: Dict[str, str],
    extra_where: Optional[str] = None,
    extra_params: Optional[Sequence] = None,
) -> Tuple[List[Tuple], int]:
    hide_empty_client = True
    where, params = build_where_and_params(columns, q, hide_empty_client, col_filters)
    params = list(params)
    if extra_where:
        where = f"{where} AND ({extra_where})" if where else f"({extra_where})"
        if extra_params:
            params.extend(extra_params)
    params_tuple = tuple(params)
    where_sql = f" WHERE {where}" if where else ""
    order_sql = ' ORDER BY ROWID DESC'
    if sort_col and sort_col in columns:
        key = sort_col.strip().lower()
        if table == "interventions_peyrehorade" and is_numero_short_header(sort_col):
            order_sql = f' ORDER BY CAST(substr("{sort_col}", -4, 4) AS INTEGER) {("DESC" if sort_dir=="desc" else "ASC")}'
        elif key in {"numero","numéro","n° or","numero short","numero_short","n_or","n°or","numéro short"}:
            order_sql = f' ORDER BY CAST("{sort_col}" AS INTEGER) {("DESC" if sort_dir=="desc" else "ASC")}'
        else:
            order_sql = f' ORDER BY "{sort_col}" COLLATE NOCASE {("DESC" if sort_dir=="desc" else "ASC")}'
    limit_offset = " LIMIT ? OFFSET ?"
    cur = conn.execute(f'SELECT COUNT(1) FROM "{table}"{where_sql}', params_tuple)
    total = cur.fetchone()[0]
    quoted = ", ".join([f'"{c}"' for c in columns]) if columns else "*"
    cur = conn.execute(
        f'SELECT {quoted} FROM "{table}"{where_sql}{order_sql}{limit_offset}',
        params_tuple + (page_size, (page - 1) * page_size),
    )
    rows = cur.fetchall()
    if table == "interventions_peyrehorade" and columns:
        idx = None
        for i, c in enumerate(columns):
            if is_numero_short_header(c):
                idx = i; break
        if idx is not None:
            new_rows = []
            for r in rows:
                r = list(r)
                r[idx] = fmt_last4_no_dot(r[idx])
                new_rows.append(tuple(r))
            rows = new_rows
    return rows, total

def export_csv_response(headers: List[str], rows: List[Tuple], filename: str):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    w.writerows(rows)
    buf.seek(0)
    from fastapi.responses import StreamingResponse
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})
