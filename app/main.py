"""Process entry point for stages 1–2: database boot, integrity check, clean exit.

Trading, Telegram, and the WebSocket client are not started here.
"""

import asyncio
import logging
from pathlib import Path

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


def main() -> None:
    settings = _configure()
    if not is_valid_fernet_key(settings.encryption_key.get_secret_value()):
        logger.error("ENCRYPTION_KEY is missing or is not a Fernet key")
        raise SystemExit(1)
    project_root = Path(__file__).resolve().parent.parent
    try:
        upgrade_database(project_root, settings.database_url)
    except Exception:
        logger.exception("database migration failed")
        raise SystemExit(1) from None
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
