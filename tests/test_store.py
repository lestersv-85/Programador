from __future__ import annotations

from datetime import datetime, timezone

import pytest

from conftest import make_message
from programador.models import Attachment, Extraction
from programador.store import build_fts_query


def test_upsert_and_get_roundtrip(store):
    message = make_message(
        subject="Factura 2026-114",
        attachments=[Attachment(filename="factura.pdf", content_type="application/pdf", size=2048)],
        entity="rambaid",
        doc_types=["invoice"],
    )
    key = store.upsert_message(message)
    stored = store.get_message(key)
    assert stored["subject"] == "Factura 2026-114"
    assert stored["entity"] == "rambaid"
    assert stored["doc_types"] == ["invoice"]
    assert stored["attachments"][0]["filename"] == "factura.pdf"
    assert stored["has_attachments" if "has_attachments" in stored else "attachments"]


def test_upsert_is_idempotent_and_updates_mutable_fields(store):
    message = make_message(entity="personal", flags=[])
    store.upsert_message(message)
    message.entity = "sand"
    message.flags = ["\\Seen"]
    store.upsert_message(message)

    assert len(store.list_messages(limit=100)) == 1
    stored = store.get_message(message.key)
    assert stored["entity"] == "sand"
    assert stored["flags"] == ["\\Seen"]


def test_fts_search_matches_body_subject_and_attachment_name(store):
    store.upsert_message(make_message(uid=1, subject="Conocimiento de embarque MSCU7788"))
    store.upsert_message(make_message(uid=2, body_text="El contenedor sale de Valencia el lunes"))
    store.upsert_message(
        make_message(
            uid=3,
            attachments=[Attachment(filename="packing-list-ago.pdf", content_type="application/pdf", size=1)],
        )
    )
    assert len(store.search("MSCU7788")) == 1
    assert len(store.search("contenedor Valencia")) == 1
    assert len(store.search("packing")) == 1
    assert store.search("inexistente") == []


def test_fts_search_is_accent_insensitive(store):
    store.upsert_message(make_message(subject="Facturación de septiembre"))
    assert len(store.search("facturacion")) == 1


@pytest.mark.parametrize(
    "hostile",
    ['"', 'factura OR "', "NEAR(a b", "*", "a AND (b", 'b/l "MSCU', "-- drop"],
)
def test_search_never_raises_on_hostile_input(store, hostile):
    """Los operadores de FTS5 en texto libre no pueden reventar la busqueda."""
    store.upsert_message(make_message(subject="Factura"))
    store.search(hostile)  # no debe lanzar


def test_build_fts_query_quotes_tokens():
    assert build_fts_query('factura "MSCU 12"') == '"factura" AND "MSCU" AND "12"'
    assert build_fts_query("   ") == ""


def test_doc_type_filter_uses_exact_array_membership(store):
    store.upsert_message(make_message(uid=1, doc_types=["invoice"]))
    store.upsert_message(make_message(uid=2, doc_types=["invoice_correction"]))
    # Un LIKE sobre el JSON serializado devolveria los dos: aqui debe devolver uno.
    assert len(store.list_messages(doc_type="invoice")) == 1


def test_list_filters_combine(store):
    store.upsert_message(
        make_message(uid=1, entity="rambaid", doc_types=["invoice"], flags=["\\Seen"])
    )
    store.upsert_message(make_message(uid=2, entity="rambaid", doc_types=["customs"]))
    store.upsert_message(make_message(uid=3, entity="sand", doc_types=["invoice"]))

    assert len(store.list_messages(entity="rambaid")) == 2
    assert len(store.list_messages(entity="rambaid", doc_type="invoice")) == 1
    assert len(store.list_messages(unread_only=True)) == 2
    assert len(store.list_messages(since="2026-09-05T00:00:00+00:00")) == 0


def test_extraction_upsert_by_message_and_doc_type(store):
    key = store.upsert_message(make_message(entity="rambaid"))
    first = store.save_extraction(
        Extraction(message_key=key, doc_type="invoice", entity="rambaid", fields={"total": 1200})
    )
    second = store.save_extraction(
        Extraction(
            message_key=key, doc_type="invoice", entity="rambaid", fields={"total": 1350, "iva": 283.5}
        )
    )
    assert first == second, "Reextraer el mismo documento debe actualizar, no duplicar"
    rows = store.list_extractions(entity="rambaid")
    assert len(rows) == 1
    assert rows[0]["fields"] == {"total": 1350, "iva": 283.5}


def test_extraction_status_validation(store):
    key = store.upsert_message(make_message())
    with pytest.raises(ValueError):
        store.save_extraction(
            Extraction(message_key=key, doc_type="invoice", entity="personal", fields={}, status="raro")
        )
    eid = store.save_extraction(
        Extraction(message_key=key, doc_type="invoice", entity="personal", fields={})
    )
    assert store.set_extraction_status(eid, "confirmed")
    assert not store.set_extraction_status(9999, "confirmed")


def test_uidvalidity_purge_removes_messages_and_index(store):
    store.upsert_message(make_message(uid=1, uidvalidity=100, subject="viejo"))
    store.upsert_message(make_message(uid=2, uidvalidity=100, subject="viejo tambien"))
    removed = store.delete_folder_messages("lester@icloud.com", "INBOX", 100)
    assert removed == 2
    assert store.list_messages(limit=10) == []
    assert store.search("viejo") == [], "El indice FTS debe purgarse con los mensajes"


def test_stats_group_by_entity_and_doc_type(store):
    store.upsert_message(make_message(uid=1, entity="rambaid", doc_types=["invoice", "customs"]))
    store.upsert_message(make_message(uid=2, entity="rambaid", doc_types=["invoice"]))
    store.upsert_message(make_message(uid=3, entity="personal"))
    stats = store.stats()
    assert stats["total_messages"] == 3
    assert stats["by_entity"] == {"rambaid": 2, "personal": 1}
    assert stats["by_doc_type"]["invoice"] == 2


def test_sync_state_roundtrip(store):
    from programador.models import SyncState

    assert store.get_sync_state("lester@icloud.com", "INBOX") is None
    store.set_sync_state(SyncState("lester@icloud.com", "INBOX", uidvalidity=7, last_uid=42))
    state = store.get_sync_state("lester@icloud.com", "INBOX")
    assert (state.uidvalidity, state.last_uid) == (7, 42)
    store.set_sync_state(SyncState("lester@icloud.com", "INBOX", uidvalidity=7, last_uid=99))
    assert store.get_sync_state("lester@icloud.com", "INBOX").last_uid == 99


def test_known_uids_scoped_to_uidvalidity(store):
    store.upsert_message(make_message(uid=5, uidvalidity=100))
    store.upsert_message(make_message(uid=6, uidvalidity=200))
    assert store.known_uids("lester@icloud.com", "INBOX", 100) == {5}
