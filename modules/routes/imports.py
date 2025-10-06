from fastapi import APIRouter, Request, UploadFile, File
from fastapi.responses import PlainTextResponse
from typing import List, Optional, Tuple
import io
import os
import glob
import sqlite3
import unicodedata
import pandas as pd
import xml.etree.ElementTree as ET

from ..core.ui import templates
from ..core.db import (

def _upsert_rows_preserve_traiter(conn, table: str, cols: list[str], rows: list[dict], numero_col: str | None):
    """
    Upsert rows into `table`. If numero_col is provided, we use it as conflict target.
    We do NOT overwrite "traiter" on update.
    """
    if not rows:
        return 0, 0
    if not numero_col:
        # No key: pure INSERT, no deletes
        placeholders = ", ".join(["?"] * len(cols))
        col_list = ", ".join([f'"{c}"' for c in cols])
        data = [[r.get(c, None) for c in cols] for r in rows]
        conn.executemany(f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders})', data)
        return len(rows), 0

    # Build SET list excluding numero_col and traiter
    set_list = ", ".join([f'"{c}" = excluded."{c}"' for c in cols if c not in (numero_col, "traiter")]) or f'"{numero_col}"="{numero_col}"'
    placeholders = ", ".join(["?"] * len(cols))
    col_list = ", ".join([f'"{c}"' for c in cols])

    inserted = updated = 0
    for r in rows:
        vals = [r.get(c, None) for c in cols]
        before = conn.total_changes
        try:
            conn.execute(
                f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders}) '
                f'ON CONFLICT("{numero_col}") DO UPDATE SET {set_list}',
                vals,
            )
        except Exception as e:
            # If table is a view without ON CONFLICT support, fallback to pure UPDATE then INSERT if no row
            try:
                # try UPDATE first
                keyval = r.get(numero_col, None)
                if keyval is not None:
                    # craft simple update statement
                    upd_cols = [c for c in cols if c not in (numero_col, "traiter")]
                    upd_set = ", ".join([f'"{c}"=?' for c in upd_cols]) or f'"{numero_col}"="{numero_col}"'
                    upd_vals = [r.get(c, None) for c in upd_cols] + [keyval]
                    cur = conn.execute(f'UPDATE "{table}" SET {upd_set} WHERE "{numero_col}"=?', upd_vals)
                    if cur.rowcount == 0:
                        conn.execute(f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders})', vals)
                else:
                    conn.execute(f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders})', vals)
            except Exception:
                # last resort: skip this row
                pass
        after = conn.total_changes
        delta = after - before
        if delta == 1:
            inserted += 1
        elif delta >= 2:
            updated += 1
    return inserted, updated
    get_conn,
    table_exists,
    pragma_columns,
    ensure_table_has_column,
    ensure_unique_index_for_key,             # version tolérante
    ensure_flag_column,         # conservé pour compat
    ensure_unique_indexes,      # garde-fou global au démarrage
    ensure_unique_index_for_key # index unique sur la vraie colonne détectée
)

router = APIRouter()

# ------------------------------------------------------------------------------
# Paramètres d’import Excel (adapte si besoin)
# ------------------------------------------------------------------------------

SHEET_MAP: dict[str, str] = {
    "Historique OR Narrosse": "interventions_narrosse",
    "Historique OR Peyrehorade": "interventions_peyrehorade",
    "Registre_Garantie": "garantie",
}

# ------------------------------------------------------------------------------
# Utilitaires Excel / normalisation
# ------------------------------------------------------------------------------

def ensure_table_from_excel_headers(conn, table: str, headers: List[str]):
    """Crée la table si absente ; sinon ajoute les colonnes manquantes (TEXT)."""
    if not table_exists(conn, table):
        cols_sql = ", ".join([f'"{h}" TEXT' for h in headers])
        conn.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({cols_sql})')
    else:
        existing = pragma_columns(conn, table)
        existing_norm = {c.strip().casefold(): c for c in existing}
        for h in headers:
            key = h.strip().casefold()
            if key not in existing_norm:
                try:
                    conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{h}" TEXT')
                except Exception:
                    pass

def _coerce_cell(v):
    """Strip / supprime .0 Excel / vide->None."""
    if v is None:
        return None
    s = str(v).strip()
    if s == "" or s.lower() in {"nan", "none"}:
        return None
    if s.endswith(".0") and s.replace(".0", "").isdigit():
        return s[:-2]
    return s

def last4_digits(x):
    s = str(x or "").strip()
    if not s:
        return None
    only = "".join(ch for ch in s if ch.isdigit())
    if len(only) >= 4:
        return only[-4:]
    return only or None

def int_string(x):
    s = str(x or "").strip()
    if not s:
        return None
    if s.endswith(".0"):
        s = s[:-2]
    try:
        return str(int(s))
    except Exception:
        digits = "".join(ch for ch in s if ch.isdigit())
        return digits or None

def coerce_df_for_sqlite(df: pd.DataFrame) -> pd.DataFrame:
    # Datetimes -> texte SQL
    for col in df.columns:
        try:
            if pd.api.types.is_datetime64_any_dtype(df[col]):
                df[col] = df[col].dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    for col in df.columns:
        df[col] = df[col].map(_coerce_cell)
    return df

# ------------------------------------------------------------------------------
# Détection / normalisation des colonnes clés
# ------------------------------------------------------------------------------

NUMERO_ALIASES = ("Numéro", "Numero", "N°", "No", "Nº")
NUM_UNIQUE_ALIASES = (
    "num unique","num_unique","numunique",
    "numero unique","numéro unique","n° unique","no unique","nº unique"
)

def _norm(s: str) -> str:
    """normalise: trim, supprime accents, lower/casefold."""
    s = (s or "").strip()
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return s.casefold()

_NUM_ALIASES_NORM = {_norm(x) for x in NUMERO_ALIASES}
_NUM_UNIQUE_ALIASES_NORM = {_norm(x) for x in NUM_UNIQUE_ALIASES}

def detect_numero_col(cols: List[str]) -> Optional[str]:
    for c in cols:
        if _norm(c) in _NUM_ALIASES_NORM:
            return c
    return None

def detect_num_unique_col(cols: List[str]) -> Optional[str]:
    for c in cols:
        if _norm(c) in _NUM_UNIQUE_ALIASES_NORM:
            return c
    return None

# ------------------------------------------------------------------------------
# Helpers objets SQLite (vue vs table) + colonnes existantes robustes
# ------------------------------------------------------------------------------

def _object_type(conn, name: str) -> Optional[str]:
    cur = conn.execute("SELECT type FROM sqlite_master WHERE name=?", (name,))
    row = cur.fetchone()
    return row[0] if row else None

def _is_view(conn, name: str) -> bool:
    return _object_type(conn, name) == "view"

def _get_existing_columns(conn, name: str) -> List[str]:
    """Récupère les colonnes d'un objet (table/vue) sans lever d’exception."""
    try:
        rows = conn.execute(f'PRAGMA table_info("{name}")').fetchall()
        cols = [r[1] for r in rows]
        if cols:
            return cols
    except Exception:
        pass
    try:
        rows = conn.execute(f'PRAGMA table_xinfo("{name}")').fetchall()
        cols = [r[1] for r in rows if r[1] is not None]
        if cols:
            return cols
    except Exception:
        pass
    try:
        cur = conn.execute(f'SELECT * FROM "{name}" WHERE 0=1')
        return [d[0] for d in (cur.description or [])]
    except Exception:
        return []

# ------------------------------------------------------------------------------
# Alignement des colonnes Excel -> cible (TABLE ou VUE)
# ------------------------------------------------------------------------------

def align_df_columns_for_target(conn, table: str, df: pd.DataFrame) -> pd.DataFrame:
    """
    - TABLE → ajout colonnes manquantes (ALTER TABLE).
    - VUE   → rename vers colonnes existantes + drop colonnes inconnues.
    + mapping spécial pour Numéro et Num Unique (alias -> vrai nom existant).
    """
    obj_type = _object_type(conn, table)  # "table" | "view" | None
    existing_cols = _get_existing_columns(conn, table) if obj_type else []
    existing_norm = {_norm(c): c for c in existing_cols}

    # Vrais noms cibles si présents
    target_numero_name = None
    target_num_unique_name = None
    for k_norm, real in existing_norm.items():
        if k_norm in _NUM_ALIASES_NORM and not target_numero_name:
            target_numero_name = real
        if k_norm in _NUM_UNIQUE_ALIASES_NORM and not target_num_unique_name:
            target_num_unique_name = real

    rename_map = {}
    add_cols = []
    drop_cols = []

    for c in list(df.columns):
        n = _norm(str(c))
        if n in existing_norm:
            rename_map[c] = existing_norm[n]
        elif n in _NUM_ALIASES_NORM and target_numero_name:
            rename_map[c] = target_numero_name
        elif n in _NUM_UNIQUE_ALIASES_NORM and target_num_unique_name:
            rename_map[c] = target_num_unique_name
        else:
            if obj_type == "table":
                add_cols.append(c)
            else:
                drop_cols.append(c)

    # pour TABLE : ajouter les colonnes manquantes
    if add_cols and obj_type == "table":
        for c in add_cols:
            try:
                conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{c}" TEXT')
            except Exception:
                pass
        conn.commit()

    if rename_map:
        df = df.rename(columns=rename_map)
    if drop_cols:
        df = df.drop(columns=drop_cols)
    return df

# ------------------------------------------------------------------------------
# Routes : Import Excel
# ------------------------------------------------------------------------------
def only_digits_or_none(x):
    s = str(x or "").strip()
    digits = "".join(ch for ch in s if ch.isdigit())
    return digits or None

@router.get("/import-excel")
async def page_import_excel(request: Request):
    return templates.TemplateResponse("import_excel.html", {"request": request, "sheet_map": SHEET_MAP})

@router.post("/import-excel", response_class=PlainTextResponse)
async def import_excel(file: UploadFile = File(...)):
    # Garde-fou global
    with get_conn() as _c:
        ensure_unique_indexes(_c)

    content = await file.read()
    xls = pd.ExcelFile(io.BytesIO(content))
    report: List[str] = []
    stats: dict[str, dict[str, int]] = {}  # {table: {"insert": n, "update": n}}

    def _add_stats(table: str, ins: int, upd: int):
        stats.setdefault(table, {"insert": 0, "update": 0})
        stats[table]["insert"] += ins
        stats[table]["update"] += upd

    with get_conn() as conn:
        for sheet in xls.sheet_names:
            table = SHEET_MAP.get(sheet) or SHEET_MAP.get(sheet.strip()) or sheet.strip().lower().replace(" ", "_")

            # Charge la feuille
            try:
                df = xls.parse(sheet)
            except Exception as e:
                report.append(f"- {sheet} → ERREUR lecture: {e}")
                continue

            # Normalisation de base
            df = coerce_df_for_sqlite(df)

            # Alignement des colonnes (gère vues/tables + alias Numéro/Num Unique)
            df = align_df_columns_for_target(conn, table, df)

            # Recalcule + assainit la liste de colonnes
            cols = [str(c) for c in df.columns]
            cols = [c for c in cols if c.strip() != ""]
            seen = set()
            cols = [c for c in cols if not (c in seen or seen.add(c))]

            if not cols:
                report.append(f"- {sheet} → {table} : aucune colonne exploitable après alignement.")
                continue

            # Règles de nettoyage par site (sur la colonne 'Numéro' détectée)
            numero_col = detect_numero_col(cols)
            if table.endswith("peyrehorade") and numero_col:
                df[numero_col] = df[numero_col].map(last4_digits)
            elif table.endswith("narrosse") and numero_col:
                df[numero_col] = df[numero_col].map(int_string)

            # Construit les données finales
            df = df[cols]
            rows_list = df.values.tolist()
            col_list = ", ".join(f'"{c}"' for c in cols)
            placeholders = ", ".join(["?"] * len(cols))

            # ------------------ GARANTIE (vue possible) ------------------
            if table == "garantie":
                key_col = detect_num_unique_col(cols)
                # Forcer "num unique" au format numérique côté données
                if key_col and key_col in df.columns:
                    df[key_col] = df[key_col].map(only_digits_or_none)
                    # Recalcule rows_list après modif
                    rows_list = df[cols].values.tolist()
                

                if _is_view(conn, "garantie"):
                                    # Vue / table: safe UPSERT without DELETE
                inserted, updated = _upsert_rows_preserve_traiter(conn, table, cols, [dict(zip(cols, r)) for r in rows_list], numero_col)
                _add_stats(table, inserted, updated)
                report.append(f"- {sheet} → {table} : {len(df)} lignes traitées ({inserted} insert, {updated} update)")
                continue  # feuille suivante

            # ------------------ INTERVENTIONS_* ------------------
            if numero_col:
                if _is_view(conn, table):
                    # Vue : fallback DELETE + INSERT (update si DELETE>0)
                    key_idx = cols.index(numero_col)
                    inserted = updated = 0
                    for r in rows_list:
                        k = r[key_idx]
                        k = "" if k is None else str(k).strip()
                        if k:
                            cur = conn.execute(f'DELETE FROM "{table}" WHERE "{numero_col}" = ?', (k,))
                            if cur.rowcount > 0:
                                updated += 1
                        conn.execute(f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders})', r)
                        inserted += 1
                    _add_stats(table, inserted, updated)
                else:
                    # TABLE : UPSERT natif (comptage par ligne)
                    ensure_unique_index_for_key(conn, table, numero_col)
                    set_list = ", ".join([f'"{c}" = excluded."{c}"' for c in cols if c != numero_col]) or f'"{numero_col}"="{numero_col}"'
                    sql = (
                        f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders}) '
                        f'ON CONFLICT("{numero_col}") DO UPDATE SET {set_list}'
                    )
                    inserted = updated = 0
                    for r in rows_list:
                        before = conn.total_changes
                        try:
                            conn.execute(sql, r)
                        except sqlite3.OperationalError as e:
                            if "ON CONFLICT" in str(e):
                    inserted, updated = _upsert_rows_preserve_traiter(conn, table, cols, [dict(zip(cols, r)) for r in rows_list], numero_col)
                    _add_stats(table, inserted, updated)
                    continue
                            else:
                                raise
                        after = conn.total_changes
                        delta = after - before
                        if delta == 1:
                            inserted += 1
                        elif delta == 2:
                            updated += 1
                    _add_stats(table, inserted, updated)
            else:
                # Pas de clé détectée → insert brut
                conn.executemany(f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders})', rows_list)
                _add_stats(table, len(rows_list), 0)

            ins = stats.get(table, {}).get("insert", 0)
            upd = stats.get(table, {}).get("update", 0)
            report.append(f"- {sheet} → {table} : {len(df)} lignes traitées ({ins} insert, {upd} update cumulés)")

        conn.commit()

    # Rendu final lisible
    details = "\n".join(report)
    synthese = []
    for t, val in stats.items():
        synthese.append(f"{t}: {val['insert']} insert, {val['update']} update")
    synthese_txt = "\n\nSynthèse:\n" + "\n".join(synthese) if synthese else ""
    return "Import Excel terminé :\n" + details + synthese_txt

# ------------------------------------------------------------------------------
# Routes : Import XML (upload) + parseurs
# ------------------------------------------------------------------------------

@router.get("/import-xml")
async def page_import_xml(request: Request):
    return templates.TemplateResponse("import_xml.html", {"request": request})

def detect_record_tag(root: ET.Element) -> Optional[str]:
    counts = {}
    for ch in list(root):
        counts[ch.tag] = counts.get(ch.tag, 0) + 1
    if counts:
        return max(counts.items(), key=lambda kv: kv[1])[0]
    return None

def rows_from_xml(xml_bytes: bytes) -> Tuple[List[str], List[dict]]:
    tree = ET.ElementTree(ET.fromstring(xml_bytes))
    root = tree.getroot()
    rec_tag = detect_record_tag(root)
    items = list(root.iter(rec_tag)) if rec_tag else list(root)

    rows = []
    all_cols = []
    for it in items:
        row = {}
        for k, v in it.attrib.items():
            col = f"@{k}"
            row[col] = v
            all_cols.append(col)
        for ch in list(it):
            txt = (ch.text or "").strip()
            row[ch.tag] = txt or None
            all_cols.append(ch.tag)
        rows.append(row)

    cols = sorted(set(all_cols))
    return cols, rows

def ensure_table_from_xml_columns(conn, table: str, columns: List[str]):
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
    if not cur.fetchone():
        cols_sql = ", ".join([f'"{c}" TEXT' for c in columns])
        conn.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({cols_sql})')
    else:
        existing = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
        existing_norm = {c.strip().casefold(): c for c in existing}
        for c in columns:
            key = c.strip().casefold()
            if key not in existing_norm:
                conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{c}" TEXT')

@router.post("/import-xml", response_class=PlainTextResponse)
async def import_xml(file: UploadFile = File(...)):
    xml_bytes = await file.read()
    cols, rows = rows_from_xml(xml_bytes)

    with get_conn() as conn:
        ensure_table_from_xml_columns(conn, "interventions", cols)
        placeholders = ", ".join(["?"] * len(cols))
        col_list = ", ".join([f'"{c}"' for c in cols])
        data = [[r.get(c, None) for c in cols] for r in rows]

        numero_col = detect_numero_col(cols)
        if numero_col:
            existing = set()
            cur = conn.execute(f'SELECT "{numero_col}" FROM "interventions" WHERE "{numero_col}" IS NOT NULL')
            for (val,) in cur.fetchall():
                if val is None:
                    continue
                existing.add(str(val).strip())

            seen_batch = set()
            filtered = []
            idx_num = cols.index(numero_col)
            for row in data:
                num = row[idx_num]
                k = "" if num is None else str(num).strip()
                if not k or k in existing or k in seen_batch:
                    continue
                seen_batch.add(k)
                filtered.append(row)
            if filtered:
                conn.executemany(f'INSERT INTO "interventions" ({col_list}) VALUES ({placeholders})', filtered)
        else:
            if data:
                conn.executemany(f'INSERT INTO "interventions" ({col_list}) VALUES ({placeholders})', data)

        conn.commit()

    return f"Import XML terminé : {len(rows)} enregistrements analysés."

# ------------------------------------------------------------------------------
# Helper démarrage : lire un dossier XML (utilisé par main.py)
# ------------------------------------------------------------------------------

def import_xml_dir_on_startup(dir_path: str = "data/xml") -> str:
    files = sorted(glob.glob(os.path.join(dir_path, "*.xml")))
    if not files:
        return "Aucun XML trouvé."
    total = 0
    with get_conn() as conn:
        for path in files:
            try:
                with open(path, "rb") as fh:
                    xml_bytes = fh.read()
                cols, rows = rows_from_xml(xml_bytes)
                if not cols:
                    continue
                ensure_table_from_xml_columns(conn, "interventions", cols)
                placeholders = ", ".join(["?"] * len(cols))
                col_list = ", ".join([f'"{c}"' for c in cols])

                numero_col = detect_numero_col(cols)
                for r in rows:
            pass  # legacy path removed
        # Use safe UPSERT preserving "traiter"
        inserted, updated = _upsert_rows_preserve_traiter(conn, "interventions", cols, rows, numero_col)
        total += len(rows)
            except Exception as e:
                print(f"[startup-xml] Erreur {path}: {e}")
        conn.commit()
    return f"{total} enregistrements XML traités"


def _norm_str(s: str) -> str:
    s = str(s or "")
    import unicodedata as _ud
    s = "".join(ch for ch in _ud.normalize("NFD", s) if ord(ch) < 128)
    return s.strip().casefold()

def _find_key_ignore_at(cols, target_norm: str):
    target_norm = _norm_str(target_norm)
    for c in cols:
        n = _norm_str(c.lstrip("@"))
        if n == target_norm:
            return c
    return None



@router.post("/import-xml", response_class=PlainTextResponse)
async def import_xml(file: UploadFile = File(...)):
    xml_bytes = await file.read()
    cols, rows = rows_from_xml(xml_bytes)

    with get_conn() as conn:
        # 1) Schéma cible et colonne 'traiter'
        ensure_table_from_xml_columns(conn, "interventions", cols)
        ensure_table_has_column(conn, "interventions", "traiter", "INTEGER", "0")

        # 2) Détection 'Etat' (avec ou sans @)
        etat_key = _find_key_ignore_at(cols, "etat")

        # 3) Ajoute 'traiter' côté DataFrame si absent
        if "traiter" not in cols:
            cols.append("traiter")

        # 4) Clé et index unique pour l’UPSERT
        numero_col = detect_numero_col(cols)
        ensure_unique_index_for_key(conn, "interventions", numero_col or cols[0])

        # 5) UPSERT en préservant 'traiter'
        placeholders = ", ".join(["?"] * len(cols))
        col_list = ", ".join([f'"{c}"' for c in cols])
        set_list = ", ".join(
            [f'"{c}" = excluded."{c}"' for c in cols if c not in (numero_col, "traiter")]
        ) or f'"{numero_col}"="{numero_col}"'

        inserted = updated = 0
        for r in rows:
            # 'traiter' initial à l'INSERT seulement (si Etat ≈ validé)
            etat_val = r.get(etat_key) if etat_key else r.get("Etat") or r.get("@Etat")
            traiter_val = 1 if _norm_str(etat_val) in {"valide", "validée", "validé", "validee"} else 0
            r.setdefault("traiter", traiter_val)

            vals = [r.get(c, None) for c in cols]
            before = conn.total_changes
            conn.execute(
                f'INSERT INTO "interventions" ({col_list}) VALUES ({placeholders}) '
                f'ON CONFLICT("{numero_col}") DO UPDATE SET {set_list}',
                vals,
            )
            after = conn.total_changes
            if after - before == 1:
                inserted += 1
            else:
                updated += 1

        conn.commit()
    return PlainTextResponse(f"OK: {inserted} insérés, {updated} mis à jour")



def import_xml_dir_on_startup(dirpath: str) -> str:
    import glob, os
    files = []
    for ext in ("*.xml", "*.XML"):
        files.extend(glob.glob(os.path.join(dirpath, ext)))
    files = sorted(files)
    if not files:
        return f"Aucun XML trouvé dans {dirpath}"

    total = 0
    with get_conn() as conn:
        for path in files:
            try:
                with open(path, "rb") as fh:
                    xml_bytes = fh.read()
                cols, rows = rows_from_xml(xml_bytes)
                if not cols:
                    continue

                # 1) Schéma cible et colonne 'traiter'
                ensure_table_from_xml_columns(conn, "interventions", cols)
                ensure_table_has_column(conn, "interventions", "traiter", "INTEGER", "0")

                # 2) Détection 'Etat' (avec ou sans @)
                etat_key = _find_key_ignore_at(cols, "etat")

                # 3) Ajoute 'traiter' côté DataFrame si absent
                if "traiter" not in cols:
                    cols.append("traiter")

                # 4) Clé et index unique pour l’UPSERT
                numero_col = detect_numero_col(cols)
                ensure_unique_index_for_key(conn, "interventions", numero_col or cols[0])

                # 5) UPSERT en préservant 'traiter'
                placeholders = ", ".join(["?"] * len(cols))
                col_list = ", ".join([f'"{c}"' for c in cols])
                set_list = ", ".join(
                    [f'"{c}" = excluded."{c}"' for c in cols if c not in (numero_col, "traiter")]
                ) or f'"{numero_col}"="{numero_col}"'

                for r in rows:
                    etat_val = r.get(etat_key) if etat_key else r.get("Etat") or r.get("@Etat")
                    traiter_val = 1 if _norm_str(etat_val) in {"valide", "validée", "validé", "validee"} else 0
                    r.setdefault("traiter", traiter_val)

                    vals = [r.get(c, None) for c in cols]
                    conn.execute(
                        f'INSERT INTO "interventions" ({col_list}) VALUES ({placeholders}) '
                        f'ON CONFLICT("{numero_col}") DO UPDATE SET {set_list}',
                        vals,
                    )
                total += len(rows)

            except Exception as e:
                print(f"[startup-xml] Erreur {path}: {e}")

        conn.commit()
    return f"{total} enregistrements XML traités"
