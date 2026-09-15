from __future__ import annotations

import json

import pytest
import yaml

from conftest import ROUTING_YAML, make_message
from programador import server as srv


@pytest.fixture
def configured_server(tmp_path, monkeypatch):
    config_path = tmp_path / "entities.yaml"
    config_path.write_text(yaml.safe_dump(ROUTING_YAML), encoding="utf-8")
    monkeypatch.setenv("PROGRAMADOR_EMAIL", "lester@icloud.com")
    monkeypatch.setenv("PROGRAMADOR_APP_PASSWORD", "aaaa-bbbb-cccc-dddd")
    monkeypatch.setenv("PROGRAMADOR_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("PROGRAMADOR_CONFIG", str(config_path))
    monkeypatch.setattr(srv, "_ctx", srv._Context())
    assert srv._ctx.load(), srv._ctx.error
    return srv


def test_tools_report_config_errors_instead_of_crashing(monkeypatch):
    monkeypatch.delenv("PROGRAMADOR_EMAIL", raising=False)
    monkeypatch.delenv("PROGRAMADOR_APP_PASSWORD", raising=False)
    monkeypatch.setattr(srv, "_ctx", srv._Context())
    result = srv.estado()
    assert result["ok"] is False
    assert "PROGRAMADOR_EMAIL" in result["error"]
    assert "remedio" in result


def test_enviar_without_confirmation_only_previews(configured_server, monkeypatch):
    def explode(*_args, **_kwargs):  # pragma: no cover - debe no llamarse nunca
        raise AssertionError("send_message no debe invocarse sin confirmar=True")

    monkeypatch.setattr(srv, "send_message", explode)
    result = srv.enviar(para=["cliente@example.com"], asunto="Oferta", cuerpo="Texto")

    assert result["ok"] is True
    assert result["enviado"] is False
    assert result["previsualizacion"]["para"] == ["cliente@example.com"]


def test_enviar_with_confirmation_calls_smtp(configured_server, monkeypatch):
    llamadas = []

    def fake_send(settings, message, **_kwargs):
        llamadas.append(message["Subject"])
        return {"sent": True, "from": message["From"], "to": message["To"],
                "subject": message["Subject"], "refused_recipients": []}

    monkeypatch.setattr(srv, "send_message", fake_send)
    result = srv.enviar(
        para=["cliente@example.com"], asunto="Oferta", cuerpo="Texto", confirmar=True
    )
    assert result["enviado"] is True
    assert llamadas == ["Oferta"]


def test_estado_does_not_expose_the_app_password(configured_server):
    payload = json.dumps(srv.estado(), default=str)
    assert "aaaa-bbbb-cccc-dddd" not in payload
    assert "<set>" in payload


def test_read_tools_report_missing_message_cleanly(configured_server):
    for result in (
        srv.leer("clave-inexistente"),
        srv.registrar_extraccion("clave-inexistente", "invoice", {"total": 1}),
    ):
        assert result["ok"] is False
        assert "clave-inexistente" in result["error"]


def test_extraction_roundtrip_through_tools(configured_server):
    key = srv._ctx.store.upsert_message(
        srv._ctx.router.apply(make_message(to_addrs=["rambaid@lester.es"], subject="Factura 9"))
    )
    saved = srv.registrar_extraccion(key, "invoice", {"total_eur": 12500, "incoterm": "FOB"})
    assert saved["ok"] and saved["entidad"] == "rambaid"

    listed = srv.listar_extracciones(entidad="rambaid")
    assert listed["extracciones"][0]["fields"]["incoterm"] == "FOB"

    assert srv.marcar_extraccion(saved["id"], "confirmed")["ok"]
    assert srv.listar_extracciones(estado_registro="confirmed")["total"] == 1


def test_triaje_groups_by_entity(configured_server):
    store, router = srv._ctx.store, srv._ctx.router
    store.upsert_message(router.apply(make_message(uid=1, to_addrs=["rambaid@lester.es"])))
    store.upsert_message(router.apply(make_message(uid=2, subject="FOREGO INTL pedido")))
    result = srv.triaje(dias=3650)
    assert set(result["por_entidad"]) == {"rambaid", "forego_intl"}
    assert result["total"] == 2


def test_mover_removes_the_stale_row_from_the_index(configured_server, monkeypatch, tmp_path):
    """Tras mover, el UID de origen ya no existe: dejar la fila permitiria actuar
    sobre un mensaje que no esta donde el indice dice."""
    import programador.server as module

    key = srv._ctx.store.upsert_message(make_message())
    srv._ctx.store.save_extraction(
        __import__("programador.models", fromlist=["Extraction"]).Extraction(
            message_key=key, doc_type="invoice", entity="personal", fields={"total": 10}
        )
    )

    class FakeClient:
        def __init__(self, *_a, **_k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def select(self, *_a, **_k):
            return {"uidvalidity": 100}

        def move_message(self, _uid, _dest):
            return "MOVE"

    monkeypatch.setattr(module, "MailboxClient", FakeClient)
    result = srv.mover(key, "Archive")

    assert result["ok"] and result["retirado_del_indice"] is True
    assert srv._ctx.store.get_message(key) is None
    assert srv._ctx.store.search("prueba") == [], "El indice FTS tambien debe limpiarse"
    assert result["destino_sincronizado"] is False
    assert any("PROGRAMADOR_FOLDERS" in aviso for aviso in result["avisos"])
    assert any("extraccion" in aviso for aviso in result["avisos"])
    assert srv._ctx.store.list_extractions()[0]["fields"] == {"total": 10}
