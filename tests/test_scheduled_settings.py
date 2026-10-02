"""A scheduled run uses the settings saved now, not the copy made at Apply."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from test_dry_run_write_safety import with_plugin  # noqa: E402


def _scheduled(plugin, queued, live):
    seen = {}
    plugin._live_settings = lambda: live
    plugin._apply_retry_switch = lambda *_a: None
    plugin._run_pass = lambda settings, _logger: seen.update(settings) or {"status": "ok", "message": ""}
    plugin._probe_run(queued, scheduled=True)
    return seen


def test_saved_settings_win_over_the_copy_made_at_apply():
    with with_plugin() as plugin:
        seen = _scheduled(plugin, {"dry_run": True, "batch_limit": 5}, {"dry_run": False, "batch_limit": 50})
    assert seen["dry_run"] is False and seen["batch_limit"] == 50


def test_a_scheduled_run_ignores_id_lists_and_schedule_keys():
    with with_plugin() as plugin:
        seen = _scheduled(plugin, {}, {"only_relation_ids": "1,2", "schedule_cron": "0 4 * * *", "dry_run": False})
    assert seen["only_relation_ids"] == "" and "schedule_cron" not in seen


def test_the_copy_is_the_fallback_when_saved_settings_cannot_be_read():
    with with_plugin() as plugin:
        seen = _scheduled(plugin, {"dry_run": False, "batch_limit": 5}, None)
    assert seen["batch_limit"] == 5
