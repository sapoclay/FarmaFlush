from __future__ import annotations

POLITICAS_ENVIO = {
    "dosfarma": {
        "tiene_gastos_envio": True,
        "coste_envio": None,
        "coste_envio_texto": "Coste no confirmado públicamente",
        "envio_gratis_desde": 49.0,
        "plazo_entrega": "24-48 h",
        "texto_resumen": "Envío gratis desde 49€",
    },
    "tedin": {
        "tiene_gastos_envio": True,
        "coste_envio": None,
        "coste_envio_texto": "Coste no confirmado públicamente",
        "envio_gratis_desde": 29.0,
        "plazo_entrega": "24-48 h",
        "texto_resumen": "Envío gratis desde 29€",
    },
    "farmaciasdirect": {
        "tiene_gastos_envio": True,
        "coste_envio": 3.99,
        "coste_envio_texto": "3,99 € en Península · 7,99 € en Baleares",
        "envio_gratis_desde": 75.0,
        "plazo_entrega": "24-48 h",
        "texto_resumen": "Envío gratis desde 75€",
    },
    "castrofarma": {
        "tiene_gastos_envio": True,
        "coste_envio": 4.99,
        "coste_envio_texto": "4,99 € en Península",
        "envio_gratis_desde": 50.0,
        "plazo_entrega": "24-48 h",
        "texto_resumen": "Envío gratis desde 50€",
    },
    "farmaciabarata": {
        "tiene_gastos_envio": True,
        "coste_envio": None,
        "coste_envio_texto": "Coste no confirmado públicamente",
        "envio_gratis_desde": 65.0,
        "plazo_entrega": "24-48 h",
        "texto_resumen": "Envío gratis desde 65€",
    },
    "farmagalicia": {
        "tiene_gastos_envio": False,
        "coste_envio": 0.0,
        "coste_envio_texto": "Envío gratis desde 30 €",
        "envio_gratis_desde": 30.0,
        "plazo_entrega": "24-72 h",
        "texto_resumen": "Envío gratis desde 30€",
    },
    "openfarma": {
        "tiene_gastos_envio": True,
        "coste_envio": None,
        "coste_envio_texto": "Coste no confirmado públicamente",
        "envio_gratis_desde": 95.0,
        "plazo_entrega": "24-48 h",
        "texto_resumen": "Envío gratis desde 95€",
    },
    "pontevea": {
        "tiene_gastos_envio": True,
        "coste_envio": None,
        "coste_envio_texto": "Coste no confirmado públicamente",
        "envio_gratis_desde": 50.0,
        "plazo_entrega": "24-48 h",
        "texto_resumen": "Envío gratis desde 50€",
    },
    "gomezulla": {
        "tiene_gastos_envio": True,
        "coste_envio": 7.42,
        "coste_envio_texto": "7,42 € en Península",
        "envio_gratis_desde": 28.0,
        "plazo_entrega": "24-48 h",
        "texto_resumen": "Envío gratis desde 28€",
    },
}


def obtener_politica_envio(fuente_id: str) -> dict:
    politica = POLITICAS_ENVIO.get(fuente_id, {})
    return {
        "tiene_gastos_envio": bool(politica.get("tiene_gastos_envio")),
        "coste_envio": politica.get("coste_envio"),
        "coste_envio_texto": politica.get("coste_envio_texto", ""),
        "envio_gratis_desde": politica.get("envio_gratis_desde"),
        "plazo_entrega": politica.get("plazo_entrega", ""),
        "texto_resumen": politica.get("texto_resumen", "Consultar condiciones de envío"),
    }


def describir_envio(fuente_id: str) -> str:
    politica = obtener_politica_envio(fuente_id)
    if politica.get("envio_gratis_desde") is not None:
        return politica["texto_resumen"]
    if politica.get("tiene_gastos_envio"):
        return "Con gastos de envío"
    return "Sin gastos de envío"