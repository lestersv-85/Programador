from __future__ import annotations

from conftest import make_message
from programador.models import SyncState
from programador.sync import sync_folder

ACCOUNT = "lester@icloud.com"


class FakeClient:
    """Sustituye a MailboxClient: devuelve UIDs y mensajes guionizados."""

    def __init__(self, uidvalidity: int, uids: list[int]) -> None:
        self.uidvalidity = uidvalidity
        self.uids = uids
        self.search_calls: list[dict] = []
        self.fetched: list[int] = []
        self.flags_by_uid: dict[int, list[str]] = {}

    def select(self, folder, *, readonly=True):
        return {"uidvalidity": self.uidvalidity, "uidnext": max(self.uids, default=0) + 1}

    def search_uids(self, *, min_uid=None, since_days=None):
        self.search_calls.append({"min_uid": min_uid, "since_days": since_days})
        if min_uid is not None:
            return [uid for uid in self.uids if uid >= min_uid]
        return list(self.uids)

    def fetch_flags(self, uids):
        return {uid: self.flags_by_uid[uid] for uid in uids if uid in self.flags_by_uid}

    def fetch_messages(self, uids, *, folder, uidvalidity):
        for uid in uids:
            self.fetched.append(uid)
            yield make_message(
                account=ACCOUNT,
                folder=folder,
                uid=uid,
                uidvalidity=uidvalidity,
                subject=f"Mensaje {uid}",
            )


def test_first_sync_uses_date_window_and_stores_everything(store, router):
    client = FakeClient(uidvalidity=100, uids=[1, 2, 3])
    report = sync_folder(client, store, router, "INBOX", account=ACCOUNT, initial_days=180)

    assert report.first_sync is True
    assert report.fetched == 3
    assert client.search_calls == [{"min_uid": None, "since_days": 180}]
    assert store.get_sync_state(ACCOUNT, "INBOX").last_uid == 3


def test_second_sync_is_incremental_and_refetches_nothing(store, router):
    client = FakeClient(uidvalidity=100, uids=[1, 2, 3])
    sync_folder(client, store, router, "INBOX", account=ACCOUNT, initial_days=180)

    client.uids = [1, 2, 3, 4, 5]
    client.fetched.clear()
    report = sync_folder(client, store, router, "INBOX", account=ACCOUNT)

    assert report.first_sync is False
    assert client.search_calls[-1] == {"min_uid": 4, "since_days": None}
    assert client.fetched == [4, 5], "No debe volver a descargar lo ya indexado"
    assert report.fetched == 2
    assert len(store.list_messages(limit=100)) == 5


def test_uid_search_wildcard_echo_is_not_reprocessed(store, router):
    """`UID n:*` devuelve el ultimo mensaje aunque su UID sea menor que n."""
    client = FakeClient(uidvalidity=100, uids=[1, 2, 3])
    sync_folder(client, store, router, "INBOX", account=ACCOUNT, initial_days=180)
    client.fetched.clear()

    report = sync_folder(client, store, router, "INBOX", account=ACCOUNT)
    assert client.fetched == []
    assert report.fetched == 0
    assert report.skipped == 0


def test_uidvalidity_change_purges_and_resyncs(store, router):
    client = FakeClient(uidvalidity=100, uids=[1, 2, 3])
    sync_folder(client, store, router, "INBOX", account=ACCOUNT, initial_days=180)
    assert len(store.list_messages(limit=100)) == 3

    # El servidor reconstruyo la carpeta: los UID antiguos ya no significan nada.
    client.uidvalidity = 200
    client.uids = [1, 2]
    report = sync_folder(client, store, router, "INBOX", account=ACCOUNT, initial_days=180)

    assert report.uidvalidity_reset is True
    assert report.fetched == 2
    remaining = store.list_messages(limit=100)
    assert len(remaining) == 2
    assert all(m["uidvalidity"] == 200 for m in remaining)


def test_sync_applies_routing(store, router):
    class RoutingClient(FakeClient):
        def fetch_messages(self, uids, *, folder, uidvalidity):
            yield make_message(
                account=ACCOUNT,
                folder=folder,
                uid=1,
                uidvalidity=uidvalidity,
                to_addrs=["rambaid@lester.es"],
                subject="Factura de aduana",
            )

    client = RoutingClient(uidvalidity=100, uids=[1])
    sync_folder(client, store, router, "INBOX", account=ACCOUNT, initial_days=180)
    stored = store.list_messages(limit=1)[0]
    assert stored["entity"] == "rambaid"
    assert stored["routing_reason"] == "to_addr=rambaid@lester.es"
    assert sorted(stored["doc_types"]) == ["customs", "invoice"]


def test_select_failure_is_reported_not_raised(store, router):
    from programador.imap_client import ImapError

    class BrokenClient(FakeClient):
        def select(self, folder, *, readonly=True):
            raise ImapError("carpeta inexistente")

    report = sync_folder(
        BrokenClient(100, []), store, router, "Fantasma", account=ACCOUNT, initial_days=180
    )
    assert report.error == "carpeta inexistente"
    assert report.fetched == 0


def test_resume_after_interruption_does_not_lose_messages(store, router):
    """Si la sincronizacion se corta a medias, la siguiente recupera lo que falto."""
    client = FakeClient(uidvalidity=100, uids=[1, 2, 3])
    store.set_sync_state(SyncState(ACCOUNT, "INBOX", uidvalidity=100, last_uid=1))
    store.upsert_message(make_message(account=ACCOUNT, uid=1, uidvalidity=100))

    report = sync_folder(client, store, router, "INBOX", account=ACCOUNT)
    assert sorted(m["uid"] for m in store.list_messages(limit=10)) == [1, 2, 3]
    assert report.fetched == 2


def test_flag_refresh_updates_indexed_messages(store, router):
    client = FakeClient(uidvalidity=100, uids=[1, 2])
    sync_folder(client, store, router, "INBOX", account=ACCOUNT, initial_days=180)
    assert len(store.list_messages(unread_only=True, limit=10)) == 2

    client.flags_by_uid = {1: ["\\Seen"]}
    report = sync_folder(client, store, router, "INBOX", account=ACCOUNT)

    assert report.flags_updated == 1
    assert len(store.list_messages(unread_only=True, limit=10)) == 1


def test_flag_refresh_can_be_disabled(store, router):
    client = FakeClient(uidvalidity=100, uids=[1])
    sync_folder(client, store, router, "INBOX", account=ACCOUNT, initial_days=180)
    client.flags_by_uid = {1: ["\\Seen"]}
    report = sync_folder(
        client, store, router, "INBOX", account=ACCOUNT, refresh_flags_limit=0
    )
    assert report.flags_updated == 0
