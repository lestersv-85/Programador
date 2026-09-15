"""Carga de configuracion: credenciales por entorno, enrutado por YAML."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .models import Entity, RoutingRule

__all__ = ["Settings", "RoutingConfig", "load_settings", "load_routing_config"]


class ConfigError(RuntimeError):
    """Configuracion ausente o invalida."""


@dataclass(slots=True)
class Settings:
    """Todo lo sensible vive aqui y solo llega por variables de entorno.

    La contrasena especifica de app NUNCA se escribe en disco dentro del
    repositorio ni se devuelve por ninguna herramienta MCP.
    """

    email: str
    app_password: str
    imap_host: str = "imap.mail.me.com"
    imap_port: int = 993
    smtp_host: str = "smtp.mail.me.com"
    smtp_port: int = 587
    db_path: Path = Path("./programador.db")
    config_path: Path = Path("./config/entities.yaml")
    attachments_dir: Path = Path("./attachments")
    folders: tuple[str, ...] = ("INBOX",)
    initial_days: int = 180
    # Publicacion opcional de la ventana reciente en Supabase (ver publish.py).
    supabase_url: str = ""
    supabase_key: str = ""
    publish_days: int = 3

    @property
    def publish_enabled(self) -> bool:
        return bool(self.supabase_url and self.supabase_key)

    def redacted(self) -> dict[str, object]:
        """Vista segura para diagnostico: sin contrasena."""
        return {
            "email": self.email,
            "imap": f"{self.imap_host}:{self.imap_port}",
            "smtp": f"{self.smtp_host}:{self.smtp_port}",
            "db_path": str(self.db_path),
            "config_path": str(self.config_path),
            "attachments_dir": str(self.attachments_dir),
            "folders": list(self.folders),
            "initial_days": self.initial_days,
            "app_password": "<set>" if self.app_password else "<MISSING>",
            "supabase_url": self.supabase_url or "<not set>",
            "supabase_key": "<set>" if self.supabase_key else "<not set>",
            "publish_days": self.publish_days,
        }


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def load_settings(env: dict[str, str] | None = None) -> Settings:
    """Construye Settings desde el entorno. Falla ruidosamente si falta lo critico."""
    source = env if env is not None else os.environ
    email = source.get("PROGRAMADOR_EMAIL", "").strip()
    password = source.get("PROGRAMADOR_APP_PASSWORD", "").strip()
    missing = [
        name
        for name, value in (
            ("PROGRAMADOR_EMAIL", email),
            ("PROGRAMADOR_APP_PASSWORD", password),
        )
        if not value
    ]
    if missing:
        raise ConfigError(
            "Faltan variables de entorno: "
            + ", ".join(missing)
            + ". Copia .env.example a .env y rellenalo."
        )

    folders_raw = source.get("PROGRAMADOR_FOLDERS", "INBOX")
    folders = tuple(f.strip() for f in folders_raw.split(",") if f.strip()) or ("INBOX",)

    return Settings(
        email=email,
        app_password=password,
        imap_host=source.get("PROGRAMADOR_IMAP_HOST", "imap.mail.me.com"),
        imap_port=int(source.get("PROGRAMADOR_IMAP_PORT", "993")),
        smtp_host=source.get("PROGRAMADOR_SMTP_HOST", "smtp.mail.me.com"),
        smtp_port=int(source.get("PROGRAMADOR_SMTP_PORT", "587")),
        db_path=Path(source.get("PROGRAMADOR_DB", "./programador.db")),
        config_path=Path(source.get("PROGRAMADOR_CONFIG", "./config/entities.yaml")),
        attachments_dir=Path(source.get("PROGRAMADOR_ATTACHMENTS_DIR", "./attachments")),
        folders=folders,
        initial_days=int(source.get("PROGRAMADOR_INITIAL_DAYS", "180")),
        supabase_url=source.get("PROGRAMADOR_SUPABASE_URL", "").strip(),
        supabase_key=source.get("PROGRAMADOR_SUPABASE_KEY", "").strip(),
        publish_days=int(source.get("PROGRAMADOR_PUBLISH_DAYS", "3")),
    )


@dataclass(slots=True)
class RoutingConfig:
    default_entity: str = "personal"
    entities: list[Entity] = field(default_factory=list)
    rules: list[RoutingRule] = field(default_factory=list)
    doc_type_hints: dict[str, list[str]] = field(default_factory=dict)

    def entity_ids(self) -> list[str]:
        return [e.id for e in self.entities]

    def entity(self, entity_id: str) -> Entity | None:
        return next((e for e in self.entities if e.id == entity_id), None)


def _as_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value]
    raise ConfigError(f"Se esperaba una lista o cadena, llego {type(value).__name__}")


def parse_routing_config(data: dict) -> RoutingConfig:
    """Valida y convierte el YAML de enrutado."""
    if not isinstance(data, dict):
        raise ConfigError("El fichero de configuracion debe ser un mapa YAML")

    entities = [
        Entity(
            id=str(item["id"]),
            name=str(item.get("name", item["id"])),
            jurisdiction=item.get("jurisdiction"),
            description=str(item.get("description", "")),
        )
        for item in data.get("entities", [])
        if isinstance(item, dict) and item.get("id")
    ]
    known = {e.id for e in entities}

    default_entity = str(data.get("default_entity", "personal"))
    if entities and default_entity not in known:
        raise ConfigError(
            f"default_entity '{default_entity}' no esta en la lista de entidades: {sorted(known)}"
        )

    rules: list[RoutingRule] = []
    for item in data.get("routing_rules", []) or []:
        if not isinstance(item, dict):
            continue
        entity_id = str(item.get("entity", "")).strip()
        if not entity_id:
            raise ConfigError("Una regla de enrutado no declara 'entity'")
        if known and entity_id not in known:
            raise ConfigError(
                f"La regla apunta a la entidad desconocida '{entity_id}'. "
                f"Entidades declaradas: {sorted(known)}"
            )
        match = item.get("match") or {}
        rules.append(
            RoutingRule(
                entity=entity_id,
                priority=int(item.get("priority", 0)),
                from_domain=_as_list(match.get("from_domain")),
                from_addr=_as_list(match.get("from_addr")),
                to_addr=_as_list(match.get("to_addr")),
                subject_contains=_as_list(match.get("subject_contains")),
                body_contains=_as_list(match.get("body_contains")),
            )
        )

    hints = {
        str(doc_type): _as_list(keywords)
        for doc_type, keywords in (data.get("doc_type_hints") or {}).items()
    }

    return RoutingConfig(
        default_entity=default_entity,
        entities=entities,
        rules=rules,
        doc_type_hints=hints,
    )


def load_routing_config(path: Path) -> RoutingConfig:
    if not path.exists():
        example = path.with_name("entities.example.yaml")
        hint = f" Copia {example} a {path} y editalo." if example.exists() else ""
        raise ConfigError(f"No existe el fichero de configuracion {path}.{hint}")
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    return parse_routing_config(data)
