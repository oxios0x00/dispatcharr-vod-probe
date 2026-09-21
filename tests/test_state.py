import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from state import State


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
