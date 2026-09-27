"""Backup API copies and retention."""

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from services.backup_service import BACKUP_NAME_RE, BackupService


def _seed_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE marks (id INTEGER PRIMARY KEY, label TEXT NOT NULL)")
        connection.execute("INSERT INTO marks (label) VALUES ('kept')")
        connection.commit()
    finally:
        connection.close()


def test_backup_file_is_valid_and_contains_rows(tmp_path: Path) -> None:
    source = tmp_path / "data" / "autotrade.db"
    _seed_database(source)
    service = BackupService(source, tmp_path / "backups", retention=7)
    when = datetime(2026, 9, 27, 12, 30, 45)
    backup_path = service.create_backup(when=when)

    assert backup_path.name == "autotrade_2026-09-27_12-30-45.db"
    assert BACKUP_NAME_RE.match(backup_path.name)
    assert service.verify_backup(backup_path)

    connection = sqlite3.connect(backup_path)
    try:
        row = connection.execute("SELECT label FROM marks").fetchone()
    finally:
        connection.close()
    assert row == ("kept",)


def test_retention_keeps_the_newest_files(tmp_path: Path) -> None:
    source = tmp_path / "data" / "autotrade.db"
    _seed_database(source)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    stamps = [
        "2026-01-01_00-00-00",
        "2026-01-02_00-00-00",
        "2026-01-03_00-00-00",
        "2026-01-04_00-00-00",
        "2026-01-05_00-00-00",
        "2026-01-06_00-00-00",
        "2026-01-07_00-00-00",
        "2026-01-08_00-00-00",
        "2026-01-09_00-00-00",
        "2026-01-10_00-00-00",
    ]
    for stamp in stamps:
        (backup_dir / f"autotrade_{stamp}.db").write_bytes(b"")
    notes = backup_dir / "readme.txt"
    notes.write_text("keep me", encoding="utf-8")
    stray = backup_dir / "autotrade_not-a-timestamp.db"
    stray.write_bytes(b"")

    service = BackupService(source, backup_dir, retention=7)
    removed = service.apply_retention()

    remaining = sorted(path.name for path in backup_dir.glob("autotrade_*.db"))
    assert [path.name for path in removed] == [f"autotrade_{stamp}.db" for stamp in stamps[:3]]
    assert remaining == [f"autotrade_{stamp}.db" for stamp in stamps[3:]] + ["autotrade_not-a-timestamp.db"]
    assert notes.read_text(encoding="utf-8") == "keep me"
    assert stray.exists()


def test_create_backup_applies_retention(tmp_path: Path) -> None:
    source = tmp_path / "data" / "autotrade.db"
    _seed_database(source)
    backup_dir = tmp_path / "backups"
    service = BackupService(source, backup_dir, retention=2)
    service.create_backup(when=datetime(2026, 1, 1, 0, 0, 1))
    service.create_backup(when=datetime(2026, 1, 1, 0, 0, 2))
    newest = service.create_backup(when=datetime(2026, 1, 1, 0, 0, 3))

    names = sorted(path.name for path in backup_dir.glob("autotrade_*.db"))
    assert names == [
        "autotrade_2026-01-01_00-00-02.db",
        "autotrade_2026-01-01_00-00-03.db",
    ]
    assert newest.name == "autotrade_2026-01-01_00-00-03.db"
    assert service.verify_backup(newest)
