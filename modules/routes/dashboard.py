
import sqlite3
import re
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, JSONResponse
from starlette.status import HTTP_303_SEE_OTHER

# --- Imports robustes selon l'arborescence du projet -------------------------
try:
    from ..core.db import get_conn, ensure_schema  # type: ignore
except ImportError:
    from ..db import get_conn, ensure_schema  # type: ignore

try:
    from ..core.utils import is_pending_row, row_age_days, to_ddmmyyyy  # type: ignore
except ImportError:
    from ..utils import is_pending_row, row_age_days, to_ddmmyyyy  # type: ignore

# TEMPLATES : si aucun module interne n'existe, on crée l'instance ici
try:
    from ..core.ui import templates  # type: ignore
except ImportError:
    from starlette.templating import Jinja2Templates
    templates = Jinja2Templates(directory="templates")

router = APIRouter()

# ------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------

def _cession_prefix_from_row(row: dict) -> str:
    """
    Extrait le préfixe de cession depuis une ligne intervention (avant espace ou tiret).
    Ex: 'MAXI-CARE 123' -> 'MAXI', 'PEYRE 45' -> 'PEYRE'
    """
    c = str(row.get("Cession", "") or "").strip().upper()
    if not c:
        return ""
    c = c.replace("_", "-")
    part = c.split()[0]
    return part.split("-")[0]

def _parse_date_ddmmyyyy(s: str):
    from datetime import datetime
    try:
        return datetime.strptime(s, "%d/%m/%Y")
    except Exception:
        return None


def _wants_json(request: Request) -> bool:
    accept = (request.headers.get("accept") or "").lower()
    if "application/json" in accept or "text/json" in accept:
        return True
    xrw = (request.headers.get("x-requested-with") or "").lower()
    if xrw in {"xmlhttprequest", "fetch"}:
        return True
    return False


def _fmt_agenda_date(value: str) -> str:
    if not value:
        return ""
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%d/%m/%Y")
    except Exception:
        return value


def _set_intervention_traiter_flag(conn: sqlite3.Connection, rowid: int, value: str):
    if not rowid:
        return
    try:
        cols = [r[1] for r in conn.execute('PRAGMA table_info("interventions")')]
    except Exception:
        return
    target_col = None
    for name in ("Traiter", "traiter", "Traité", "Traite", "Traitee", "Traitée"):
        if name in cols:
            target_col = name
            break
    if not target_col:
        return
    conn.execute(f'UPDATE "interventions" SET "{target_col}" = ? WHERE ROWID = ?', (value, rowid))


def _build_agenda_context(conn: sqlite3.Connection, request: Request) -> dict:
    ensure_schema(conn)
    today = date.today()
    agenda_pending: list[dict] = []
    agenda_done: list[dict] = []
    agenda_counts: dict[str, dict[str, int]] = {}

    try:
        cur = conn.execute(
            """
            SELECT id, title, kind, event_date, event_time, status,
                   intervention_rowid, notes, created_at, updated_at
            FROM agenda_items
            WHERE status <> 'done'
            ORDER BY
                CASE WHEN event_date IS NULL OR TRIM(event_date) = '' THEN 1 ELSE 0 END,
                event_date,
                event_time
            """
        )
        for row in cur.fetchall():
            item = dict(row)
            item["DateTxt"] = _fmt_agenda_date(item.get("event_date", ""))
            item["TimeTxt"] = _fmt_agenda_time(item.get("event_time", ""))
            inter_id = item.get("intervention_rowid")
            item["LinkHref"] = f"/interventions-xml/item/{inter_id}" if inter_id else None
            iso_date = str(item.get("event_date") or "").strip()
            if iso_date:
                info = agenda_counts.setdefault(iso_date, {"pending": 0, "done": 0})
                info["pending"] += 1
            item["is_overdue"] = False
            item["is_today"] = False
            if iso_date:
                try:
                    d = datetime.strptime(iso_date, "%Y-%m-%d").date()
                    if d < today:
                        item["is_overdue"] = True
                    elif d == today:
                        item["is_today"] = True
                except Exception:
                    pass
            agenda_pending.append(item)
    except Exception:
        agenda_pending = []

    try:
        cur = conn.execute(
            """
            SELECT id, title, kind, event_date, event_time, status,
                   intervention_rowid, notes, created_at, updated_at
            FROM agenda_items
            WHERE status = 'done'
            ORDER BY updated_at DESC, event_date DESC
            LIMIT 30
            """
        )
        for row in cur.fetchall():
            item = dict(row)
            item["DateTxt"] = _fmt_agenda_date(item.get("event_date", ""))
            item["TimeTxt"] = _fmt_agenda_time(item.get("event_time", ""))
            inter_id = item.get("intervention_rowid")
            item["LinkHref"] = f"/interventions-xml/item/{inter_id}" if inter_id else None
            iso_date = str(item.get("event_date") or "").strip()
            if iso_date:
                info = agenda_counts.setdefault(iso_date, {"pending": 0, "done": 0})
                info["done"] += 1
            agenda_done.append(item)
    except Exception:
        agenda_done = []

    selected_date_iso = (request.query_params.get("agenda_date") or "").strip()
    selected_date = None
    if selected_date_iso and re.fullmatch(r"\d{4}-\d{2}-\d{2}", selected_date_iso):
        try:
            selected_date = datetime.strptime(selected_date_iso, "%Y-%m-%d").date()
        except ValueError:
            selected_date = None
    if not selected_date:
        selected_date = today
        selected_date_iso = selected_date.isoformat()

    month_param = (request.query_params.get("agenda_month") or "").strip()
    calendar_month_ref = selected_date.replace(day=1)
    if month_param and re.fullmatch(r"\d{4}-\d{2}", month_param):
        try:
            year_val, month_val = map(int, month_param.split("-"))
            calendar_month_ref = date(year_val, month_val, 1)
        except ValueError:
            pass

    prev_month_ref = (calendar_month_ref - timedelta(days=1)).replace(day=1)
    next_month_ref = (calendar_month_ref + timedelta(days=32)).replace(day=1)

    def _build_calendar(month_ref: date, counts: dict[str, dict[str, int]], selected_iso: str, today_date: date):
        first_day = month_ref.replace(day=1)
        start = first_day - timedelta(days=first_day.weekday())
        weeks: list[list[dict[str, object]]] = []
        day_iter = start
        for _ in range(6):
            week: list[dict[str, object]] = []
            for _ in range(7):
                iso = day_iter.isoformat()
                info = counts.get(iso, {})
                week.append({
                    "iso": iso,
                    "day": day_iter.day,
                    "in_month": day_iter.month == month_ref.month,
                    "count_pending": info.get("pending", 0),
                    "count_done": info.get("done", 0),
                    "is_today": day_iter == today_date,
                    "is_selected": iso == selected_iso,
                })
                day_iter += timedelta(days=1)
            weeks.append(week)
        return weeks

    calendar_weeks = _build_calendar(calendar_month_ref, agenda_counts, selected_date_iso, today)
    calendar_month_label = calendar_month_ref.strftime("%B %Y")
    calendar_month_str = calendar_month_ref.strftime("%Y-%m")
    calendar_prev_str = prev_month_ref.strftime("%Y-%m")
    calendar_next_str = next_month_ref.strftime("%Y-%m")

    agenda_selected_pending = [item for item in agenda_pending if str(item.get("event_date") or "").strip() == selected_date_iso]
    agenda_selected_done = [item for item in agenda_done if str(item.get("event_date") or "").strip() == selected_date_iso]
    agenda_selected_label = _fmt_agenda_date(selected_date_iso)
    if not agenda_selected_label and selected_date_iso:
        try:
            agenda_selected_label = datetime.strptime(selected_date_iso, "%Y-%m-%d").strftime("%d/%m/%Y")
        except Exception:
            agenda_selected_label = selected_date_iso

    prev_day_iso = (selected_date - timedelta(days=1)).isoformat()
    next_day_iso = (selected_date + timedelta(days=1)).isoformat()

    def _pending_sort_key(item: dict):
        date_key = str(item.get("event_date") or "9999-99-99")
        time_key = str(item.get("event_time") or "99:99")
        return (date_key, time_key, item.get("title") or "")

    agenda_pending_sorted = sorted(agenda_pending, key=_pending_sort_key)

    return {
        "agenda_pending": agenda_pending,
        "agenda_done": agenda_done,
        "agenda_pending_sorted": agenda_pending_sorted,
        "agenda_selected_pending": agenda_selected_pending,
        "agenda_selected_done": agenda_selected_done,
        "agenda_selected_label": agenda_selected_label,
        "agenda_selected_iso": selected_date_iso,
        "calendar_weeks": calendar_weeks,
        "calendar_month_label": calendar_month_label,
        "calendar_month_str": calendar_month_str,
        "calendar_prev_str": calendar_prev_str,
        "calendar_next_str": calendar_next_str,
        "today_iso": today.isoformat(),
        "agenda_prev_day_iso": prev_day_iso,
        "agenda_next_day_iso": next_day_iso,
    }

# ------------------------------------------------------------------------------
# Dashboard
# ------------------------------------------------------------------------------

@router.get("/", name="home_dashboard", include_in_schema=False)
def home_dashboard(request: Request):
    etl_stats = {"total": 0, "updated": 0, "inserted": 0, "run_at": None}

    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        ensure_schema(conn)

        # 1) Compteurs ETL (dernier run)
        try:
            row = conn.execute("SELECT MAX(run_at) AS last_run FROM etl_updates").fetchone()
            if row and row["last_run"]:
                etl_stats["run_at"] = row["last_run"]
                agg = conn.execute("""
                    SELECT
                        COALESCE(SUM(total), 0)    AS total,
                        COALESCE(SUM(updated), 0)  AS updated,
                        COALESCE(SUM(inserted), 0) AS inserted
                    FROM etl_updates
                    WHERE run_at = ?
                """, (row["last_run"],)).fetchone()
                etl_stats["total"]    = agg["total"] or 0
                etl_stats["updated"]  = agg["updated"] or 0
                etl_stats["inserted"] = agg["inserted"] or 0
        except Exception:
            # table etl_updates absente : on laisse les zéros
            pass

        # 2) Liste des INSERTS du dernier run (accordéon)
        etl_insert_list = []
        last_run = etl_stats.get("run_at")
        if last_run:
            # numéros insérés (journal détaillé)
            nums = [r[0] for r in conn.execute(
                "SELECT numero FROM etl_updates_rows WHERE run_at=? AND action='insert' ORDER BY numero",
                (last_run,)
            ).fetchall()]

            if nums:
                # Essayer de lire les infos complètes depuis 'interventions'
                # (si ta table d’UI principale s’appelle autrement, adapte ici)
                try:
                    placeholders = ",".join("?" * len(nums))
                    cur = conn.execute(
                        f'''
                        SELECT ROWID AS id,
                               "Numéro"            AS Numero,
                               COALESCE("Nom","Client") AS Client,
                               COALESCE("Libellé_Court","Machine") AS Machine,
                               "Cession"           AS Cession,
                               "Travaux"           AS Travaux
                        FROM interventions
                        WHERE "Numéro" IN ({placeholders})
                        ORDER BY "Numéro"
                        ''',
                        tuple(nums)
                    )
                    cols = [d[0] for d in cur.description]
                    etl_insert_list = [dict(zip(cols, r)) for r in cur.fetchall()]
                except Exception:
                    # Fallback minimal : afficher ce qu’on a dans le log
                    cur = conn.execute(
                        '''SELECT numero AS Numero, cession AS Cession,
                                  travaux_snippet AS Travaux
                           FROM etl_updates_rows
                           WHERE run_at=? AND action='insert'
                           ORDER BY numero''',
                        (last_run,)
                    )
                    cols = [d[0] for d in cur.description]
                    etl_insert_list = [dict(zip(cols, r)) for r in cur.fetchall()]

        # 3) Si la table interventions n’existe pas : rendre quand même la page avec les compteurs + liste inserts
        exists_interventions = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='interventions'"
        ).fetchone() is not None
        if not exists_interventions:
            return templates.TemplateResponse("dashboard.html", {
                "request": request,
                "etl_stats": etl_stats,
                "etl_insert_list": etl_insert_list,   # 👈 fourni au template
                "total_a_traiter": 0,
                "gt90": 0,
                "gt20_cession": 0,
                "gt90_list": [],
                "gt20_list": [],
            })

        # 4) KPIs et listes existantes (inchangés par rapport à ton code)
        cur = conn.execute('SELECT ROWID AS _rowid_, * FROM "interventions"')
        cols = [d[0] for d in cur.description]
        all_rows = [dict(zip(cols, r)) for r in cur.fetchall()]

        def _is_traiter_zero(row: dict) -> bool:
            v = row.get("traiter", 0)
            try:
                return int(str(v).strip() or "0") == 0
            except Exception:
                return str(v).strip().lower() in {"", "0", "non", "false", "no"}

        pending = [r for r in all_rows if is_pending_row(r) and _is_traiter_zero(r)]
        total_a_traiter = len(pending)

        gt90 = sum(1 for r in pending if (row_age_days(r) or 0) > 90)

        # Préfixes cession actifs pour le filtre >20 jours
        try:
            pref_rows = conn.execute(
                'SELECT "code_prefix" FROM "cession_codes" WHERE COALESCE("active",1)=1 AND COALESCE("include_gt20",1)=1'
            ).fetchall()
            prefixes_set = {p[0] for p in pref_rows if p and p[0]}
        except Exception:
            prefixes_set = set()

        def _cession_prefix_from_row(row: dict) -> str:
            c = str(row.get("Cession", "") or "").strip().upper()
            if not c:
                return ""
            c = c.replace("_", "-")
            part = c.split()[0]
            return part.split("-")[0]

        def _parse_date_ddmmyyyy(s: str):
            from datetime import datetime
            try:
                return datetime.strptime(s, "%d/%m/%Y")
            except Exception:
                return None

        def _date_txt(r):
            for k in ("Date_Deb_Trav", "Date", "Date_creation", "Date_Creation", "Date_Ouverture"):
                if r.get(k):
                    try:
                        return to_ddmmyyyy(str(r[k]))
                    except Exception:
                        pass
            return ""

        # Liste > 90 jours
        gt90_list = []
        for r in pending:
            age = row_age_days(r)
            if age is None or age <= 90:
                continue
            gt90_list.append({
                "id": r.get("_rowid_"),
                "Numero": r.get("Numéro") or "",
                "Client": r.get("Nom") or "",
                "Machine": r.get("Libellé_Court") or "",
                "DateTxt": _date_txt(r),
                "Type": r.get("Type") or "",
                "Cession": r.get("Cession") or "",
                "Jours": age,
            })

        # Tri >90
        sort_col90 = (request.query_params.get("sort_gt90") or "").strip()
        sort_dir90 = (request.query_params.get("dir_gt90") or "desc").lower()
        reverse90 = sort_dir90 == "desc"
        allowed90 = {"Numero", "Client", "Machine", "DateTxt", "Type", "Cession", "Jours"}

        def _key90(r: dict):
            if sort_col90 == "Jours":
                v = r.get("Jours")
                return (-10**9 if v is None else v) if reverse90 else (10**9 if v is None else v)
            if sort_col90 == "DateTxt":
                d = _parse_date_ddmmyyyy(r.get("DateTxt") or "")
                from datetime import datetime
                return d or (datetime.max if not reverse90 else datetime.min)
            if sort_col90 in {"Numero", "Client", "Machine", "Type", "Cession"}:
                t = (r.get(sort_col90) or "").strip().lower()
                return ("" if reverse90 else "zzz") if t == "" else t
            v = r.get("Jours")
            return (-10**9 if v is None else v) if reverse90 else (10**9 if v is None else v)

        if sort_col90 in allowed90:
            gt90_list.sort(key=_key90, reverse=reverse90)
        else:
            gt90_list.sort(key=lambda a: a["Jours"], reverse=True)

        # Liste > 20 jours (filtrée cession)
        gt20_cession = 0
        gt20_list = []
        for r in pending:
            age = row_age_days(r)
            if age is None or age <= 20:
                continue
            pref = _cession_prefix_from_row(r)
            if prefixes_set and not any(pref.startswith(p) for p in prefixes_set):
                continue
            gt20_cession += 1
            gt20_list.append({
                "id": r.get("_rowid_"),
                "Numero": r.get("Numéro") or "",
                "Client": r.get("Nom") or "",
                "Machine": r.get("Libellé_Court") or "",
                "DateTxt": _date_txt(r),
                "Type": r.get("Type") or "",
                "Cession": r.get("Cession") or "",
                "Jours": age,
            })

        # Tri >20
        sort_col = (request.query_params.get("sort_gt20") or "").strip()
        sort_dir = (request.query_params.get("dir_gt20") or "desc").lower()
        reverse = sort_dir == "desc"
        allowed = {"Numero", "Client", "Machine", "DateTxt", "Type", "Cession", "Jours"}

        def _key(r: dict):
            if sort_col == "Jours":
                v = r.get("Jours")
                return (-10**9 if v is None else v) if reverse else (10**9 if v is None else v)
            if sort_col == "DateTxt":
                d = _parse_date_ddmmyyyy(r.get("DateTxt") or "")
                from datetime import datetime
                return d or (datetime.max if not reverse else datetime.min)
            if sort_col in {"Numero", "Client", "Machine", "Type", "Cession"}:
                t = (r.get(sort_col) or "").strip().lower()
                return ("" if reverse else "zzz") if t == "" else t
            v = r.get("Jours")
            return (-10**9 if v is None else v) if reverse else (10**9 if v is None else v)

        if sort_col in allowed:
            gt20_list.sort(key=_key, reverse=reverse)
        else:
            gt20_list.sort(key=lambda a: a["Jours"], reverse=True)

        def _fmt_agenda_date(value: str) -> str:
            if not value:
                return ""
            try:
                return datetime.strptime(value, "%Y-%m-%d").strftime("%d/%m/%Y")
            except Exception:
                return value

        def _fmt_agenda_time(value: str) -> str:
            if not value:
                return ""
            return value[:5]

        agenda_ctx = _build_agenda_context(conn, request)

    # 5) Rendu avec la nouvelle variable etl_insert_list
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "etl_stats": etl_stats,
            "etl_insert_list": etl_insert_list,   # 👈 ajout essentiel
            "total_a_traiter": total_a_traiter,
            "gt90": gt90,
            "gt20_cession": gt20_cession,
            "gt90_list": gt90_list,
            "gt20_list": gt20_list,
            **agenda_ctx,
        },
    )


def _resolve_intervention_rowid(conn: sqlite3.Connection, ref: str | None) -> int | None:
    if not ref:
        return None
    s = str(ref).strip()
    if not s:
        return None
    try:
        rid = int(s)
        exists = conn.execute('SELECT 1 FROM "interventions" WHERE ROWID = ? LIMIT 1', (rid,)).fetchone()
        if exists:
            return rid
    except Exception:
        pass

    digits = re.sub(r"\D+", "", s)
    if not digits:
        return None
    san_expr = (
        "REPLACE(REPLACE(REPLACE(REPLACE(REPLACE("
        "CAST(\"Numéro\" AS TEXT),' ',''),'-',''),'.',''),'/',''),'_','')"
    )
    row = conn.execute(
        f'SELECT ROWID FROM "interventions" WHERE {san_expr} LIKE ? ORDER BY ROWID DESC LIMIT 1',
        (f'%{digits}',)
    ).fetchone()
    if row:
        return int(row[0])
    return None


@router.get("/agenda", name="agenda_page")
def agenda_page(request: Request):
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        ensure_schema(conn)
        agenda_ctx = _build_agenda_context(conn, request)

    return templates.TemplateResponse(
        "agenda.html",
        {
            "request": request,
            **agenda_ctx,
        },
    )


@router.post("/agenda/add", name="agenda_add")
async def agenda_add(request: Request):
    form = await request.form()
    title = (form.get("title") or "").strip()
    if not title:
        return RedirectResponse(url='/?agenda_error=title', status_code=HTTP_303_SEE_OTHER)

    kind = (form.get("kind") or "autre").strip().lower()
    if kind not in {"intervention", "rendezvous", "autre"}:
        kind = "autre"

    event_date = (form.get("event_date") or "").strip()
    event_time = (form.get("event_time") or "").strip()
    notes = (form.get("notes") or "").strip()
    intervention_ref = (form.get("intervention_id") or "").strip()

    with get_conn() as conn:
        ensure_schema(conn)
        intervention_rowid = _resolve_intervention_rowid(conn, intervention_ref)
        cur = conn.execute(
            """
            INSERT INTO agenda_items (title, kind, event_date, event_time, notes, intervention_rowid, status)
            VALUES (?, ?, ?, ?, ?, ?, 'pending')
            """,
            (
                title,
                kind,
                event_date or None,
                event_time or None,
                notes or None,
                intervention_rowid,
            ),
        )
        conn.commit()
        agenda_id = cur.lastrowid

    if _wants_json(request):
        return JSONResponse({"status": "ok", "id": agenda_id})

    return RedirectResponse(url='/?agenda=added', status_code=HTTP_303_SEE_OTHER)


@router.post("/agenda/{item_id}/toggle", name="agenda_toggle")
async def agenda_toggle(item_id: int, request: Request):
    with get_conn() as conn:
        ensure_schema(conn)
        conn.row_factory = sqlite3.Row
        row = conn.execute('SELECT status, intervention_rowid FROM agenda_items WHERE id = ?', (item_id,)).fetchone()
        if not row:
            if _wants_json(request):
                return JSONResponse({"status": "error", "detail": "agenda item not found"}, status_code=404)
            return RedirectResponse(url='/?agenda=missing', status_code=HTTP_303_SEE_OTHER)

        current_status = (row["status"] or '').strip().lower()
        new_status = 'pending' if current_status == 'done' else 'done'

        conn.execute(
            """
            UPDATE agenda_items
            SET status = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (new_status, item_id),
        )
        if new_status == 'pending':
            try:
                _set_intervention_traiter_flag(conn, int(row["intervention_rowid"] or 0), '0')
            except Exception:
                pass
        conn.commit()

    if _wants_json(request):
        return JSONResponse({"status": "ok", "new_status": new_status})

    return RedirectResponse(url='/?agenda=toggled', status_code=HTTP_303_SEE_OTHER)


@router.post("/agenda/{item_id}/delete", name="agenda_delete")
async def agenda_delete(item_id: int, request: Request):
    with get_conn() as conn:
        ensure_schema(conn)
        conn.execute('DELETE FROM agenda_items WHERE id = ?', (item_id,))
        conn.commit()

    if _wants_json(request):
        return JSONResponse({"status": "ok"})

    return RedirectResponse(url='/?agenda=deleted', status_code=HTTP_303_SEE_OTHER)
