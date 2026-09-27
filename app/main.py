"""Process entry point.

Without flags this boots the database and exits. ``--paper`` runs the paper
engine on public market data. ``--telegram`` runs the menu and the same paper
engine. Neither path places exchange orders.
"""

import argparse
import asyncio
import logging
from pathlib import Path

from app.paper import run_paper
from bot.runner import run_telegram

from alembic import command
from alembic.config import Config

from core.app_logging import configure_logging
from core.config import Settings, get_settings
from core.security import is_valid_fernet_key
from database.integrity import check_integrity
from database.repositories.heartbeats import HeartbeatRepository
from database.session import close_db, get_sessionmaker, init_db
from database.sqlite_utils import sqlite_file_from_url
from services.backup_service import BackupService

logger = logging.getLogger("autotrade")


def upgrade_database(project_root: Path, database_url: str) -> None:
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "database" / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")


def _configure() -> Settings:
    settings = get_settings()
    configure_logging(
        settings.log_level,
        secrets=[
            settings.bot_token.get_secret_value(),
            settings.encryption_key.get_secret_value(),
        ],
    )
    return settings


async def run() -> int:
    settings = get_settings()
    init_db(settings)
    try:
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            report = await check_integrity(session)
        if not report.ok:
            logger.error("SQLite integrity_check failed: %s", " ".join(report.messages))
            database_path = sqlite_file_from_url(settings.database_url)
            if database_path is not None and database_path.is_file():
                backup_dir = Path(__file__).resolve().parent.parent / "backups"
                backup = BackupService(
                    database_path=database_path,
                    backup_dir=backup_dir,
                    retention=settings.backup_retention,
                )
                backup.create_backup()
                logger.error("emergency backup written because integrity_check failed")
            return 1

        async with session_factory() as session:
            await HeartbeatRepository().beat(session, "bootstrap", status="ok", details="database ready")
        logger.info("database ready, integrity_check ok, trading is not started")
        return 0
    finally:
        await close_db()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.main")
    parser.add_argument(
        "--paper",
        action="store_true",
        help="Run paper trading on public market data. Exchange orders stay off.",
    )
    parser.add_argument(
        "--cycles",
        type=int,
        default=None,
        help="With --paper, stop after this many cycles. Omit to run until SIGINT or SIGTERM.",
    )
    parser.add_argument(
        "--telegram",
        action="store_true",
        help="Run the Telegram menu and a paper trading loop. Exchange orders stay off.",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    settings = _configure()
    if args.paper and args.telegram:
        logger.error("Укажите один режим: --paper или --telegram")
        raise SystemExit(2)
    if args.cycles is not None and not args.paper:
        logger.error("--cycles is used together with --paper")
        raise SystemExit(2)
    if args.telegram and settings.bot_token.get_secret_value().strip() == "":
        logger.error("BOT_TOKEN пуст. Укажите токен бота в окружении.")
        raise SystemExit(2)
    if not is_valid_fernet_key(settings.encryption_key.get_secret_value()):
        logger.error("ENCRYPTION_KEY is missing or is not a Fernet key")
        raise SystemExit(1)
    project_root = Path(__file__).resolve().parent.parent
    try:
        upgrade_database(project_root, settings.database_url)
    except Exception:
        logger.exception("database migration failed")
        raise SystemExit(1) from None
    if args.paper:
        settings = settings.model_copy(update={"execution_mode": "paper", "allow_exchange_orders": False})
        raise SystemExit(asyncio.run(run_paper(settings, cycles=args.cycles)))
    if args.telegram:
        settings = settings.model_copy(update={"allow_exchange_orders": False})
        raise SystemExit(asyncio.run(run_telegram(settings)))
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
