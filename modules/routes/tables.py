from fastapi import APIRouter, Request, Query, HTTPException
from fastapi.responses import RedirectResponse
from typing import Optional, List, Any
from datetime import datetime, date
import unicodedata
import sqlite3

from ..core.ui import templates
from ..core.db import get_conn, table_exists, pragma_columns
from ..core.query import fetch_rows
from ..core.utils import parse_column_filters

router = APIRouter()

TABLES = {
    "interventions": "Interventions (XML)",
    "interventions_narrosse": "Interventions Narrosse",
    "interventions_peyrehorade": "Interventions Peyrehorade",
    "garanties": "Registre Garantie",
}

# Libellés / colonnes masquées / PK depuis garanties.py (fallback sûrs)
try:
    from .garanties import DISPLAY_LABELS as GARANTIE_LABELS
except Exception:
    GARANTIE_LABELS = {}

try:
    from .garanties import HIDE_FIELDS as GARANTIE_HIDE_FIELDS
except Exception:
    GARANTIE_HIDE_FIELDS = set()

try:
    from .garanties import PK_COL as GARANTIE_PK_COL
except Exception:
    GARANTIE_PK_COL = "NUM_DG"  # fallback

try:
    from .garanties import OR_UNIQ as GARANTIE_OR_UNIQ
except Exception:
    GARANTIE_OR_UNIQ = "num_unique"  # fallback


# ------------------------------ Helpers génériques ------------------------------

def _norm_txt(s) -> str:
    if s is None:
        return ""
    s = str(s).strip()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return s.lower()


def _parse_date_text(s: str) -> Optional[date]:
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            pass
    return None


def _fiscal_year(d: Optional[date]) -> Optional[int]:
    if not d:
        return None
    # Exercice : 01/10/(N-1) → 30/09/N, le nom affiché = N
    return d.year + 1 if d.month >= 10 else d.year


def _load_type_garantie_options(conn) -> list[str]:
    opts: list[str] = []

    def _query(table: str, col: str) -> list[str]:
        try:
            rows = conn.execute(
                f'SELECT DISTINCT {col} FROM "{table}" '
                f'WHERE COALESCE(active,1)=1 ORDER BY 1'
            ).fetchall()
            return [str(r[0]).strip() for r in rows if r and str(r[0]).strip()]
        except Exception:
            return []

    for c in ("type_garantie", "libelle", "label", "Libelle", "Label"):
        opts = _query("cession_garantie", c)
        if opts:
            break

    if not opts:
        try:
            rows = conn.execute(
                'SELECT DISTINCT type_garantie FROM "cession_codes" '
                'WHERE COALESCE(active,1)=1 ORDER BY 1'
            ).fetchall()
            opts = [str(r[0]).strip() for r in rows if r and str(r[0]).strip()]
        except Exception:
            opts = []

    seen = set()
    dedup = []
    for x in opts:
        if x not in seen:
            dedup.append(x)
            seen.add(x)
    return dedup


def render_table_page(
    request: Request,
    table: str,
    title: str,
    sort_col: Optional[str],
    sort_dir: str,
    q: Optional[str],
):
    with get_conn() as conn:
        if not table_exists(conn, table):
            return templates.TemplateResponse("table.html", {
                "request": request,
                "title": title,
                "columns": [],
                "rows": [],
                "sort_col": sort_col,
                "sort_dir": sort_dir,
                "q": q or "",
                "filters": {},
                "error": f"La table « {table} » est introuvable."
            })

        cols = pragma_columns(conn, table)
        filters = parse_column_filters(request)

        if sort_col is not None and str(sort_col).strip() == "":
            sort_col = None

        # Sans pagination : on charge tout
        rows, _ = fetch_rows(
            conn, table, cols,
            page=1, page_size=10**9,
            sort_col=sort_col, sort_dir=sort_dir, q=q, col_filters=filters
        )

        return templates.TemplateResponse("table.html", {
            "request": request,
            "title": title,
            "columns": cols,
            "rows": rows,
            "sort_col": sort_col,
            "sort_dir": sort_dir,
            "q": q or "",
            "filters": filters,
            "error": "",
            "labels": {},  # pas de libellés spécifiques sur ces pages
        })


# ------------------------------ Pages génériques ------------------------------

@router.get("/interventions")
async def page_interventions(
    request: Request,
    sort_col: Optional[str] = Query(None),
    sort_dir: str = Query("asc"),
    q: Optional[str] = Query(None),
):
    return render_table_page(
        request, "interventions", TABLES["interventions"], sort_col, sort_dir, q
    )


@router.get("/historique/narrosse")
async def page_hist_narrosse(
    request: Request,
    sort_col: Optional[str] = Query(None),
    sort_dir: str = Query("asc"),
    q: Optional[str] = Query(None),
):
    return render_table_page(
        request, "interventions_narrosse", TABLES["interventions_narrosse"], sort_col, sort_dir, q
    )


@router.get("/historique/peyrehorade")
async def page_hist_peyrehorade(
    request: Request,
    sort_col: Optional[str] = Query(None),
    sort_dir: str = Query("asc"),
    q: Optional[str] = Query(None),
):
    return render_table_page(
        request, "interventions_peyrehorade", TABLES["interventions_peyrehorade"], sort_col, sort_dir, q
    )


# ------------------------------ Registre Garantie ------------------------------

@router.get("/registre-garantie")
async def page_registre_garantie(
    request: Request,
    sort_col: Optional[str] = Query(None),
    sort_dir: str = Query("asc"),
    q: Optional[str] = Query(None),
    ex: Optional[str] = Query(None, description="Exercice (année de clôture, 01/10/(N-1)→30/09/N)"),
    etat: Optional[str] = Query(None, description="Statut paiement: non_paye | paye | all"),
    gtype: Optional[str] = Query(None, description="Type de garantie (depuis cession_garantie)"),
    tg: Optional[str] = Query(None, description="Alias de gtype"),
    non_paye: int = Query(0, ge=0, le=1),
):
    # ex peut être vide
    ex_int: Optional[int] = None
    if ex is not None and str(ex).strip() != "":
        try:
            ex_int = int(str(ex).strip())
        except Exception:
            ex_int = None

    if sort_col is not None and str(sort_col).strip() == "":
        sort_col = None

    selected_type = (gtype if (gtype is not None and gtype != "") else tg) or ""
    gtype_norm = _norm_txt(selected_type)

    ex_clause = None
    ex_params = None
    if ex_int is not None:
        start_year = ex_int - 1
        start_date = date(start_year, 10, 1)
        end_date = date(ex_int, 9, 30)
        ex_clause = (
            'DATE(CASE '
            'WHEN "Date" GLOB "____-__-__" THEN "Date" '
            'WHEN "Date" LIKE "__/__/____" THEN '
            'substr("Date",7,4)||"-"||substr("Date",4,2)||"-"||substr("Date",1,2) '
            'ELSE "Date" END) BETWEEN DATE(?) AND DATE(?)'
        )
        ex_params = (
            start_date.strftime("%Y-%m-%d"),
            end_date.strftime("%Y-%m-%d"),
        )

    closing_years: set[int] = set()

    with get_conn() as conn:
        cols = pragma_columns(conn, "garanties")
        filters = parse_column_filters(request)

        distinct_dates = conn.execute(
            'SELECT DISTINCT "Date" FROM "garanties" '
            'WHERE "Date" IS NOT NULL AND TRIM("Date") <> ""'
        ).fetchall()
        for (raw_date,) in distinct_dates:
            d = _parse_date_text(raw_date)
            closing = _fiscal_year(d)
            if closing is not None:
                closing_years.add(closing)

        rows_all, _ = fetch_rows(
            conn,
            "garanties",
            cols,
            page=1,
            page_size=10**9,
            sort_col=sort_col,
            sort_dir=sort_dir,
            q=q,
            col_filters=filters,
            extra_where=ex_clause,
            extra_params=ex_params,
        )
        type_options = _load_type_garantie_options(conn)

    if not closing_years:
        today_closing = _fiscal_year(date.today())
        if today_closing is not None:
            closing_years.add(today_closing)

    ex_list = sorted(closing_years, reverse=True)

    # mapping colonnes
    col_index = {c: i for i, c in enumerate(cols)}

    def _idx(*names: str) -> Optional[int]:
        for n in names:
            if n in col_index:
                return col_index[n]
        return None

    idx_date = col_index.get("Date")
    idx_date_pay = _idx("date_paiement", "Date_Paiement", "Date paiement")
    idx_type = _idx("Type", "TYPE", "type")

    # statut par défaut si ?non_paye=1
    etat_norm = (etat or "").strip().lower()
    if non_paye and not etat_norm:
        etat_norm = "non_paye"

    def _row_date_ref(row: List[Any]) -> Optional[date]:
        if idx_date is None or idx_date >= len(row):
            return None
        val = row[idx_date]
        if not val:
            return None
        return _parse_date_text(val)

    def _is_paid(row: List[Any]) -> Optional[bool]:
        if idx_date_pay is None or idx_date_pay >= len(row):
            return None
        v = row[idx_date_pay]
        if v in (None, "", 0):
            return False
        return _parse_date_text(v) is not None or bool(str(v).strip())

    # 1) Filtrage (exercice / statut / type)
    filtered: List[List[Any]] = []
    for r in rows_all:
        dref = _row_date_ref(r)
        fy = _fiscal_year(dref)
        if ex_int is not None and fy is not None and fy != ex_int:
            continue

        if etat_norm == "non_paye":
            paid = _is_paid(r)
            if paid not in (False, None):
                continue
        elif etat_norm == "paye":
            paid = _is_paid(r)
            if paid is not True:
                continue

        if gtype_norm and idx_type is not None and idx_type < len(r):
            if _norm_txt(r[idx_type]) != gtype_norm:
                continue

        filtered.append(r)

    # 2) Colonnes visibles (HIDE_FIELDS) + PK en tête
    pk_col = GARANTIE_PK_COL if GARANTIE_PK_COL in cols else (cols[0] if cols else None)

    hide = set(GARANTIE_HIDE_FIELDS or set())
    if pk_col:
        hide.discard(pk_col)

    cols_vis = ([pk_col] if pk_col else []) + [c for c in cols if c != pk_col and c not in hide]
    idx_map = [col_index[c] for c in cols_vis]
    rows_vis = [[r[i] for i in idx_map] for r in filtered]

    # 3) Clé de ligne = num_unique si dispo, sinon PK
    idx_uniq = (col_index.get(GARANTIE_OR_UNIQ) if GARANTIE_OR_UNIQ in col_index else col_index.get("num_unique"))
    row_keys = []
    for r in filtered:
        key = None
        if idx_uniq is not None and idx_uniq < len(r):
            key = r[idx_uniq]
        if (key is None or str(key).strip() == "") and pk_col:
            key = r[col_index[pk_col]]
        row_keys.append(str(key) if key is not None else "")
    

    # 5) Rendu
    ctx = {
        "request": request,
        "title": TABLES["garanties"],
        "columns": cols_vis,
        "rows": rows_vis,
        "row_keys": row_keys,                 # ← utilisé par table.html pour le clic
        "sort_col": sort_col,
        "sort_dir": sort_dir,
        "q": q or "",
        "filters": filters,
        "error": "",
        "ex": ex_int,
        "etat": etat_norm or ("non_paye" if non_paye else None),
        "non_paye": non_paye,
        "ex_list": ex_list,
        "labels": GARANTIE_LABELS,
        "type_options": type_options,
        "tg": selected_type,
        "gtype": selected_type
    }
    return templates.TemplateResponse("table.html", ctx)


# ------------------------------ Redirection par num_unique (option A) ------------------------------

# @router.get("/garanties/by-unique/{u}")
# async def garanties_redirect_by_unique(u: str):
#     u = (u or "").strip()
#     if not u:
#         raise HTTPException(status_code=400, detail="num_unique manquant")

#     with get_conn() as conn:
#         conn.row_factory = sqlite3.Row
#         try:
#             cols = [r[1] for r in conn.execute('PRAGMA table_info("garanties")').fetchall()]
#         except Exception as e:
#             raise HTTPException(status_code=500, detail=f"Erreur DB (pragma): {e}")

#         pk_col = GARANTIE_PK_COL if GARANTIE_PK_COL in cols else ("NUM_DG" if "NUM_DG" in cols else cols[0])
#         uniq_col = GARANTIE_OR_UNIQ if GARANTIE_OR_UNIQ in cols else ("num_unique" if "num_unique" in cols else None)
#         if not uniq_col:
#             raise HTTPException(status_code=500, detail="Aucune colonne num_unique trouvée dans garanties")

#         # sanitization identique à garanties_detail
#         import re as _re
#         raw_u = u
#         norm_u = _re.sub(r"[^0-9A-Za-z]", "", raw_u)
#         san_expr = (
#             f'REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE('
#             f'CAST("{uniq_col}" AS TEXT),\' \',\'\'),\'-\',\'\'),\'/\',\'\'),\'.\',\'\'),\',\',\'\'),\'_\',\'\')'
#         )

#         # 1) match sur version "nettoyée"
#         row = conn.execute(
#             f'SELECT "{pk_col}" FROM "garanties" WHERE {san_expr} = ? LIMIT 1',
#             (norm_u,)
#         ).fetchone()

#         # 2) fallback sur valeur brute exacte
#         if not row:
#             row = conn.execute(
#                 f'SELECT "{pk_col}" FROM "garanties" WHERE CAST("{uniq_col}" AS TEXT) = ? LIMIT 1',
#                 (raw_u,)
#             ).fetchone()

#         if not row:
#             raise HTTPException(status_code=404, detail=f"Aucune garantie avec {uniq_col}={raw_u}")

#         pk = row[0]

#     return RedirectResponse(url=f"/garanties/item/{pk}", status_code=303)
