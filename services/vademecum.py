"""Integracion ligera con Vademecum para enlaces y datos complementarios."""

from __future__ import annotations

import logging
import re
import unicodedata
from functools import lru_cache
from html import unescape
from urllib.parse import quote_plus, urljoin

import httpx
from services.busqueda_texto import cubre_consulta, normalizar_texto, puntuar_coincidencia

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.vademecum.es"
_SEARCH_PATH = "/buscar?q={}"
_TIMEOUT = httpx.Timeout(12.0, connect=8.0)

_MEDICAMENTO_LINK_RE = re.compile(
    r'<a[^>]+title="medicamento"[^>]+href="(?P<href>/[^"]+)"[^>]*>(?P<label>.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_PROSPECTO_RE = re.compile(
    r'<a[^>]+href="(?P<href>/espana/prospecto/[^"]+)"[^>]*>\s*Prospecto\s*</a>',
    re.IGNORECASE | re.DOTALL,
)
_PRECIO_PVL_RE = re.compile(
    r'id="precio_pvl_\d+"[^>]*>[\s\S]*?<span[^>]*>\s*([\d.,]+)\s*&#8364;',
    re.IGNORECASE,
)
_PRECIO_PVPIVA_RE = re.compile(
    r'id="precio_pvpiva_\d+"[^>]*>[\s\S]*?<span[^>]*>[\s\S]*?([\d.,]+)&#8364;',
    re.IGNORECASE,
)
_TITLE_RE = re.compile(r"<title>(?P<value>.*?)</title>", re.IGNORECASE | re.DOTALL)
_H1_RE = re.compile(r"<h1[^>]*>(?P<value>.*?)</h1>", re.IGNORECASE | re.DOTALL)
_LAB_RE = re.compile(
    r"<strong>\s*Laboratorio Comercializador:\s*</strong>\s*(?:<a[^>]*>)?\s*(?:<span[^>]*itemprop=\"manufacturer\"[^>]*>)?(?P<value>.*?)(?:</span>)?\s*(?:</a>)?\s*</",
    re.IGNORECASE | re.DOTALL,
)
_PRINCIPIO_RE = re.compile(
    r"<strong>\s*Principio Activo:\s*</strong>\s*(?P<value>.*?)\s*</div>",
    re.IGNORECASE | re.DOTALL,
)
_INDICACIONES_RE = re.compile(
    r'<div class="indicaciones-wrapper"[^>]*>(?P<value>.*?)</div>',
    re.IGNORECASE | re.DOTALL,
)
_DOSIS_RE = re.compile(
    r"\b\d+(?:[.,/]\d+)*(?:\s*(?:mg|mcg|g|gr|ml|ui|ug|%|mcg/ml|mg/ml|g/ml|ui/ml)\b)?(?:\s*/\s*(?:mg|mcg|g|gr|ml|ui|ug))?",
    re.IGNORECASE,
)
_UNIDADES_RE = re.compile(r"\b(?:mg|mcg|g|gr|ml|ui|ug)\b", re.IGNORECASE)
_PRESENTACION_RE = re.compile(
    r"\b(?:comprimidos?|c[aá]psulas?|caps\.?|tabletas?|sobres?|jarabe|soluci[oó]n|susp(?:ension|ensi[oó]n)?|polvo|granulado|inyectable|colirio|pomada|crema|gel|ampollas?|viales?|efg|comp\.?|recub\.?|pel[ií]cula|oral|bucal|cut[aá]nea|oft[aá]lmica|nasal|duras?|blandas?|gastroresistentes?)\b",
    re.IGNORECASE,
)


def construir_url_busqueda(consulta: str) -> str:
    termino = _normalizar_consulta_externa(limpiar_consulta(consulta))
    return urljoin(_BASE_URL, _SEARCH_PATH.format(quote_plus(termino)))


def limpiar_consulta(consulta: str) -> str:
    termino = unescape((consulta or "").strip())
    termino = re.sub(r"\([^)]*\)", " ", termino)
    termino = _DOSIS_RE.sub(" ", termino)
    termino = termino.replace("/", " ")
    termino = _PRESENTACION_RE.sub(" ", termino)
    termino = _UNIDADES_RE.sub(" ", termino)
    termino = re.sub(r"\b(?:de|del|la|el)\b", " ", termino, flags=re.IGNORECASE)
    termino = re.sub(r"[^A-Za-zÁÉÍÓÚÜÑáéíóúüñ0-9+ -]", " ", termino)
    termino = re.sub(r"\s+", " ", termino).strip(" -+")
    return termino or (consulta or "").strip()


def _normalizar_consulta_externa(consulta: str) -> str:
    termino = unicodedata.normalize("NFKD", consulta or "")
    termino = "".join(char for char in termino if not unicodedata.combining(char))
    termino = re.sub(r"[^A-Za-z0-9+ -]", " ", termino)
    return re.sub(r"\s+", " ", termino).strip()


def buscar_medicamentos(consulta: str, limit: int = 10) -> list[dict]:
    consulta_limpia = limpiar_consulta(consulta)
    if not consulta_limpia:
        return []

    html = _buscar_html(consulta_limpia)
    if not html:
        return []

    resultados = []
    vistos = set()
    for match in _MEDICAMENTO_LINK_RE.finditer(html):
        href = match.group("href").strip()
        if href in vistos:
            continue

        titulo = _limpiar_html(match.group("label"))
        if not titulo:
            continue
        if not cubre_consulta(titulo, consulta_limpia):
            continue

        vistos.add(href)
        resultados.append(
            {
                "titulo": titulo,
                "url": urljoin(_BASE_URL, href),
                "comparacion_score": puntuar_coincidencia(titulo, consulta_limpia),
            }
        )

    resultados.sort(key=lambda item: item.get("comparacion_score", 0), reverse=True)
    return resultados[:limit]


def obtener_complemento(nombre: str, principio_activo: str = "") -> dict | None:
    consultas = []
    if (nombre or "").strip():
        consultas.append(limpiar_consulta(nombre))
    if (principio_activo or "").strip():
        principio = limpiar_consulta(principio_activo)
        if _normalizar_texto(principio) != _normalizar_texto(limpiar_consulta(nombre)):
            consultas.append(principio)

    consultas = [consulta for consulta in dict.fromkeys(consultas) if consulta]

    referencia_principal = limpiar_consulta(nombre or principio_activo or "")
    busqueda_url = construir_url_busqueda(referencia_principal)

    mejor = None
    for consulta in consultas:
        candidatos = buscar_medicamentos(consulta, limit=12)
        if not candidatos:
            continue

        candidato = _seleccionar_mejor_coincidencia(candidatos, nombre, principio_activo)
        if candidato:
            mejor = {**candidato, "busqueda_url": construir_url_busqueda(consulta)}
            break

    if not mejor:
        return {"busqueda_url": busqueda_url}

    detalle = _obtener_detalle(mejor["url"])
    if detalle:
        mejor.update(detalle)

    mejor.setdefault("busqueda_url", busqueda_url)
    return mejor


def obtener_precio(nombre_comercial: str) -> dict | None:
    """Obtiene el precio (PVL y PVP IVA) buscando en vademecum.es por nombre comercial.

    Útil como fuente de precio alternativa para medicamentos no incluidos
    en el Nomenclátor del SNS.
    """
    # Usar solo el primer término del nombre para evitar que limpiar_consulta
    # degrade demasiado el nombre largo con dosis y forma farmacéutica
    termino = (nombre_comercial or "").strip().split()[0] if nombre_comercial else ""
    if not termino:
        return None
    candidatos = buscar_medicamentos(termino, limit=5)
    if not candidatos:
        return None
    # Seleccionar el que más se parezca al nombre completo
    mejor = _seleccionar_mejor_coincidencia(candidatos, nombre_comercial, "")
    if not mejor:
        mejor = candidatos[0]
    detalle = _obtener_detalle(mejor["url"])
    if not detalle:
        return None
    return {**mejor, **detalle}


@lru_cache(maxsize=64)
def _buscar_html(consulta: str) -> str:
    try:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as client:
            resp = client.get(construir_url_busqueda(consulta))
            resp.raise_for_status()
            return resp.text
    except Exception:
        logger.debug("No se pudo consultar Vademecum para %s", consulta, exc_info=True)
        return ""


@lru_cache(maxsize=64)
def _obtener_detalle(url: str) -> dict | None:
    try:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
    except Exception:
        logger.debug("No se pudo cargar detalle de Vademecum: %s", url, exc_info=True)
        return None

    html = resp.text
    if not html:
        return None

    titulo = _extraer_campo(_H1_RE, html) or _extraer_campo(_TITLE_RE, html)
    if titulo.endswith(" - Vademecum.es"):
        titulo = titulo.removesuffix(" - Vademecum.es").strip()
    if titulo.endswith(" - Vademecum"):
        titulo = titulo.removesuffix(" - Vademecum").strip()

    principio_activo = _extraer_campo(_PRINCIPIO_RE, html)
    laboratorio = _extraer_campo(_LAB_RE, html)
    indicaciones = _extraer_campo(_INDICACIONES_RE, html)

    prospecto = None
    prospecto_match = _PROSPECTO_RE.search(html)
    if prospecto_match:
        prospecto = urljoin(_BASE_URL, prospecto_match.group("href"))

    # Precio: está en el HTML aunque oculto por CSS (display:none)
    pvl = None
    pvpiva = None
    pvl_match = _PRECIO_PVL_RE.search(html)
    if pvl_match:
        try:
            pvl = float(pvl_match.group(1).replace(",", "."))
        except ValueError:
            pass
    pvpiva_match = _PRECIO_PVPIVA_RE.search(html)
    if pvpiva_match:
        try:
            pvpiva = float(pvpiva_match.group(1).replace(",", "."))
        except ValueError:
            pass

    return {
        "titulo": titulo,
        "url": url,
        "prospecto_url": prospecto,
        "principio_activo": principio_activo,
        "laboratorio": laboratorio,
        "indicaciones": indicaciones,
        "pvl": pvl,
        "pvpiva": pvpiva,
    }


def _seleccionar_mejor_coincidencia(candidatos: list[dict], nombre: str, principio_activo: str) -> dict | None:
    referencias = [valor for valor in [nombre, principio_activo] if (valor or "").strip()]
    if not referencias:
        return candidatos[0] if candidatos else None

    mejor = None
    mejor_puntuacion = -1
    for candidato in candidatos:
        puntuacion = max(_puntuar_coincidencia(candidato["titulo"], referencia) for referencia in referencias)
        if puntuacion > mejor_puntuacion:
            mejor = candidato
            mejor_puntuacion = puntuacion

    return mejor


def _puntuar_coincidencia(candidato: str, referencia: str) -> int:
    return puntuar_coincidencia(candidato, referencia)


def _extraer_campo(pattern: re.Pattern[str], html: str) -> str:
    match = pattern.search(html)
    if not match:
        return ""
    return _limpiar_html(match.group("value"))


def _limpiar_html(valor: str) -> str:
    limpio = re.sub(r"<script.*?</script>", " ", valor, flags=re.IGNORECASE | re.DOTALL)
    limpio = re.sub(r"<style.*?</style>", " ", limpio, flags=re.IGNORECASE | re.DOTALL)
    limpio = re.sub(r"<[^>]+>", " ", limpio)
    limpio = unescape(limpio)
    limpio = re.sub(r"\s+", " ", limpio).strip()
    return limpio


def _normalizar_texto(valor: str) -> str:
    return normalizar_texto(valor)