from __future__ import annotations

import imaplib
import smtplib

import pytest
import yaml

from conftest import ROUTING_YAML
from fake_imap import FakeFolder, FakeImapServer
from programador.doctor import run_diagnostics
from programador.imap_client import MailboxClient


@pytest.fixture
def env(tmp_path):
    config = tmp_path / "entities.yaml"
    config.write_text(yaml.safe_dump(ROUTING_YAML), encoding="utf-8")
    return {
        "PROGRAMADOR_EMAIL": "lestersv@icloud.com",
        "PROGRAMADOR_APP_PASSWORD": "aaaa-bbbb-cccc-dddd",
        "PROGRAMADOR_CONFIG": str(config),
        "PROGRAMADOR_DB": str(tmp_path / "t.db"),
    }


def _names(result):
    return [c.nombre for c in result.checks]


def test_missing_env_stops_immediately_with_a_remedy():
    result = run_diagnostics({}, skip_network=True)
    assert _names(result) == ["entorno"]
    assert not result.ok
    assert ".env" in result.checks[0].remedio


def test_missing_config_file_is_reported_before_touching_the_network(tmp_path):
    result = run_diagnostics(
        {
            "PROGRAMADOR_EMAIL": "a@icloud.com",
            "PROGRAMADOR_APP_PASSWORD": "x",
            "PROGRAMADOR_CONFIG": str(tmp_path / "no-existe.yaml"),
            "PROGRAMADOR_DB": str(tmp_path / "t.db"),
        },
        skip_network=True,
    )
    assert _names(result) == ["entorno", "configuracion"]
    assert not result.ok
    assert "entities.example.yaml" in result.checks[1].remedio


def test_skip_network_does_not_claim_icloud_works(env):
    result = run_diagnostics(env, skip_network=True)
    assert result.ok
    assert result.red_comprobada is False
    assert "NO se ha comprobado" in result.to_dict()["resumen"]


def test_placeholders_in_config_are_flagged_as_a_warning(env, tmp_path):
    routing = {**ROUTING_YAML}
    routing["routing_rules"] = [
        {"entity": "rambaid", "priority": 10, "match": {"to_addr": ["CAMBIAME-rambaid@x.es"]}}
    ]
    config = tmp_path / "entities.yaml"
    config.write_text(yaml.safe_dump(routing), encoding="utf-8")
    env["PROGRAMADOR_CONFIG"] = str(config)

    result = run_diagnostics(env, skip_network=True)
    assert result.ok, "Un marcador sin sustituir es un aviso, no un fallo"
    assert [c.nombre for c in result.avisos] == ["configuracion"]
    assert "CAMBIAME" in result.to_dict()["resumen"]


def test_full_chain_against_a_live_imap_server(env, monkeypatch):
    """Recorre entorno, config, base de datos, TCP, login IMAP y carpetas."""
    folders = [FakeFolder("INBOX"), FakeFolder("Facturación")]
    with FakeImapServer(folders) as server:
        env["PROGRAMADOR_IMAP_HOST"] = "127.0.0.1"
        env["PROGRAMADOR_IMAP_PORT"] = str(server.port)

        def imap_factory(settings):
            def build():
                conn = imaplib.IMAP4("127.0.0.1", server.port)
                conn.login(settings.email, settings.app_password)
                return conn

            return MailboxClient(settings, build)

        def smtp_factory(_settings):
            raise smtplib.SMTPAuthenticationError(535, b"credenciales invalidas")

        env["PROGRAMADOR_SMTP_HOST"] = "127.0.0.1"
        env["PROGRAMADOR_SMTP_PORT"] = str(server.port)
        result = run_diagnostics(env, imap_factory=imap_factory, smtp_factory=smtp_factory)

    assert _names(result) == [
        "entorno", "configuracion", "base_de_datos", "imap_puerto",
        "imap_login", "carpetas", "smtp_puerto", "smtp_login",
    ]
    assert [c.nombre for c in result.checks if not c.ok] == ["smtp_login"]
    assert "account.apple.com" in result.checks[-1].remedio


def test_configured_folder_that_does_not_exist_is_reported(env):
    with FakeImapServer([FakeFolder("INBOX")]) as server:
        env.update(
            {
                "PROGRAMADOR_IMAP_HOST": "127.0.0.1",
                "PROGRAMADOR_IMAP_PORT": str(server.port),
                "PROGRAMADOR_SMTP_HOST": "127.0.0.1",
                "PROGRAMADOR_SMTP_PORT": "9",
                "PROGRAMADOR_FOLDERS": "INBOX,Facturación",
            }
        )

        def imap_factory(settings):
            def build():
                conn = imaplib.IMAP4("127.0.0.1", server.port)
                conn.login(settings.email, settings.app_password)
                return conn

            return MailboxClient(settings, build)

        result = run_diagnostics(env, imap_factory=imap_factory)

    carpetas = next(c for c in result.checks if c.nombre == "carpetas")
    assert not carpetas.ok
    assert "Facturación" in carpetas.detalle
    assert "ingles" in carpetas.remedio


def test_bad_app_password_gives_the_apple_remedy(env):
    with FakeImapServer([FakeFolder("INBOX")], reject_login=True) as server:
        env.update(
            {
                "PROGRAMADOR_IMAP_HOST": "127.0.0.1",
                "PROGRAMADOR_IMAP_PORT": str(server.port),
                "PROGRAMADOR_SMTP_HOST": "127.0.0.1",
                "PROGRAMADOR_SMTP_PORT": "9",
            }
        )

        def imap_factory(settings):
            def build():
                conn = imaplib.IMAP4("127.0.0.1", server.port)
                conn.login(settings.email, settings.app_password)
                return conn

            return MailboxClient(settings, build)

        result = run_diagnostics(env, imap_factory=imap_factory)

    login = next(c for c in result.checks if c.nombre == "imap_login")
    assert not login.ok
    assert "account.apple.com" in login.remedio


def test_closed_port_is_diagnosed_as_network_not_credentials(env):
    env.update({"PROGRAMADOR_IMAP_HOST": "127.0.0.1", "PROGRAMADOR_IMAP_PORT": "9"})
    result = run_diagnostics(env)
    puerto = next(c for c in result.checks if c.nombre == "imap_puerto")
    assert not puerto.ok
    assert "bloquea el puerto" in puerto.remedio
    assert "imap_login" not in _names(result), "Sin puerto no tiene sentido probar credenciales"
