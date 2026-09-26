"""Envio SMTP por iCloud (smtp.mail.me.com:587, STARTTLS).

Misma contrasena especifica de app que IMAP. El From tiene que ser tu direccion
de iCloud o uno de tus alias verificados en Apple: iCloud rechaza cualquier otro.
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import Callable, Sequence

from .config import Settings

__all__ = ["SmtpError", "build_message", "send_message"]


class SmtpError(RuntimeError):
    """Fallo de envio SMTP."""


def build_message(
    settings: Settings,
    *,
    to: Sequence[str],
    subject: str,
    body: str,
    cc: Sequence[str] | None = None,
    bcc: Sequence[str] | None = None,
    from_addr: str | None = None,
    in_reply_to: str | None = None,
    attachments: Sequence[Path] | None = None,
) -> EmailMessage:
    if not to:
        raise SmtpError("Hace falta al menos un destinatario")
    message = EmailMessage()
    message["From"] = from_addr or settings.email
    message["To"] = ", ".join(to)
    if cc:
        message["Cc"] = ", ".join(cc)
    if bcc:
        message["Bcc"] = ", ".join(bcc)
    message["Subject"] = subject
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
        message["References"] = in_reply_to
    message.set_content(body)

    for path in attachments or []:
        file_path = Path(path)
        if not file_path.is_file():
            raise SmtpError(f"Adjunto no encontrado: {file_path}")
        message.add_attachment(
            file_path.read_bytes(),
            maintype="application",
            subtype="octet-stream",
            filename=file_path.name,
        )
    return message


def send_message(
    settings: Settings,
    message: EmailMessage,
    *,
    connection_factory: Callable[[], smtplib.SMTP] | None = None,
) -> dict[str, object]:
    def default_factory() -> smtplib.SMTP:
        server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30)
        server.starttls()
        server.login(settings.email, settings.app_password)
        return server

    factory = connection_factory or default_factory
    try:
        server = factory()
    except smtplib.SMTPAuthenticationError as exc:
        raise SmtpError(
            "SMTP rechazo las credenciales. Regenera la contrasena especifica de app "
            "en account.apple.com."
        ) from exc
    try:
        refused = server.send_message(message)
    except smtplib.SMTPException as exc:
        raise SmtpError(f"Fallo el envio: {exc}") from exc
    finally:
        try:
            server.quit()
        except Exception:  # pragma: no cover
            pass

    return {
        "sent": True,
        "from": message["From"],
        "to": message["To"],
        "subject": message["Subject"],
        "refused_recipients": list(refused or {}),
    }
