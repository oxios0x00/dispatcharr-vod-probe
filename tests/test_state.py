import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from state import DATA_DIR_NAME, STATE_FILE, State, data_dir_for, move_legacy_state


def test_lock_is_exclusive_and_released():
    with tempfile.TemporaryDirectory() as tmp:
        state = State(tmp)
        assert state.try_acquire_lock("run", 900) == (True, None)
        acquired, held_since = state.try_acquire_lock("run", 900)
        assert acquired is False and held_since is not None
        state.release_lock("run")
        assert state.try_acquire_lock("run", 900)[0] is True


def test_stale_lock_is_taken_over():
    with tempfile.TemporaryDirectory() as tmp:
        state = State(tmp)
        state.try_acquire_lock("run", 900)
        assert state.lock_held_since("run", 900) is not None
        assert state.lock_held_since("run", -1) is None  # silent for longer than allowed
        assert state.try_acquire_lock("run", -1)[0] is True


def test_key_value_and_pause():
    with tempfile.TemporaryDirectory() as tmp:
        state = State(tmp)
        assert state.get("run", {"x": 1}) == {"x": 1}
        state.set("run", {"processed": 3})
        assert state.get("run") == {"processed": 3}
        assert state.is_paused() is False
        state.set_paused(True)
        assert state.is_paused() is True


def test_data_dir_is_next_to_the_plugin_folder():
    assert data_dir_for("/data/plugins/vod_probe", environ={}) == os.path.join("/data/plugins", DATA_DIR_NAME)
    assert data_dir_for("/data/plugins/vod_probe", environ={"VOD_PROBE_DATA_DIR": "/elsewhere"}) == "/elsewhere"


def test_legacy_state_is_moved_once_and_kept_readable():
    with tempfile.TemporaryDirectory() as root:
        plugin_dir = os.path.join(root, "vod_probe")
        data_dir = data_dir_for(plugin_dir, environ={})
        old = State(os.path.join(plugin_dir, "data"))
        old.set("run", {"processed": 7})
        old.set_paused(True)

        assert move_legacy_state(plugin_dir, data_dir) is True
        assert not os.path.exists(os.path.join(plugin_dir, "data"))
        moved = State(data_dir)
        assert moved.get("run") == {"processed": 7}
        assert moved.is_paused() is True
        assert move_legacy_state(plugin_dir, data_dir) is False  # nothing left to move


def test_legacy_state_never_overwrites_a_newer_one():
    with tempfile.TemporaryDirectory() as root:
        plugin_dir = os.path.join(root, "vod_probe")
        data_dir = data_dir_for(plugin_dir, environ={})
        State(os.path.join(plugin_dir, "data")).set("run", {"processed": 1})
        State(data_dir).set("run", {"processed": 2})

        assert move_legacy_state(plugin_dir, data_dir) is False
        assert State(data_dir).get("run") == {"processed": 2}
        assert os.path.exists(os.path.join(plugin_dir, "data", STATE_FILE))


def test_nothing_to_move_on_a_fresh_install():
    with tempfile.TemporaryDirectory() as root:
        plugin_dir = os.path.join(root, "vod_probe")
        os.makedirs(plugin_dir)
        assert move_legacy_state(plugin_dir, data_dir_for(plugin_dir, environ={})) is False
