"""Publicacion de la ventana reciente del indice en Supabase.

Por que existe: las sesiones de Claude en la nube -- donde corre el resumen
matutino -- solo pueden hablar HTTPS. Se comprobo con un control: el mismo
ClientHello por el mismo tunel recibe ServerHello de www.icloud.com:443 y cero
bytes de imap.mail.me.com:993. Asi que el IMAP lo hace el Mac y aqui se sube por
HTTPS lo que el brief necesita: los ultimos dias, ya enrutados.

Escritura ciega a proposito: la clave publicable de Supabase solo tiene permiso
de INSERT (RLS), nunca de lectura. Si esa clave se filtrara, lo peor que puede
pasar es que alguien meta filas basura; no puede leer tu correo. La lectura la
hace el brief con el conector de Supabase, que usa la clave de servicio.

Se inserta con `resolution=ignore-duplicates` (INSERT ... ON CONFLICT DO
NOTHING): sin UPDATE no hace falta ningun permiso de lectura, a cambio de que un
mensaje ya publicado no se corrige si cambian sus flags o su enrutado.
"""

from __future__ import annotations

import json
import logging
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib import error, request

from .config import Settings
from .store import Store

__all__ = ["PublishError", "PublishReport", "publish", "MESSAGES_TABLE", "LOG_TABLE"]

logger = logging.getLogger(__name__)

MESSAGES_TABLE = "programador_mensajes"
LOG_TABLE = "programador_sync_log"
BATCH_SIZE = 200
BODY_LIMIT = 4000
PAGE_SIZE = 500

Opener = Callable[[request.Request], Any]


class PublishError(RuntimeError):
    """Supabase no acepto la publicacion."""


@dataclass(slots=True)
class PublishReport:
    enabled: bool
    days: int = 0
    candidates: int = 0
    sent: int = 0
    batches: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "ventana_dias": self.days,
            "candidatos": self.candidates,
            "enviados": self.sent,
            "lotes": self.batches,
            "error": self.error,
        }


def _default_opener(req: request.Request) -> Any:
    return request.urlopen(req, timeout=30)  # noqa: S310 - URL configurada por el usuario


def _row(message: dict[str, Any]) -> dict[str, Any]:
    """Proyeccion de un mensaje del indice a la fila publicada."""
    body = message.get("body_text") or ""
    return {
        "key": message["key"],
        "account": message["account"],
        "folder": message["folder"],
        "uid": message["uid"],
        "uidvalidity": message["uidvalidity"],
        "message_id": message.get("message_id"),
        "from_addr": message.get("from_addr") or "",
        "from_name": message.get("from_name") or "",
        "to_addrs": message.get("to_addrs") or [],
        "cc_addrs": message.get("cc_addrs") or [],
        "subject": message.get("subject") or "",
        "date_utc": message.get("date_utc"),
        "snippet": message.get("snippet") or "",
        "body_text": body[:BODY_LIMIT],
        "body_truncado": len(body) > BODY_LIMIT,
        "attachments": [a.get("filename", "") for a in (message.get("attachments") or [])],
        "flags": message.get("flags") or [],
        "entity": message.get("entity") or "personal",
        "routing_reason": message.get("routing_reason") or "default",
        "doc_types": message.get("doc_types") or [],
    }


def _post(settings: Settings, table: str, payload: list[dict[str, Any]], opener: Opener) -> None:
    url = f"{settings.supabase_url.rstrip('/')}/rest/v1/{table}"
    headers = {
        "apikey": settings.supabase_key,
        "Content-Type": "application/json",
        "Prefer": "resolution=ignore-duplicates,return=minimal",
    }
    # Las claves legadas son JWT y van tambien como Bearer; las nuevas
    # (sb_publishable_...) no son JWT y la pasarela las rechaza en Authorization.
    if not settings.supabase_key.startswith("sb_"):
        headers["Authorization"] = f"Bearer {settings.supabase_key}"
    req = request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers=headers,
    )
    try:
        response = opener(req)
    except error.HTTPError as exc:
        detail = exc.read()[:300].decode("utf-8", "replace")
        raise PublishError(f"Supabase respondio {exc.code} en {table}: {detail}") from exc
    except (error.URLError, OSError) as exc:
        raise PublishError(f"No se pudo alcanzar Supabase ({settings.supabase_url}): {exc}") from exc
    status = getattr(response, "status", 200)
    if status >= 300:
        raise PublishError(f"Supabase respondio {status} en {table}")


def _recent_rows(store: Store, since_iso: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = store.list_messages(since=since_iso, limit=PAGE_SIZE, offset=offset)
        rows.extend(_row(m) for m in page)
        if len(page) < PAGE_SIZE:
            return rows
        offset += PAGE_SIZE


def publish(
    store: Store,
    settings: Settings,
    *,
    days: int | None = None,
    opener: Opener | None = None,
    now: datetime | None = None,
) -> PublishReport:
    """Sube a Supabase los mensajes de los ultimos `days` dias y una fila de estado."""
    if not settings.publish_enabled:
        return PublishReport(enabled=False)

    send = opener or _default_opener
    window = days if days is not None else settings.publish_days
    moment = now or datetime.now(timezone.utc)
    since_iso = (moment - timedelta(days=max(0, window))).isoformat(timespec="seconds")

    rows = _recent_rows(store, since_iso)
    report = PublishReport(enabled=True, days=window, candidates=len(rows))

    log_row: dict[str, Any] = {
        "account": settings.email,
        "synced_at": moment.isoformat(timespec="seconds"),
        "total_mensajes": store.stats()["total_messages"],
        "publicados": 0,
        "ventana_dias": window,
        "host": socket.gethostname(),
        "error": None,
    }

    try:
        for start in range(0, len(rows), BATCH_SIZE):
            batch = rows[start : start + BATCH_SIZE]
            _post(settings, MESSAGES_TABLE, batch, send)
            report.batches += 1
            report.sent += len(batch)
        log_row["publicados"] = report.sent
        _post(settings, LOG_TABLE, [log_row], send)
    except PublishError as exc:
        report.error = str(exc)
        logger.warning("Publicacion en Supabase fallida: %s", exc)
        # Dejamos constancia del fallo para que el brief pueda decir "no sincronizado".
        log_row["publicados"] = report.sent
        log_row["error"] = str(exc)[:500]
        try:
            _post(settings, LOG_TABLE, [log_row], send)
        except PublishError:
            logger.debug("Tampoco se pudo registrar el error en Supabase", exc_info=True)
    return report
