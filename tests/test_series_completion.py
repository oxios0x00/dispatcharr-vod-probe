"""A series whose every episode fails must end up "error", not stuck at
"pending" forever: a real vod-manager report (2026-09-25) found series with
every episode confirmed unreachable but still "pending", which Probe Run and
Scan never picked up again — series_work freezes a non-"ok" summary the same
way a failed relation is never retried on its own."""
import types

from test_dry_run_write_safety import BASE_SETTINGS, Row, fake_django, run, with_plugin
from vod_probe_pkg.contract import series_work


def episode(id, series_relation_id, n=1):
    row = Row(id=id, custom_properties={}, series_relation_id=series_relation_id,
              episode=types.SimpleNamespace(season_number=1, episode_number=n))
    row.get_stream_url = lambda: "http://provider/episode"
    return row


def test_a_series_whose_every_episode_fails_becomes_error_not_stuck_pending():
    with fake_django() as models:
        series = Row(id=10, custom_properties={"episodes_fetched": True}, last_episode_refresh=None)
        episodes = [episode(100 + n, 10, n) for n in range(1, 4)]
        models.M3USeriesRelation.objects.rows.append(series)
        models.M3UEpisodeRelation.objects.rows.extend(episodes)
        with with_plugin() as plugin:
            run(plugin, [("series", 10, False)], {**BASE_SETTINGS, "dry_run": False}, ok=False)

            assert series.custom_properties["probe"]["status"] == "error"
            assert all(ep.custom_properties["probe"]["status"] == "unreachable" for ep in episodes)
            # Frozen like a failed relation: Scan and Probe Run leave it alone from here.
            assert series_work(series.custom_properties, 3, "first_of_series") is None

            from vod_probe_pkg.contract import flag_retry, flag_series_retry
            for ep in episodes:
                ep.custom_properties = flag_retry(ep.custom_properties)
            series.custom_properties = flag_series_retry(series.custom_properties)
            assert series_work(series.custom_properties, 3, "first_of_series") == {"reason": "retry", "reload": False}

            # The provider is still down: a retry that fails again stays "error", not "pending".
            run(plugin, [("series", 10, False)], {**BASE_SETTINGS, "dry_run": False}, ok=False)
            assert series.custom_properties["probe"]["status"] == "error"
            assert series.custom_properties["probe"]["attempts"] == 2


def test_a_series_with_nothing_due_to_try_stays_pending_not_error():
    """This pass genuinely probed nothing (the one episode already failed
    without a retry flag, so it is not due) — "pending", not "error": the
    series was not actually re-examined, so it is not an answer either way.
    In the normal flow series_work would not even call this again without a
    retry flag, which would also flag the episode itself due — this forces
    the call directly to check the "tried" signal on its own."""
    with fake_django() as models:
        series = Row(id=10, custom_properties={"episodes_fetched": True}, last_episode_refresh=None)
        already_failed = Row(id=101, custom_properties={"probe": {"status": "unreachable", "schema_version": 5}},
                              series_relation_id=10, episode=types.SimpleNamespace(season_number=1, episode_number=1))
        models.M3USeriesRelation.objects.rows.append(series)
        models.M3UEpisodeRelation.objects.rows.append(already_failed)
        with with_plugin() as plugin:
            run(plugin, [("series", 10, False)], {**BASE_SETTINGS, "dry_run": False}, ok=False)
            assert series.custom_properties["probe"]["status"] == "pending"
