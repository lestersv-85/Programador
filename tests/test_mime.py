from __future__ import annotations

from email.message import EmailMessage

from programador.mime import attachment_payloads, decode_mime_header, html_to_text, parse_message


def _build(subject="Asunto", body="Hola", html=None, attachments=(), headers=None) -> bytes:
    msg = EmailMessage()
    msg["From"] = "Proveedor Solar <ventas@proveedor.example>"
    msg["To"] = "Lester <lestersv@icloud.com>"
    msg["Cc"] = "copia@example.com"
    msg["Subject"] = subject
    msg["Date"] = "Tue, 01 Sep 2026 10:30:00 +0200"
    msg["Message-ID"] = "<abc123@proveedor.example>"
    for key, value in (headers or {}).items():
        msg[key] = value
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    for filename, payload in attachments:
        msg.add_attachment(payload, maintype="application", subtype="pdf", filename=filename)
    return msg.as_bytes()


def _parse(raw: bytes):
    return parse_message(raw, account="lestersv@icloud.com", folder="INBOX", uid=7, uidvalidity=99)


def test_parse_basic_headers_and_body():
    message = _parse(_build(subject="Factura 114", body="Adjunto la factura."))
    assert message.subject == "Factura 114"
    assert message.from_addr == "ventas@proveedor.example"
    assert message.from_name == "Proveedor Solar"
    assert message.to_addrs == ["lestersv@icloud.com"]
    assert message.cc_addrs == ["copia@example.com"]
    assert "Adjunto la factura." in message.body_text
    assert message.message_id == "<abc123@proveedor.example>"
    assert message.key == "lestersv@icloud.com|INBOX|99|7"


def test_date_is_normalized_to_utc():
    message = _parse(_build())
    assert message.date_utc is not None
    assert message.date_utc.tzinfo is not None
    assert message.date_utc.hour == 8, "10:30 +0200 son las 08:30 UTC"


def test_missing_or_broken_date_does_not_raise():
    raw = _build().replace(b"Date: Tue, 01 Sep 2026 10:30:00 +0200", b"Date: no-es-una-fecha")
    assert _parse(raw).date_utc is None


def test_attachments_are_listed_with_size_but_body_stays_clean():
    message = _parse(_build(attachments=[("factura-114.pdf", b"%PDF-1.4 contenido")]))
    assert [a.filename for a in message.attachments] == ["factura-114.pdf"]
    assert message.attachments[0].size == len(b"%PDF-1.4 contenido")
    assert message.has_attachments
    assert "%PDF" not in message.body_text


def test_attachment_payloads_returns_bytes():
    raw = _build(attachments=[("bl.pdf", b"bytes-del-bl")])
    payloads = attachment_payloads(raw)
    assert payloads[0][0] == "bl.pdf"
    assert payloads[0][2] == b"bytes-del-bl"


def test_encoded_subject_is_decoded():
    message = _parse(_build(subject="Facturación de septiembre — envío"))
    assert message.subject == "Facturación de septiembre — envío"


def test_decode_mime_header_survives_garbage():
    assert decode_mime_header("=?utf-8?B?bm90LWJhc2U2NA?=") != ""
    assert decode_mime_header(None) == ""


def test_html_only_message_falls_back_to_stripped_text():
    msg = EmailMessage()
    msg["From"] = "n@example.com"
    msg["To"] = "lestersv@icloud.com"
    msg["Subject"] = "Solo HTML"
    msg.set_content(
        "<html><head><style>p{color:red}</style></head>"
        "<body><p>Importe&nbsp;total: 1.200&euro;</p><script>alert(1)</script></body></html>",
        subtype="html",
    )
    message = _parse(msg.as_bytes())
    assert "Importe total: 1.200€" in message.body_text
    assert "alert(1)" not in message.body_text
    assert "<p>" not in message.body_text


def test_html_to_text_inserts_line_breaks():
    assert html_to_text("<p>uno</p><p>dos</p>").splitlines()[0].strip() == "uno"


def test_snippet_is_bounded():
    message = _parse(_build(body="palabra " * 500))
    assert 0 < len(message.snippet) <= 320
