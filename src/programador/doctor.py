"""Diagnostico de primer arranque.

La primera conexion real contra iCloud es donde se pierde el tiempo: la
contrasena de app mal copiada, el puerto bloqueado por la red de la oficina, la
carpeta configurada con otro nombre. Este modulo comprueba la cadena entera en
orden y, cuando algo falla, dice exactamente que hacer en vez de escupir una
excepcion de imaplib.
"""

from __future__ import annotations

import argparse
import imaplib
import os
import smtplib
import socket
import sys
from dataclasses import dataclass, field
from typing import Callable, Sequence

from .config import ConfigError, Settings, load_routing_config, load_settings
from .imap_client import ImapError, MailboxClient
from .store import Store

__all__ = ["Check", "run_diagnostics", "main"]


@dataclass(slots=True)
class Check:
    nombre: str
    ok: bool
    detalle: str = ""
    remedio: str = ""

    def to_dict(self) -> dict[str, object]:
        return {"check": self.nombre, "ok": self.ok, "detalle": self.detalle, "remedio": self.remedio}


@dataclass(slots=True)
class Diagnostics:
    checks: list[Check] = field(default_factory=list)
    red_comprobada: bool = False

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    @property
    def avisos(self) -> list[Check]:
        """Comprobaciones que pasan pero tienen algo que decir."""
        return [c for c in self.checks if c.ok and c.remedio]

    def _resumen(self) -> str:
        fallos = [c for c in self.checks if not c.ok]
        if fallos:
            return (
                f"{len(fallos)} comprobacion(es) fallan. Empieza por '{fallos[0].nombre}': "
                f"{fallos[0].remedio or fallos[0].detalle}"
            )
        base = (
            "Todo listo: el motor puede leer y enviar."
            if self.red_comprobada
            else "Configuracion y base de datos correctas. La conexion con iCloud NO se ha "
            "comprobado: vuelve a ejecutarlo sin --sin-red."
        )
        if self.avisos:
            base += f" Con {len(self.avisos)} aviso(s): " + "; ".join(
                f"{c.nombre} - {c.remedio}" for c in self.avisos
            )
        return base

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "red_comprobada": self.red_comprobada,
            "avisos": [c.nombre for c in self.avisos],
            "checks": [c.to_dict() for c in self.checks],
            "resumen": self._resumen(),
        }


APP_PASSWORD_REMEDY = (
    "Genera una contrasena especifica de app en account.apple.com -> Inicio de sesion "
    "y seguridad -> Contrasenas especificas de app, y ponla en PROGRAMADOR_APP_PASSWORD. "
    "La contrasena normal del Apple ID no funciona."
)


def _check_tcp(host: str, port: int, etiqueta: str, *, timeout: float = 8.0) -> Check:
    try:
        with socket.create_connection((host, port), timeout):
            return Check(etiqueta, True, f"{host}:{port} accesible")
    except OSError as exc:
        return Check(
            etiqueta,
            False,
            f"{host}:{port} inaccesible ({type(exc).__name__}: {exc})",
            "Tu red bloquea el puerto o no hay salida a internet. En una red corporativa "
            "o tras un proxy, ese puerto suele estar cerrado: pruebalo desde otra red.",
        )


def run_diagnostics(
    env: dict[str, str] | None = None,
    *,
    imap_factory: Callable[[Settings], MailboxClient] | None = None,
    smtp_factory: Callable[[Settings], smtplib.SMTP] | None = None,
    skip_network: bool = False,
) -> Diagnostics:
    """Ejecuta la cadena completa de comprobaciones y se detiene donde deja de tener sentido."""
    resultado = Diagnostics()

    # 1. Variables de entorno.
    try:
        settings = load_settings(env if env is not None else dict(os.environ))
    except ConfigError as exc:
        resultado.checks.append(
            Check("entorno", False, str(exc), "Copia .env.example a .env, rellenalo y cargalo "
                  "con `set -a && . ./.env && set +a`.")
        )
        return resultado
    resultado.checks.append(Check("entorno", True, f"cuenta {settings.email}"))

    # 2. Configuracion de enrutado.
    try:
        routing = load_routing_config(settings.config_path)
    except ConfigError as exc:
        resultado.checks.append(
            Check("configuracion", False, str(exc),
                  "Copia config/entities.example.yaml a config/entities.yaml y edita los CAMBIAME.")
        )
        return resultado
    pendientes = sum(
        1
        for rule in routing.rules
        for valores in (rule.to_addr, rule.from_domain, rule.from_addr)
        for valor in valores
        if "CAMBIAME" in valor.upper()
    )
    resultado.checks.append(
        Check(
            "configuracion",
            True,
            f"{len(routing.entities)} entidades, {len(routing.rules)} reglas"
            + (f", {pendientes} marcador(es) CAMBIAME sin sustituir" if pendientes else ""),
            "Sustituye los CAMBIAME por tus alias y dominios reales o el enrutado no servira "
            "de nada." if pendientes else "",
        )
    )

    # 3. Base de datos.
    try:
        Store(settings.db_path).close()
        resultado.checks.append(Check("base_de_datos", True, f"{settings.db_path} escribible"))
    except Exception as exc:
        resultado.checks.append(
            Check("base_de_datos", False, f"{type(exc).__name__}: {exc}",
                  f"Comprueba los permisos del directorio de {settings.db_path}.")
        )
        return resultado

    if skip_network:
        return resultado
    resultado.red_comprobada = True

    # 4-6. IMAP: puerto, login, carpetas.
    tcp_imap = _check_tcp(settings.imap_host, settings.imap_port, "imap_puerto")
    resultado.checks.append(tcp_imap)
    if not tcp_imap.ok:
        return resultado

    try:
        client = (imap_factory(settings) if imap_factory else MailboxClient(settings))
        with client as conectado:
            resultado.checks.append(Check("imap_login", True, "autenticacion aceptada"))
            carpetas = conectado.list_folders()
    except (ImapError, imaplib.IMAP4.error) as exc:
        resultado.checks.append(Check("imap_login", False, str(exc), APP_PASSWORD_REMEDY))
        return resultado
    except OSError as exc:
        resultado.checks.append(
            Check("imap_login", False, f"{type(exc).__name__}: {exc}", "Fallo de red durante el login.")
        )
        return resultado

    faltan = [f for f in settings.folders if f not in carpetas]
    resultado.checks.append(
        Check(
            "carpetas",
            not faltan,
            f"{len(carpetas)} carpetas en el buzon"
            + (f"; no existen: {faltan}" if faltan else ""),
            f"PROGRAMADOR_FOLDERS nombra carpetas que no existen. Las reales son: {carpetas}. "
            "En iCloud las del sistema estan en ingles aunque la interfaz este en espanol."
            if faltan
            else "",
        )
    )

    # 7-8. SMTP: puerto y login (sin enviar nada).
    tcp_smtp = _check_tcp(settings.smtp_host, settings.smtp_port, "smtp_puerto")
    resultado.checks.append(tcp_smtp)
    if not tcp_smtp.ok:
        return resultado

    try:
        if smtp_factory:
            server = smtp_factory(settings)
        else:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15)
            server.starttls()
            server.login(settings.email, settings.app_password)
        try:
            server.quit()
        except Exception:  # pragma: no cover
            pass
        resultado.checks.append(Check("smtp_login", True, "autenticacion aceptada (sin enviar nada)"))
    except smtplib.SMTPException as exc:
        resultado.checks.append(Check("smtp_login", False, str(exc), APP_PASSWORD_REMEDY))
    except OSError as exc:
        resultado.checks.append(
            Check("smtp_login", False, f"{type(exc).__name__}: {exc}", "Fallo de red en el envio.")
        )

    return resultado


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="programador-doctor",
        description="Comprueba que el motor puede leer y enviar correo por iCloud.",
    )
    parser.add_argument(
        "--sin-red", action="store_true", help="Solo comprueba configuracion y base de datos"
    )
    args = parser.parse_args(argv)

    resultado = run_diagnostics(skip_network=args.sin_red)
    for check in resultado.checks:
        marca = "OK  " if check.ok else "FALLA"
        print(f"[{marca}] {check.nombre}: {check.detalle}")
        if check.remedio:
            print(f"         -> {check.remedio}")
    print()
    print(resultado.to_dict()["resumen"])
    return 0 if resultado.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
