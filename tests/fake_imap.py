"""Servidor IMAP4rev1 minimo, en un hilo, para tests de integracion.

Existe por una razon concreta: el resto de la suite prueba el cliente con dobles
de Python, asi que nunca ejercita el trozo mas fragil del codigo -- el dialogo
real con imaplib, el parseo de literales {n} en UID FETCH y el formato de LIST y
STATUS. Este servidor habla el protocolo de verdad por un socket, asi que los
tests que lo usan corren el MailboxClient autentico sobre imaplib autentico.

No pretende ser un servidor conforme: implementa solo los comandos que usa este
proyecto.
"""

from __future__ import annotations

import socket
import threading
from dataclasses import dataclass, field
from email.message import EmailMessage

__all__ = ["FakeImapServer", "FakeMessage", "build_eml"]


def build_eml(subject: str, body: str = "cuerpo", to: str = "lestersv@icloud.com") -> bytes:
    msg = EmailMessage()
    msg["From"] = "Proveedor <ventas@proveedor.example>"
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = "Tue, 01 Sep 2026 10:30:00 +0200"
    msg["Message-ID"] = f"<{abs(hash(subject))}@proveedor.example>"
    msg.set_content(body)
    return msg.as_bytes()


@dataclass
class FakeMessage:
    uid: int
    raw: bytes
    flags: list[str] = field(default_factory=list)


@dataclass
class FakeFolder:
    name: str
    uidvalidity: int = 100
    messages: list[FakeMessage] = field(default_factory=list)

    @property
    def uidnext(self) -> int:
        return max((m.uid for m in self.messages), default=0) + 1


class FakeImapServer:
    """Uso: `with FakeImapServer(folders) as server: ... server.port`."""

    def __init__(
        self,
        folders: list[FakeFolder] | None = None,
        *,
        supports_move: bool = True,
        reject_login: bool = False,
        utf8_folder_names: bool = False,
    ) -> None:
        self.folders = {f.name: f for f in (folders or [])}
        self.supports_move = supports_move
        self.reject_login = reject_login
        self.utf8_folder_names = utf8_folder_names
        self.commands: list[str] = []
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(4)
        # Cerrar el socket no siempre despierta un accept() bloqueado en Linux:
        # con timeout el bucle comprueba la bandera de parada y sale al momento.
        self._socket.settimeout(0.1)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    @property
    def port(self) -> int:
        return self._socket.getsockname()[1]

    def __enter__(self) -> "FakeImapServer":
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        try:
            self._socket.close()
        except OSError:
            pass
        self._thread.join(timeout=3)
        if self._thread.is_alive():  # pragma: no cover
            raise RuntimeError("El servidor IMAP falso no se detuvo a tiempo")

    # ------------------------------------------------------------------ bucle

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._socket.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            conn.settimeout(5)
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        stream = conn.makefile("rwb")
        selected: FakeFolder | None = None
        try:
            stream.write(b"* OK [CAPABILITY IMAP4REV1] Fake IMAP listo\r\n")
            stream.flush()
            while not self._stop.is_set():
                line = stream.readline()
                if not line:
                    return
                selected = self._dispatch(stream, line.decode("utf-8", "replace"), selected)
                if selected is StopIteration:  # type: ignore[comparison-overlap]
                    return
        except (OSError, ValueError):
            return
        finally:
            try:
                stream.close()
                conn.close()
            except OSError:
                pass

    # --------------------------------------------------------------- comandos

    def _dispatch(self, stream, line: str, selected: FakeFolder | None):
        parts = line.strip().split(" ")
        tag, command = parts[0], (parts[1].upper() if len(parts) > 1 else "")
        args = parts[2:]
        self.commands.append(line.strip())

        def send(text: str) -> None:
            stream.write(text.encode("utf-8") + b"\r\n")
            stream.flush()

        def send_bytes(payload: bytes) -> None:
            stream.write(payload)
            stream.flush()

        if command == "CAPABILITY":
            caps = "IMAP4REV1 UIDPLUS" + (" MOVE" if self.supports_move else "")
            send(f"* CAPABILITY {caps}")
            send(f"{tag} OK CAPABILITY completado")

        elif command == "LOGIN":
            if self.reject_login:
                send(f"{tag} NO [AUTHENTICATIONFAILED] credenciales invalidas")
            else:
                send(f"{tag} OK LOGIN completado")

        elif command == "LIST":
            from programador import mutf7

            for name in self.folders:
                # Un servidor conforme envia el nombre en modified UTF-7; con
                # utf8_folder_names=True imitamos a los que responden UTF-8 crudo.
                wire = name if self.utf8_folder_names else mutf7.encode(name)
                send(f'* LIST (\\HasNoChildren) "/" "{wire}"')
            send(f"{tag} OK LIST completado")

        elif command == "STATUS":
            folder = self._folder(args[0] if args else "")
            if folder is None:
                send(f"{tag} NO carpeta inexistente")
            else:
                send(
                    f'* STATUS "{folder.name}" (UIDVALIDITY {folder.uidvalidity} '
                    f"UIDNEXT {folder.uidnext} MESSAGES {len(folder.messages)})"
                )
                send(f"{tag} OK STATUS completado")

        elif command in ("SELECT", "EXAMINE"):
            folder = self._folder(args[0] if args else "")
            if folder is None:
                send(f"{tag} NO carpeta inexistente")
            else:
                send(f"* {len(folder.messages)} EXISTS")
                send("* 0 RECENT")
                send(f"* OK [UIDVALIDITY {folder.uidvalidity}] UIDs validos")
                send(f"* OK [UIDNEXT {folder.uidnext}] siguiente UID")
                suffix = " [READ-ONLY]" if command == "EXAMINE" else " [READ-WRITE]"
                send(f"{tag} OK{suffix} {command} completado")
                return folder

        elif command == "UID":
            return self._dispatch_uid(tag, args, selected, send, send_bytes)

        elif command == "EXPUNGE":
            send(f"{tag} OK EXPUNGE completado")

        elif command == "CLOSE":
            send(f"{tag} OK CLOSE completado")
            return None

        elif command == "LOGOUT":
            send("* BYE hasta luego")
            send(f"{tag} OK LOGOUT completado")
            return StopIteration  # type: ignore[return-value]

        else:
            send(f"{tag} BAD comando no soportado: {command}")

        return selected

    def _dispatch_uid(self, tag, args, selected, send, send_bytes):
        subcommand = args[0].upper() if args else ""
        rest = args[1:]

        if selected is None:
            send(f"{tag} BAD no hay carpeta seleccionada")
            return selected

        if subcommand == "SEARCH":
            uids = [m.uid for m in selected.messages]
            criteria = [a.upper() for a in rest]
            if "UID" in criteria:
                spec = rest[criteria.index("UID") + 1]
                low = int(spec.split(":")[0])
                matching = [uid for uid in uids if uid >= low]
                # Un servidor real responde al menos con el ultimo mensaje aunque
                # su UID sea menor que el pedido: es el caso que rompe las
                # sincronizaciones ingenuas, asi que lo reproducimos.
                uids = matching or ([max(uids)] if uids else [])
            send("* SEARCH " + " ".join(str(u) for u in uids))
            send(f"{tag} OK UID SEARCH completado")

        elif subcommand == "FETCH":
            requested = _parse_uid_set(rest[0] if rest else "")
            items = " ".join(rest[1:]).upper()
            wants_body = "BODY" in items
            for index, message in enumerate(selected.messages, start=1):
                if message.uid not in requested:
                    continue
                flags = " ".join(message.flags)
                if not wants_body:
                    # Sin BODY no hay literal: linea suelta, como un servidor real.
                    send(f"* {index} FETCH (UID {message.uid} FLAGS ({flags}))")
                    continue
                header = (
                    f"* {index} FETCH (UID {message.uid} FLAGS ({flags}) "
                    f"BODY[] {{{len(message.raw)}}}\r\n"
                ).encode()
                send_bytes(header + message.raw + b")\r\n")
            send(f"{tag} OK UID FETCH completado")

        elif subcommand == "STORE":
            requested = _parse_uid_set(rest[0] if rest else "")
            operation = rest[1] if len(rest) > 1 else "+FLAGS"
            flags = [f.strip("()") for f in rest[2:] if f.strip("()")]
            for message in selected.messages:
                if message.uid not in requested:
                    continue
                if operation.startswith("+"):
                    message.flags = sorted(set(message.flags) | set(flags))
                else:
                    message.flags = [f for f in message.flags if f not in flags]
            send(f"{tag} OK UID STORE completado")

        elif subcommand == "MOVE":
            if not self.supports_move:
                send(f"{tag} NO [CANNOT] MOVE no soportado")
                return selected
            self._relocate(selected, rest, remove=True)
            send(f"{tag} OK UID MOVE completado")

        elif subcommand == "COPY":
            self._relocate(selected, rest, remove=False)
            send(f"{tag} OK UID COPY completado")

        else:
            send(f"{tag} BAD subcomando UID no soportado: {subcommand}")

        return selected

    def _relocate(self, source: FakeFolder, rest: list[str], *, remove: bool) -> None:
        requested = _parse_uid_set(rest[0] if rest else "")
        destination = self._folder(rest[1] if len(rest) > 1 else "")
        if destination is None:
            return
        moving = [m for m in source.messages if m.uid in requested]
        for message in moving:
            destination.messages.append(FakeMessage(destination.uidnext, message.raw, list(message.flags)))
        if remove:
            source.messages = [m for m in source.messages if m.uid not in requested]

    def _folder(self, raw_name: str) -> FakeFolder | None:
        from programador import mutf7

        name = mutf7.decode(raw_name.strip().strip('"').replace('\\"', '"'))
        return self.folders.get(name)


def _parse_uid_set(spec: str) -> set[int]:
    uids: set[int] = set()
    for chunk in spec.strip().split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" in chunk:
            low, _, high = chunk.partition(":")
            if high == "*":
                uids.update(range(int(low), int(low) + 1000))
            else:
                uids.update(range(int(low), int(high) + 1))
        elif chunk.isdigit():
            uids.add(int(chunk))
    return uids
