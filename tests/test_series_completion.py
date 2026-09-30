"""A series whose every episode fails must end up "error", not stuck at
"pending" forever: series_work freezes a non-"ok" summary the same way a
failed relation is never retried on its own, so without a terminal state a
fully-failed series would never be picked up again by Probe Run or Scan."""
import types

from test_dry_run_write_safety import BASE_SETTINGS, Row, fake_django, run, with_plugin
from vod_probe_pkg.contract import series_work
from vod_probe_pkg.probe import PROBE_SCHEMA_VERSION


def episode(id, series_relation_id, n=1):
    row = Row(id=id, custom_properties={}, series_relation_id=series_relation_id,
              episode=types.SimpleNamespace(season_number=1, episode_number=n))
    row.get_stream_url = lambda: f"http://provider/episode/{id}"
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


def test_a_series_with_nothing_left_to_try_at_all_is_error_even_reached_indirectly():
    """The one episode already failed without a retry flag, so nothing is due
    — this group has always been exhausted, whichever pass established that.
    In the normal flow series_work would not call this again without a retry
    flag, which would also make the episode itself due — this calls it
    directly to check the "exhausted" computation on its own, regardless of
    when the failure happened."""
    with fake_django() as models:
        series = Row(id=10, custom_properties={"episodes_fetched": True}, last_episode_refresh=None)
        already_failed = Row(id=101, custom_properties={"probe": {"status": "unreachable", "schema_version": PROBE_SCHEMA_VERSION}},
                              series_relation_id=10, episode=types.SimpleNamespace(season_number=1, episode_number=1))
        models.M3USeriesRelation.objects.rows.append(series)
        models.M3UEpisodeRelation.objects.rows.append(already_failed)
        with with_plugin() as plugin:
            run(plugin, [("series", 10, False)], {**BASE_SETTINGS, "dry_run": False}, ok=False)
            assert series.custom_properties["probe"]["status"] == "error"


def test_a_season_with_more_untried_episodes_than_the_sample_cap_stays_pending():
    """MAX_SAMPLE_TRIES caps a season's sample to 3 attempts per pass: with 5
    untried episodes, failing the 3 tried ones does not settle the season —
    2 are still untried, so the series stays "pending", not a final answer."""
    with fake_django() as models:
        series = Row(id=10, custom_properties={"episodes_fetched": True}, last_episode_refresh=None)
        episodes = [episode(100 + n, 10, n) for n in range(1, 6)]
        models.M3USeriesRelation.objects.rows.append(series)
        models.M3UEpisodeRelation.objects.rows.extend(episodes)
        with with_plugin() as plugin:
            run(plugin, [("series", 10, False)], {**BASE_SETTINGS, "dry_run": False}, ok=False)
            assert series.custom_properties["probe"]["status"] == "pending"
            tried = [ep for ep in episodes if ep.custom_properties]
            assert len(tried) == 3  # MAX_SAMPLE_TRIES
            assert all(ep.custom_properties["probe"]["status"] == "unreachable" for ep in tried)


def test_a_pending_series_keeps_progressing_across_runs_with_no_retry_errors():
    """A season with more due episodes than MAX_SAMPLE_TRIES must not get
    stuck after only its first 3 are tried: without this, series_work would
    freeze the series at "pending" the same way it freezes "error"/"partial",
    and Retry Errors would only re-arm the same 3 already-failed episodes
    ahead of the untried ones (episode order), never reaching the untried
    tail. A "pending" series must keep sampling on its own, with no Retry
    Errors at all, converging to a final status."""
    with fake_django() as models:
        series = Row(id=10, custom_properties={"episodes_fetched": True}, last_episode_refresh=None)
        episodes = [episode(100 + n, 10, n) for n in range(1, 8)]  # 7 episodes, cap is 3
        models.M3USeriesRelation.objects.rows.append(series)
        models.M3UEpisodeRelation.objects.rows.extend(episodes)
        from vod_probe_pkg.contract import series_work

        with with_plugin() as plugin:
            for _ in range(3):  # ceil(7 / MAX_SAMPLE_TRIES) passes, no Retry Errors between them
                # The real gate a scheduled run goes through — not forced by the test.
                if series_work(series.custom_properties, len(episodes), "first_of_series") is None:
                    break
                run(plugin, [("series", 10, False)], {**BASE_SETTINGS, "dry_run": False}, ok=False)

            assert series.custom_properties["probe"]["status"] == "error"  # settled: none of the 7 came back usable
            assert all(ep.custom_properties.get("probe", {}).get("status") == "unreachable" for ep in episodes)


def test_a_series_with_one_season_confirmed_dead_and_the_rest_ok_is_partial():
    """The real case: a series in "one per season" mode where every other
    season answers and exactly one is confirmed dead must say so — not vanish
    into "error" (which would say nothing worked) or stay "pending" (which
    would say nothing is known yet)."""
    with fake_django() as models:
        series = Row(id=10, custom_properties={"episodes_fetched": True}, last_episode_refresh=None)
        season_1 = [episode(100 + n, 10, n) for n in range(1, 3)]
        for ep in season_1:
            ep.episode.season_number = 1
        dead_season = [episode(200 + n, 10, n) for n in range(1, 3)]
        for ep in dead_season:
            ep.episode.season_number = 2
        models.M3USeriesRelation.objects.rows.append(series)
        models.M3UEpisodeRelation.objects.rows.extend(season_1 + dead_season)
        with with_plugin() as plugin:
            probe_module = __import__("test_dry_run_write_safety").probe_module
            alive_ids = {ep.id for ep in season_1}

            def by_episode(url, **_k):
                ok = any(str(rid) in url for rid in alive_ids)
                return {"ok": ok, "width": 1920, "height": 1080, "quality_label": "1080p",
                        "video_codec": "h264", "hdr_type": "sdr", "summary": {}, "error": "dead"}

            plugin._units = lambda _s: [("series", 10, False)]
            probe_module.probe_stream = by_episode
            settings = {**BASE_SETTINGS, "episode_probing": "first_of_season", "dry_run": False, "batch_limit": 0, "max_probes_per_second": 0}
            plugin._probe_run(settings, scheduled=False)

            assert series.custom_properties["probe"]["status"] == "partial"
            assert all(ep.custom_properties["probe"]["status"] in ("error", "unreachable") for ep in dead_season)
            assert any(ep.custom_properties["probe"]["status"] in ("ok", "inferred") for ep in season_1)
