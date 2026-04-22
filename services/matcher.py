"""Matcher híbrido (FTS5 + rapidfuzz + features) para Nomenclátor SNS.

Pipeline:
  1. Normalizar query (sin acentos, sin stopwords farmacéuticos).
  2. Construir consulta FTS5 → recuperar hasta 60 candidatos (recall alto).
  3. Puntuar cada candidato con rapidfuzz.token_set_ratio.
  4. Aplicar bonuses por dosis, unidades y forma farmacéutica coincidentes.
  5. Devolver el mejor resultado con su nivel de confianza.

Confianza:
  - "seguro"   score ≥ 92
  - "probable" score ≥ 80
  - "debil"    score < 80
  - "no_match" sin candidatos
"""

from __future__ import annotations

import logging
import re
import unicodedata
from functools import lru_cache
from typing import Any

from rapidfuzz import fuzz

from database import db_session

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Umbrales
# ---------------------------------------------------------------------------
_UMBRAL_SEGURO = 92
_UMBRAL_PROBABLE = 80

# ---------------------------------------------------------------------------
# Stopwords farmacéuticos (se eliminan en la normalización)
# ---------------------------------------------------------------------------
_STOPWORDS: frozenset[str] = frozenset({
    "mg", "ml", "g", "mcg", "ui", "ug", "gr",
    "comprimidos", "comprimido", "capsulas", "capsula", "caps",
    "tabletas", "tableta", "sobres", "sobre",
    "suspension", "solucion", "solución", "suspensión",
    "efg", "e.f.g", "generico", "genérico",
    "via", "vía", "oral", "cutanea", "cutánea", "topica", "tópica",
    "recubiertos", "recubierto", "blandas", "blando", "duras", "dura",
    "liberacion", "liberación", "prolongada", "modificada", "retard",
    "mas", "más", "con", "de", "del", "la", "el", "para", "un", "una",
    "y", "e", "o", "en",
})

# Formas farmacéuticas canónicas (text → clave)
_FORMAS: dict[str, str] = {
    "comprimidos": "comprimidos", "comprimido": "comprimidos",
    "tabletas": "comprimidos", "tableta": "comprimidos",
    "capsulas": "capsulas", "capsula": "capsulas", "caps": "capsulas",
    "suspension": "suspension", "suspensión": "suspension",
    "solucion": "solucion", "solución": "solucion",
    "crema": "crema", "pomada": "pomada",
    "colirio": "colirio", "gotas": "gotas",
    "jarabe": "jarabe",
    "inyectable": "inyectable", "ampollas": "inyectable", "viales": "inyectable",
    "parches": "parches",
    "supositorios": "supositorios", "supositorio": "supositorios",
    "inhalador": "inhalador", "spray": "spray", "aerosol": "aerosol",
    "gel": "gel",
    "sobres": "sobres",
}


# ---------------------------------------------------------------------------
# Normalización
# ---------------------------------------------------------------------------

def normalizar(texto: str) -> str:
    """Normalización agresiva: minúsculas, sin acentos, sin puntuación, sin stopwords ni números.

    Los valores numéricos (dosis, unidades) se extraen por separado en ``extraer_features``
    y se usan como bonuses, evitando que dominen la similitud textual.
    """
    texto = (texto or "").lower()
    # Quitar acentos
    texto = unicodedata.normalize("NFD", texto)
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    # Solo letras + espacios (eliminar números y puntuación)
    texto = re.sub(r"[^a-z\s]", " ", texto)
    # Eliminar stopwords y tokens de un solo carácter
    tokens = [t for t in texto.split() if t not in _STOPWORDS and len(t) > 1]
    return " ".join(tokens)


def _fts_query(texto_norm: str) -> str:
    """Construye una consulta FTS5 con OR entre tokens (mayor recall).

    Solo incluye tokens alfabéticos (sin números): los valores numéricos
    ya están filtrados en ``normalizar`` y se usan como bonuses de features.
    """
    tokens = [t for t in texto_norm.split() if len(t) > 1 and t.isalpha()]
    if not tokens:
        return ""
    # Escapar caracteres especiales de FTS5
    tokens_safe = [re.sub(r'["\*\(\)\[\]\{\}^:,\.]+', "", t) for t in tokens]
    tokens_safe = [t for t in tokens_safe if t]
    return " OR ".join(tokens_safe)


# ---------------------------------------------------------------------------
# Extracción de features estructuradas
# ---------------------------------------------------------------------------

def extraer_features(texto: str) -> dict:
    """Extrae dosis (mg), unidades, forma farmacéutica y base del nombre."""
    texto_l = (texto or "").lower()

    # Dosis en mg (puede ser "600 mg", "600mg", "600 mg/ml")
    dosis_mg: int | None = None
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*mg(?:/ml|/g)?", texto_l)
    if m:
        try:
            dosis_mg = int(float(m.group(1).replace(",", ".")))
        except ValueError:
            pass

    # Dosis en g → convertir a mg (si no se encontró mg)
    if dosis_mg is None:
        m = re.search(r"(\d+(?:[.,]\d+)?)\s*g(?:\s|/|$)", texto_l)
        if m:
            try:
                dosis_mg = int(float(m.group(1).replace(",", ".")) * 1000)
            except ValueError:
                pass

    # Unidades: número antes de forma sólida
    unidades: int | None = None
    m = re.search(
        r"(\d+)\s*(?:comprimidos?|c[aá]psulas?|caps|tabletas?|sobres?|"
        r"supositorios?|parches?|ampollas?|viales?)",
        texto_l,
    )
    if m:
        unidades = int(m.group(1))

    # Forma farmacéutica
    forma: str | None = None
    texto_sin_num = re.sub(r"\d+", "", texto_l)
    for keyword, forma_norm in _FORMAS.items():
        if re.search(r"\b" + keyword + r"\b", texto_sin_num):
            forma = forma_norm
            break

    # Base del nombre: texto antes del primer dígito (principio activo + lab)
    base = ""
    m = re.match(r"^([a-záéíóúüñ /\-]+?)(?=\s*\d|\s*$)", texto_l)
    if m:
        base = m.group(1).strip()

    return {
        "dosis_mg": dosis_mg,
        "unidades": unidades,
        "forma": forma,
        "base": base,
    }


# ---------------------------------------------------------------------------
# Población de la tabla de features
# ---------------------------------------------------------------------------

def poblar_features(force: bool = False) -> int:
    """Reconstruye ``medicamento_features`` + FTS5 desde ``nomenclator_producto``.

    Solo actualiza si la tabla está vacía o tiene más de un 5 % de filas
    nuevas sin procesar (o si se llama con ``force=True``).

    Returns el número de registros insertados.
    """
    with db_session() as conn:
        total_nom = conn.execute(
            "SELECT COUNT(*) FROM nomenclator_producto"
        ).fetchone()[0]
        total_feat = conn.execute(
            "SELECT COUNT(*) FROM medicamento_features"
        ).fetchone()[0]

    if not force and total_nom > 0 and total_feat >= int(total_nom * 0.95):
        logger.debug(
            "medicamento_features ya actualizada (%d/%d). Omitiendo.",
            total_feat, total_nom,
        )
        return total_feat

    logger.info("Poblando medicamento_features desde %d registros de Nomenclátor…", total_nom)

    with db_session() as conn:
        rows = conn.execute(
            """SELECT np.cn, np.nombre, p.pvp_iva
               FROM nomenclator_producto np
               LEFT JOIN precio p ON p.cn = np.cn AND p.fuente = 'nomenclator'"""
        ).fetchall()

    registros: list[tuple] = []
    for row in rows:
        cn = row["cn"]
        nombre = row["nombre"] or ""
        pvp = row["pvp_iva"]
        nombre_norm = normalizar(nombre)
        feats = extraer_features(nombre)
        registros.append((
            cn,
            nombre,
            nombre_norm,
            feats["base"],
            feats["dosis_mg"],
            feats["unidades"],
            feats["forma"],
            pvp,
        ))

    with db_session() as conn:
        conn.execute("DELETE FROM medicamento_features")
        conn.executemany(
            """INSERT INTO medicamento_features
               (cn, nombre, nombre_norm, principio_activo, dosis_mg, unidades, forma, pvp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            registros,
        )
        # Reconstruir FTS5
        conn.execute("DELETE FROM medicamento_fts")
        conn.execute(
            """INSERT INTO medicamento_fts(cn, nombre_norm, principio_activo)
               SELECT cn, nombre_norm, principio_activo FROM medicamento_features"""
        )

    match_producto.cache_clear()
    logger.info("medicamento_features poblada con %d registros.", len(registros))
    return len(registros)


# ---------------------------------------------------------------------------
# Pipeline de matching
# ---------------------------------------------------------------------------

@lru_cache(maxsize=4096)
def match_producto(query: str) -> dict | None:
    """Encuentra el medicamento del Nomenclátor que mejor coincide con *query*.

    Returns:
        ``dict`` con:
        - ``cn``         (str)  Código Nacional
        - ``nombre``     (str)  Nombre oficial
        - ``pvp``        (float | None)
        - ``score``      (int)  0-100
        - ``confianza``  (str)  "seguro" | "probable" | "debil"
        - ``features``   (dict) features extraídas del query
        o ``None`` si no hay candidatos.
    """
    query_norm = normalizar(query)
    if not query_norm:
        return None

    features = extraer_features(query)
    fts_q = _fts_query(query_norm)
    if not fts_q:
        return None

    candidatos: list = []

    with db_session() as conn:
        # 1. Candidatos vía FTS5
        try:
            fts_hits = conn.execute(
                "SELECT cn FROM medicamento_fts WHERE medicamento_fts MATCH ? LIMIT 60",
                (fts_q,),
            ).fetchall()
            if fts_hits:
                cns = [r["cn"] for r in fts_hits]
                placeholders = ",".join("?" * len(cns))
                candidatos = conn.execute(
                    f"""SELECT cn, nombre, nombre_norm, principio_activo,
                               dosis_mg, unidades, forma, pvp
                        FROM medicamento_features WHERE cn IN ({placeholders})""",
                    cns,
                ).fetchall()
        except Exception as exc:
            logger.debug("FTS5 falló (query='%s'): %s", fts_q, exc)

        # 2. Fallback por dosis si FTS no devuelve nada
        if not candidatos and features["dosis_mg"] is not None:
            candidatos = conn.execute(
                """SELECT cn, nombre, nombre_norm, principio_activo,
                          dosis_mg, unidades, forma, pvp
                   FROM medicamento_features WHERE dosis_mg = ? LIMIT 30""",
                (features["dosis_mg"],),
            ).fetchall()

    if not candidatos:
        return None

    scored: list[tuple[int, Any]] = []
    for c in candidatos:
        score = int(round(fuzz.token_set_ratio(query_norm, c["nombre_norm"])))

        # Bonus por dosis exacta (muy discriminante, ±0 mg)
        if features["dosis_mg"] is not None and c["dosis_mg"] == features["dosis_mg"]:
            score += 15
        # Bonus por unidades exactas
        if features["unidades"] is not None and c["unidades"] == features["unidades"]:
            score += 10
        # Bonus por forma farmacéutica
        if features["forma"] and c["forma"] == features["forma"]:
            score += 8
        # Bonus si el principio activo del candidato aparece en el query normalizado
        if c["principio_activo"] and c["principio_activo"].lower() in query_norm:
            score += 10
        # Penalización si el principio activo NO aparece en el query (evita falsos positivos
        # cuando dosis/unidades coinciden pero el principio activo es diferente)
        elif c["principio_activo"] and len(c["principio_activo"]) > 3:
            score -= 20

        scored.append((score, c))  # sin clamp para que los bonuses discriminen

    scored.sort(key=lambda x: x[0], reverse=True)
    top_score_raw, top = scored[0]
    top_score = min(max(int(top_score_raw), 0), 100)
    
    # Validación especial: si el query normalizado es exactamente igual al nombre normalizado,
    # es un match perfecto (típico cuando el usuario escribe en minúsculas)
    if query_norm.strip() == str(top["nombre_norm"] or "").strip():
        top_score = 100

    nreg = _nregistro_por_cn(top["cn"])
    return {
        "cn": top["cn"],
        "nregistro": nreg,
        "nombre": top["nombre"],
        "pvp": top["pvp"],
        "score": top_score,
        "confianza": interpretar_confianza(top_score),
        "features": features,
    }


def _nregistro_por_cn(cn: str) -> str | None:
    """Devuelve el nregistro asociado a un CN consultando la tabla presentacion."""
    try:
        with db_session() as conn:
            row = conn.execute(
                "SELECT nregistro FROM presentacion WHERE cn = ?", (cn,)
            ).fetchone()
            return row["nregistro"] if row else None
    except Exception:
        return None


def interpretar_confianza(score: int) -> str:
    """Convierte un score numérico en un nivel de confianza textual."""
    if score >= _UMBRAL_SEGURO:
        return "seguro"
    if score >= _UMBRAL_PROBABLE:
        return "probable"
    return "debil"


def buscar_candidatos(query: str, limit: int = 10) -> list[dict]:
    """Devuelve los *limit* mejores candidatos con scores y confianza.

    Útil para mostrar múltiples opciones cuando la confianza es baja.
    """
    query_norm = normalizar(query)
    if not query_norm:
        return []

    features = extraer_features(query)
    fts_q = _fts_query(query_norm)
    if not fts_q:
        return []

    candidatos: list = []
    with db_session() as conn:
        try:
            fts_hits = conn.execute(
                "SELECT cn FROM medicamento_fts WHERE medicamento_fts MATCH ? LIMIT 80",
                (fts_q,),
            ).fetchall()
            if fts_hits:
                cns = [r["cn"] for r in fts_hits]
                placeholders = ",".join("?" * len(cns))
                candidatos = conn.execute(
                    f"""SELECT cn, nombre, nombre_norm, principio_activo,
                               dosis_mg, unidades, forma, pvp
                        FROM medicamento_features WHERE cn IN ({placeholders})""",
                    cns,
                ).fetchall()
        except Exception as exc:
            logger.debug("FTS5 (buscar_candidatos) falló: %s", exc)

    if not candidatos and features["dosis_mg"] is not None:
        with db_session() as conn:
            candidatos = conn.execute(
                """SELECT cn, nombre, nombre_norm, principio_activo,
                          dosis_mg, unidades, forma, pvp
                   FROM medicamento_features WHERE dosis_mg = ? LIMIT 40""",
                (features["dosis_mg"],),
            ).fetchall()

    if not candidatos:
        return []

    scored: list[tuple[int, Any]] = []
    for c in candidatos:
        score = int(round(fuzz.token_set_ratio(query_norm, c["nombre_norm"])))
        
        # Validación especial: si el query normalizado es exactamente igual al nombre normalizado,
        # es un match perfecto (típico cuando el usuario escribe en minúsculas)
        if query_norm.strip() == str(c["nombre_norm"] or "").strip():
            score = 100
        
        if features["dosis_mg"] is not None and c["dosis_mg"] == features["dosis_mg"]:
            score += 15
        if features["unidades"] is not None and c["unidades"] == features["unidades"]:
            score += 10
        if features["forma"] and c["forma"] == features["forma"]:
            score += 8
        if c["principio_activo"] and c["principio_activo"].lower() in query_norm:
            score += 10
        elif c["principio_activo"] and len(c["principio_activo"]) > 3:
            score -= 20
        scored.append((score, c))  # sin clamp para discriminar correctamente

    scored.sort(key=lambda x: x[0], reverse=True)

    return [
        {
            "cn": c["cn"],
            "nombre": c["nombre"],
            "pvp": c["pvp"],
            "score": min(max(int(s), 0), 100),
            "confianza": interpretar_confianza(min(max(int(s), 0), 100)),
        }
        for s, c in scored[:limit]
    ]


def buscar_por_cn(cn: str) -> dict | None:
    """Busca un medicamento exactamente por su Código Nacional (CN).

    Returns el mismo formato que ``match_producto`` con confianza 'seguro',
    o ``None`` si el CN no existe.
    """
    cn = cn.strip().lstrip("0")  # normalizar: eliminar ceros a la izquierda
    with db_session() as conn:
        row = conn.execute(
            """SELECT cn, nombre, pvp FROM medicamento_features
               WHERE CAST(CAST(cn AS INTEGER) AS TEXT) = CAST(CAST(? AS INTEGER) AS TEXT)
               LIMIT 1""",
            (cn,),
        ).fetchone()
    if not row:
        return None
    return {
        "cn": row["cn"],
        "nombre": row["nombre"],
        "pvp": row["pvp"],
        "score": 100,
        "confianza": "seguro",
        "features": {},
    }
