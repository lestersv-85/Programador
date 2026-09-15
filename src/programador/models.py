"""Modelos de datos del dominio."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

__all__ = ["Attachment", "Message", "Entity", "RoutingRule", "Extraction", "SyncState"]


@dataclass(slots=True)
class Attachment:
    filename: str
    content_type: str
    size: int
    saved_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "content_type": self.content_type,
            "size": self.size,
            "saved_path": self.saved_path,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Attachment":
        return cls(
            filename=data.get("filename", ""),
            content_type=data.get("content_type", ""),
            size=int(data.get("size", 0)),
            saved_path=data.get("saved_path"),
        )


@dataclass(slots=True)
class Message:
    """Un correo tal y como lo guardamos, ya enrutado."""

    account: str
    folder: str
    uid: int
    uidvalidity: int
    message_id: str | None = None
    in_reply_to: str | None = None
    from_addr: str = ""
    from_name: str = ""
    to_addrs: list[str] = field(default_factory=list)
    cc_addrs: list[str] = field(default_factory=list)
    subject: str = ""
    date_utc: datetime | None = None
    body_text: str = ""
    snippet: str = ""
    attachments: list[Attachment] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    entity: str = "personal"
    routing_reason: str = "default"
    doc_types: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        """Clave estable: un UID solo es unico dentro de (carpeta, uidvalidity)."""
        return f"{self.account}|{self.folder}|{self.uidvalidity}|{self.uid}"

    @property
    def has_attachments(self) -> bool:
        return bool(self.attachments)


@dataclass(slots=True)
class Entity:
    id: str
    name: str
    jurisdiction: str | None = None
    description: str = ""


@dataclass(slots=True)
class RoutingRule:
    entity: str
    priority: int = 0
    from_domain: list[str] = field(default_factory=list)
    from_addr: list[str] = field(default_factory=list)
    to_addr: list[str] = field(default_factory=list)
    subject_contains: list[str] = field(default_factory=list)
    body_contains: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Extraction:
    """Campos estructurados que Claude saca de un correo y persiste aqui."""

    message_key: str
    doc_type: str
    entity: str
    fields: dict[str, Any]
    status: str = "pending"
    note: str = ""
    id: int | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(slots=True)
class SyncState:
    account: str
    folder: str
    uidvalidity: int
    last_uid: int
    last_sync_at: str | None = None
