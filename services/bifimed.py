"""Scraper del buscador BIFIMED (Ministerio de Sanidad).

Consulta la situación oficial de financiación de medicamentos:
https://www.sanidad.gob.es/profesionales/medicamentos.do

Requiere dos pasos: GET para obtener cookies de sesión + POST con la búsqueda.
Los resultados se cachean 24 h (el Nomenclátor se actualiza mensualmente).
"""

import logging
import re
import time

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
_FORM_URL = "https://www.sanidad.gob.es/profesionales/medicamentos.do?metodo=buscarMedicamentos"
_POST_URL = "https://www.sanidad.gob.es/profesionales/medicamentos.do?metodo=buscarMedicamentos"
_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"

# Caché simple en memoria: clave → (timestamp_monotonic, resultados)
_CACHE: dict[str, tuple[float, list[dict]]] = {}
_CACHE_TTL = 86400.0  # 24 horas

# Situaciones que indican financiación activa
SITUACIONES_FINANCIADO = {
    "Sí",
    "Sí para determinadas indicaciones/condiciones",
}
# Situaciones que indican exclusión o no financiación explícita
SITUACIONES_EXCLUIDO = {
    "Excluido",
    "No financiado por resolución",
}


_NORMALIZAR_SITUACION = {
    "Si": "Sí",
    "Si para determinadas indicaciones/condiciones": "Sí para determinadas indicaciones/condiciones",
}


def _parse_tabla(html: str) -> list[dict]:
    """Extrae filas de resultados de la tabla HTML de BIFIMED."""
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.DOTALL)
    resultados = []
    for row in rows:
        # Ignorar filas de cabecera
        if "<th" in row:
            continue
        celdas_raw = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)
        celdas = [re.sub(r"<[^>]+>", "", c).strip() for c in celdas_raw]
        # Columnas esperadas: CN, principio activo, nombre, situación, tipo
        if len(celdas) >= 4:
            situacion_raw = celdas[3].strip()
            resultados.append(
                {
                    "cn": celdas[0].strip(),
                    "principio_activo": celdas[1].strip(),
                    "nombre": celdas[2].strip(),
                    "situacion": _NORMALIZAR_SITUACION.get(situacion_raw, situacion_raw),
                    "tipo": celdas[4].strip() if len(celdas) > 4 else "",
                }
            )
    return resultados


def consultar_financiacion(nombre_o_cn: str) -> list[dict]:
    """Consulta BIFIMED y devuelve los resultados de financiación.

    Args:
        nombre_o_cn: nombre del medicamento o código nacional (CN).

    Returns:
        Lista de dicts con cn, nombre, principio_activo, situacion, tipo.
        Lista vacía si no hay resultados o hay error de red.
    """
    clave = nombre_o_cn.strip().lower()
    if not clave:
        return []

    entrada = _CACHE.get(clave)
    if entrada is not None and (time.monotonic() - entrada[0]) < _CACHE_TTL:
        return entrada[1]

    try:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as client:
            # Paso 1: GET para obtener cookies de sesión
            client.get(_FORM_URL, headers={"User-Agent": _USER_AGENT})
            # Paso 2: POST con el término de búsqueda
            resp = client.post(
                _POST_URL,
                data={"nombre_cn": nombre_o_cn.strip()},
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": _USER_AGENT,
                    "Referer": _FORM_URL,
                },
            )
            resp.raise_for_status()
            html = resp.text
    except Exception as exc:
        logger.warning("BIFIMED: error consultando '%s': %s", nombre_o_cn, exc)
        return []

    resultados = _parse_tabla(html)
    _CACHE[clave] = (time.monotonic(), resultados)
    logger.debug("BIFIMED: '%s' → %d resultados", nombre_o_cn, len(resultados))
    return resultados


def situacion_por_cn(cn: str) -> str | None:
    """Devuelve la situación de financiación para un CN concreto, o None.

    Busca directamente por CN y compara con precisión para evitar falsos
    positivos de CNs parcialmente coincidentes.
    """
    if not cn:
        return None
    resultados = consultar_financiacion(cn)
    for r in resultados:
        if r["cn"] == cn:
            return r["situacion"] or None
    return None
