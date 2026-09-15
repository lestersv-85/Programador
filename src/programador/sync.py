"""Sincronizacion incremental IMAP -> SQLite.

Se apoya en el par (UIDVALIDITY, UID) que define IMAP: los UID son crecientes y
estables dentro de una carpeta mientras el UIDVALIDITY no cambie. Si cambia
--- el servidor reconstruyo la carpeta --- los UID guardados dejan de significar
nada y hay que purgar y rebajar esa carpeta desde cero.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Callable, Sequence

from .config import ConfigError, Settings, load_routing_config, load_settings
from .imap_client import ImapError, MailboxClient
from .models import SyncState
from .routing import Router
from .store import Store

__all__ = ["SyncReport", "sync_folder", "sync_all", "main"]

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SyncReport:
    folder: str
    uidvalidity: int = 0
    fetched: int = 0
    skipped: int = 0
    uidvalidity_reset: bool = False
    first_sync: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "folder": self.folder,
            "uidvalidity": self.uidvalidity,
            "fetched": self.fetched,
            "skipped_already_known": self.skipped,
            "uidvalidity_reset": self.uidvalidity_reset,
            "first_sync": self.first_sync,
            "error": self.error,
        }


def sync_folder(
    client: MailboxClient,
    store: Store,
    router: Router,
    folder: str,
    *,
    account: str,
    initial_days: int = 180,
) -> SyncReport:
    report = SyncReport(folder=folder)
    try:
        status = client.select(folder, readonly=True)
    except ImapError as exc:
        report.error = str(exc)
        return report

    uidvalidity = status["uidvalidity"]
    report.uidvalidity = uidvalidity
    state = store.get_sync_state(account, folder)

    if state is not None and state.uidvalidity != uidvalidity:
        logger.warning(
            "UIDVALIDITY de %s cambio (%s -> %s): se purga y se resincroniza",
            folder,
            state.uidvalidity,
            uidvalidity,
        )
        store.delete_folder_messages(account, folder, state.uidvalidity)
        report.uidvalidity_reset = True
        state = None

    if state is None:
        report.first_sync = True
        uids = client.search_uids(since_days=initial_days)
        last_uid = 0
    else:
        uids = client.search_uids(min_uid=state.last_uid + 1)
        last_uid = state.last_uid

    known = store.known_uids(account, folder, uidvalidity)
    pending = [uid for uid in uids if uid not in known]
    report.skipped = len(uids) - len(pending)

    for message in client.fetch_messages(pending, folder=folder, uidvalidity=uidvalidity):
        store.upsert_message(router.apply(message))
        report.fetched += 1
        last_uid = max(last_uid, message.uid)

    # Avanzamos la marca aunque no llegara nada nuevo: evita repetir la busqueda
    # completa en la siguiente pasada.
    if uids:
        last_uid = max(last_uid, max(uids))
    store.set_sync_state(
        SyncState(account=account, folder=folder, uidvalidity=uidvalidity, last_uid=last_uid)
    )
    return report


def sync_all(
    settings: Settings,
    store: Store,
    router: Router,
    *,
    folders: Sequence[str] | None = None,
    connection_factory: Callable[[], object] | None = None,
) -> list[SyncReport]:
    targets = list(folders or settings.folders)
    reports: list[SyncReport] = []
    with MailboxClient(settings, connection_factory) as client:  # type: ignore[arg-type]
        for folder in targets:
            reports.append(
                sync_folder(
                    client,
                    store,
                    router,
                    folder,
                    account=settings.email,
                    initial_days=settings.initial_days,
                )
            )
    return reports


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="programador-sync",
        description="Sincroniza el buzon IMAP configurado con la base local.",
    )
    parser.add_argument("--folder", action="append", dest="folders", help="Carpeta (repetible)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        settings = load_settings(dict(os.environ))
        routing = load_routing_config(settings.config_path)
    except ConfigError as exc:
        print(f"Error de configuracion: {exc}", file=sys.stderr)
        return 2

    router = Router(routing)
    with Store(settings.db_path) as store:
        try:
            reports = sync_all(settings, store, router, folders=args.folders)
        except ImapError as exc:
            print(f"Error IMAP: {exc}", file=sys.stderr)
            return 1
        for report in reports:
            if report.error:
                print(f"[{report.folder}] ERROR: {report.error}", file=sys.stderr)
            else:
                print(
                    f"[{report.folder}] nuevos={report.fetched} ya_conocidos={report.skipped}"
                    + (" (primera sincronizacion)" if report.first_sync else "")
                    + (" (UIDVALIDITY reiniciado)" if report.uidvalidity_reset else "")
                )
        stats = store.stats()
        print(
            f"Total en base: {stats['total_messages']} mensajes | "
            f"por entidad: {stats['by_entity']}"
        )
    return 1 if any(r.error for r in reports) else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
