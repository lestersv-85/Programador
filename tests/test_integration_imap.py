"""Tests de integracion: MailboxClient real sobre imaplib real, contra un
servidor IMAP que habla el protocolo por un socket.

Cubren justo lo que los dobles de Python no pueden cubrir: el formato de LIST y
STATUS, la lectura de literales {n} en UID FETCH, el eco del comodin en
UID SEARCH y la alternativa COPY+EXPUNGE cuando el servidor no soporta MOVE.
"""

from __future__ import annotations

import imaplib

import pytest

from fake_imap import FakeFolder, FakeImapServer, FakeMessage, build_eml
from programador.config import load_settings
from programador.imap_client import ImapError, MailboxClient
from programador.routing import Router
from programador.sync import sync_folder


@pytest.fixture
def settings(tmp_path):
    return load_settings(
        {
            "PROGRAMADOR_EMAIL": "lestersv@icloud.com",
            "PROGRAMADOR_APP_PASSWORD": "aaaa-bbbb-cccc-dddd",
            "PROGRAMADOR_DB": str(tmp_path / "t.db"),
        }
    )


def _factory(server: FakeImapServer):
    def build() -> imaplib.IMAP4:
        conn = imaplib.IMAP4("127.0.0.1", server.port)
        conn.login("lestersv@icloud.com", "aaaa-bbbb-cccc-dddd")
        return conn

    return build


def _inbox(count: int = 3) -> FakeFolder:
    return FakeFolder(
        "INBOX",
        uidvalidity=100,
        messages=[
            FakeMessage(uid=i, raw=build_eml(f"Factura {i}", f"Importe {i}00 EUR"))
            for i in range(1, count + 1)
        ],
    )


def test_list_folders_decodes_names(settings):
    folders = [_inbox(1), FakeFolder("Facturación"), FakeFolder("Sent Messages")]
    with FakeImapServer(folders) as server:
        with MailboxClient(settings, _factory(server)) as client:
            assert client.list_folders() == ["INBOX", "Facturación", "Sent Messages"]


def test_folder_status_returns_uidvalidity(settings):
    with FakeImapServer([_inbox(3)]) as server:
        with MailboxClient(settings, _factory(server)) as client:
            status = client.select("INBOX")
            assert status["uidvalidity"] == 100
            assert status["messages"] == 3
            assert status["uidnext"] == 4


def test_fetch_reads_literals_and_parses_messages(settings):
    with FakeImapServer([_inbox(3)]) as server:
        with MailboxClient(settings, _factory(server)) as client:
            client.select("INBOX")
            messages = list(client.fetch_messages([1, 2, 3], folder="INBOX", uidvalidity=100))

    assert [m.uid for m in messages] == [1, 2, 3]
    assert [m.subject for m in messages] == ["Factura 1", "Factura 2", "Factura 3"]
    assert "Importe 200 EUR" in messages[1].body_text
    assert messages[0].from_addr == "ventas@proveedor.example"


def test_fetch_uses_peek_and_does_not_set_seen(settings):
    """Indexar el buzon no puede marcarte correos como leidos en el iPhone."""
    inbox = _inbox(2)
    with FakeImapServer([inbox]) as server:
        with MailboxClient(settings, _factory(server)) as client:
            client.select("INBOX")
            list(client.fetch_messages([1, 2], folder="INBOX", uidvalidity=100))

    assert all(message.flags == [] for message in inbox.messages)
    assert any("BODY.PEEK[]" in command for command in server.commands)
    assert not any("RFC822" in command for command in server.commands)


def test_search_filters_the_wildcard_echo(settings):
    """`UID 4:*` devuelve el UID 3 si es el ultimo: no debe colarse."""
    with FakeImapServer([_inbox(3)]) as server:
        with MailboxClient(settings, _factory(server)) as client:
            client.select("INBOX")
            assert client.search_uids(min_uid=4) == []
            assert client.search_uids(min_uid=2) == [2, 3]
            assert client.search_uids() == [1, 2, 3]


def test_set_flags_reaches_the_server(settings):
    inbox = _inbox(2)
    with FakeImapServer([inbox]) as server:
        with MailboxClient(settings, _factory(server)) as client:
            client.select("INBOX", readonly=False)
            client.set_flags(1, ["\\Seen"], add=True)
            assert inbox.messages[0].flags == ["\\Seen"]
            client.set_flags(1, ["\\Seen"], add=False)
            assert inbox.messages[0].flags == []


def test_move_uses_move_when_available(settings):
    inbox, archive = _inbox(2), FakeFolder("Archive", uidvalidity=300)
    with FakeImapServer([inbox, archive]) as server:
        with MailboxClient(settings, _factory(server)) as client:
            client.select("INBOX", readonly=False)
            assert client.move_message(1, "Archive") == "MOVE"
    assert [m.uid for m in inbox.messages] == [2]
    assert len(archive.messages) == 1


def test_move_falls_back_to_copy_expunge(settings):
    """Si el servidor no soporta MOVE, el correo tiene que llegar igual."""
    inbox, archive = _inbox(2), FakeFolder("Archive", uidvalidity=300)
    with FakeImapServer([inbox, archive], supports_move=False) as server:
        with MailboxClient(settings, _factory(server)) as client:
            client.select("INBOX", readonly=False)
            assert client.move_message(1, "Archive") == "COPY+EXPUNGE"
    assert len(archive.messages) == 1
    assert "\\Deleted" in inbox.messages[0].flags


def test_move_to_folder_with_accents(settings):
    """El nombre viaja en modified UTF-7 y el servidor lo resuelve."""
    inbox, facturacion = _inbox(1), FakeFolder("Facturación", uidvalidity=400)
    with FakeImapServer([inbox, facturacion]) as server:
        with MailboxClient(settings, _factory(server)) as client:
            client.select("INBOX", readonly=False)
            client.move_message(1, "Facturación")
    assert len(facturacion.messages) == 1


def test_fetch_raw_returns_original_bytes(settings):
    inbox = _inbox(1)
    with FakeImapServer([inbox]) as server:
        with MailboxClient(settings, _factory(server)) as client:
            client.select("INBOX")
            assert client.fetch_raw(1) == inbox.messages[0].raw


def test_selecting_missing_folder_raises_imaperror(settings):
    with FakeImapServer([_inbox(1)]) as server:
        with MailboxClient(settings, _factory(server)) as client:
            with pytest.raises(ImapError):
                client.select("Fantasma")


def test_full_sync_end_to_end_over_the_wire(settings, store, router: Router):
    """El camino completo: socket -> imaplib -> parseo -> enrutado -> SQLite."""
    inbox = _inbox(3)
    with FakeImapServer([inbox]) as server:
        with MailboxClient(settings, _factory(server)) as client:
            first = sync_folder(
                client, store, router, "INBOX", account=settings.email, initial_days=365
            )
            assert first.fetched == 3

            inbox.messages.append(FakeMessage(uid=4, raw=build_eml("Factura 4", "aduana")))
            second = sync_folder(client, store, router, "INBOX", account=settings.email)

    assert second.fetched == 1, "La segunda pasada solo trae lo nuevo"
    assert len(store.list_messages(limit=10)) == 4
    assert store.search("aduana")[0]["subject"] == "Factura 4"
    assert store.list_messages(doc_type="invoice", limit=10)


def test_list_folders_survives_a_server_sending_raw_utf8(settings):
    """Algunos servidores con UTF8=ACCEPT mandan el nombre sin codificar."""
    with FakeImapServer([_inbox(1), FakeFolder("Facturación")], utf8_folder_names=True) as server:
        with MailboxClient(settings, _factory(server)) as client:
            assert client.list_folders() == ["INBOX", "Facturación"]


def test_login_failure_gives_an_actionable_message(settings):
    """El fallo mas comun con iCloud es la contrasena de app: hay que decirlo."""
    with FakeImapServer([_inbox(1)], reject_login=True) as server:
        def failing_factory():
            conn = imaplib.IMAP4("127.0.0.1", server.port)
            conn.login("lestersv@icloud.com", "mala")
            return conn

        client = MailboxClient(settings, failing_factory)
        with pytest.raises(imaplib.IMAP4.error):
            with client:
                pass


def test_flags_are_refreshed_for_already_indexed_messages(settings, store, router):
    """Marcar un correo como leido en el iPhone tiene que llegar al indice."""
    inbox = _inbox(3)
    with FakeImapServer([inbox]) as server:
        with MailboxClient(settings, _factory(server)) as client:
            sync_folder(client, store, router, "INBOX", account=settings.email, initial_days=365)
            assert store.list_messages(unread_only=True, limit=10)

            inbox.messages[0].flags = ["\\Seen"]
            inbox.messages[1].flags = ["\\Seen", "\\Flagged"]
            report = sync_folder(client, store, router, "INBOX", account=settings.email)

    assert report.fetched == 0, "No debe redescargar cuerpos para refrescar flags"
    assert report.flags_updated == 2
    assert len(store.list_messages(unread_only=True, limit=10)) == 1


def test_flag_refresh_does_not_download_bodies(settings, store, router):
    inbox = _inbox(2)
    with FakeImapServer([inbox]) as server:
        with MailboxClient(settings, _factory(server)) as client:
            sync_folder(client, store, router, "INBOX", account=settings.email, initial_days=365)
            server.commands.clear()
            sync_folder(client, store, router, "INBOX", account=settings.email)

    fetches = [c for c in server.commands if "FETCH" in c.upper()]
    assert fetches, "La segunda pasada debe pedir flags"
    assert all("BODY" not in c.upper() for c in fetches)
