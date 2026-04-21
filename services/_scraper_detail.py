"""Helper compartido para obtener la ficha de producto de tiendas Magento 2 y PrestaShop.

Parsea JSON-LD (schema.org/Product) y, como fallback, Open Graph meta-tags.
Las tiendas Magento 2 exponen /catalog/product/view/id/{pid} que redirige al slug.
Las tiendas PrestaShop exponen la URL directa o /index.php?id_product={pid}&controller=product.
"""

from __future__ import annotations

import json
import logging
import re
from html import unescape

from services.envios import obtener_politica_envio

_log = logging.getLogger(__name__)

_JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_OG_TITLE_RE = re.compile(
    r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']*)["\']',
    re.IGNORECASE,
)
_OG_IMAGE_RE = re.compile(
    r'<meta[^>]+property=["\']og:image(?::secure_url)?["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_OG_DESC_RE = re.compile(
    r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']*)["\']',
    re.IGNORECASE,
)
_PRICE_DATA_RE = re.compile(r'data-price-amount="(?P<price>[^"]+)"')
_PRICE_META_RE = re.compile(r'<meta[^>]+itemprop=["\']price["\'][^>]+content=["\']([^"\']+)["\']', re.IGNORECASE)
_STRIP_TAGS_RE = re.compile(r"<[^>]+>")


def fetch_detalle_producto(
    client,
    esperar_cortesia_fn,
    url: str,
    object_id: str,
    fuente_id: str,
    fuente_nombre: str,
    url_alternativa: str | None = None,
) -> dict | None:
    """Descarga y parsea la ficha de un producto desde la tienda externa.

    Intenta primero `url`. Si falla o no devuelve un nombre de producto,
    intenta `url_alternativa` si se proporciona.
    Devuelve None si no se puede extraer al menos el nombre del producto.
    """
    for target_url in filter(None, [url, url_alternativa]):
        try:
            esperar_cortesia_fn()
            resp = client.get(target_url)
            resp.raise_for_status()
        except Exception as exc:
            _log.debug("fetch_detalle_producto %s: %s", target_url, exc)
            continue

        producto = _parsear_html(resp.text, resp.url if hasattr(resp, "url") else target_url, object_id, fuente_id, fuente_nombre)
        if producto:
            return producto

    return None


def _parsear_html(
    html: str,
    url: object,
    object_id: str,
    fuente_id: str,
    fuente_nombre: str,
) -> dict | None:
    url_str = str(url)
    nombre = ""
    imagen_url = ""
    descripcion = ""
    precio: float | None = None

    # --- JSON-LD (más fiable) ---
    for m in _JSONLD_RE.finditer(html):
        try:
            data = json.loads(m.group(1))
        except (json.JSONDecodeError, ValueError):
            continue

        # Puede ser un array con distintos tipos
        if isinstance(data, list):
            data = next(
                (d for d in data if isinstance(d, dict) and d.get("@type") == "Product"),
                None,
            )
        if not (isinstance(data, dict) and data.get("@type") == "Product"):
            continue

        nombre = data.get("name", "").strip()

        img = data.get("image")
        if isinstance(img, str):
            imagen_url = img
        elif isinstance(img, dict):
            imagen_url = img.get("url", "")
        elif isinstance(img, list) and img:
            first = img[0]
            imagen_url = first if isinstance(first, str) else (first.get("url", "") if isinstance(first, dict) else "")

        desc_raw = data.get("description", "")
        descripcion = _STRIP_TAGS_RE.sub(" ", desc_raw).strip()

        offers = data.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        precio_raw = offers.get("price") if isinstance(offers, dict) else None
        if precio_raw is not None:
            try:
                precio = round(float(str(precio_raw).replace(",", ".")), 2)
            except (ValueError, TypeError):
                pass
        break  # primer bloque Product encontrado

    # --- Fallback Open Graph ---
    if not nombre:
        m = _OG_TITLE_RE.search(html)
        if m:
            nombre = unescape(m.group(1)).strip()
    if not imagen_url:
        m = _OG_IMAGE_RE.search(html)
        if m:
            imagen_url = unescape(m.group(1)).strip()
    if not descripcion:
        m = _OG_DESC_RE.search(html)
        if m:
            descripcion = unescape(m.group(1)).strip()

    # --- Precio desde microdata/data-price-amount ---
    if precio is None:
        pm = _PRICE_META_RE.search(html)
        if pm:
            try:
                precio = round(float(pm.group(1).replace(",", ".")), 2)
            except (ValueError, TypeError):
                pass
    if precio is None:
        pm = _PRICE_DATA_RE.search(html)
        if pm:
            try:
                precio = round(float(pm.group("price")), 2)
            except (ValueError, TypeError):
                pass

    if not nombre:
        return None

    politica_envio = obtener_politica_envio(fuente_id)
    oferta = {
        "fuente": fuente_id,
        "nombre_fuente": fuente_nombre,
        "precio": precio,
        "precio_anterior": None,
        "url": url_str,
        "stock": True,
        "sku": "",
        "envio": politica_envio,
    }
    return {
        "object_id": object_id,
        "nombre": nombre,
        "marca": "",
        "precio": precio,
        "url": url_str,
        "url_venta": url_str,
        "imagen_url": imagen_url,
        "fuente": fuente_id,
        "nombre_fuente": fuente_nombre,
        "envio": politica_envio,
        "descripcion": descripcion,
        "descripcion_html": "",
        "sku": "",
        "stock": True,
        "categorias": [],
        "categoria_principal": "",
        "ofertas": [oferta],
    }
