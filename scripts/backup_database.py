"""Create a transactionally consistent SQLite backup before migration."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import tempfile

from sqlalchemy.engine import make_url

from database import DATABASE_URL


def backup_sqlite_database(
    database_url: str,
    *,
    timestamp: datetime | None = None,
) -> Path | None:
    url = make_url(database_url)
    if not url.drivername.startswith("sqlite"):
        raise RuntimeError("Agent App Server supports SQLite database backups only")
    if not url.database or url.database == ":memory:":
        return None

    source = Path(url.database).resolve()
    if not source.exists():
        return None

    backup_directory = source.parent / "backups"
    backup_directory.mkdir(parents=True, exist_ok=True)
    timestamp = timestamp or datetime.now(timezone.utc)
    destination = backup_directory / (
        f"{source.stem}-{timestamp.strftime('%Y%m%dT%H%M%SZ')}{source.suffix}"
    )
    if destination.exists():
        raise FileExistsError(f"Database backup already exists: {destination.name}")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{source.name}.",
        suffix=".backup",
        dir=backup_directory,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with (
            sqlite3.connect(source) as source_connection,
            sqlite3.connect(temporary) as backup_connection,
        ):
            source_connection.backup(backup_connection)
        temporary.chmod(source.stat().st_mode & 0o777)
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def main() -> None:
    backup = backup_sqlite_database(DATABASE_URL)
    if backup is None:
        print("SQLite database does not exist yet; skipping pre-migration backup")
    else:
        print(f"SQLite backup created: {backup}")


if __name__ == "__main__":
    main()
