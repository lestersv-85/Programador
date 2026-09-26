from __future__ import annotations

import pytest

from programador.config import ConfigError, load_routing_config, load_settings, parse_routing_config


def test_load_settings_requires_credentials():
    with pytest.raises(ConfigError, match="PROGRAMADOR_EMAIL"):
        load_settings({})
    with pytest.raises(ConfigError, match="PROGRAMADOR_APP_PASSWORD"):
        load_settings({"PROGRAMADOR_EMAIL": "a@icloud.com"})


def test_load_settings_defaults_to_icloud():
    settings = load_settings(
        {"PROGRAMADOR_EMAIL": "a@icloud.com", "PROGRAMADOR_APP_PASSWORD": "aaaa-bbbb-cccc-dddd"}
    )
    assert settings.imap_host == "imap.mail.me.com"
    assert settings.imap_port == 993
    assert settings.smtp_port == 587
    assert settings.folders == ("INBOX",)


def test_redacted_never_leaks_the_app_password():
    settings = load_settings(
        {"PROGRAMADOR_EMAIL": "a@icloud.com", "PROGRAMADOR_APP_PASSWORD": "secreto-real-aqui"}
    )
    dumped = repr(settings.redacted())
    assert "secreto-real-aqui" not in dumped
    assert settings.redacted()["app_password"] == "<set>"


def test_folders_are_split_and_trimmed():
    settings = load_settings(
        {
            "PROGRAMADOR_EMAIL": "a@icloud.com",
            "PROGRAMADOR_APP_PASSWORD": "x",
            "PROGRAMADOR_FOLDERS": " INBOX , Facturación ,, Archive ",
        }
    )
    assert settings.folders == ("INBOX", "Facturación", "Archive")


def test_missing_config_file_points_at_the_example(tmp_path):
    with pytest.raises(ConfigError, match="No existe el fichero de configuracion"):
        load_routing_config(tmp_path / "entities.yaml")


def test_shipped_example_config_is_valid():
    """El fichero que el usuario copia tiene que parsear tal cual."""
    from pathlib import Path

    example = Path(__file__).resolve().parents[1] / "config" / "entities.example.yaml"
    config = parse_routing_config(__import__("yaml").safe_load(example.read_text(encoding="utf-8")))
    assert {"rambaid", "ecme_forego", "forego_intl", "sand", "personal"} <= set(config.entity_ids())
    assert config.rules
    assert "invoice" in config.doc_type_hints
