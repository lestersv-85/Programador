"""Servidor MCP: la superficie que ve Claude.

Reparto de trabajo deliberado:
  * Este servidor hace lo DETERMINISTA y barato -- IMAP, SQLite, reglas, FTS.
  * Claude hace lo SEMANTICO -- leer una factura y sacar importe, vencimiento,
    incoterm -- y devuelve el resultado por `registrar_extraccion`.

Por eso el proyecto no llama a ninguna API de IA ni paga inferencia aparte: el
modelo ya esta al otro lado de esta tuberia.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import __version__
from .config import ConfigError, RoutingConfig, Settings, load_routing_config, load_settings
from .imap_client import ICLOUD_SYSTEM_FOLDERS, ImapError, MailboxClient
from .mime import attachment_payloads
from .models import Extraction
from .routing import Router
from .smtp_client import SmtpError, build_message, send_message
from .store import Store
from .sync import sync_all

logger = logging.getLogger(__name__)

server = MCPServer(
    name="programador",
    version=__version__,
    instructions=(
        "Motor de triaje documental multi-entidad sobre el buzon iCloud de Lester "
        "Sinconegui. Cada correo se enruta de forma determinista a una entidad "
        "(rambaid, ecme_forego, forego_intl, sand, personal) y se marca con tipos "
        "documentales sugeridos (invoice, proforma, bill_of_lading, customs...). "
        "Flujo tipico: sincronizar -> triaje -> leer -> registrar_extraccion. "
        "Las pistas doc_types son heuristicas de palabra clave: confirmalas leyendo "
        "el correo antes de dar un dato por bueno. 'enviar' exige confirmar=True."
    ),
)

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class _Context:
    """Inicializacion perezosa: si falta configuracion, el servidor arranca igual
    y cada herramienta explica el problema en vez de morir al abrir."""

    def __init__(self) -> None:
        self._settings: Settings | None = None
        self._routing: RoutingConfig | None = None
        self._router: Router | None = None
        self._store: Store | None = None
        self.error: str | None = None

    def load(self) -> bool:
        if self._settings and self._router and self._store:
            return True
        try:
            self._settings = load_settings(dict(os.environ))
            self._routing = load_routing_config(self._settings.config_path)
            self._router = Router(self._routing)
            self._store = Store(self._settings.db_path)
            self.error = None
            return True
        except ConfigError as exc:
            self.error = str(exc)
            return False
        except Exception as exc:  # pragma: no cover
            self.error = f"{type(exc).__name__}: {exc}"
            return False

    @property
    def settings(self) -> Settings:
        assert self._settings is not None
        return self._settings

    @property
    def router(self) -> Router:
        assert self._router is not None
        return self._router

    @property
    def store(self) -> Store:
        assert self._store is not None
        return self._store


_ctx = _Context()


def _guard() -> dict[str, Any] | None:
    if _ctx.load():
        return None
    return {
        "ok": False,
        "error": _ctx.error,
        "remedio": (
            "Revisa las variables de entorno (.env) y que config/entities.yaml exista. "
            "Parte de config/entities.example.yaml."
        ),
    }


def _since_iso(days: int | None) -> str | None:
    if days is None:
        return None
    return (datetime.now(timezone.utc) - timedelta(days=max(0, days))).isoformat(timespec="seconds")


def _brief(message: dict[str, Any]) -> dict[str, Any]:
    """Version ligera de un mensaje: lo que basta para decidir si vale la pena abrirlo."""
    return {
        "key": message["key"],
        "fecha": message["date_utc"],
        "de": f"{message['from_name']} <{message['from_addr']}>".strip(),
        "asunto": message["subject"],
        "entidad": message["entity"],
        "motivo_enrutado": message["routing_reason"],
        "doc_types": message["doc_types"],
        "adjuntos": [a["filename"] for a in message["attachments"]],
        "resumen": message["snippet"],
    }


# --------------------------------------------------------------------- lectura


@server.tool(description="Estado del motor: configuracion (sin secretos), volumen indexado y estado de sincronizacion por carpeta.")
def estado() -> dict[str, Any]:
    error = _guard()
    if error:
        return error
    settings = _ctx.settings
    store = _ctx.store
    sync_states = []
    for folder in settings.folders:
        state = store.get_sync_state(settings.email, folder)
        sync_states.append(
            {
                "carpeta": folder,
                "sincronizada": state is not None,
                "ultimo_uid": state.last_uid if state else None,
                "ultima_sync": state.last_sync_at if state else None,
            }
        )
    return {
        "ok": True,
        "version": __version__,
        "configuracion": settings.redacted(),
        "entidades": [
            {"id": e.id, "nombre": e.name, "jurisdiccion": e.jurisdiction}
            for e in _ctx.router.config.entities
        ],
        "reglas_de_enrutado": len(_ctx.router.config.rules),
        "tipos_documentales": sorted(_ctx.router.config.doc_type_hints),
        "sincronizacion": sync_states,
        "estadisticas": store.stats(),
    }


@server.tool(description="Vista de triaje: cuanto ha entrado en los ultimos N dias, repartido por entidad y tipo documental, con los correos mas recientes de cada entidad.")
def triaje(dias: int = 7, por_entidad_limite: int = 5) -> dict[str, Any]:
    error = _guard()
    if error:
        return error
    store = _ctx.store
    since = _since_iso(dias)
    stats = store.stats(since=since)
    desglose = {}
    for entity_id in stats["by_entity"]:
        mensajes = store.list_messages(entity=entity_id, since=since, limit=por_entidad_limite)
        desglose[entity_id] = {
            "total": stats["by_entity"][entity_id],
            "recientes": [_brief(m) for m in mensajes],
        }
    return {
        "ok": True,
        "ventana_dias": dias,
        "total": stats["total_messages"],
        "por_tipo_documental": stats["by_doc_type"],
        "extracciones_pendientes": stats["pending_extractions"],
        "por_entidad": desglose,
    }


@server.tool(description="Lista correos indexados filtrando por entidad, tipo documental, antiguedad, no leidos o presencia de adjuntos. Devuelve resumenes, no cuerpos completos.")
def listar(
    entidad: str | None = None,
    doc_type: str | None = None,
    dias: int | None = 30,
    solo_no_leidos: bool = False,
    solo_con_adjuntos: bool = False,
    limite: int = 50,
) -> dict[str, Any]:
    error = _guard()
    if error:
        return error
    mensajes = _ctx.store.list_messages(
        entity=entidad,
        doc_type=doc_type,
        since=_since_iso(dias),
        unread_only=solo_no_leidos,
        with_attachments=True if solo_con_adjuntos else None,
        limit=limite,
    )
    return {"ok": True, "total": len(mensajes), "mensajes": [_brief(m) for m in mensajes]}


@server.tool(description="Busqueda de texto completo sobre asunto, cuerpo, remitente y nombres de adjuntos de los correos indexados. Acepta texto libre en espanol o ingles.")
def buscar(
    consulta: str,
    entidad: str | None = None,
    doc_type: str | None = None,
    limite: int = 30,
) -> dict[str, Any]:
    error = _guard()
    if error:
        return error
    mensajes = _ctx.store.search(consulta, entity=entidad, doc_type=doc_type, limit=limite)
    return {
        "ok": True,
        "consulta": consulta,
        "total": len(mensajes),
        "mensajes": [_brief(m) for m in mensajes],
    }


@server.tool(description="Devuelve un correo completo por su clave, con cuerpo integro y metadatos de adjuntos. Usalo antes de registrar cualquier extraccion.")
def leer(clave: str, max_caracteres: int = 20000) -> dict[str, Any]:
    error = _guard()
    if error:
        return error
    message = _ctx.store.get_message(clave)
    if not message:
        return {"ok": False, "error": f"No hay ningun mensaje con la clave '{clave}'"}
    cuerpo = message["body_text"]
    truncado = len(cuerpo) > max_caracteres
    return {
        "ok": True,
        "clave": message["key"],
        "fecha": message["date_utc"],
        "de": f"{message['from_name']} <{message['from_addr']}>".strip(),
        "para": message["to_addrs"],
        "cc": message["cc_addrs"],
        "asunto": message["subject"],
        "entidad": message["entity"],
        "motivo_enrutado": message["routing_reason"],
        "doc_types": message["doc_types"],
        "flags": message["flags"],
        "adjuntos": message["attachments"],
        "cuerpo": cuerpo[:max_caracteres],
        "cuerpo_truncado": truncado,
    }


# ----------------------------------------------------------------- extracciones


@server.tool(description="Persiste los campos estructurados que has extraido leyendo un correo (importe, vencimiento, incoterm, numero de BL...). Es la memoria del motor: lo que guardes aqui se puede consultar despues sin releer el correo.")
def registrar_extraccion(
    clave: str,
    doc_type: str,
    campos: dict[str, Any],
    nota: str = "",
    estado_registro: str = "pending",
) -> dict[str, Any]:
    error = _guard()
    if error:
        return error
    message = _ctx.store.get_message(clave)
    if not message:
        return {"ok": False, "error": f"No hay ningun mensaje con la clave '{clave}'"}
    try:
        extraction_id = _ctx.store.save_extraction(
            Extraction(
                message_key=clave,
                doc_type=doc_type,
                entity=message["entity"],
                fields=campos,
                status=estado_registro,
                note=nota,
            )
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "id": extraction_id,
        "clave": clave,
        "doc_type": doc_type,
        "entidad": message["entity"],
        "estado": estado_registro,
    }


@server.tool(description="Consulta las extracciones guardadas, filtrando por entidad, tipo documental y estado (pending/confirmed/rejected).")
def listar_extracciones(
    entidad: str | None = None,
    doc_type: str | None = None,
    estado_registro: str | None = None,
    limite: int = 100,
) -> dict[str, Any]:
    error = _guard()
    if error:
        return error
    filas = _ctx.store.list_extractions(
        entity=entidad, doc_type=doc_type, status=estado_registro, limit=limite
    )
    return {"ok": True, "total": len(filas), "extracciones": filas}


@server.tool(description="Cambia el estado de una extraccion a confirmed o rejected, una vez revisada.")
def marcar_extraccion(id_extraccion: int, estado_registro: str) -> dict[str, Any]:
    error = _guard()
    if error:
        return error
    try:
        updated = _ctx.store.set_extraction_status(id_extraccion, estado_registro)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not updated:
        return {"ok": False, "error": f"No existe la extraccion {id_extraccion}"}
    return {"ok": True, "id": id_extraccion, "estado": estado_registro}


# ------------------------------------------------------------------ buzon real


@server.tool(description="Sincroniza el buzon IMAP con la base local. Requiere red hacia imap.mail.me.com:993.")
def sincronizar(carpetas: list[str] | None = None) -> dict[str, Any]:
    error = _guard()
    if error:
        return error
    try:
        reports = sync_all(_ctx.settings, _ctx.store, _ctx.router, folders=carpetas)
    except ImapError as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": all(r.error is None for r in reports),
        "carpetas": [r.to_dict() for r in reports],
        "estadisticas": _ctx.store.stats(),
    }


@server.tool(description="Lista las carpetas reales del buzon iCloud, con los nombres de sistema que usa Apple.")
def carpetas() -> dict[str, Any]:
    error = _guard()
    if error:
        return error
    try:
        with MailboxClient(_ctx.settings) as client:
            folders = client.list_folders()
    except ImapError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "carpetas": folders, "carpetas_sistema_icloud": ICLOUD_SYSTEM_FOLDERS}


@server.tool(description="Mueve un correo a otra carpeta del buzon real de iCloud. La accion se ve en el iPhone y en iCloud.com.")
def mover(clave: str, carpeta_destino: str) -> dict[str, Any]:
    error = _guard()
    if error:
        return error
    message = _ctx.store.get_message(clave)
    if not message:
        return {"ok": False, "error": f"No hay ningun mensaje con la clave '{clave}'"}
    try:
        with MailboxClient(_ctx.settings) as client:
            client.select(message["folder"], readonly=False)
            method = client.move_message(int(message["uid"]), carpeta_destino)
    except ImapError as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "clave": clave,
        "destino": carpeta_destino,
        "metodo": method,
        "nota": "El indice local seguira mostrandolo en la carpeta antigua hasta la proxima sincronizacion.",
    }


@server.tool(description="Anade o quita flags IMAP de un correo (por ejemplo \\\\Seen para marcar leido, \\\\Flagged para destacar).")
def marcar(clave: str, flags: list[str], anadir: bool = True) -> dict[str, Any]:
    error = _guard()
    if error:
        return error
    message = _ctx.store.get_message(clave)
    if not message:
        return {"ok": False, "error": f"No hay ningun mensaje con la clave '{clave}'"}
    try:
        with MailboxClient(_ctx.settings) as client:
            client.select(message["folder"], readonly=False)
            client.set_flags(int(message["uid"]), flags, add=anadir)
    except ImapError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "clave": clave, "flags": flags, "anadido": anadir}


@server.tool(description="Descarga los adjuntos de un correo al directorio local configurado y devuelve las rutas, para poder leerlos.")
def descargar_adjuntos(clave: str) -> dict[str, Any]:
    error = _guard()
    if error:
        return error
    message = _ctx.store.get_message(clave)
    if not message:
        return {"ok": False, "error": f"No hay ningun mensaje con la clave '{clave}'"}
    destino = Path(_ctx.settings.attachments_dir) / str(message["uidvalidity"]) / str(message["uid"])
    try:
        with MailboxClient(_ctx.settings) as client:
            client.select(message["folder"], readonly=True)
            raw = client.fetch_raw(int(message["uid"]))
    except ImapError as exc:
        return {"ok": False, "error": str(exc)}

    destino.mkdir(parents=True, exist_ok=True)
    guardados = []
    for filename, content_type, payload in attachment_payloads(raw):
        safe = _SAFE_NAME.sub("_", Path(filename).name) or "adjunto"
        path = destino / safe
        path.write_bytes(payload)
        guardados.append(
            {"filename": filename, "content_type": content_type, "ruta": str(path.resolve()),
             "bytes": len(payload)}
        )
    return {"ok": True, "clave": clave, "adjuntos": guardados, "directorio": str(destino.resolve())}


@server.tool(description="Envia un correo desde la cuenta iCloud. Exige confirmar=True de forma explicita: sin ese flag solo devuelve una previsualizacion y NO envia nada.")
def enviar(
    para: list[str],
    asunto: str,
    cuerpo: str,
    cc: list[str] | None = None,
    responder_a_clave: str | None = None,
    confirmar: bool = False,
) -> dict[str, Any]:
    error = _guard()
    if error:
        return error

    in_reply_to = None
    if responder_a_clave:
        original = _ctx.store.get_message(responder_a_clave)
        if not original:
            return {"ok": False, "error": f"No hay ningun mensaje con la clave '{responder_a_clave}'"}
        in_reply_to = original["message_id"]

    if not confirmar:
        return {
            "ok": True,
            "enviado": False,
            "previsualizacion": {
                "de": _ctx.settings.email,
                "para": para,
                "cc": cc or [],
                "asunto": asunto,
                "cuerpo": cuerpo,
                "responde_a": in_reply_to,
            },
            "nota": "Nada se ha enviado. Ensena esto al usuario y vuelve a llamar con confirmar=True solo si lo aprueba.",
        }

    try:
        message = build_message(
            _ctx.settings,
            to=para,
            subject=asunto,
            body=cuerpo,
            cc=cc,
            in_reply_to=in_reply_to,
        )
        result = send_message(_ctx.settings, message)
    except SmtpError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "enviado": True, **result}


def main() -> None:
    logging.basicConfig(level=os.environ.get("PROGRAMADOR_LOG_LEVEL", "INFO"))
    server.run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
