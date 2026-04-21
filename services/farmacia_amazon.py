"""Proveedor Amazon.es para productos de parafarmacia y droguería."""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from functools import lru_cache
from html import unescape
from urllib.parse import quote_plus

import httpx
from services.envios import obtener_politica_envio

logger = logging.getLogger(__name__)

FUENTE_ID = "amazon"
FUENTE_NOMBRE = "Amazon.es"
FUENTE_URL = "https://www.amazon.es"

_TIMEOUT = httpx.Timeout(12.0, connect=6.0)
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Cache-Control": "no-cache",
    "Upgrade-Insecure-Requests": "1",
}
_CLIENT = httpx.Client(
    timeout=_TIMEOUT,
    follow_redirects=True,
    headers=_HEADERS,
    http2=False,
)

# Cortesía: circuit breaker (CAPTCHA/bloqueo) y cadencia mínima
_CB_COOLDOWN = 300.0  # Amazon bloquea agresivamente; cooldown de 5 min
_cb_fallo_at: float = 0.0
_last_request_at: float = 0.0
_MIN_DELAY = 1.5  # Amazon requiere más pausa entre búsquedas


def _esperar_cortesia() -> None:
    """Pausa mínima entre búsquedas a Amazon para no saturar el servidor."""
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if elapsed < _MIN_DELAY:
        time.sleep(_MIN_DELAY - elapsed)
    _last_request_at = time.monotonic()

# Regex para extraer cards de búsqueda de Amazon
# Cada resultado tiene data-asin="XXXXXX" en el div contenedor
_RESULT_RE = re.compile(
    r'data-asin="(?P<asin>[A-Z0-9]{10})"[^>]*data-component-type="s-search-result".*?'
    r'(?=data-asin="[A-Z0-9]{10}"[^>]*data-component-type="s-search-result"|$)',
    re.DOTALL,
)
_TITLE_RE = re.compile(
    r'<span[^>]*class="[^"]*a-size-base-plus[^"]*a-color-base[^"]*a-text-normal[^"]*"[^>]*>'
    r'(?P<title>[^<]+)</span>|'
    r'<span[^>]*class="[^"]*a-text-normal[^"]*"[^>]*>(?P<title2>[^<]+)</span>',
)
_PRICE_RE = re.compile(
    r'<span class="a-offscreen">(?P<price>[^<]+)</span>'
)
_IMG_RE = re.compile(
    r'<img[^>]*class="[^"]*s-image[^"]*"[^>]*src="(?P<src>[^"]+)"',
)
_IMG_DATA_RE = re.compile(
    r'<img[^>]*class="[^"]*s-image[^"]*"[^>]*data-src="(?P<src>[^"]+)"',
)
_URL_RE = re.compile(
    r'<a[^>]*class="[^"]*a-link-normal[^"]*s-no-outline[^"]*"[^>]*href="(?P<href>/[^"?#]+(?:/dp/[A-Z0-9]{10})[^"]*)"',
)
_HREF_RE = re.compile(
    r'href="(?P<href>/[^"?]*dp/[A-Z0-9]{10}[^"]*)"'
)


def buscar_productos(consulta: str, limit: int = 12) -> list[dict]:
    productos: list[dict] = []
    vistos: set[str] = set()

    for query in _variantes_consulta(consulta):
        for item in _buscar_pagina(query, limit=max(limit, 12)):
            producto = _normalizar_item(item)
            if not producto:
                continue
            if not _es_relevante(producto["nombre"], consulta):
                continue
            oid = producto.get("object_id")
            if not oid or oid in vistos:
                continue
            vistos.add(oid)
            productos.append(producto)
            if len(productos) >= limit:
                return productos

    return productos


def obtener_producto(object_id: str) -> dict | None:
    """Devuelve None; el detalle de Amazon requeriría scraping adicional."""
    return None


@lru_cache(maxsize=128)
def _buscar_pagina(consulta: str, limit: int = 24) -> tuple[dict, ...]:
    global _cb_fallo_at

    # Circuit breaker: skip si hubo bloqueo/CAPTCHA reciente
    if time.monotonic() - _cb_fallo_at < _CB_COOLDOWN:
        return ()

    query = _normalizar_consulta_externa(consulta)
    if not query:
        return ()

    url = f"{FUENTE_URL}/s"
    params = {
        "k": query,
        "i": "beauty",      # categoría Belleza
        "s": "relevancerank",
    }

    _esperar_cortesia()
    try:
        resp = _CLIENT.get(url, params=params)
        if resp.status_code == 429:
            espera = int(resp.headers.get("Retry-After", int(_CB_COOLDOWN)))
            _cb_fallo_at = time.monotonic() + espera - _CB_COOLDOWN
            logger.warning("Amazon.es 429 — pausando %ds", espera)
            return ()
        resp.raise_for_status()
    except httpx.TimeoutException:
        _cb_fallo_at = time.monotonic()
        logger.debug("Timeout — Amazon pausado %ds", int(_CB_COOLDOWN))
        return ()
    except Exception:
        logger.debug("No se pudo consultar Amazon.es para %s", consulta, exc_info=True)
        return ()

    # Detectar página de bloqueo/captcha: páginas de bloqueo reales son muy pequeñas
    # o contienen mensajes específicos de CAPTCHA de Amazon
    body = resp.text
    if len(body) < 5000 or "captcha" in body.lower() or "type the characters you see" in body.lower():
        logger.warning("Amazon.es ha bloqueado la petición para '%s'", consulta)
        _cb_fallo_at = time.monotonic()  # activar CB ante CAPTCHA
        return ()

    items = _parsear_resultados(body, limit=limit)
    return tuple(items)


def _parsear_resultados(html: str, limit: int = 24) -> list[dict]:
    """Extrae productos de la página de resultados de Amazon."""
    items: list[dict] = []

    # Dividir por bloques de resultado: cada div contiene un ASIN
    # Estrategia: localizar bloques que tengan data-asin y data-component-type="s-search-result"
    bloques_re = re.compile(
        r'<div[^>]+data-asin="([A-Z0-9]{10})"[^>]+data-component-type="s-search-result"'
        r'(.*?)'
        r'(?=<div[^>]+data-asin="[A-Z0-9]{10}"[^>]+data-component-type="s-search-result"|</main>)',
        re.DOTALL,
    )

    for m in bloques_re.finditer(html):
        if len(items) >= limit:
            break

        asin = m.group(1)
        bloque = m.group(2)

        # Título
        titulo = _extraer_titulo(bloque)
        if not titulo:
            continue

        # URL del producto
        url = _extraer_url(bloque, asin)

        # Precio
        precio = _extraer_precio(bloque)

        # Imagen
        imagen = _extraer_imagen(bloque)

        items.append({
            "asin": asin,
            "titulo": titulo,
            "precio": precio,
            "imagen": imagen,
            "url": url,
        })

    return items


def _extraer_titulo(bloque: str) -> str:
    # Buscar en diferentes patrones de título de Amazon
    patrones = [
        re.compile(r'<span[^>]*class="[^"]*a-size-medium[^"]*a-color-base[^"]*a-text-normal[^"]*"[^>]*>([^<]+)</span>'),
        re.compile(r'<span[^>]*class="[^"]*a-size-base-plus[^"]*"[^>]*>([^<]+)</span>'),
        re.compile(r'<span[^>]*class="[^"]*a-text-normal[^"]*"[^>]*>([^<]+)</span>'),
        re.compile(r'<h2[^>]*>.*?<span[^>]*>([^<]+)</span>', re.DOTALL),
    ]
    for pat in patrones:
        m = pat.search(bloque)
        if m:
            titulo = unescape(m.group(1)).strip()
            if titulo and len(titulo) > 3:
                return titulo
    return ""


def _extraer_url(bloque: str, asin: str) -> str:
    # Buscar enlace /dp/ en el bloque
    m = _HREF_RE.search(bloque)
    if m:
        href = m.group("href")
        # Limpiar parámetros de tracking y quedarnos con la URL canónica
        href = re.sub(r'\?.*$', '', href)
        href = href.rstrip('/')
        return f"{FUENTE_URL}{href}"
    # Fallback: URL directa por ASIN
    return f"{FUENTE_URL}/dp/{asin}"


def _extraer_precio(bloque: str) -> float | None:
    m = _PRICE_RE.search(bloque)
    if not m:
        return None
    return _parsear_precio(m.group("price"))


def _extraer_imagen(bloque: str) -> str:
    # Intentar src directo
    m = _IMG_RE.search(bloque)
    if m:
        return m.group("src")
    # Intentar data-src (lazy loading)
    m = _IMG_DATA_RE.search(bloque)
    if m:
        return m.group("src")
    return ""


def _normalizar_item(item: dict) -> dict | None:
    nombre = item.get("titulo", "").strip()
    asin = item.get("asin", "").strip()
    if not nombre or not asin:
        return None

    precio = item.get("precio")
    url = item.get("url") or f"{FUENTE_URL}/dp/{asin}"
    imagen = item.get("imagen", "")
    politica_envio = obtener_politica_envio(FUENTE_ID)

    oferta = {
        "fuente": FUENTE_ID,
        "nombre_fuente": FUENTE_NOMBRE,
        "precio": precio,
        "precio_anterior": None,
        "url": url,
        "stock": True,
        "sku": asin,
        "envio": politica_envio,
    }

    return {
        "object_id": asin,
        "nombre": nombre,
        "marca": "",
        "precio": precio,
        "url": url,
        "url_venta": url,
        "imagen_url": imagen,
        "fuente": FUENTE_ID,
        "nombre_fuente": FUENTE_NOMBRE,
        "envio": politica_envio,
        "descripcion": "",
        "descripcion_html": "",
        "sku": asin,
        "stock": True,
        "categorias": [],
        "categoria_principal": "",
        "ofertas": [oferta],
    }


def _parsear_precio(precio_raw: str) -> float | None:
    """Convierte '7,49\u00a0€' o '7.49 EUR' → 7.49."""
    texto = re.sub(r"[^\d,.]", "", (precio_raw or "").replace("\xa0", "").replace(" ", ""))
    if not texto:
        return None
    # Formato europeo: punto=miles, coma=decimales
    if "," in texto and "." in texto:
        texto = texto.replace(".", "").replace(",", ".")
    elif "," in texto:
        texto = texto.replace(",", ".")
    try:
        val = float(texto)
        return round(val, 2) if val > 0 else None
    except (ValueError, TypeError):
        return None


def _normalizar_consulta_externa(consulta: str) -> str:
    valor = unicodedata.normalize("NFKD", consulta or "")
    valor = "".join(c for c in valor if not unicodedata.combining(c))
    valor = re.sub(r"[^a-zA-Z0-9\s\-]", " ", valor)
    return re.sub(r"\s+", " ", valor).strip()


def _variantes_consulta(consulta: str) -> list[str]:
    base = _normalizar_consulta_externa(consulta)
    if not base:
        return []
    variantes = [base]
    # Si tiene más de 2 palabras, probar solo las 2 primeras
    partes = base.split()
    if len(partes) > 2:
        variantes.append(" ".join(partes[:2]))
    return list(dict.fromkeys(v for v in variantes if v))


_STOPWORDS = {
    "de", "del", "la", "el", "los", "las", "un", "una", "unos", "unas",
    "con", "sin", "para", "por", "en", "y", "e", "o", "a", "al",
    "ml", "mg", "gr", "g", "l", "kg", "x", "pack",
}


def _normalizar_texto(texto: str) -> str:
    """Pasa a minúsculas y elimina acentos."""
    valor = unicodedata.normalize("NFKD", texto.lower())
    return "".join(c for c in valor if not unicodedata.combining(c))


def _es_relevante(titulo: str, consulta: str) -> bool:
    """Devuelve True si el título del producto contiene al menos la mitad
    de las palabras significativas de la consulta original."""
    titulo_norm = _normalizar_texto(titulo)
    palabras = [
        p for p in _normalizar_texto(consulta).split()
        if len(p) > 2 and p not in _STOPWORDS
    ]
    if not palabras:
        return True  # Consulta sin palabras significativas: no filtrar
    coincidencias = sum(1 for p in palabras if p in titulo_norm)
    return coincidencias >= max(1, len(palabras) // 2)
