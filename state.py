"""Small SQLite sidecar for what cannot live in the catalogue: the run lock,
the pause flag and the progress of the current or last run.

A plugin cannot declare Django models, so this follows vod-manager: one file
in the plugin's own data directory. Probe results themselves are not kept
here, they go into the relations' custom_properties."""
import json
import os
import sqlite3
import time
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS plugin_state (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS run_locks (
    name TEXT PRIMARY KEY,
    started_at REAL NOT NULL,
    pid INTEGER
);
"""


class State:
    def __init__(self, data_dir):
        os.makedirs(data_dir, exist_ok=True)
        self.db_path = os.path.join(data_dir, "state.sqlite3")
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # --- key/value (paused flag, run progress) -------------------------------

    def get(self, key, default=None):
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM plugin_state WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return default

    def set(self, key, value):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO plugin_state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)),
            )

    def is_paused(self):
        return bool(self.get("paused", False))

    def set_paused(self, paused):
        self.set("paused", bool(paused))

    # --- run lock -------------------------------------------------------------
    # Stops a re-click or a retry from starting a second run on top of one that
    # is still going, across Dispatcharr's separate worker processes. A lock
    # silent for stale_after seconds belongs to a run that died (a restart) and
    # is taken over, so a crash never blocks the plugin for good.

    def try_acquire_lock(self, name, stale_after):
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO run_locks (name, started_at, pid) VALUES (?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET started_at = excluded.started_at, "
                "pid = excluded.pid WHERE run_locks.started_at < ?",
                (name, now, os.getpid(), now - stale_after),
            )
            row = conn.execute(
                "SELECT started_at, pid FROM run_locks WHERE name = ?", (name,)
            ).fetchone()
        acquired = row is not None and row["started_at"] == now and row["pid"] == os.getpid()
        return acquired, (None if acquired else row["started_at"])

    def renew_lock(self, name):
        with self._connect() as conn:
            conn.execute("UPDATE run_locks SET started_at = ? WHERE name = ?", (time.time(), name))

    def release_lock(self, name):
        with self._connect() as conn:
            conn.execute("DELETE FROM run_locks WHERE name = ?", (name,))

    def lock_held_since(self, name, stale_after):
        with self._connect() as conn:
            row = conn.execute("SELECT started_at FROM run_locks WHERE name = ?", (name,)).fetchone()
        if row is None or row["started_at"] < time.time() - stale_after:
            return None
        return row["started_at"]
