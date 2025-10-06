from __future__ import annotations
from fastapi import APIRouter, Request, HTTPException, Query
from fastapi.responses import RedirectResponse
from starlette.status import HTTP_303_SEE_OTHER
import sqlite3
from typing import Any, Dict, List, Optional
from datetime import datetime
import re

# --- Core imports from your project ---
from ..core.ui import templates
from ..core.db import get_conn, pragma_columns
from ..core.utils import detect_columns_for_detail, extract_num_base_and_short

router = APIRouter()

# =============================================================================
#  CONFIG — adapté à la table SQLite "garanties" de adour.db
# =============================================================================

TABLE_NAME = "garanties"
PK_COL     = "NUM_DG"        # PK numérique
OR_COL     = "NUM_OR"        # N° OR
OR_UNIQ    = "num_unique"    # clé dérivée (souvent les 5 derniers du OR)

# libellés UI
DISPLAY_LABELS: dict[str, str] = {
    "NUM_DG": "N° DG",
    "NUM_OR": "N° OR",
    "Client": "Client",
    "Type": "Type de garantie",
    "Machine": "Machine",
    "NUMERO_DE_SERIE": "N° de série",
    "Organe": "Panne",
    "Heures/Ha": "Heures / Ha",
    "Date": "Date de la demande",
    "Date_OR": "Date OR",
    "date_paiement": "Date de paiement",
    "Enregistré": "Enregistré ?",
    "Valide": "Validée ?",
    "Actif": "Actif ?",
    "Pieces_Demandes": "Pièces demandées",
    "Pieces_Rembourses": "Pièces remboursées",
    "Pieces_Envoyes": "Pièces envoyées",
    "Pieces_Refuses": "Pièces refusées",
    "MO_Demande": "Main d’œuvre demandée",
    "MO_Rembourse": "Main d’œuvre remboursée",
    "MO_Refuse": "Main d’œuvre refusée",
    "Montant_non_rembourse": "Montant non remboursé",
    "Emplacement": "Emplacement (local garantie)",
    "Date_rebus": "Date rebut",
    "MOTIF_REFUS": "Motif du refus",
    "AUDIT": "Audit",
    "Compte_rendu_audit": "Compte rendu d’audit",
    "Echantillons_huiles": "Échantillon d’huiles",
    "franchise": "Franchise",
    "NUM_Recepice": "N° Récépicé",
    "REMBOUSEMENT_PARTIEL": "Remboursement partiel",
    "Piéces_mise_au rebus": "Pièces mise au rebut",
}

HELP_TEXT: dict[str, str] = {
    "NUM_DG": "Identifiant interne du dossier garantie.",
    "NUM_OR": "Numéro d’ordre de réparation (OR).",
    "Date_OR": "Format jj/mm/aaaa.",
    "Date": "Format jj/mm/aaaa.",
    "date_paiement": "Format jj/mm/aaaa.",
    "Montant_non_rembourse": "Calculé automatiquement: (Pièces + MO demandés) - (Pièces + MO remboursés).",
    "NUMÉRO DE SÉRIE": "Saisir le n° complet tel que sur la plaque.",
    "franchise": "Cochez pour appliquer une franchise de 150.",
    "Emplacement": "Se remplit avec le mois de la Date (ex : 09/2025).",
    "Date_rebus": "Peut être auto-calculée 90 jours après paiement.",
    "Piéces mise au rebus": "✅ si la date rebus est passée ; ❌ sinon (valeur 1/0 enregistrée).",
}

# Fallback statique si la base ne fournit rien
TYPE_GARANTIE_OPTIONS = [
    "GARANTIE PIECES","PARTICIPATION COMMERCIALE","OS","GARANTIE COMMERCIALE","MOL","SOL",
    "INCIDENT","GARANTIE","MAXI CARE","GARANTIE AMAZONE","GARANTIE MASHIO","CAMPA CARE",
    "MOL CLAAS LOCATION","SOL CLAAS LOCATION","VISITE FIN GARANTIE","GARANTIE MAUGUIN",
    "GARANTIE ROUSSEAU","Amazone Ticket service","GARANTIE ALO",
]

WIDGETS: dict[str, str] = {
    "Type": "select-typegarantie",
    "Montant_non_rembourse": "number",
    "Heures/Ha": "number",
    "Date": "date",
    "Date_OR": "date",
    "date_paiement": "date",
    "Commentaire": "textarea",
    "Observations": "textarea",
    "Panne": "textarea",
    "Détails": "textarea",
    "Description": "textarea",
    "Notes": "textarea",
    "Compte_rendu_audit": "textarea",
    "franchise": "checkbox-franchise",
    "MOTIF_REFUS": "textarea",
    "AUDIT":"checkbox",
    # numériques
    "Pieces_Demandes": "number",
    "Pieces_Rembourses": "number",
    "Pieces_Refuses": "number",
    "MO_Demande": "number",
    "MO_Rembourse": "number",
    "MO_Refuse": "number",
    "Piéces_mise_au rebus": "rebus-indicator",
}

PLACEHOLDERS: dict[str, str] = {
    "Client": "Nom du client…",
    "Machine": "Ex: ARION 610 CONCEPT…",
    "NUMERO_DE_SERIE": "Ex: V123XYZ456…",
    "Organe": "Ex: Fuite hydraulique…",
    "NUM_OR": "Numéro OR si connu…",
}

HIDE_FIELDS: set[str] = {
    "Colonne1",
    "_rowid_",
    "num_unique",
    "REMBOUSEMENT_PARTIEL",
}

READONLY_FIELDS: set[str] = {
    PK_COL,
}

TABS: list[tuple[str, list[str]]] = [
    ("Général", [PK_COL, OR_COL, "Type", "Client", "Date_OR", "Date", "NUM_Recepice"]),
    ("Machine", ["Machine", "NUMERO_DE_SERIE", "Organe", "Heures/Ha", "franchise", "Echantillons_huiles"]),
    ("Pièces et Main d'Oeuvre", [
        "Pieces_Demandes","Pieces_Rembourses","Pieces_Refuses","MO_Demande","MO_Rembourse","MO_Refuse","Pieces_Envoyes"
    ]),
    ("Remboursement", ["date_paiement","Montant_non_rembourse","MOTIF_REFUS","REMBOUSEMENT_PARTIEL"]),
    ("Local Garantie", ["Emplacement","Date_rebus","Piéces_mise_au rebus"]),
    ("Audit", ["AUDIT","Compte_rendu_audit"]),
]

# =============================================================================
#  Utils dates / formats
# =============================================================================

DATE_COL_HINTS = {"Date", "Date_OR", "Date facture", "date_paiement", "Date_rebus"}

def _is_date_col(name: str) -> bool:
    return ("date" in (name or "").lower()) or (name in DATE_COL_HINTS)

def _try_parse_date(value: str) -> datetime | None:
    if not value:
        return None
    value = str(value).strip()
    for fmt in ("%Y-%m-%d","%Y-%m-%d %H:%M:%S","%d/%m/%Y","%d-%m-%Y","%d.%m.%Y","%Y/%m/%d","%Y.%m.%d","%d/%m/%y","%d-%m-%y","%Y%m%d"):
        try:
            return datetime.strptime(value, fmt)
        except Exception:
            pass
    return None

def _format_date_for_display(value: str) -> str:
    if not value:
        return ""
    dt = _try_parse_date(value)
    return dt.strftime("%d/%m/%Y") if dt else value

def _format_date_for_storage(value: str) -> str:
    if not value:
        return ""
    dt = _try_parse_date(value)
    return dt.strftime("%Y-%m-%d") if dt else value

def _digits(s: str | int | None) -> str:
    return "".join(re.findall(r"\d", str(s or "")))

# =============================================================================
#  Calculs montants
# =============================================================================

def _to_float(x) -> float:
    if x is None:
        return 0.0
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip().replace(" ", "")
    if s == "":
        return 0.0
    s = s.replace(",", ".")
    try:
        return float(s)
    except Exception:
        return 0.0

def _compute_refused_and_non_remb(payload: dict) -> None:
    pd = _to_float(payload.get("Pieces_Demandes"))
    pr = _to_float(payload.get("Pieces_Rembourses"))
    md = _to_float(payload.get("MO_Demande"))
    mr = _to_float(payload.get("MO_Rembourse"))
    pieces_ref = max(pd - pr, 0.0)
    mo_ref = max(md - mr, 0.0)
    montant_non = max((pd + md) - (pr + mr), 0.0)
    payload["Pieces_Refuses"] = f"{pieces_ref:.2f}".rstrip("0").rstrip(".")
    payload["MO_Refuse"] = f"{mo_ref:.2f}".rstrip("0").rstrip(".")
    payload["Montant_non_rembourse"] = f"{montant_non:.2f}".rstrip("0").rstrip(".")

# =============================================================================
#  DB helpers
# =============================================================================

def _all_columns(conn: sqlite3.Connection) -> List[str]:
    cols = pragma_columns(conn, TABLE_NAME)
    preferred = [PK_COL, OR_COL, "Type", "Client", "Machine", "Date_OR"]
    return [c for c in preferred if c in cols] + [c for c in cols if c not in preferred]

def _next_numero(conn: sqlite3.Connection) -> str:
    cur = conn.execute(f'SELECT COALESCE(MAX(CAST("{PK_COL}" AS INTEGER)), 0) + 1 FROM "{TABLE_NAME}"')
    return str(cur.fetchone()[0])

def _type_from_cession(conn: sqlite3.Connection, cession_code: str | None) -> str | None:
    """
    Calcule le type de garantie depuis le code de cession.
    Priorité: table 'cession_code' (singulier) avec colonne 'type_garantie'.
    Fallback: 'cession_codes' si besoin.
    Règle: préfixe actif le plus long.
    """
    if not cession_code:
        return None
    code = str(cession_code).strip().upper()

    for table in ("cession_code", "cession_codes"):
        try:
            rows = conn.execute(
                f'SELECT code_prefix, type_garantie, COALESCE(active,1) as active FROM "{table}"'
            ).fetchall()
        except sqlite3.Error:
            rows = []

        if not rows:
            continue

        best = None; best_len = -1
        fallback = None; fallback_len = -1
        for prefix, typ, active in rows:
            if not prefix or not typ or not active:
                continue
            p = str(prefix).strip().upper()
            if code.startswith(p) and len(p) > best_len:
                best = str(typ).strip(); best_len = len(p)
            trimmed = p.rstrip(" 0123456789")
            if trimmed and trimmed != p and code.startswith(trimmed) and len(trimmed) > fallback_len:
                fallback = str(typ).strip(); fallback_len = len(trimmed)
        if best:
            return best
        if fallback:
            return fallback

    return None
def _load_type_garantie_from_cession_garantie_only(conn: sqlite3.Connection) -> list[str]:
    """
    Retourne uniquement la liste depuis la table cession_garantie.type_garantie (active=1).
    Pas de fallback vers cession_codes.
    """
    try:
        cur = conn.execute('SELECT type_garantie FROM "cession_garantie" WHERE COALESCE(active,1)=1 ORDER BY 1')
        opts = [str(r[0]).strip() for r in cur.fetchall() if r and str(r[0]).strip()]
    except sqlite3.Error:
        opts = []
    # dédup
    seen, dedup = set(), []
    for x in opts:
        if x not in seen:
            dedup.append(x); seen.add(x)
    return dedup

def _load_type_garantie_options(conn: sqlite3.Connection) -> list[str]:
    """
    Options de la liste déroulante Type:
    1) DISTINCT type_garantie depuis cession_code (singulier) actifs
    2) fallback: cession_codes
    3) fallback: cession_garantie (type_garantie/libelle/label)
    4) fallback: liste statique
    """
    def _distinct_from(table: str, col: str = "type_garantie") -> list[str]:
        try:
            cur = conn.execute(
                f'SELECT DISTINCT {col} FROM "{table}" WHERE COALESCE(active,1)=1 ORDER BY 1'
            )
            return [str(r[0]).strip() for r in cur.fetchall() if r and str(r[0]).strip()]
        except sqlite3.Error:
            return []

    # 1) cession_code
    opts = _distinct_from("cession_code")
    # 2) cession_codes (fallback)
    if not opts:
        opts = _distinct_from("cession_codes")
    # 3) cession_garantie (colonnes possibles)
    if not opts:
        for col in ("type_garantie","libelle","label","Libelle","Label"):
            opts = _distinct_from("cession_garantie", col)
            if opts:
                break
    # 4) statique
    if not opts:
        opts = list(TYPE_GARANTIE_OPTIONS)

    # déduplication
    seen = set(); dedup = []
    for x in opts:
        if x not in seen:
            dedup.append(x); seen.add(x)
    return dedup

def _load_type_garantie_from_cession_garantie(conn: sqlite3.Connection) -> list[str]:
    """
    Charge la liste depuis la table cession_garantie (priorité à la colonne type_garantie,
    sinon libelle/label, avec ORDER BY et déduplication).
    """
    def _distinct(col: str) -> list[str]:
        try:
            cur = conn.execute(f'SELECT {col} FROM "cession_garantie" WHERE {col} IS NOT NULL AND TRIM({col}) <> "" ORDER BY 1')
            return [str(r[0]).strip() for r in cur.fetchall() if r and str(r[0]).strip()]
        except sqlite3.Error:
            return []

    for col in ("type_garantie", "libelle", "label", "Libelle", "Label"):
        opts = _distinct(col)
        if opts:
            # dédup au cas où
            seen, out = set(), []
            for x in opts:
                if x not in seen:
                    out.append(x); seen.add(x)
            return out

    return []

def _prefill_from_intervention(conn: sqlite3.Connection, rowid: int) -> tuple[dict[str, str], bool, str]:
    """
    Préremplit une garantie à partir d'une intervention (ROWID).
    IMPORTANT: 'Type' est dérivé UNIQUEMENT du 'code cession' via _type_from_cession.
    """
    candidates = ["interventions","interventions_xml","interventions_narrosse","interventions_peyrehorade"]
    row = None
    for t in candidates:
        try:
            r = conn.execute(f'SELECT ROWID AS _rowid_, * FROM "{t}" WHERE ROWID = ?', (rowid,)).fetchone()
            if r:
                row = r; break
        except sqlite3.Error:
            continue
    if not row:
        return ({}, False, "")

    d = dict(row)
    cols_names = [k for k in d.keys() if k != "_rowid_"]
    try:
        colmap = detect_columns_for_detail(cols_names)
    except Exception:
        colmap = {"numero": None, "nom": None, "libcourt": None, "date": None, "cession": None, "site": None}

    numero_col  = colmap.get("numero")
    nom_col     = colmap.get("nom")
    lib_col     = colmap.get("libcourt")
    date_col    = colmap.get("date")
    cession_col = colmap.get("cession")
    site_col    = colmap.get("site")

    num_or_long = (d.get(numero_col) if numero_col else None) or d.get("Numéro") or d.get("N° OR") or d.get("N°OR") or d.get("N_OR") or d.get("num_short") or ""
    client      = (d.get(nom_col) if nom_col else None) or d.get("Nom") or ""
    machine     = (d.get(lib_col) if lib_col else None) or d.get("Libellé_Court") or d.get("Libelle_Court") or ""
    # ⛔️ On NE lit plus "Type" depuis l'intervention
    date_or     = (d.get(date_col) if date_col else None) or d.get("Date") or d.get("Date_Deb_Trav") or d.get("Date_Ouverture") or ""
    cession     = (d.get(cession_col) if cession_col else None) \
                  or d.get("Code cession") or d.get("code_cession") \
                  or d.get("CESSION") or d.get("cession") or d.get("Cession")

    try:
        _, num_short = extract_num_base_and_short(d, site_col, numero_col)
    except Exception:
        digits = _digits(num_or_long); num_short = digits[-5:] if digits else ""

    mapped = _type_from_cession(conn, cession)

    pre = {
        OR_COL: str(num_short or "").strip(),
        "Client": client,
        "Machine": machine,
        "Type": mapped or "",   # ✅ toujours via cession
        "Date_OR": date_or,
    }

    no_serie = d.get("No_Serie") or d.get("No Serie") or d.get("NoSerie")
    if no_serie:
        pre["NUMERO_DE_SERIE"] = str(no_serie).strip()

    note_int = d.get("Note") or d.get("Notes") or d.get("Panne") or d.get("Commentaire") or d.get("Commentaires")
    if note_int and not pre.get("Organe"):
        pre["Organe"] = str(note_int).strip()

    heures = d.get("Heures") or d.get("Heure") or d.get("Heure/ha") or d.get("Heure_Ha")
    if heures is not None and pre.get("Heures/Ha", "") == "":
        pre["Heures/Ha"] = str(heures).strip()

    # formatage dates
    for k, v in list(pre.items()):
        if _is_date_col(k) and isinstance(v, str):
            pre[k] = _format_date_for_display(v)

    if OR_UNIQ in pragma_columns(conn, TABLE_NAME):
        digits = _digits(pre.get(OR_COL, ""))
        pre[OR_UNIQ] = digits[-5:] if digits else ""

    prefilled_from_cession = bool(mapped)
    return (pre, prefilled_from_cession, (mapped or ""))

def _type_from_existing_interventions(conn: sqlite3.Connection, num_or: str | None) -> Optional[str]:
    if not num_or:
        return None
    digits = _digits(num_or)
    if not digits:
        return None
    num_short = digits[-5:]
    if not num_short:
        return None

    candidates = ["interventions","interventions_xml","interventions_narrosse","interventions_peyrehorade"]
    for t in candidates:
        try:
            cols_info = conn.execute(f'PRAGMA table_info("{t}")').fetchall()
        except sqlite3.Error:
            continue
        table_cols = [c[1] for c in cols_info]
        try:
            colmap = detect_columns_for_detail(table_cols)
        except Exception:
            colmap = {"numero": None, "cession": None}
        numero_col = colmap.get("numero")
        cession_col = colmap.get("cession")

        row = None
        if "num_short" in table_cols:
            try:
                row = conn.execute(
                    f'SELECT ROWID AS _rowid_, * FROM "{t}" WHERE CAST("num_short" AS TEXT) = ? LIMIT 1',
                    (num_short,),
                ).fetchone()
            except sqlite3.Error:
                row = None
        if not row and numero_col:
            san_expr = (
                f'REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE('
                f'CAST("{numero_col}" AS TEXT),\' \',\'\'),\'-\',\'\'),\'/\',\'\'),\'.\',\'\'),\',\',\'\'),\'_\',\'\')'
            )
            try:
                row = conn.execute(
                    f'SELECT ROWID AS _rowid_, * FROM "{t}" WHERE {san_expr} = ? LIMIT 1',
                    (digits,),
                ).fetchone()
            except sqlite3.Error:
                row = None
        if not row:
            continue

        d = dict(row)
        cession_val = (d.get(cession_col) if cession_col else None) or d.get("Code cession") or d.get("code_cession") \
                      or d.get("CESSION") or d.get("cession") or d.get("Cession")
        mapped = _type_from_cession(conn, cession_val)
        if mapped:
            return mapped

    return None

# =============================================================================
#  PAGES
# =============================================================================

@router.get("/garanties")
def page_garanties(request: Request, q: Optional[str] = Query(None, description="Recherche globale")):
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        sql = f'SELECT "{PK_COL}","{OR_COL}","Type","Client","Machine","Date_OR" FROM "{TABLE_NAME}"'
        params: List[Any] = []
        if q:
            like = f"%{q}%"
            sql += f' WHERE "{PK_COL}" LIKE ? OR "Client" LIKE ? OR "Machine" LIKE ? OR "Type" LIKE ? OR "Date_OR" LIKE ? OR "{OR_COL}" LIKE ?'
            params = [like, like, like, like, like, like]
        sql += f' ORDER BY CAST("{PK_COL}" AS INTEGER) DESC'
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        for r in rows:
            if isinstance(r.get("Date_OR"), str):
                r["Date_OR"] = _format_date_for_display(r["Date_OR"])
            r["__href__"] = f'/garanties/item/{r[PK_COL]}'

    return templates.TemplateResponse("garanties.html", {
        "request": request,
        "title": "Registre des Garanties",
        "q": q or "",
        "rows": rows,
        "prefill": {},
        "sort_col": PK_COL,
        "sort_dir": "desc",
        "filters": {},
    })

@router.post("/garanties")
async def create_garantie(request: Request):
    form = dict(await request.form())
    with get_conn() as conn:
        mapped_type_from_cession = _type_from_existing_interventions(conn, record.get(OR_COL))
        current_type = str(record.get("Type") or "").strip()
        if mapped_type_from_cession:
            if not current_type:
                record["Type"] = mapped_type_from_cession
                current_type = mapped_type_from_cession
            if current_type.upper() == mapped_type_from_cession.upper():
                type_prefilled_from_cession = True

        cols = _all_columns(conn)
        payload = {c: form.get(c, "") for c in cols}

        _compute_refused_and_non_remb(payload)

        for c in list(payload.keys()):
            if _is_date_col(c):
                payload[c] = _format_date_for_storage(payload[c])

        if not str(payload.get(PK_COL) or "").strip():
            payload[PK_COL] = _next_numero(conn)

        if not str(payload.get("Type") or "").strip():
            inferred_type = _type_from_existing_interventions(conn, payload.get(OR_COL, ""))
            if inferred_type:
                payload["Type"] = inferred_type

        if OR_UNIQ in cols:
            digits = _digits(payload.get(OR_COL, ""))
            if digits:
                candidate = digits[-5:]
                exists = conn.execute(
                    f'SELECT 1 FROM "{TABLE_NAME}" WHERE "{OR_UNIQ}" = ? LIMIT 1',
                    (candidate,),
                ).fetchone()
                if exists:
                    payload.pop(OR_UNIQ, None)
                else:
                    payload[OR_UNIQ] = candidate
            elif not str(payload.get(OR_UNIQ, "")).strip():
                payload.pop(OR_UNIQ, None)

        keys = '","'.join(payload.keys())
        qmarks = ",".join(["?"] * len(payload))
        sql = f'INSERT INTO "{TABLE_NAME}" ("{keys}") VALUES ({qmarks})'
        conn.execute(sql, list(payload.values()))
        conn.commit()

    return RedirectResponse(url=f'/garanties/item/{payload[PK_COL]}?saved=1', status_code=HTTP_303_SEE_OTHER)

def _fetch_record(pk_value: str) -> Dict[str, Any]:
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{TABLE_NAME}")').fetchall()]
        if not cols:
            raise HTTPException(status_code=500, detail=f"Aucune colonne trouvée sur '{TABLE_NAME}'.")
        sql = f'SELECT ROWID AS _rowid_, * FROM "{TABLE_NAME}" WHERE "{PK_COL}" = ? LIMIT 1'
        row = conn.execute(sql, (pk_value,)).fetchone()
        if not row and str(pk_value).isdigit():
            row = conn.execute(sql, (int(pk_value),)).fetchone()
        if row:
            rec = dict(row)
            rec["_pk_col"] = PK_COL
            rec["_table"] = TABLE_NAME
            return {k: (v if v is not None else "") for k, v in rec.items()}
    raise HTTPException(status_code=404, detail=f"Garantie {pk_value} introuvable.")

# ✅ Route détail unique : résout d'abord pk via ?num_unique=...
@router.get("/garanties/item/{pk}", name="garanties_detail")
def garanties_detail(request: Request, pk: str, num_unique: Optional[str] = Query(None)):
    # ---- Résoudre pk via num_unique si fourni ----
    if num_unique:
        with get_conn() as conn:
            conn.row_factory = sqlite3.Row
            cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{TABLE_NAME}")').fetchall()]
            uniq_col = OR_UNIQ if OR_UNIQ in cols else ("num_unique" if "num_unique" in cols else None)

            if uniq_col:
                raw_u = str(num_unique).strip()
                norm_u = re.sub(r"[^0-9A-Za-z]", "", raw_u)
                san_expr = (
                    f'REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE('
                    f'CAST("{uniq_col}" AS TEXT),\' \',\'\'),\'-\',\'\'),\'/\',\'\'),\'.\',\'\'),\',\',\'\'),\'_\',\'\')'
                )
                row = conn.execute(
                    f'SELECT "{PK_COL}" FROM "{TABLE_NAME}" '
                    f'WHERE {san_expr} = ? '
                    f'ORDER BY CAST("{PK_COL}" AS INTEGER) DESC LIMIT 1',
                    (norm_u,)
                ).fetchone()

                if row:
                    pk = str(row[0])
                else:
                    row = conn.execute(
                        f'SELECT "{PK_COL}" FROM "{TABLE_NAME}" '
                        f'WHERE CAST("{uniq_col}" AS TEXT) = ? '
                        f'ORDER BY CAST("{PK_COL}" AS INTEGER) DESC LIMIT 1',
                        (raw_u,)
                    ).fetchone()
                    if row:
                        pk = str(row[0])
                    else:
                        raise HTTPException(status_code=404, detail=f"Aucune garantie avec {uniq_col}={raw_u}")

    record = _fetch_record(pk)

    for k, v in list(record.items()):
        if isinstance(k, str) and _is_date_col(k) and isinstance(v, str):
            record[k] = _format_date_for_display(v)

    type_prefilled_from_cession = False
    mapped_type_from_cession = None

    with get_conn() as conn:
        cols = _all_columns(conn)
        # Règle demandée :
        # - si un type a été mappé via code cession (type_prefilled_from_cession = True) → liste "habituelle"
        # - sinon → charger la liste depuis cession_garantie en priorité
        if type_prefilled_from_cession:
            type_opts = _load_type_garantie_options(conn)  # comportement standard
        else:
            type_opts = _load_type_garantie_from_cession_garantie(conn)
            if not type_opts:
                # fallback si la table n'existe pas/est vide → comportement standard
                type_opts = _load_type_garantie_options(conn)

    if "_rowid_" in record and "_rowid_" not in cols:
        cols = ["_rowid_"] + cols

    return templates.TemplateResponse("garantie_detail.html", {
        "request": request,
        "title": f"Garantie — {pk}",
        "record": record,
        "cols": cols,
        "saved": request.query_params.get("saved"),
        "error": "",
        "UI_HIDE": HIDE_FIELDS,
        "UI_READONLY": READONLY_FIELDS,
        "UI_TABS": TABS,
        "UI_LABELS": DISPLAY_LABELS,
        "UI_HELP": HELP_TEXT,
        "UI_WIDGETS": WIDGETS,
        "UI_PLACEHOLDERS": PLACEHOLDERS,
        "TYPE_GARANTIE_OPTIONS": type_opts,
        "TYPE_MAPPED_FROM_CESSION": mapped_type_from_cession or "",
        "TYPE_PREFILLED_FROM_CESSION": type_prefilled_from_cession,
    })

@router.post("/garanties/item/{pk}", name="garanties_update")
async def garanties_update(request: Request, pk: str):
    form = dict(await request.form())
    with get_conn() as conn:
        cols = _all_columns(conn)
        set_cols = [c for c in cols if c in form and c not in READONLY_FIELDS]
        if not set_cols:
            return RedirectResponse(url=f'/garanties/item/{pk}?saved=1', status_code=HTTP_303_SEE_OTHER)

        if any(k in set_cols for k in ("Pieces_Demandes","Pieces_Rembourses","MO_Demande","MO_Rembourse")):
            tmp = {c: form.get(c, "") for c in cols}
            _compute_refused_and_non_remb(tmp)
            for k in ("Pieces_Refuses","MO_Refuse","Montant_non_rembourse"):
                form[k] = tmp.get(k, "")
                if k not in set_cols:
                    set_cols.append(k)

        for c in set_cols:
            if _is_date_col(c):
                form[c] = _format_date_for_storage(form[c])

        if OR_UNIQ in cols and OR_COL in set_cols:
            digits = _digits(form.get(OR_COL, ""))
            if digits:
                candidate = digits[-5:]
                exists = conn.execute(
                    f'SELECT 1 FROM "{TABLE_NAME}" WHERE "{OR_UNIQ}" = ? AND "{PK_COL}" != ? LIMIT 1',
                    (candidate, pk),
                ).fetchone()
                if exists:
                    form.pop(OR_UNIQ, None)
                    if OR_UNIQ in set_cols:
                        set_cols.remove(OR_UNIQ)
                else:
                    form[OR_UNIQ] = candidate
                    if OR_UNIQ not in set_cols:
                        set_cols.append(OR_UNIQ)
            else:
                form.pop(OR_UNIQ, None)
                if OR_UNIQ in set_cols:
                    set_cols.remove(OR_UNIQ)

        set_clause = ','.join([f'"{c}" = ?' for c in set_cols])
        params = [form[c] for c in set_cols] + [pk]
        sql = f'UPDATE "{TABLE_NAME}" SET {set_clause} WHERE "{PK_COL}" = ?'
        conn.execute(sql, params)
        conn.commit()

    return RedirectResponse(url=f'/garanties/item/{pk}?saved=1', status_code=HTTP_303_SEE_OTHER)

# -----------------------------------------------------------------------------
#  Création (form)
# -----------------------------------------------------------------------------

@router.get("/garanties/new", name="garanties_create_form")
def garanties_create_form(
    request: Request,
    from_intervention: int | None = Query(None),
    n: str | None = Query(None, alias="num"),
    client: str | None = None,
    machine: str | None = None,
    type: str | None = Query(None, alias="type"),
    date_or: str | None = Query(None, alias="date_or"),
):
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        cols = _all_columns(conn)
        record = {c: "" for c in cols}
        record[PK_COL] = _next_numero(conn)

        type_prefilled_from_cession = False
        mapped_from_cession = ""
        if from_intervention:
            pre, flag, mapped = _prefill_from_intervention(conn, from_intervention)
            record.update(pre)
            type_prefilled_from_cession = flag
            mapped_from_cession = mapped

        if n is not None: record[PK_COL] = n
        if client is not None: record["Client"] = client
        if machine is not None: record["Machine"] = machine
        if type is not None: record["Type"] = type  # override manuel autorisé
        if date_or is not None: record["Date_OR"] = _format_date_for_display(date_or)

        for k, v in list(record.items()):
            if _is_date_col(k) and isinstance(v, str):
                record[k] = _format_date_for_display(v)

        # doublons potentiels
        dup_list = []
        or_short = _digits(record.get(OR_COL, ""))
        if or_short:
            san_expr = f'''
                REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(CAST("{OR_COL}" AS TEXT),' ',''),'.',''),',',''),'-',''),'/',''),'_','')
            '''
            where_clauses = []
            params = []
            if OR_UNIQ in cols:
                where_clauses.append(f'CAST("{OR_UNIQ}" AS TEXT) = ?')
                params.append(or_short[-5:])
            where_clauses.append(san_expr + " = ?")
            params.append(or_short)
            where_clauses.append(san_expr + " LIKE '%' || ?")
            params.append(or_short)

            sql_dup = f'''
                SELECT ROWID AS _rowid_, "{PK_COL}","{OR_COL}","Type","Client","Machine","Date_OR"
                FROM "{TABLE_NAME}"
                WHERE (''' + " OR ".join(where_clauses) + ''')
                ORDER BY CAST("{PK_COL}" AS INTEGER) DESC
            '''
            cur = conn.execute(sql_dup, tuple(params))
            dup_list = [dict(r) for r in cur.fetchall()]
            for r in dup_list:
                if isinstance(r.get("Date_OR"), str):
                    r["Date_OR"] = _format_date_for_display(r["Date_OR"])

        type_opts = _load_type_garantie_options(conn)
    # Si aucun mapping cession->type n'a été trouvé, forcer la liste depuis cession_garantie
    if not type_prefilled_from_cession:
        opts_cg = _load_type_garantie_from_cession_garantie_only(conn)
        if opts_cg:
            type_opts = opts_cg

        # Si aucun mapping cession->type n'a été trouvé pour cette intervention,
        # on force la liste à venir de cession_garantie.type_garantie
        if not type_prefilled_from_cession:
            opts_cg = _load_type_garantie_from_cession_garantie_only(conn)
            if opts_cg:
                type_opts = opts_cg

        return templates.TemplateResponse("garantie_create.html", {
            "request": request,
            "title": "Nouvelle garantie",
            "record": record,
            "cols": cols,
            "error": "",
            "UI_HIDE": HIDE_FIELDS,
            "UI_READONLY": READONLY_FIELDS - {PK_COL},
            "UI_TABS": TABS,
            "UI_LABELS": DISPLAY_LABELS,
            "UI_HELP": HELP_TEXT,
            "UI_WIDGETS": WIDGETS,
            "UI_PLACEHOLDERS": PLACEHOLDERS,
            "TYPE_GARANTIE_OPTIONS": type_opts,
            "TYPE_PREFILLED_FROM_CESSION": type_prefilled_from_cession,
            "TYPE_MAPPED_FROM_CESSION": mapped_from_cession,
            "DUP_OR_LIST": dup_list,
            "DUP_OR_VALUE": or_short[-5:] if or_short else "",
        })

@router.post("/garanties/new", name="garanties_create_submit")
async def garanties_create_submit(request: Request):
    form = dict(await request.form())
    with get_conn() as conn:
        cols = _all_columns(conn)
        payload = {c: form.get(c, "") for c in cols}

        _compute_refused_and_non_remb(payload)

        for c in list(payload.keys()):
            if _is_date_col(c):
                payload[c] = _format_date_for_storage(payload[c])

        if not str(payload.get(PK_COL) or "").strip():
            payload[PK_COL] = _next_numero(conn)

        if OR_UNIQ in cols:
            digits = _digits(payload.get(OR_COL, ""))
            if digits:
                candidate = digits[-5:]
                exists = conn.execute(
                    f'SELECT 1 FROM "{TABLE_NAME}" WHERE "{OR_UNIQ}" = ? LIMIT 1',
                    (candidate,),
                ).fetchone()
                if exists:
                    payload.pop(OR_UNIQ, None)
                else:
                    payload[OR_UNIQ] = candidate
            elif not str(payload.get(OR_UNIQ, "")).strip():
                payload.pop(OR_UNIQ, None)

        keys = '","'.join(payload.keys())
        qmarks = ",".join(["?"] * len(payload))
        sql = f'INSERT INTO "{TABLE_NAME}" ("{keys}") VALUES ({qmarks})'
        conn.execute(sql, list(payload.values()))
        conn.commit()

    return RedirectResponse(url=f'/garanties/item/{payload[PK_COL]}?saved=1', status_code=HTTP_303_SEE_OTHER)

@router.post("/garanties/merge", name="garanties_merge")
async def garanties_merge(request: Request):
    form = dict(await request.form())
    target = (form.get("__merge_target") or "").strip()
    if not target:
        return RedirectResponse(url="/garanties/new", status_code=HTTP_303_SEE_OTHER)

    form.pop(PK_COL, None)

    with get_conn() as conn:
        cols = _all_columns(conn)
        set_cols = [c for c in cols if c in form and c not in READONLY_FIELDS]

        if any(k in set_cols for k in ("Pieces_Demandes","Pieces_Rembourses","MO_Demande","MO_Rembourse")):
            tmp = {c: form.get(c, "") for c in cols}
            _compute_refused_and_non_remb(tmp)
            for k in ("Pieces_Refuses","MO_Refuse","Montant_non_rembourse"):
                form[k] = tmp.get(k, "")
                if k not in set_cols:
                    set_cols.append(k)

        for c in set_cols:
            if _is_date_col(c):
                form[c] = _format_date_for_storage(form[c])

        if OR_UNIQ in cols and OR_COL in set_cols:
            digits = _digits(form.get(OR_COL, ""))
            if digits:
                candidate = digits[-5:]
                exists = conn.execute(
                    f'SELECT 1 FROM "{TABLE_NAME}" WHERE "{OR_UNIQ}" = ? AND "{PK_COL}" != ? LIMIT 1',
                    (candidate, target),
                ).fetchone()
                if exists:
                    form.pop(OR_UNIQ, None)
                    if OR_UNIQ in set_cols:
                        set_cols.remove(OR_UNIQ)
                else:
                    form[OR_UNIQ] = candidate
                    if OR_UNIQ not in set_cols:
                        set_cols.append(OR_UNIQ)
            else:
                form.pop(OR_UNIQ, None)
                if OR_UNIQ in set_cols:
                    set_cols.remove(OR_UNIQ)

        if not set_cols:
            return RedirectResponse(url=f'/garanties/item/{target}?saved=1', status_code=HTTP_303_SEE_OTHER)

        set_clause = ','.join([f'"{c}" = ?' for c in set_cols])
        params = [form[c] for c in set_cols] + [target]
        sql = f'UPDATE "{TABLE_NAME}" SET {set_clause} WHERE "{PK_COL}" = ?'
        conn.execute(sql, params)
        conn.commit()

    return RedirectResponse(url=f'/garanties/item/{target}?saved=1&merged=1', status_code=HTTP_303_SEE_OTHER)

@router.post("/garanties/duplicate", name="garanties_duplicate")
async def garanties_duplicate(request: Request):
    form = dict(await request.form())
    src = (form.get("__duplicate_src") or "").strip()
    if not src:
        return RedirectResponse(url="/garanties/new?error=Merci de saisir un N° DG à dupliquer", status_code=HTTP_303_SEE_OTHER)

    try:
        src_rec = _fetch_record(src)
    except HTTPException:
        return RedirectResponse(url=f"/garanties/new?error=Garantie {src} introuvable", status_code=HTTP_303_SEE_OTHER)

    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        cols = _all_columns(conn)

        payload = {c: src_rec.get(c, "") for c in cols}
        new_num = _next_numero(conn)
        payload[PK_COL] = new_num

        for k in ("Date", "Date_OR", "date_paiement", "Date_rebus"):
            if k in payload:
                payload[k] = ""

        _compute_refused_and_non_remb(payload)

        if OR_UNIQ in payload:
            payload.pop(OR_UNIQ, "")
            digits = _digits(payload.get(OR_COL, ""))
            if digits:
                candidate = digits[-5:]
                exists = conn.execute(
                    f'SELECT 1 FROM "{TABLE_NAME}" WHERE "{OR_UNIQ}" = ? LIMIT 1',
                    (candidate,),
                ).fetchone()
                if not exists:
                    payload[OR_UNIQ] = candidate

        keys = '","'.join(payload.keys())
        qmarks = ",".join(["?"] * len(payload))
        sql = f'INSERT INTO "{TABLE_NAME}" ("{keys}") VALUES ({qmarks})'
        conn.execute(sql, list(payload.values()))
        conn.commit()

    return RedirectResponse(url=f'/garanties/item/{new_num}?duplicated=1&from={src}', status_code=HTTP_303_SEE_OTHER)

# ▼ Ouvre directement la fiche via num_unique, sans redirection et sans exposer le numéro
@router.get("/garanties/u/{u}", name="garanties_detail_by_unique")
def garanties_detail_by_unique(request: Request, u: str):
    """
    Ouvre la fiche directement par num_unique, sans exposer le numéro DG dans l'URL.
    - 1er essai : égalité TEXTE stricte sur la colonne num_unique
    - fallback : égalité après "sanitization"
    """
    raw_u = (u or "").strip()
    if not raw_u:
        raise HTTPException(status_code=400, detail="num_unique manquant")

    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{TABLE_NAME}")').fetchall()]
        uniq_col = OR_UNIQ if OR_UNIQ in cols else ("num_unique" if "num_unique" in cols else None)
        if not uniq_col:
            raise HTTPException(status_code=500, detail="Aucune colonne num_unique trouvée")

        # 1) Égalité stricte
        row = conn.execute(
            f'SELECT ROWID AS _rowid_, * FROM "{TABLE_NAME}" '
            f'WHERE CAST("{uniq_col}" AS TEXT) = ? '
            f'LIMIT 1',
            (raw_u,)
        ).fetchone()

        if not row:
            norm_u = re.sub(r"[^0-9A-Za-z]", "", raw_u)
            san_expr = (
                f'REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE('
                f'CAST("{uniq_col}" AS TEXT),\' \',\'\'),\'-\',\'\'),\'/\',\'\'),\'.\',\'\'),\',\',\'\'),\'_\',\'\')'
            )
            row = conn.execute(
                f'SELECT ROWID AS _rowid_, * FROM "{TABLE_NAME}" '
                f'WHERE {san_expr} = ? '
                f'ORDER BY CAST("{PK_COL}" AS INTEGER) DESC LIMIT 1',
                (norm_u,)
            ).fetchone()

        if not row:
            raise HTTPException(status_code=404, detail=f"Aucune garantie avec {uniq_col}={raw_u}")

        record = {k: (v if v is not None else "") for k, v in dict(row).items()}
        pk = str(record.get(PK_COL, ""))

    for k, v in list(record.items()):
        if isinstance(k, str) and _is_date_col(k) and isinstance(v, str):
            record[k] = _format_date_for_display(v)

    with get_conn() as conn:
        cols = _all_columns(conn)
        type_opts = _load_type_garantie_options(conn)
    if "_rowid_" in record and "_rowid_" not in cols:
        cols = ["_rowid_"] + cols

    return templates.TemplateResponse("garantie_detail.html", {
        "request": request,
        "title": f"Garantie — {pk or '(num_unique)'}",
        "record": record,
        "cols": cols,
        "saved": request.query_params.get("saved"),
        "error": "",
        "UI_HIDE": HIDE_FIELDS,
        "UI_READONLY": READONLY_FIELDS,
        "UI_TABS": TABS,
        "UI_LABELS": DISPLAY_LABELS,
        "UI_HELP": HELP_TEXT,
        "UI_WIDGETS": WIDGETS,
        "UI_PLACEHOLDERS": PLACEHOLDERS,
        "TYPE_GARANTIE_OPTIONS": type_opts,
    })
