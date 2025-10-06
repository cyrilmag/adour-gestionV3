from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, JSONResponse
from ..core.ui import templates
from ..core.db import get_conn, ensure_schema
import sqlite3

router = APIRouter()

# --- helpers de schéma -----------------------------------------------

def _ensure_cession_garantie(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cession_garantie (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          type_garantie TEXT NOT NULL UNIQUE,
          active INTEGER NOT NULL DEFAULT 1,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()

def _ensure_type_facturation(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS type_facturation (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          libelle TEXT NOT NULL UNIQUE,
          active INTEGER NOT NULL DEFAULT 1,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Seed si vide
    count = conn.execute("SELECT COUNT(*) FROM type_facturation").fetchone()[0]
    if count == 0:
        conn.executemany("INSERT INTO type_facturation(libelle, active) VALUES (?,1)", [
            ("FACTURABLE",),("GARANTIE",),("MAXI CARE",),("CESSION",),("OS",),
            ("ENTRETIEN PREVENTIF",),("AVOIR",),("MALFACON",),("AFFAIRE COMMERCIALE",),
            ("SUPPRIMER",),("CAMPA CARE",),("LOCATION CLAAS",),
            ("GARANTIE OCCASION",),("ASSURANCE",),
        ])
    conn.commit()

# --- page Paramètres --------------------------------------------------
def _ensure_cession_garantie(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cession_garantie (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          type_garantie TEXT NOT NULL UNIQUE,
          active INTEGER NOT NULL DEFAULT 1,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # --- NEW: colonne is_remboursable (0/1)
    try:
        conn.execute('ALTER TABLE cession_garantie ADD COLUMN is_remboursable INTEGER NOT NULL DEFAULT 0')
        conn.commit()
    except Exception:
        pass
    conn.commit()

@router.get("/parametres")
def page_parametres(request: Request):
    with get_conn() as conn:
        ensure_schema(conn)
        _ensure_cession_garantie(conn)
        _ensure_type_facturation(conn)

        # S'assurer des colonnes sur cession_codes (silencieux si déjà présentes)
        for col in ("type_garantie", "type_facturation"):
            try:
                conn.execute(f"ALTER TABLE cession_codes ADD COLUMN {col} TEXT")
                conn.commit()
            except Exception:
                pass

        # CODES CESSION
        cur = conn.execute(
            "SELECT code_prefix, label, include_gt20, active, type_garantie, type_facturation "
            "FROM cession_codes ORDER BY code_prefix"
        )
        cols = [d[0] for d in cur.description] if cur.description else \
               ["code_prefix","label","include_gt20","active","type_garantie","type_facturation"]
        cessions = [dict(zip(cols, r)) for r in cur.fetchall()]

        # LISTE TYPES GARANTIE (alias -> libelle)
        gcur = conn.execute(
            "SELECT id, type_garantie AS libelle, active, "
            "COALESCE(is_remboursable,0) AS is_remboursable "
            "FROM cession_garantie ORDER BY type_garantie COLLATE NOCASE"
        )
        garanties_types = [dict(zip([d[0] for d in gcur.description], r)) for r in gcur.fetchall()]


        # LISTE TYPES FACTURATION
        fcur = conn.execute(
            "SELECT id, libelle, active FROM type_facturation ORDER BY libelle COLLATE NOCASE"
        )
        facturation_types = [dict(zip([d[0] for d in fcur.description], r)) for r in fcur.fetchall()]

    return templates.TemplateResponse("parametres.html", {
        "request": request,
        "cessions": cessions,
        "garanties_types": garanties_types,
        "facturation_types": facturation_types,
    })

# --- CODES CESSION : actions -----------------------------------------

@router.post("/parametres/cession/add")
async def add_cession(request: Request):
    form = await request.form()
    prefix = str(form.get("code_prefix","")).strip().upper()
    label = str(form.get("label","")).strip()
    if prefix:
        with get_conn() as conn:
            ensure_schema(conn)
            conn.execute(
                "INSERT OR IGNORE INTO cession_codes(code_prefix, label, include_gt20, active) "
                "VALUES (?, ?, 1, 1)",
                (prefix, label or None)
            )
            conn.commit()
    return RedirectResponse(url="/parametres", status_code=303)

@router.post("/parametres/cession/toggle-gt20")
async def toggle_gt20(request: Request):
    form = await request.form()
    prefix = str(form.get("prefix","")).strip().upper()
    with get_conn() as conn:
        conn.execute(
            "UPDATE cession_codes SET include_gt20 = CASE include_gt20 WHEN 1 THEN 0 ELSE 1 END "
            "WHERE code_prefix = ?", (prefix,)
        )
        conn.commit()
    return RedirectResponse(url="/parametres", status_code=303)

@router.post("/parametres/cession/toggle-active")
async def toggle_active(request: Request):
    form = await request.form()
    prefix = str(form.get("prefix","")).strip().upper()
    with get_conn() as conn:
        conn.execute(
            "UPDATE cession_codes SET active = CASE active WHEN 1 THEN 0 ELSE 1 END "
            "WHERE code_prefix = ?", (prefix,)
        )
        conn.commit()
    return RedirectResponse(url="/parametres", status_code=303)

@router.post("/parametres/cession/delete")
async def delete_cession(request: Request):
    form = await request.form()
    prefix = str(form.get("prefix","")).strip().upper()
    with get_conn() as conn:
        conn.execute("DELETE FROM cession_codes WHERE code_prefix = ?", (prefix,))
        conn.commit()
    return RedirectResponse(url="/parametres", status_code=303)

@router.post("/parametres/cession/bulk")
async def bulk_cession(request: Request):
    form = await request.form()
    op = str(form.get("op","")).strip()
    prefixes = form.getlist("prefix")
    if not prefixes:
        return RedirectResponse(url="/parametres", status_code=303)
    with get_conn() as conn:
        if op == "include_on":
            conn.executemany("UPDATE cession_codes SET include_gt20 = 1 WHERE code_prefix = ?", [(p,) for p in prefixes])
        elif op == "include_off":
            conn.executemany("UPDATE cession_codes SET include_gt20 = 0 WHERE code_prefix = ?", [(p,) for p in prefixes])
        elif op == "toggle_include":
            for p in prefixes:
                conn.execute(
                    "UPDATE cession_codes SET include_gt20 = CASE include_gt20 WHEN 1 THEN 0 ELSE 1 END "
                    "WHERE code_prefix = ?", (p,)
                )
        conn.commit()
    return RedirectResponse(url="/parametres", status_code=303)

@router.post("/parametres/cession/update-type")
async def cession_update_type(request: Request):
    form = await request.form()
    prefix = (form.get("prefix") or "").strip()
    tg = (form.get("type_garantie") or "").strip() or None
    if prefix:
        with get_conn() as conn:
            conn.execute("UPDATE cession_codes SET type_garantie=? WHERE code_prefix=?", (tg, prefix))
            conn.commit()
    return RedirectResponse(url="/parametres", status_code=303)

@router.post("/parametres/cession/update-facturation")
async def cession_update_facturation(request: Request):
    form = await request.form()
    prefix = (form.get("prefix") or "").strip()
    tf = (form.get("type_facturation") or "").strip() or None
    if prefix:
        with get_conn() as conn:
            conn.execute("UPDATE cession_codes SET type_facturation=? WHERE code_prefix=?", (tf, prefix))
            conn.commit()
    return RedirectResponse(url="/parametres", status_code=303)

# --- GARANTIE : CRUD --------------------------------------------------

@router.post("/parametres/garantie-type/add")
async def add_garantie_type(request: Request):
    form = await request.form()
    tg = (form.get("type_garantie") or "").strip()
    if tg:
        with get_conn() as conn:
            _ensure_cession_garantie(conn)
            conn.execute("INSERT OR IGNORE INTO cession_garantie(type_garantie, active) VALUES (?, 1)", (tg,))
            conn.commit()
    return RedirectResponse(url="/parametres", status_code=303)

@router.post("/parametres/garantie-type/toggle-active")
async def toggle_garantie_type_active(request: Request):
    form = await request.form()
    gid = int(form.get("id", "0"))
    if gid:
        with get_conn() as conn:
            conn.execute(
                "UPDATE cession_garantie SET active = CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id = ?", (gid,)
            )
            conn.commit()
    return RedirectResponse(url="/parametres", status_code=303)

@router.post("/parametres/garantie-type/rename")
async def rename_garantie_type(request: Request):
    form = await request.form()
    gid = int(form.get("id", "0"))
    tg  = (form.get("type_garantie") or "").strip()
    if gid and tg:
        with get_conn() as conn:
            conn.execute("UPDATE cession_garantie SET type_garantie = ? WHERE id = ?", (tg, gid))
            conn.commit()
    return RedirectResponse(url="/parametres", status_code=303)

@router.post("/parametres/garantie-type/delete")
async def delete_garantie_type(request: Request):
    form = await request.form()
    gid = int(form.get("id", "0"))
    if gid:
        with get_conn() as conn:
            conn.execute("DELETE FROM cession_garantie WHERE id = ?", (gid,))
            conn.commit()
    return RedirectResponse(url="/parametres", status_code=303)

# --- FACTURATION : CRUD -----------------------------------------------

@router.post("/parametres/facturation-type/add")
async def add_facturation_type(request: Request):
    form = await request.form()
    lib = (form.get("libelle") or "").strip()
    if lib:
        with get_conn() as conn:
            _ensure_type_facturation(conn)
            conn.execute("INSERT OR IGNORE INTO type_facturation(libelle, active) VALUES (?,1)", (lib,))
            conn.commit()
    return RedirectResponse(url="/parametres", status_code=303)

@router.post("/parametres/facturation-type/rename")
async def rename_facturation_type(request: Request):
    form = await request.form()
    fid = int(form.get("id", "0"))
    lib = (form.get("libelle") or "").strip()
    if fid and lib:
        with get_conn() as conn:
            conn.execute("UPDATE type_facturation SET libelle=? WHERE id=?", (lib, fid))
            conn.commit()
    return RedirectResponse(url="/parametres", status_code=303)

@router.post("/parametres/facturation-type/toggle-active")
async def toggle_facturation_type_active(request: Request):
    form = await request.form()
    fid = int(form.get("id", "0"))
    if fid:
        with get_conn() as conn:
            conn.execute("UPDATE type_facturation SET active = CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=?", (fid,))
            conn.commit()
    return RedirectResponse(url="/parametres", status_code=303)

@router.post("/parametres/facturation-type/delete")
async def delete_facturation_type(request: Request):
    form = await request.form()
    fid = int(form.get("id", "0"))
    if fid:
        with get_conn() as conn:
            conn.execute("DELETE FROM type_facturation WHERE id=?", (fid,))
            conn.commit()
    return RedirectResponse(url="/parametres", status_code=303)

# --- ALIAS JSON (si ton template appelle ...-json via fetch) ----------

@router.post("/parametres/cession/toggle-gt20-json")
async def toggle_gt20_json(request: Request):
    form = await request.form()
    prefix = str(form.get("prefix","")).strip().upper()
    with get_conn() as conn:
        conn.execute(
            "UPDATE cession_codes SET include_gt20 = CASE include_gt20 WHEN 1 THEN 0 ELSE 1 END "
            "WHERE code_prefix = ?", (prefix,)
        )
        conn.commit()
    return JSONResponse({"ok": True})

@router.post("/parametres/cession/toggle-active-json")
async def toggle_active_json(request: Request):
    form = await request.form()
    prefix = str(form.get("prefix","")).strip().upper()
    with get_conn() as conn:
        conn.execute(
            "UPDATE cession_codes SET active = CASE active WHEN 1 THEN 0 ELSE 1 END "
            "WHERE code_prefix = ?", (prefix,)
        )
        conn.commit()
    return JSONResponse({"ok": True})

@router.post("/parametres/cession/delete-json")
async def delete_cession_json(request: Request):
    form = await request.form()
    prefix = str(form.get("prefix","")).strip().upper()
    with get_conn() as conn:
        conn.execute("DELETE FROM cession_codes WHERE code_prefix = ?", (prefix,))
        conn.commit()
    return JSONResponse({"ok": True})

@router.post("/parametres/garantie-type/toggle-remboursable")
async def toggle_garantie_type_remboursable(request: Request):
    form = await request.form()
    gid = int(form.get("id", "0"))
    if gid:
        with get_conn() as conn:
            conn.execute(
                "UPDATE cession_garantie "
                "SET is_remboursable = CASE COALESCE(is_remboursable,0) WHEN 1 THEN 0 ELSE 1 END "
                "WHERE id = ?",
                (gid,)
            )
            conn.commit()
    return RedirectResponse(url="/parametres", status_code=303)
