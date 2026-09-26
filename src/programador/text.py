"""Normalizacion de texto compartida por el enrutado y las pistas documentales."""

from __future__ import annotations

import unicodedata

__all__ = ["normalize", "contains"]


def normalize(value: str | None) -> str:
    """Minusculas, sin acentos y con espacios colapsados.

    El correo de Lester mezcla espanol e ingles y viene de proveedores que
    escriben "FACTURA", "Factura" y "factura" indistintamente, asi que todas
    las comparaciones del motor pasan por aqui.
    """
    if not value:
        return ""
    decomposed = unicodedata.normalize("NFKD", value)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(stripped.lower().split())


def contains(haystack: str | None, needle: str) -> bool:
    """True si `needle` aparece en `haystack`, ambos normalizados."""
    needle_n = normalize(needle)
    if not needle_n:
        return False
    return needle_n in normalize(haystack)
