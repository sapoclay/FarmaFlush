"""Motor de precios: obtiene, agrega y cachea precios de distintas fuentes."""

from database import db_session
from services.nomenclator import construir_url_detalle


def _obtener_presentaciones(conn, nregistro: str):
    return conn.execute(
        "SELECT cn, nombre FROM presentacion WHERE nregistro = ?",
        (nregistro,),
    ).fetchall()


def _normalizar_url_fuente(fuente: str, cn: str, url: str | None) -> str | None:
    if fuente == "nomenclator" and cn:
        return construir_url_detalle(cn)
    return url


def obtener_precios(nregistro: str) -> dict:
    """Devuelve la info de precios para un medicamento.

    Busca los CN asociados a un nregistro en la tabla presentacion
    y luego cruza con la tabla precio (indexada por CN).

    Retorna:
        {
            "precio_oficial": float | None,   # PVP del Nomenclátor si existe
            "precio_medio":   float | None,   # Media de todas las fuentes
            "fuentes": [
                {"fuente": str, "pvp_iva": float, "precio_ref": float,
                 "url": str, "cn": str, "presentacion": str},
                ...
            ]
        }
    """
    with db_session() as conn:
        # Obtener los CN de este medicamento
        cn_rows = _obtener_presentaciones(conn, nregistro)

    if not cn_rows:
        from services import cima

        cima.guardar_presentaciones(nregistro)
        with db_session() as conn:
            cn_rows = _obtener_presentaciones(conn, nregistro)

    if not cn_rows:
        return {"precio_oficial": None, "precio_medio": None, "fuentes": []}

    cn_list = [r["cn"] for r in cn_rows]
    cn_nombres = {r["cn"]: r["nombre"] for r in cn_rows}

    placeholders = ",".join("?" * len(cn_list))
    with db_session() as conn:
        rows = conn.execute(
            f"""SELECT cn, fuente, pvp, pvp_iva, precio_ref, url_fuente
                FROM precio WHERE cn IN ({placeholders})
                ORDER BY fuente""",
            cn_list,
        ).fetchall()

    fuentes = []
    precio_oficial = None
    precios_para_media = []

    for r in rows:
        entry = {
            "fuente": r["fuente"],
            "pvp": r["pvp"],
            "pvp_iva": r["pvp_iva"],
            "precio_ref": r["precio_ref"],
            "url": _normalizar_url_fuente(r["fuente"], r["cn"], r["url_fuente"]),
            "cn": r["cn"],
            "presentacion": cn_nombres.get(r["cn"], ""),
        }
        fuentes.append(entry)

        if r["fuente"] == "nomenclator" and r["pvp_iva"] is not None:
            # Si hay varias presentaciones, tomamos la menor como oficial
            if precio_oficial is None or r["pvp_iva"] < precio_oficial:
                precio_oficial = r["pvp_iva"]

        val = r["pvp_iva"] if r["pvp_iva"] is not None else r["pvp"]
        if val is not None:
            precios_para_media.append(val)

    precio_medio = None
    if precios_para_media:
        precio_medio = round(sum(precios_para_media) / len(precios_para_media), 2)

    # Fallback: si no hay precio en ninguna fuente local, intentar Vademecum
    if precio_oficial is None and precio_medio is None and cn_nombres:
        try:
            from services import vademecum as _vademecum
            # Usar el nombre de la primera presentación conocida
            nombre_presentacion = next(iter(cn_nombres.values()), "")
            comp = _vademecum.obtener_precio(nombre_presentacion)
            if comp:
                pvpiva = comp.get("pvpiva")
                pvl = comp.get("pvl")
                if pvpiva is not None:
                    # precio_oficial es exclusivo del Nomenclátor SNS (precio intervenido).
                    # Vademécum solo sirve como referencia de precio medio.
                    precio_medio = pvpiva
                    fuentes.append({
                        "fuente": "vademecum",
                        "pvp": pvl,
                        "pvp_iva": pvpiva,
                        "precio_ref": None,
                        "url": comp.get("url"),
                        "cn": cn_list[0] if cn_list else "",
                        "presentacion": nombre_presentacion,
                    })
        except Exception:
            pass

    return {
        "precio_oficial": precio_oficial,
        "precio_medio": precio_medio,
        "fuentes": fuentes,
    }
