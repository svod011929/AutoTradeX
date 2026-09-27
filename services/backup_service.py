"""SQLite backups via the backup API, with retention."""

import re
import sqlite3
from datetime import datetime
from pathlib import Path

from database.retry import run_with_sqlite_lock_retry_sync

BACKUP_NAME_RE = re.compile(r"^autotrade_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.db$")


class BackupError(Exception):
    """Backup could not be created or checked."""


class BackupService:
    def __init__(self, database_path: Path, backup_dir: Path, retention: int) -> None:
        if retention < 1:
            raise ValueError("retention must be >= 1")
        self.database_path = database_path
        self.backup_dir = backup_dir
        self.retention = retention

    def create_backup(self, *, when: datetime | None = None) -> Path:
        if not self.database_path.is_file():
            raise BackupError(f"database file does not exist: {self.database_path}")
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = (when or datetime.now()).strftime("%Y-%m-%d_%H-%M-%S")
        destination = self.backup_dir / f"autotrade_{stamp}.db"
        if destination.exists():
            raise BackupError(f"backup already exists: {destination}")
        run_with_sqlite_lock_retry_sync(lambda: self._copy_with_backup_api(self.database_path, destination))
        self.apply_retention()
        return destination

    def apply_retention(self) -> list[Path]:
        files = sorted(path for path in self.backup_dir.glob("autotrade_*.db") if BACKUP_NAME_RE.match(path.name))
        overflow = files[: max(0, len(files) - self.retention)]
        for path in overflow:
            path.unlink()
        return overflow

    def verify_backup(self, path: Path) -> bool:
        if not path.is_file():
            return False
        connection = sqlite3.connect(path)
        try:
            row = connection.execute("PRAGMA integrity_check").fetchone()
        finally:
            connection.close()
        return row is not None and row[0] == "ok"

    @staticmethod
    def _copy_with_backup_api(source_path: Path, destination: Path) -> None:
        source = sqlite3.connect(source_path)
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
        except Exception:
            target.close()
            source.close()
            if destination.exists():
                destination.unlink()
            raise
        else:
            target.close()
            source.close()
