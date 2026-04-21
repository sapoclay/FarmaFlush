from __future__ import annotations

import re
import unicodedata

TOKENS_IRRELEVANTES = {
    "mg",
    "g",
    "gr",
    "ml",
    "mcg",
    "ui",
    "ug",
    "crema",
    "gel",
    "capsulas",
    "capsula",
    "comprimidos",
    "comprimido",
    "sobres",
    "suspension",
    "oral",
    "solucion",
    "efg",
    "topico",
    "pomada",
    "recubiertos",
    "recubierto",
    "blandas",
    "blanda",
    "producto",
    "medicamento",
}


def normalizar_texto(valor: str) -> str:
    valor = unicodedata.normalize("NFKD", valor or "")
    valor = "".join(char for char in valor if not unicodedata.combining(char))
    valor = valor.lower()
    valor = re.sub(r"[^a-z0-9]+", " ", valor)
    return re.sub(r"\s+", " ", valor).strip()


def tokens_significativos(tokens: set[str]) -> set[str]:
    significativos = {token for token in tokens if len(token) > 2 and token not in TOKENS_IRRELEVANTES and not token.isdigit()}
    return significativos or {token for token in tokens if len(token) > 2 and not token.isdigit()}


def tokens_consulta(consulta: str) -> set[str]:
    return tokens_significativos(set(normalizar_texto(consulta).split()))


def cubre_consulta(texto: str, consulta: str) -> bool:
    texto_norm = normalizar_texto(texto)
    consulta_norm = normalizar_texto(consulta)
    if not texto_norm or not consulta_norm:
        return False
    if consulta_norm in texto_norm:
        return True

    tokens_ref = tokens_consulta(consulta_norm)
    if not tokens_ref:
        return False

    tokens_texto = tokens_significativos(set(texto_norm.split()))
    if tokens_ref.issubset(tokens_texto):
        return True

    return all(token in texto_norm for token in tokens_ref)


def puntuar_coincidencia(texto: str, consulta: str) -> int:
    texto_norm = normalizar_texto(texto)
    consulta_norm = normalizar_texto(consulta)
    if not texto_norm or not consulta_norm:
        return 0

    tokens_ref = tokens_consulta(consulta_norm)
    if tokens_ref and not cubre_consulta(texto_norm, consulta_norm):
        return 0

    if texto_norm == consulta_norm:
        return 1000
    if consulta_norm in texto_norm:
        return 800 + len(consulta_norm)
    if texto_norm in consulta_norm:
        return 650 + len(texto_norm)

    tokens_texto = tokens_significativos(set(texto_norm.split()))
    comunes = tokens_texto & tokens_ref
    if not comunes:
        return 0

    score = len(comunes) * 28 + sum(len(token) for token in comunes)
    if tokens_ref and len(comunes) / len(tokens_ref) >= 0.6:
        score += 35
    return score