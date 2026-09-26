from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from programador.config import parse_routing_config  # noqa: E402
from programador.models import Attachment, Message  # noqa: E402
from programador.routing import Router  # noqa: E402
from programador.store import Store  # noqa: E402

ROUTING_YAML = {
    "default_entity": "personal",
    "entities": [
        {"id": "rambaid", "name": "Rambaid Iberica SL", "jurisdiction": "ES"},
        {"id": "forego_intl", "name": "FOREGO INTERNATIONAL LTD", "jurisdiction": "VG"},
        {"id": "sand", "name": "S@ND SRL", "jurisdiction": "CU"},
        {"id": "personal", "name": "Personal"},
    ],
    "routing_rules": [
        {"entity": "rambaid", "priority": 100, "match": {"to_addr": ["rambaid@lester.es"]}},
        {
            "entity": "rambaid",
            "priority": 80,
            "match": {"from_domain": ["agenciatributaria.gob.es"]},
        },
        {"entity": "forego_intl", "priority": 60, "match": {"subject_contains": ["forego intl"]}},
        {"entity": "sand", "priority": 60, "match": {"body_contains": ["s@nd srl"]}},
    ],
    "doc_type_hints": {
        "invoice": ["factura", "invoice"],
        "bill_of_lading": ["bill of lading", "b/l"],
        "customs": ["aduana"],
    },
}


@pytest.fixture
def routing_config():
    return parse_routing_config(ROUTING_YAML)


@pytest.fixture
def router(routing_config):
    return Router(routing_config)


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "test.db") as store:
        yield store


def make_message(**overrides) -> Message:
    base = dict(
        account="lester@icloud.com",
        folder="INBOX",
        uid=1,
        uidvalidity=100,
        from_addr="proveedor@example.com",
        from_name="Proveedor",
        to_addrs=["lester@icloud.com"],
        cc_addrs=[],
        subject="Asunto de prueba",
        date_utc=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
        body_text="Cuerpo de prueba",
        snippet="Cuerpo de prueba",
        attachments=[],
        flags=[],
    )
    base.update(overrides)
    return Message(**base)


__all__ = ["make_message", "Attachment", "ROUTING_YAML"]
