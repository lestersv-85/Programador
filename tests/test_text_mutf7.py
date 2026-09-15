from __future__ import annotations

import pytest

from programador import mutf7
from programador.text import contains, normalize


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("  FACTURA   Comercial ", "factura comercial"),
        ("Aduana Á É Í Ó Ú ñ", "aduana a e i o u n"),
        (None, ""),
        ("", ""),
    ],
)
def test_normalize(raw, expected):
    assert normalize(raw) == expected


def test_contains_ignores_accents_and_case():
    assert contains("Certificado de Origen", "certificado de origen")
    assert contains("FACTURACIÓN ANUAL", "facturacion")
    assert not contains("Packing list", "factura")
    assert not contains("cualquier cosa", "")


@pytest.mark.parametrize(
    "name",
    ["INBOX", "Facturación", "Aduanas & Despachos", "Año 2026", "Sent Messages", "日本"],
)
def test_mutf7_roundtrip(name):
    assert mutf7.decode(mutf7.encode(name)) == name


def test_mutf7_known_encoding():
    # Ejemplo canonico del RFC 3501.
    assert mutf7.encode("Facturación") == "Facturaci&APM-n"
    assert mutf7.decode("Facturaci&APM-n") == "Facturación"
    assert mutf7.encode("A&B") == "A&-B"


def test_mutf7_decode_survives_truncated_sequence():
    # Un servidor mal implementado no debe tumbar el listado de carpetas.
    assert mutf7.decode("Roto&APM") == "Roto&APM"
