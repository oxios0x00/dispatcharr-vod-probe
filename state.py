"""Small SQLite sidecar for what cannot live in the catalogue: the run lock,
the pause flag and the progress of the current or last run.

A plugin cannot declare Django models, so this is one file in a
vod_probe_data folder next to the plugin's own folder. Not inside it:
updating a plugin replaces its whole folder (Dispatcharr renames the old one
to a backup and deletes it once the new version is in place), and Dispatcharr
has no other place for a plugin's data. A sibling folder survives the update.
Probe results themselves are not kept here, they go into the relations'
custom_properties."""
import json
import os
import shutil
import sqlite3
import time
from contextlib import contextmanager

DATA_DIR_NAME = "vod_probe_data"
STATE_FILE = "state.sqlite3"


def data_dir_for(plugin_dir, environ=None):
    """Where the state lives: VOD_PROBE_DATA_DIR when set, otherwise a
    vod_probe_data folder next to the plugin's folder (in Dispatcharr's
    plugins directory, which ignores a folder with no plugin.py in it)."""
    environ = os.environ if environ is None else environ
    return environ.get("VOD_PROBE_DATA_DIR") or os.path.join(os.path.dirname(plugin_dir), DATA_DIR_NAME)


def move_legacy_state(plugin_dir, data_dir):
    """Up to 0.10.0 the state lived in the plugin's own data/ folder. Moves it
    once to data_dir, unless data_dir already has a state. Returns True when
    something was moved. Safe when two processes start at the same time: the
    main file moves last, so its presence at the new place means it is done."""
    old_dir = os.path.join(plugin_dir, "data")
    old = os.path.join(old_dir, STATE_FILE)
    if not os.path.exists(old) or os.path.exists(os.path.join(data_dir, STATE_FILE)):
        return False
    os.makedirs(data_dir, exist_ok=True)
    moved = False
    for suffix in ("-wal", "-shm", ""):
        try:
            shutil.move(old + suffix, os.path.join(data_dir, STATE_FILE + suffix))
            moved = True
        except FileNotFoundError:
            pass  # no such file, or another process moved it first
    try:
        os.rmdir(old_dir)
    except OSError:
        pass  # not empty, or already gone
    return moved

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
        self.db_path = os.path.join(data_dir, STATE_FILE)
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
