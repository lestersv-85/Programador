"""Parseo MIME: de bytes crudos de IMAP a un Message del dominio.

Modulo puro y sin red, para poder testearlo con ficheros .eml de ejemplo.
"""

from __future__ import annotations

import html
import re
from email import message_from_bytes, policy
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import getaddresses, parsedate_to_datetime
from datetime import datetime, timezone

from .models import Attachment, Message

__all__ = ["parse_message", "html_to_text", "decode_mime_header", "attachment_payloads"]

_SCRIPT_STYLE = re.compile(r"<(script|style)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_BLOCK_BREAK = re.compile(r"(?i)<(br\s*/?|/p|/div|/tr|/li|/h[1-6])\s*>")
_TAG = re.compile(r"<[^>]+>")
_BLANK_RUN = re.compile(r"\n{3,}")

SNIPPET_LENGTH = 320
MAX_BODY_CHARS = 200_000


def decode_mime_header(value: str | None) -> str:
    """Decodifica cabeceras RFC 2047 ('=?utf-8?B?...?=') sin lanzar excepciones."""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value))).strip()
    except Exception:
        return value.strip()


def html_to_text(raw_html: str) -> str:
    """Conversion deliberadamente tosca: solo alimenta busqueda y resumen."""
    without_code = _SCRIPT_STYLE.sub(" ", raw_html)
    with_breaks = _BLOCK_BREAK.sub("\n", without_code)
    text = _TAG.sub(" ", with_breaks)
    text = html.unescape(text)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return _BLANK_RUN.sub("\n\n", "\n".join(line for line in lines)).strip()


def _part_text(part: EmailMessage) -> str:
    try:
        payload = part.get_content()
    except Exception:
        raw = part.get_payload(decode=True)
        if not isinstance(raw, bytes):
            return ""
        charset = part.get_content_charset() or "utf-8"
        payload = raw.decode(charset, errors="replace")
    return payload if isinstance(payload, str) else ""


def _is_attachment(part: EmailMessage) -> bool:
    disposition = (part.get_content_disposition() or "").lower()
    if disposition == "attachment":
        return True
    # Muchos ERP adjuntan el PDF de la factura como 'inline' con filename.
    return disposition == "inline" and bool(part.get_filename())


def _collect(msg: EmailMessage) -> tuple[str, str, list[Attachment]]:
    plain_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[Attachment] = []

    for part in msg.walk():
        if part.is_multipart():
            continue
        content_type = (part.get_content_type() or "").lower()
        if _is_attachment(part):
            raw = part.get_payload(decode=True) or b""
            attachments.append(
                Attachment(
                    filename=decode_mime_header(part.get_filename()) or "sin-nombre",
                    content_type=content_type,
                    size=len(raw),
                )
            )
            continue
        if content_type == "text/plain":
            plain_parts.append(_part_text(part))
        elif content_type == "text/html":
            html_parts.append(_part_text(part))

    if plain_parts:
        body = "\n\n".join(p.strip() for p in plain_parts if p.strip())
    else:
        body = "\n\n".join(html_to_text(h) for h in html_parts if h.strip())
    return body.strip()[:MAX_BODY_CHARS], "\n".join(html_parts), attachments


def _addresses(msg: EmailMessage, header: str) -> list[str]:
    values = msg.get_all(header, [])
    return [addr.lower() for _, addr in getaddresses(values) if addr]


def _parse_date(msg: EmailMessage) -> datetime | None:
    raw = msg.get("Date")
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_message(
    raw: bytes,
    *,
    account: str,
    folder: str,
    uid: int,
    uidvalidity: int,
    flags: list[str] | None = None,
) -> Message:
    """Convierte los bytes de un correo en un Message sin enrutar."""
    msg: EmailMessage = message_from_bytes(raw, policy=policy.default)  # type: ignore[assignment]
    body, _html, attachments = _collect(msg)

    from_pairs = getaddresses(msg.get_all("From", []))
    from_name, from_addr = (from_pairs[0] if from_pairs else ("", ""))

    snippet = " ".join(body.split())[:SNIPPET_LENGTH]

    return Message(
        account=account,
        folder=folder,
        uid=uid,
        uidvalidity=uidvalidity,
        message_id=(msg.get("Message-ID") or "").strip() or None,
        in_reply_to=(msg.get("In-Reply-To") or "").strip() or None,
        from_addr=from_addr.lower(),
        from_name=decode_mime_header(from_name),
        to_addrs=_addresses(msg, "To"),
        cc_addrs=_addresses(msg, "Cc"),
        subject=decode_mime_header(msg.get("Subject")),
        date_utc=_parse_date(msg),
        body_text=body,
        snippet=snippet,
        attachments=attachments,
        flags=list(flags or []),
    )


def attachment_payloads(raw: bytes) -> list[tuple[str, str, bytes]]:
    """(filename, content_type, bytes) de cada adjunto. Para descarga bajo demanda."""
    msg: EmailMessage = message_from_bytes(raw, policy=policy.default)  # type: ignore[assignment]
    out: list[tuple[str, str, bytes]] = []
    for part in msg.walk():
        if part.is_multipart() or not _is_attachment(part):
            continue
        payload = part.get_payload(decode=True) or b""
        out.append(
            (
                decode_mime_header(part.get_filename()) or "sin-nombre",
                (part.get_content_type() or "application/octet-stream").lower(),
                payload,
            )
        )
    return out
