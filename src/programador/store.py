"""Persistencia en SQLite con indice de texto completo (FTS5).

SQLite y no Postgres a proposito: el motor corre en una sola maquina (tu Mac o
un VPS pequeno), el fichero se copia con `cp` y no hay servicio que mantener.
Si algun dia necesitas acceso concurrente desde varias maquinas, el unico
modulo que hay que reescribir es este.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from .models import Attachment, Extraction, Message, SyncState

__all__ = ["Store"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    key             TEXT PRIMARY KEY,
    account         TEXT NOT NULL,
    folder          TEXT NOT NULL,
    uid             INTEGER NOT NULL,
    uidvalidity     INTEGER NOT NULL,
    message_id      TEXT,
    in_reply_to     TEXT,
    from_addr       TEXT NOT NULL DEFAULT '',
    from_name       TEXT NOT NULL DEFAULT '',
    to_addrs        TEXT NOT NULL DEFAULT '[]',
    cc_addrs        TEXT NOT NULL DEFAULT '[]',
    subject         TEXT NOT NULL DEFAULT '',
    date_utc        TEXT,
    body_text       TEXT NOT NULL DEFAULT '',
    snippet         TEXT NOT NULL DEFAULT '',
    attachments     TEXT NOT NULL DEFAULT '[]',
    has_attachments INTEGER NOT NULL DEFAULT 0,
    flags           TEXT NOT NULL DEFAULT '[]',
    entity          TEXT NOT NULL DEFAULT 'personal',
    routing_reason  TEXT NOT NULL DEFAULT 'default',
    doc_types       TEXT NOT NULL DEFAULT '[]',
    synced_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_entity_date ON messages(entity, date_utc DESC);
CREATE INDEX IF NOT EXISTS idx_messages_date ON messages(date_utc DESC);
CREATE INDEX IF NOT EXISTS idx_messages_folder_uid ON messages(account, folder, uidvalidity, uid);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    key UNINDEXED,
    subject,
    body_text,
    from_addr,
    attachment_names,
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS extractions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    message_key TEXT NOT NULL,
    doc_type    TEXT NOT NULL,
    entity      TEXT NOT NULL,
    fields      TEXT NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'pending',
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE(message_key, doc_type)
);
CREATE INDEX IF NOT EXISTS idx_extractions_entity ON extractions(entity, doc_type, status);

CREATE TABLE IF NOT EXISTS sync_state (
    account      TEXT NOT NULL,
    folder       TEXT NOT NULL,
    uidvalidity  INTEGER NOT NULL,
    last_uid     INTEGER NOT NULL DEFAULT 0,
    last_sync_at TEXT,
    PRIMARY KEY (account, folder)
);
"""

_VALID_STATUS = ("pending", "confirmed", "rejected")
_FTS_TOKEN = re.compile(r"[\w@.\-+/]+", re.UNICODE)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_fts_query(raw: str) -> str:
    """Convierte texto libre en una consulta FTS5 segura.

    Los usuarios escriben `factura MSC "B/L"` y FTS5 interpreta comillas,
    parentesis, `*` y `NEAR` como sintaxis: una cadena cruda puede lanzar
    excepciones o, peor, buscar otra cosa. Aqui se extraen los tokens y se
    citan uno a uno, unidos por AND.
    """
    tokens = _FTS_TOKEN.findall(raw or "")
    if not tokens:
        return ""
    return " AND ".join('"' + token.replace('"', '""') + '"' for token in tokens)


class Store:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        if str(self.db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self.init_schema()

    def init_schema(self) -> None:
        with self._conn:
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ mensajes

    def upsert_message(self, message: Message) -> str:
        key = message.key
        attachments = [a.to_dict() for a in message.attachments]
        row = (
            key,
            message.account,
            message.folder,
            message.uid,
            message.uidvalidity,
            message.message_id,
            message.in_reply_to,
            message.from_addr,
            message.from_name,
            json.dumps(message.to_addrs),
            json.dumps(message.cc_addrs),
            message.subject,
            message.date_utc.astimezone(timezone.utc).isoformat(timespec="seconds")
            if message.date_utc
            else None,
            message.body_text,
            message.snippet,
            json.dumps(attachments),
            1 if attachments else 0,
            json.dumps(message.flags),
            message.entity,
            message.routing_reason,
            json.dumps(message.doc_types),
            _now(),
        )
        with self._conn:
            self._conn.execute(
                """INSERT INTO messages (key, account, folder, uid, uidvalidity, message_id,
                       in_reply_to, from_addr, from_name, to_addrs, cc_addrs, subject, date_utc,
                       body_text, snippet, attachments, has_attachments, flags, entity,
                       routing_reason, doc_types, synced_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(key) DO UPDATE SET
                       flags=excluded.flags, entity=excluded.entity,
                       routing_reason=excluded.routing_reason, doc_types=excluded.doc_types,
                       attachments=excluded.attachments,
                       has_attachments=excluded.has_attachments, synced_at=excluded.synced_at""",
                row,
            )
            self._conn.execute("DELETE FROM messages_fts WHERE key = ?", (key,))
            self._conn.execute(
                "INSERT INTO messages_fts (key, subject, body_text, from_addr, attachment_names)"
                " VALUES (?,?,?,?,?)",
                (
                    key,
                    message.subject,
                    message.body_text,
                    f"{message.from_name} {message.from_addr}",
                    " ".join(a.filename for a in message.attachments),
                ),
            )
        return key

    def upsert_many(self, messages: Iterable[Message]) -> int:
        return sum(1 for message in messages if self.upsert_message(message))

    def get_message(self, key: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM messages WHERE key = ?", (key,)).fetchone()
        return _row_to_message_dict(row) if row else None

    def list_messages(
        self,
        *,
        entity: str | None = None,
        doc_type: str | None = None,
        since: str | None = None,
        unread_only: bool = False,
        with_attachments: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if entity:
            clauses.append("entity = ?")
            params.append(entity)
        if doc_type:
            # doc_types es un array JSON; EXISTS sobre json_each evita falsos
            # positivos de un LIKE sobre el texto serializado.
            clauses.append(
                "EXISTS (SELECT 1 FROM json_each(messages.doc_types) WHERE value = ?)"
            )
            params.append(doc_type)
        if since:
            clauses.append("date_utc >= ?")
            params.append(since)
        if unread_only:
            clauses.append(
                "NOT EXISTS (SELECT 1 FROM json_each(messages.flags) WHERE value = '\\Seen')"
            )
        if with_attachments is not None:
            clauses.append("has_attachments = ?")
            params.append(1 if with_attachments else 0)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([max(1, min(limit, 500)), max(0, offset)])
        rows = self._conn.execute(
            f"SELECT * FROM messages {where} ORDER BY date_utc DESC LIMIT ? OFFSET ?", params
        ).fetchall()
        return [_row_to_message_dict(row) for row in rows]

    def search(
        self,
        query: str,
        *,
        entity: str | None = None,
        doc_type: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        fts_query = build_fts_query(query)
        if not fts_query:
            return []
        clauses = ["messages_fts MATCH ?"]
        params: list[Any] = [fts_query]
        if entity:
            clauses.append("m.entity = ?")
            params.append(entity)
        if doc_type:
            clauses.append("EXISTS (SELECT 1 FROM json_each(m.doc_types) WHERE value = ?)")
            params.append(doc_type)
        params.append(max(1, min(limit, 500)))
        rows = self._conn.execute(
            f"""SELECT m.* FROM messages_fts
                JOIN messages m ON m.key = messages_fts.key
                WHERE {' AND '.join(clauses)}
                ORDER BY bm25(messages_fts), m.date_utc DESC
                LIMIT ?""",
            params,
        ).fetchall()
        return [_row_to_message_dict(row) for row in rows]

    def known_uids(self, account: str, folder: str, uidvalidity: int) -> set[int]:
        rows = self._conn.execute(
            "SELECT uid FROM messages WHERE account=? AND folder=? AND uidvalidity=?",
            (account, folder, uidvalidity),
        ).fetchall()
        return {int(row["uid"]) for row in rows}

    def recent_uids(
        self, account: str, folder: str, uidvalidity: int, *, limit: int = 500
    ) -> list[int]:
        """Los UID mas recientes de una carpeta, para refrescar solo sus flags."""
        rows = self._conn.execute(
            """SELECT uid FROM messages WHERE account=? AND folder=? AND uidvalidity=?
               ORDER BY uid DESC LIMIT ?""",
            (account, folder, uidvalidity, max(0, limit)),
        ).fetchall()
        return [int(row["uid"]) for row in rows]

    def update_flags(
        self, account: str, folder: str, uidvalidity: int, flags_by_uid: dict[int, list[str]]
    ) -> int:
        """Sincroniza los flags de mensajes ya indexados. Devuelve cuantos cambiaron."""
        if not flags_by_uid:
            return 0
        changed = 0
        with self._conn:
            for uid, flags in flags_by_uid.items():
                serialized = json.dumps(sorted(flags))
                cursor = self._conn.execute(
                    """UPDATE messages SET flags=?
                       WHERE account=? AND folder=? AND uidvalidity=? AND uid=? AND flags!=?""",
                    (serialized, account, folder, uidvalidity, uid, serialized),
                )
                changed += cursor.rowcount
        return changed

    def delete_message(self, key: str) -> bool:
        """Elimina un mensaje del indice y del FTS. Las extracciones sobreviven."""
        with self._conn:
            self._conn.execute("DELETE FROM messages_fts WHERE key = ?", (key,))
            cursor = self._conn.execute("DELETE FROM messages WHERE key = ?", (key,))
        return cursor.rowcount > 0

    def count_extractions_for(self, key: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM extractions WHERE message_key = ?", (key,)
        ).fetchone()
        return int(row["n"])

    def delete_folder_messages(self, account: str, folder: str, uidvalidity: int) -> int:
        """Purga una carpeta: se usa cuando el servidor cambia el UIDVALIDITY."""
        with self._conn:
            keys = [
                row["key"]
                for row in self._conn.execute(
                    "SELECT key FROM messages WHERE account=? AND folder=? AND uidvalidity=?",
                    (account, folder, uidvalidity),
                ).fetchall()
            ]
            self._conn.executemany("DELETE FROM messages_fts WHERE key = ?", [(k,) for k in keys])
            self._conn.executemany("DELETE FROM messages WHERE key = ?", [(k,) for k in keys])
        return len(keys)

    # --------------------------------------------------------------- extracciones

    def save_extraction(self, extraction: Extraction) -> int:
        if extraction.status not in _VALID_STATUS:
            raise ValueError(f"status debe ser uno de {_VALID_STATUS}, llego '{extraction.status}'")
        now = _now()
        with self._conn:
            cursor = self._conn.execute(
                """INSERT INTO extractions
                       (message_key, doc_type, entity, fields, status, note, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(message_key, doc_type) DO UPDATE SET
                       entity=excluded.entity, fields=excluded.fields, status=excluded.status,
                       note=excluded.note, updated_at=excluded.updated_at""",
                (
                    extraction.message_key,
                    extraction.doc_type,
                    extraction.entity,
                    json.dumps(extraction.fields, ensure_ascii=False),
                    extraction.status,
                    extraction.note,
                    now,
                    now,
                ),
            )
            if cursor.lastrowid:
                return int(cursor.lastrowid)
            row = self._conn.execute(
                "SELECT id FROM extractions WHERE message_key=? AND doc_type=?",
                (extraction.message_key, extraction.doc_type),
            ).fetchone()
            return int(row["id"])

    def list_extractions(
        self,
        *,
        entity: str | None = None,
        doc_type: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (("entity", entity), ("doc_type", doc_type), ("status", status)):
            if value:
                clauses.append(f"e.{column} = ?")
                params.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(limit, 500)))
        rows = self._conn.execute(
            f"""SELECT e.*, m.subject, m.from_addr, m.date_utc
                FROM extractions e LEFT JOIN messages m ON m.key = e.message_key
                {where} ORDER BY e.updated_at DESC LIMIT ?""",
            params,
        ).fetchall()
        return [
            {
                "id": row["id"],
                "message_key": row["message_key"],
                "doc_type": row["doc_type"],
                "entity": row["entity"],
                "fields": json.loads(row["fields"]),
                "status": row["status"],
                "note": row["note"],
                "subject": row["subject"],
                "from_addr": row["from_addr"],
                "date_utc": row["date_utc"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def set_extraction_status(self, extraction_id: int, status: str) -> bool:
        if status not in _VALID_STATUS:
            raise ValueError(f"status debe ser uno de {_VALID_STATUS}, llego '{status}'")
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE extractions SET status=?, updated_at=? WHERE id=?",
                (status, _now(), extraction_id),
            )
        return cursor.rowcount > 0

    # ------------------------------------------------------------------- sync

    def get_sync_state(self, account: str, folder: str) -> SyncState | None:
        row = self._conn.execute(
            "SELECT * FROM sync_state WHERE account=? AND folder=?", (account, folder)
        ).fetchone()
        if not row:
            return None
        return SyncState(
            account=row["account"],
            folder=row["folder"],
            uidvalidity=int(row["uidvalidity"]),
            last_uid=int(row["last_uid"]),
            last_sync_at=row["last_sync_at"],
        )

    def set_sync_state(self, state: SyncState) -> None:
        with self._conn:
            self._conn.execute(
                """INSERT INTO sync_state (account, folder, uidvalidity, last_uid, last_sync_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(account, folder) DO UPDATE SET
                       uidvalidity=excluded.uidvalidity, last_uid=excluded.last_uid,
                       last_sync_at=excluded.last_sync_at""",
                (state.account, state.folder, state.uidvalidity, state.last_uid, _now()),
            )

    # ------------------------------------------------------------------ resumen

    def stats(self, *, since: str | None = None) -> dict[str, Any]:
        where, params = ("WHERE date_utc >= ?", [since]) if since else ("", [])
        total = self._conn.execute(
            f"SELECT COUNT(*) AS n FROM messages {where}", params
        ).fetchone()["n"]
        by_entity = {
            row["entity"]: row["n"]
            for row in self._conn.execute(
                f"SELECT entity, COUNT(*) AS n FROM messages {where}"
                " GROUP BY entity ORDER BY n DESC",
                params,
            ).fetchall()
        }
        by_doc_type = {
            row["value"]: row["n"]
            for row in self._conn.execute(
                f"""SELECT value, COUNT(*) AS n FROM messages, json_each(messages.doc_types)
                    {where} GROUP BY value ORDER BY n DESC""",
                params,
            ).fetchall()
        }
        pending = self._conn.execute(
            "SELECT COUNT(*) AS n FROM extractions WHERE status='pending'"
        ).fetchone()["n"]
        return {
            "total_messages": total,
            "by_entity": by_entity,
            "by_doc_type": by_doc_type,
            "pending_extractions": pending,
        }


def _row_to_message_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "key": row["key"],
        "account": row["account"],
        "folder": row["folder"],
        "uid": row["uid"],
        "uidvalidity": row["uidvalidity"],
        "message_id": row["message_id"],
        "from_addr": row["from_addr"],
        "from_name": row["from_name"],
        "to_addrs": json.loads(row["to_addrs"]),
        "cc_addrs": json.loads(row["cc_addrs"]),
        "subject": row["subject"],
        "date_utc": row["date_utc"],
        "snippet": row["snippet"],
        "body_text": row["body_text"],
        "attachments": [Attachment.from_dict(a).to_dict() for a in json.loads(row["attachments"])],
        "flags": json.loads(row["flags"]),
        "entity": row["entity"],
        "routing_reason": row["routing_reason"],
        "doc_types": json.loads(row["doc_types"]),
    }
