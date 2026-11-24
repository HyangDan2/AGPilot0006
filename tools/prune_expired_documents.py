#!/usr/bin/env python3
"""
Prunes documents and sqlite records older than 14 days in AGTools.
"""
import os
import shutil
import sqlite3
import time
from pathlib import Path

DOCS_DIR = Path("/var/www/services/AGTools/data/documents")
DB_FILE = Path("/var/www/services/AGTools/data/metadata.sqlite3")
RETENTION_DAYS = 14
RETENTION_SECONDS = RETENTION_DAYS * 86400
now = time.time()
cutoff = now - RETENTION_SECONDS

pruned_dirs = 0
if DOCS_DIR.exists():
    for item in DOCS_DIR.iterdir():
        if item.is_dir():
            try:
                mtime = item.stat().st_mtime
                if mtime < cutoff:
                    shutil.rmtree(item, ignore_errors=True)
                    pruned_dirs += 1
            except Exception as e:
                print(f"Error removing {item}: {e}")

pruned_rows = 0
if DB_FILE.exists():
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM documents WHERE created_at < ?", (cutoff,))
        pruned_rows = cursor.rowcount
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error cleaning sqlite: {e}")

print(f"Prune completed: {pruned_dirs} directories, {pruned_rows} db rows removed (cutoff: 14 days).")
