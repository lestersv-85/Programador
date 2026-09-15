"""Cliente IMAP sobre imaplib (stdlib).

Sin dependencias externas a proposito: imaplib es feo pero lleva en la stdlib
veinte anos y no se va a romper en una actualizacion. La fabrica de conexiones
es inyectable para poder testear sin servidor.

Notas especificas de iCloud:
  * host imap.mail.me.com:993, TLS implicito.
  * Requiere contrasena especifica de app (xxxx-xxxx-xxxx-xxxx). La del Apple ID
    no funciona.
  * Carpetas del sistema en ingles aunque la interfaz este en espanol:
    "Sent Messages", "Deleted Messages", "Archive", "Junk".
  * Soporta UID MOVE, pero se deja un camino alternativo COPY+STORE+EXPUNGE.
"""

from __future__ import annotations

import imaplib
import logging
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterator, Sequence

from . import mutf7
from .config import Settings
from .mime import parse_message
from .models import Message

__all__ = [
    "MailboxClient",
    "ImapError",
    "quote_folder",
    "parse_fetch_response",
    "parse_flags_response",
]

logger = logging.getLogger(__name__)

ICLOUD_SYSTEM_FOLDERS = {
    "sent": "Sent Messages",
    "trash": "Deleted Messages",
    "archive": "Archive",
    "junk": "Junk",
    "drafts": "Drafts",
}

_LIST_RE = re.compile(rb'^\((?P<flags>[^)]*)\)\s+"(?P<delim>[^"]*)"\s+(?P<name>.+)$')
_UID_RE = re.compile(rb"UID\s+(\d+)")
_FLAGS_RE = re.compile(rb"FLAGS\s+\(([^)]*)\)")
_FETCH_BATCH = 50


class ImapError(RuntimeError):
    """Fallo de protocolo o de credenciales contra el servidor IMAP."""


def quote_folder(name: str) -> str:
    """Codifica a modified UTF-7 y entrecomilla, escapando comillas y barras."""
    encoded = mutf7.encode(name)
    escaped = encoded.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def parse_fetch_response(data: Sequence[object]) -> list[tuple[int, list[str], bytes]]:
    """Extrae (uid, flags, bytes crudos) de la respuesta de UID FETCH.

    imaplib devuelve una lista heterogenea: los literales llegan como tupla
    (cabecera, cuerpo) y el cierre de parentesis como bytes sueltos.
    """
    out: list[tuple[int, list[str], bytes]] = []
    for item in data:
        if not isinstance(item, tuple) or len(item) < 2:
            continue
        header, payload = item[0], item[1]
        if not isinstance(header, (bytes, bytearray)) or not isinstance(
            payload, (bytes, bytearray)
        ):
            continue
        uid_match = _UID_RE.search(header)
        if not uid_match:
            continue
        flags_match = _FLAGS_RE.search(header)
        flags = (
            [f.decode("ascii", "replace") for f in flags_match.group(1).split()]
            if flags_match
            else []
        )
        out.append((int(uid_match.group(1)), flags, bytes(payload)))
    return out


def parse_flags_response(data: Sequence[object]) -> dict[int, list[str]]:
    """Extrae {uid: flags} de un `UID FETCH ... (UID FLAGS)`.

    Sin BODY no hay literal, asi que el servidor responde con lineas sueltas de
    bytes en vez de tuplas: necesita su propio parseo.
    """
    out: dict[int, list[str]] = {}
    for item in data:
        raw = item[0] if isinstance(item, tuple) and item else item
        if not isinstance(raw, (bytes, bytearray)):
            continue
        uid_match = _UID_RE.search(raw)
        flags_match = _FLAGS_RE.search(raw)
        if not uid_match or not flags_match:
            continue
        out[int(uid_match.group(1))] = [
            f.decode("ascii", "replace") for f in flags_match.group(1).split()
        ]
    return out


def _check(result: tuple[str, object], action: str) -> object:
    status, data = result
    if status != "OK":
        raise ImapError(f"{action} fallo: {status} {data!r}")
    return data


class MailboxClient:
    """Conexion IMAP viva a un buzon. Usar como context manager."""

    def __init__(
        self,
        settings: Settings,
        connection_factory: Callable[[], imaplib.IMAP4] | None = None,
    ) -> None:
        self._settings = settings
        self._factory = connection_factory or self._default_factory
        self._conn: imaplib.IMAP4 | None = None
        self._selected: str | None = None
        self._selected_readonly: bool | None = None

    def _default_factory(self) -> imaplib.IMAP4:
        conn = imaplib.IMAP4_SSL(self._settings.imap_host, self._settings.imap_port)
        try:
            conn.login(self._settings.email, self._settings.app_password)
        except imaplib.IMAP4.error as exc:
            raise ImapError(
                "Login IMAP rechazado. Con iCloud esto casi siempre significa que la "
                "contrasena especifica de app es incorrecta o fue revocada: generala de "
                "nuevo en account.apple.com -> Contrasenas especificas de app."
            ) from exc
        return conn

    @property
    def connection(self) -> imaplib.IMAP4:
        if self._conn is None:
            raise ImapError("No hay conexion abierta; usa el cliente como context manager")
        return self._conn

    def __enter__(self) -> "MailboxClient":
        self._conn = self._factory()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._conn is None:
            return
        try:
            if self._selected:
                self._conn.close()
            self._conn.logout()
        except Exception:  # pragma: no cover - cierre best-effort
            logger.debug("Fallo al cerrar la conexion IMAP", exc_info=True)
        finally:
            self._conn = None
            self._selected = None

    # ---------------------------------------------------------------- carpetas

    def list_folders(self) -> list[str]:
        data = _check(self.connection.list(), "LIST")
        folders: list[str] = []
        for raw in data or []:
            if not isinstance(raw, (bytes, bytearray)):
                continue
            match = _LIST_RE.match(bytes(raw).strip())
            if not match:
                continue
            # utf-8 y no ascii: el protocolo manda modified UTF-7 (ASCII puro),
            # pero un servidor con UTF8=ACCEPT envia el nombre en UTF-8 crudo y
            # decodificarlo como ASCII lo convierte en basura irrecuperable.
            name = match.group("name").decode("utf-8", "replace").strip()
            if name.startswith('"') and name.endswith('"'):
                name = name[1:-1]
            folders.append(mutf7.decode(name.replace('\\"', '"')))
        return folders

    def folder_status(self, folder: str) -> dict[str, int]:
        data = _check(
            self.connection.status(quote_folder(folder), "(UIDVALIDITY UIDNEXT MESSAGES)"),
            f"STATUS {folder}",
        )
        raw = b" ".join(x for x in (data or []) if isinstance(x, (bytes, bytearray)))
        out: dict[str, int] = {}
        for key in ("UIDVALIDITY", "UIDNEXT", "MESSAGES"):
            match = re.search(key.encode() + rb"\s+(\d+)", raw)
            if match:
                out[key.lower()] = int(match.group(1))
        if "uidvalidity" not in out:
            raise ImapError(f"El servidor no devolvio UIDVALIDITY para {folder}: {raw!r}")
        return out

    def select(self, folder: str, *, readonly: bool = True) -> dict[str, int]:
        if self._selected == folder and self._selected_readonly == readonly:
            return self.folder_status(folder)
        _check(self.connection.select(quote_folder(folder), readonly=readonly), f"SELECT {folder}")
        self._selected = folder
        self._selected_readonly = readonly
        return self.folder_status(folder)

    def create_folder(self, folder: str) -> bool:
        status, _ = self.connection.create(quote_folder(folder))
        return status == "OK"

    # ---------------------------------------------------------------- busqueda

    def search_uids(self, *, min_uid: int | None = None, since_days: int | None = None) -> list[int]:
        """UIDs de la carpeta seleccionada, filtrados por UID minimo y/o antiguedad."""
        criteria: list[str] = []
        if min_uid is not None:
            criteria += ["UID", f"{max(1, min_uid)}:*"]
        if since_days is not None:
            since = datetime.now(timezone.utc) - timedelta(days=since_days)
            criteria += ["SINCE", since.strftime("%d-%b-%Y")]
        if not criteria:
            criteria = ["ALL"]
        data = _check(self.connection.uid("SEARCH", None, *criteria), "UID SEARCH")
        uids: list[int] = []
        for chunk in data or []:
            if isinstance(chunk, (bytes, bytearray)):
                uids.extend(int(token) for token in bytes(chunk).split() if token.isdigit())
        # `UID n:*` siempre devuelve al menos el ultimo mensaje aunque su UID sea
        # menor que n; filtramos para no reprocesarlo en cada sincronizacion.
        if min_uid is not None:
            uids = [uid for uid in uids if uid >= min_uid]
        return sorted(set(uids))

    # ------------------------------------------------------------------ fetch

    def fetch_messages(
        self, uids: Sequence[int], *, folder: str, uidvalidity: int
    ) -> Iterator[Message]:
        """Descarga y parsea mensajes en lotes, sin marcarlos como leidos.

        BODY.PEEK[] en lugar de RFC822: leer el correo desde aqui no debe cambiar
        lo que ves como no leido en el iPhone.
        """
        account = self._settings.email
        for start in range(0, len(uids), _FETCH_BATCH):
            batch = uids[start : start + _FETCH_BATCH]
            uid_set = ",".join(str(u) for u in batch)
            data = _check(
                self.connection.uid("FETCH", uid_set, "(UID FLAGS BODY.PEEK[])"),
                f"UID FETCH {uid_set}",
            )
            for uid, flags, raw in parse_fetch_response(data or []):
                try:
                    yield parse_message(
                        raw,
                        account=account,
                        folder=folder,
                        uid=uid,
                        uidvalidity=uidvalidity,
                        flags=flags,
                    )
                except Exception:
                    # Un correo malformado no puede tumbar una sincronizacion de 10k.
                    logger.warning("No se pudo parsear el UID %s de %s", uid, folder, exc_info=True)

    def fetch_flags(self, uids: Sequence[int]) -> dict[int, list[str]]:
        """Solo los flags de los UID dados, sin descargar cuerpos.

        Es lo que permite enterarse de que marcaste un correo como leido en el
        iPhone sin volver a bajar el buzon entero.
        """
        flags: dict[int, list[str]] = {}
        for start in range(0, len(uids), _FETCH_BATCH):
            batch = uids[start : start + _FETCH_BATCH]
            uid_set = ",".join(str(u) for u in batch)
            data = _check(
                self.connection.uid("FETCH", uid_set, "(UID FLAGS)"), f"UID FETCH FLAGS {uid_set}"
            )
            flags.update(parse_flags_response(data or []))
        return flags

    def fetch_raw(self, uid: int) -> bytes:
        data = _check(self.connection.uid("FETCH", str(uid), "(BODY.PEEK[])"), f"UID FETCH {uid}")
        parsed = parse_fetch_response(data or [])
        if not parsed:
            raise ImapError(f"El servidor no devolvio cuerpo para el UID {uid}")
        return parsed[0][2]

    # ----------------------------------------------------------------- acciones

    def move_message(self, uid: int, destination: str) -> str:
        """Mueve un mensaje. Devuelve el metodo usado ('MOVE' o 'COPY+EXPUNGE')."""
        dest = quote_folder(destination)
        status, _ = self.connection.uid("MOVE", str(uid), dest)
        if status == "OK":
            return "MOVE"
        _check(self.connection.uid("COPY", str(uid), dest), f"UID COPY -> {destination}")
        _check(self.connection.uid("STORE", str(uid), "+FLAGS", r"(\Deleted)"), "UID STORE Deleted")
        _check(self.connection.expunge(), "EXPUNGE")
        return "COPY+EXPUNGE"

    def set_flags(self, uid: int, flags: Sequence[str], *, add: bool = True) -> list[str]:
        operation = "+FLAGS" if add else "-FLAGS"
        flag_list = " ".join(flags)
        _check(
            self.connection.uid("STORE", str(uid), operation, f"({flag_list})"),
            f"UID STORE {operation}",
        )
        return list(flags)


@contextmanager
def open_mailbox(
    settings: Settings, connection_factory: Callable[[], imaplib.IMAP4] | None = None
) -> Iterator[MailboxClient]:
    client = MailboxClient(settings, connection_factory)
    with client:
        yield client
