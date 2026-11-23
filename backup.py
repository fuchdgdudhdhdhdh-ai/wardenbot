"""Create a consistent SQLite backup using SQLite's online backup API."""
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

source = Path(os.getenv("DATABASE_PATH", "warden.db"))
target_dir = Path(os.getenv("BACKUP_DIR", "backups"))
target_dir.mkdir(parents=True, exist_ok=True)
if not source.exists():
    raise SystemExit(f"Database does not exist: {source}")
timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
target = target_dir / f"warden_{timestamp}.sqlite3"
with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
    src.backup(dst)
print(target.resolve())
