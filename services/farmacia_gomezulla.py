"""Proveedor Gomezulla en tu Piel para productos de parafarmacia y cosmética (PrestaShop)."""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from functools import lru_cache
from html import unescape

import httpx
from bs4 import BeautifulSoup
from services.envios import obtener_politica_envio

logger = logging.getLogger(__name__)

FUENTE_ID = "gomezulla"
FUENTE_NOMBRE = "Gomezulla"
FUENTE_URL = "https://www.gomezullaentupiel.com"

_TIMEOUT = httpx.Timeout(6.0, connect=5.0)
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.7",
}
_CLIENT = httpx.Client(timeout=_TIMEOUT, follow_redirects=True, headers=_HEADERS)

# Circuit breaker: pausa la fuente 60s tras un timeout
_CB_COOLDOWN = 60.0
_cb_fallo_at: float = 0.0
_last_request_at: float = 0.0
_MIN_DELAY = 0.5  # segundos mínimos entre peticiones

# Caché ligera: almacena productos por object_id para que obtener_producto
# pueda recuperar datos sin necesidad de una API de detalle.
_cache_productos: dict[str, dict] = {}


def _esperar_cortesia() -> None:
    """Pausa mínima entre peticiones HTTP al mismo servidor."""
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if elapsed < _MIN_DELAY:
        time.sleep(_MIN_DELAY - elapsed)
    _last_request_at = time.monotonic()


def buscar_productos(consulta: str, limit: int = 12) -> list[dict]:
    productos: list[dict] = []
    vistos: set[str] = set()

    for query in _variantes_consulta(consulta):
        for item in _buscar_pagina(query, limit=max(limit, 12)):
            producto = _normalizar_item(item)
            if not producto:
                continue
            oid = producto.get("object_id")
            if not oid or oid in vistos:
                continue
            vistos.add(oid)
            _cache_productos[oid] = producto  # guardar para obtener_producto
            productos.append(producto)
            if len(productos) >= limit:
                return productos

    return productos


def obtener_producto(object_id: str) -> dict | None:
    if not object_id:
        return None
    cached = _cache_productos.get(object_id)
    if cached and (cached.get("descripcion") or cached.get("descripcion_html")):
        return cached
    from services import _scraper_detail
    # PrestaShop: la URL canónica usa slug desconocido; usar controlador directo
    url = f"{FUENTE_URL}/index.php?id_product={object_id}&controller=product"
    producto = _scraper_detail.fetch_detalle_producto(
        _CLIENT, _esperar_cortesia, url, object_id, FUENTE_ID, FUENTE_NOMBRE
    )
    if producto:
        _cache_productos[object_id] = producto
        return producto
    if cached:
        return cached
    politica_envio = obtener_politica_envio(FUENTE_ID)
    oferta = {"fuente": FUENTE_ID, "nombre_fuente": FUENTE_NOMBRE, "precio": None,
              "precio_anterior": None, "url": url, "stock": None, "sku": "", "envio": politica_envio}
    return {"object_id": object_id, "nombre": "", "marca": "", "precio": None,
            "url": url, "url_venta": url, "imagen_url": "", "fuente": FUENTE_ID,
            "nombre_fuente": FUENTE_NOMBRE, "envio": politica_envio, "descripcion": "",
            "descripcion_html": "", "sku": "", "stock": None, "categorias": [],
            "categoria_principal": "", "ofertas": [oferta]}


@lru_cache(maxsize=128)
def _buscar_pagina(consulta: str, limit: int = 24) -> tuple[dict, ...]:
    global _cb_fallo_at

    # Circuit breaker: skip si hubo un fallo reciente
    if time.monotonic() - _cb_fallo_at < _CB_COOLDOWN:
        return ()
    query = _normalizar_consulta_externa(consulta)
    if not query:
        return ()

    _esperar_cortesia()
    try:
        resp = _CLIENT.get(
            f"{FUENTE_URL}/buscar",
            params={"controller": "search", "s": query},
        )
        if resp.status_code == 429:
            espera = int(resp.headers.get("Retry-After", int(_CB_COOLDOWN)))
            _cb_fallo_at = time.monotonic() + espera - _CB_COOLDOWN
            logger.warning("Gomezulla 429 — pausando %ds", espera)
            return ()
        resp.raise_for_status()
    except httpx.TimeoutException:
        _cb_fallo_at = time.monotonic()
        logger.debug("Timeout — fuente pausada %ds", int(_CB_COOLDOWN))
        return ()
    except Exception:
        logger.debug("No se pudo consultar Gomezulla para %s", consulta, exc_info=True)
        return ()

    soup = BeautifulSoup(resp.text, "html.parser")
    minis = soup.find_all(class_="product-miniature")
    if not minis:
        return ()

    items: list[dict] = []
    for mini in minis[:limit]:
        pid = mini.get("data-id-product", "")
        a = mini.find("a", class_="product-thumbnail")
        href = a["href"] if a else ""

        name_el = mini.find("h3", class_=re.compile(r"product-title")) or mini.find(class_=re.compile(r"product-title|product-name"))
        if name_el:
            name_a = name_el.find("a") or name_el
            name = name_a.get_text(strip=True)
        else:
            name = ""

        # Precio: preferir itemprop="price" content, fallback texto
        price_el = mini.find(attrs={"itemprop": "price"})
        if price_el and price_el.get("content"):
            price_raw = price_el["content"]
        else:
            price_span = mini.find(class_=re.compile(r"^price$|current-price|product-price"))
            price_raw = price_span.get_text(strip=True) if price_span else ""

        img = mini.find("img")
        img_src = ""
        if img:
            img_src = img.get("data-full-size-image-url") or img.get("data-src") or img.get("src") or ""

        if not name or not href:
            continue

        items.append({
            "pid": str(pid),
            "name": unescape(name),
            "price_raw": price_raw,
            "url": href,
            "img": img_src,
        })

    return tuple(items)


def _normalizar_item(item: dict) -> dict | None:
    nombre = item.get("name", "").strip()
    url = item.get("url", "").strip()
    if not nombre or not url:
        return None

    precio = _parsear_precio(item.get("price_raw", ""))
    if precio is None:
        return None

    pid = item.get("pid") or url.rstrip("/").rsplit("/", 1)[-1]
    imagen = item.get("img", "")
    politica_envio = obtener_politica_envio(FUENTE_ID)

    oferta = {
        "fuente": FUENTE_ID,
        "nombre_fuente": FUENTE_NOMBRE,
        "precio": precio,
        "precio_anterior": None,
        "url": url,
        "stock": True,
        "sku": "",
        "envio": politica_envio,
    }

    return {
        "object_id": pid,
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
        "sku": "",
        "stock": True,
        "categorias": [],
        "categoria_principal": "",
        "ofertas": [oferta],
    }


def _parsear_precio(precio_raw: str) -> float | None:
    """Convierte '7,95 €' o '7.95' → 7.95."""
    texto = re.sub(r"[^\d,.]", "", (precio_raw or "").replace("\xa0", " "))
    # Si hay coma como decimal (formato español)
    if "," in texto and "." in texto:
        texto = texto.replace(".", "").replace(",", ".")
    elif "," in texto:
        texto = texto.replace(",", ".")
    try:
        return round(float(texto), 2) if texto else None
    except (ValueError, TypeError):
        return None


# -- Helpers ----------------------------------------------------------------

def _variantes_consulta(consulta: str) -> list[str]:
    original = _normalizar_consulta_externa(consulta)
    if not original:
        return []
    variantes = [original]
    tokens = original.split()
    if len(tokens) > 2:
        variantes.append(" ".join(tokens[:2]))
    return list(dict.fromkeys(variantes))


def _normalizar_consulta_externa(consulta: str) -> str:
    texto = unescape((consulta or "").strip())
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    texto = re.sub(r"[^a-zA-Z0-9\s]", " ", texto)
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto[:120]
