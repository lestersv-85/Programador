"""Modified UTF-7 (RFC 3501 5.1.3): el encoding de nombres de carpeta en IMAP.

imaplib no lo implementa. Sin esto, una carpeta llamada "Facturacion" con
tilde -- o cualquiera de las carpetas en espanol que uses en iCloud -- llega
como "Factura&AMM-i&APM-n" y resulta ilegible.
"""

from __future__ import annotations

import base64

__all__ = ["encode", "decode"]


def _b64_encode(text: str) -> str:
    raw = base64.b64encode(text.encode("utf-16-be")).decode("ascii")
    return raw.rstrip("=").replace("/", ",")


def _b64_decode(chunk: str) -> str:
    padded = chunk.replace(",", "/")
    padded += "=" * (-len(padded) % 4)
    return base64.b64decode(padded).decode("utf-16-be")


def encode(name: str) -> str:
    """Nombre de carpeta legible -> modified UTF-7."""
    out: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            out.append("&" + _b64_encode("".join(buffer)) + "-")
            buffer.clear()

    for char in name:
        if char == "&":
            flush()
            out.append("&-")
        elif "\x20" <= char <= "\x7e":
            flush()
            out.append(char)
        else:
            buffer.append(char)
    flush()
    return "".join(out)


def decode(name: str) -> str:
    """Modified UTF-7 -> nombre de carpeta legible."""
    out: list[str] = []
    index = 0
    length = len(name)
    while index < length:
        char = name[index]
        if char != "&":
            out.append(char)
            index += 1
            continue
        end = name.find("-", index + 1)
        if end == -1:
            # Secuencia truncada: se devuelve tal cual antes que reventar.
            out.append(name[index:])
            break
        chunk = name[index + 1 : end]
        if chunk == "":
            out.append("&")
        else:
            try:
                out.append(_b64_decode(chunk))
            except Exception:
                out.append(name[index : end + 1])
        index = end + 1
    return "".join(out)
