from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from scripts.backup_database import backup_sqlite_database


def test_sqlite_backup_is_consistent_and_kept_outside_repository(
    tmp_path: Path,
) -> None:
    source = tmp_path / "app.db"
    with sqlite3.connect(source) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE sample (value TEXT NOT NULL)")
        connection.execute("INSERT INTO sample VALUES ('preserved')")
        connection.commit()

    backup = backup_sqlite_database(
        f"sqlite+aiosqlite:///{source}",
        timestamp=datetime(2026, 7, 26, tzinfo=timezone.utc),
    )

    assert backup == tmp_path / "backups" / "app-20260726T000000Z.db"
    assert backup is not None
    with sqlite3.connect(backup) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("SELECT value FROM sample").fetchone() == (
            "preserved",
        )


def test_sqlite_backup_skips_missing_and_in_memory_databases(tmp_path: Path) -> None:
    assert backup_sqlite_database("sqlite+aiosqlite:///:memory:") is None
    assert (
        backup_sqlite_database(
            f"sqlite+aiosqlite:///{tmp_path / 'missing.db'}"
        )
        is None
    )
