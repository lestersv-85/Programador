from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from conftest import make_message
from programador.config import load_settings
from programador.publish import BATCH_SIZE, BODY_LIMIT, LOG_TABLE, MESSAGES_TABLE, publish


class FakePostgrest:
    """Captura lo que el publicador envia y responde lo que se le diga."""

    def __init__(self, status: int = 201) -> None:
        self.status = status
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"[]")
                outer.requests.append(
                    {
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": body,
                    }
                )
                self.send_response(outer.status)
                self.end_headers()
                if outer.status >= 400:
                    self.wfile.write(b'{"message":"boom"}')

            def log_message(self, *_args) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}"

    def __enter__(self) -> "FakePostgrest":
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._server.shutdown()
        self._server.server_close()

    def posts_to(self, table: str) -> list[dict]:
        return [r for r in self.requests if r["path"].endswith(f"/rest/v1/{table}")]


def _settings(url: str, key: str = "sb_publishable_test", days: str = "3"):
    return load_settings(
        {
            "PROGRAMADOR_EMAIL": "lestersv@icloud.com",
            "PROGRAMADOR_APP_PASSWORD": "aaaa-bbbb-cccc-dddd",
            "PROGRAMADOR_SUPABASE_URL": url,
            "PROGRAMADOR_SUPABASE_KEY": key,
            "PROGRAMADOR_PUBLISH_DAYS": days,
        }
    )


NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


def test_publish_is_a_noop_without_configuration(store):
    settings = _settings(url="", key="")
    report = publish(store, settings, now=NOW)
    assert report.enabled is False
    assert report.sent == 0


def test_redacted_settings_never_expose_the_supabase_key():
    settings = _settings("https://x.supabase.co", key="sb_publishable_SECRETO")
    assert "SECRETO" not in repr(settings.redacted())


def test_publishes_only_the_recent_window_with_write_only_headers(store):
    store.upsert_message(make_message(uid=1, subject="reciente", date_utc=NOW - timedelta(days=1)))
    store.upsert_message(make_message(uid=2, subject="viejo", date_utc=NOW - timedelta(days=10)))

    with FakePostgrest() as server:
        report = publish(store, _settings(server.url, key="eyJ.legacy.jwt"), now=NOW)

    assert report.error is None
    assert (report.candidates, report.sent, report.batches) == (1, 1, 1)

    posts = server.posts_to(MESSAGES_TABLE)
    assert len(posts) == 1
    assert [row["subject"] for row in posts[0]["body"]] == ["reciente"]
    headers = posts[0]["headers"]
    assert headers["apikey"] == "eyJ.legacy.jwt"
    assert headers["authorization"] == "Bearer eyJ.legacy.jwt"
    assert "ignore-duplicates" in headers["prefer"], "Sin UPDATE no hace falta permiso de lectura"
    assert "return=minimal" in headers["prefer"]


def test_new_style_publishable_key_is_not_sent_as_bearer(store):
    store.upsert_message(make_message(uid=1, date_utc=NOW))
    with FakePostgrest() as server:
        publish(store, _settings(server.url, key="sb_publishable_abc"), now=NOW)
    headers = server.posts_to(MESSAGES_TABLE)[0]["headers"]
    assert headers["apikey"] == "sb_publishable_abc"
    assert "authorization" not in headers


def test_publishes_a_sync_log_row_after_the_messages(store):
    store.upsert_message(make_message(uid=1, date_utc=NOW))
    with FakePostgrest() as server:
        publish(store, _settings(server.url), now=NOW)

    log_posts = server.posts_to(LOG_TABLE)
    assert len(log_posts) == 1
    row = log_posts[0]["body"][0]
    assert row["account"] == "lestersv@icloud.com"
    assert row["publicados"] == 1
    assert row["total_mensajes"] == 1
    assert row["ventana_dias"] == 3
    assert row["error"] is None
    assert row["synced_at"].startswith("2026-09-15T12:00:00")
    # El log va despues de los mensajes: si el brief ve el log, los mensajes ya estan.
    assert server.requests[-1]["path"].endswith(LOG_TABLE)


def test_batches_large_windows(store):
    for uid in range(1, BATCH_SIZE + 51):
        store.upsert_message(make_message(uid=uid, date_utc=NOW - timedelta(minutes=uid)))
    with FakePostgrest() as server:
        report = publish(store, _settings(server.url), now=NOW)

    assert report.sent == BATCH_SIZE + 50
    assert report.batches == 2
    sizes = [len(p["body"]) for p in server.posts_to(MESSAGES_TABLE)]
    assert sizes == [BATCH_SIZE, 50]


def test_body_is_truncated_and_flagged(store):
    store.upsert_message(make_message(uid=1, date_utc=NOW, body_text="x" * (BODY_LIMIT + 100)))
    with FakePostgrest() as server:
        publish(store, _settings(server.url), now=NOW)
    row = server.posts_to(MESSAGES_TABLE)[0]["body"][0]
    assert len(row["body_text"]) == BODY_LIMIT
    assert row["body_truncado"] is True
    assert row["attachments"] == []
    assert row["entity"] == "personal"


def test_server_error_is_reported_not_raised_and_logged(store):
    store.upsert_message(make_message(uid=1, date_utc=NOW))
    with FakePostgrest(status=500) as server:
        report = publish(store, _settings(server.url), now=NOW)

    assert report.error is not None
    assert "500" in report.error
    assert report.sent == 0
    # Intenta dejar constancia del fallo en el log aunque el servidor este mal.
    assert server.posts_to(LOG_TABLE), "Debe intentar registrar el error"
    assert server.posts_to(LOG_TABLE)[-1]["body"][0]["error"]


def test_unreachable_supabase_is_reported(store):
    store.upsert_message(make_message(uid=1, date_utc=NOW))
    report = publish(store, _settings("http://127.0.0.1:9"), now=NOW)
    assert report.error is not None
    assert "No se pudo alcanzar" in report.error


def test_sync_cli_publishes_after_sync(monkeypatch, tmp_path, capsys):
    """El camino real del launchd del Mac: programador-sync sincroniza y publica."""
    import yaml

    from conftest import ROUTING_YAML
    from programador import sync as sync_module

    config_path = tmp_path / "entities.yaml"
    config_path.write_text(yaml.safe_dump(ROUTING_YAML), encoding="utf-8")

    def fake_sync_all(settings, store, router, *, folders=None, connection_factory=None):
        store.upsert_message(make_message(uid=1, date_utc=datetime.now(timezone.utc)))
        return [sync_module.SyncReport(folder="INBOX", uidvalidity=1, fetched=1)]

    monkeypatch.setattr(sync_module, "sync_all", fake_sync_all)

    with FakePostgrest() as server:
        for key, value in {
            "PROGRAMADOR_EMAIL": "lestersv@icloud.com",
            "PROGRAMADOR_APP_PASSWORD": "x",
            "PROGRAMADOR_CONFIG": str(config_path),
            "PROGRAMADOR_DB": str(tmp_path / "t.db"),
            "PROGRAMADOR_SUPABASE_URL": server.url,
            "PROGRAMADOR_SUPABASE_KEY": "sb_publishable_test",
        }.items():
            monkeypatch.setenv(key, value)
        exit_code = sync_module.main([])

    assert exit_code == 0
    assert "[supabase] publicados 1/1" in capsys.readouterr().out
    assert server.posts_to(MESSAGES_TABLE) and server.posts_to(LOG_TABLE)
