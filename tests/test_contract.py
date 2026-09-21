import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contract import (
    build_probe_block, coverage_bucket, merge_failure, merge_success, needs_probe, quality_fields,
)
from probe import PROBE_SCHEMA_VERSION

NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)

RESULT = {
    "ok": True, "width": 3840, "height": 1608, "quality_label": "2160p",
    "video_codec": "hevc", "video_bitrate": 18_000_000, "hdr_type": "dolby_vision",
    "subtitle_languages": ["fre", "eng"], "duration_secs": 7200.5,
    "summary": {
        "format": {"format_name": "matroska,webm", "bit_rate": "19762136"},
        "video": [{"profile": "Main 10", "pix_fmt": "yuv420p10le", "avg_frame_rate": "24000/1001"}],
        "audio": [
            {"codec_name": "eac3", "channels": 6, "language": "fre"},
            {"codec_name": "aac", "channels": 2, "language": "fre", "flags": ["visual_impaired"]},
        ],
        "subtitles": {"codecs": {"subrip": 2}},
    },
}


def test_quality_uses_dispatcharr_vocabulary():
    assert quality_fields(RESULT) == {"quality": "4K", "resolution": "3840x1608"}


def test_unknown_tier_writes_no_quality():
    assert quality_fields({"quality_label": "unknown", "width": None, "height": None}) == {}


def test_probe_block_details():
    block = build_probe_block(RESULT, NOW)
    assert block["schema_version"] == PROBE_SCHEMA_VERSION
    assert block["probed_at"] == "2026-09-21T12:00:00Z"
    assert block["status"] == "ok" and block["tier"] == "2160p" and block["hdr"] == "dolby_vision"
    assert block["video"] == {
        "codec": "hevc", "profile": "Main 10", "bit_depth": 10, "bit_rate": 18_000_000, "frame_rate": 23.976,
    }
    assert "audio_description" not in block["audio"][0]
    assert block["audio"][1]["audio_description"] is True
    assert block["audio_languages"] == ["fre"]
    assert "subtitles" not in block
    assert block["container"] == "matroska,webm"
    assert block["bit_rate"] == 19_762_136


def test_merge_success_keeps_other_keys():
    existing = {"basic_data": {"a": 1}, "detailed_fetched": True}
    merged = merge_success(existing, RESULT, NOW)
    assert merged["basic_data"] == {"a": 1} and merged["detailed_fetched"] is True
    assert merged["quality"] == "4K"
    assert "quality" not in existing  # input not mutated


def test_failure_keeps_last_quality_and_counts_attempts():
    good = merge_success({}, RESULT, NOW)
    failed = merge_failure(good, "timeout after 25s", NOW)
    assert failed["quality"] == "4K" and failed["resolution"] == "3840x1608"
    assert failed["probe"]["status"] == "unreachable" and failed["probe"]["attempts"] == 1
    assert failed["probe"]["tier"] == "2160p"
    assert merge_failure(failed, "no video stream found", NOW)["probe"]["attempts"] == 2
    assert merge_failure({}, "no video stream found", NOW)["probe"]["status"] == "error"


def test_needs_probe():
    assert needs_probe({})
    assert needs_probe(None)
    ok = merge_success({}, RESULT, NOW)
    assert not needs_probe(ok)
    stale = {"probe": {**ok["probe"], "schema_version": PROBE_SCHEMA_VERSION - 1}}
    assert needs_probe(stale)


def test_a_failed_probe_is_never_retried_on_its_own_only_when_flagged():
    from contract import flag_retry

    failed = merge_failure({}, "boom", NOW)
    assert not needs_probe(failed)
    flagged = flag_retry(failed)
    assert flagged["probe"]["retry"] is True and "retry" not in failed["probe"]  # the input is not modified
    assert needs_probe(flagged)
    again = merge_failure(flagged, "boom", NOW)  # the retry happened and failed again
    assert "retry" not in again["probe"] and again["probe"]["attempts"] == 2
    assert not needs_probe(again)


def test_flag_retry_leaves_good_and_missing_blocks_alone():
    from contract import flag_retry

    ok = merge_success({}, RESULT, NOW)
    assert flag_retry(ok) == ok and flag_retry({}) == {}


def test_coverage_bucket():
    assert coverage_bucket({}) == "never"
    assert coverage_bucket(merge_success({}, RESULT, NOW)) == "ok"
    assert coverage_bucket(merge_failure({}, "x", NOW)) == "error"


def test_error_never_carries_the_provider_url():
    error = "http://host.example/movie/user123/pass456/42.mkv: Server returned 400 Bad Request"
    block = merge_failure({}, error, NOW)["probe"]
    assert "user123" not in block["error"] and "pass456" not in block["error"]
    assert block["error"] == "<url>: Server returned 400 Bad Request"


def test_inferred_copy_is_marked_and_drops_the_episode_duration():
    from contract import merge_inferred

    source = merge_success({"basic_data": {"a": 1}}, RESULT, NOW)
    target = merge_inferred({"basic_data": {"b": 2}}, source, 42, NOW)
    assert target["basic_data"] == {"b": 2}
    assert target["quality"] == "4K" and target["resolution"] == "3840x1608"
    block = target["probe"]
    assert block["status"] == "inferred" and block["inferred_from"] == 42
    assert "duration_secs" not in block and block["tier"] == "2160p" and block["hdr"] == "dolby_vision"
    assert "duration_secs" in source["probe"]  # the source is not modified


def test_inferred_block_counts_as_done_unless_every_episode_must_be_measured():
    from contract import merge_inferred

    inferred = merge_inferred({}, merge_success({}, RESULT, NOW), 1, NOW)
    assert not needs_probe(inferred)
    assert needs_probe(inferred, inferred_due=True)
    assert coverage_bucket(inferred) == "inferred"


def test_needs_inference():
    from contract import needs_inference

    ok = merge_success({}, RESULT, NOW)
    assert needs_inference({}) and needs_inference(merge_failure({}, "x", NOW))
    assert not needs_inference(ok)
    assert not needs_inference(ok["probe"] and {"probe": {**ok["probe"], "status": "inferred"}})
    assert needs_inference({"probe": {**ok["probe"], "schema_version": 1}})


def test_series_marker_ok_and_pending():
    from contract import series_marker

    ok = series_marker({"basic_data": {"last_modified": "5"}, "episodes_fetched": True}, 5, 20, 2, "first_of_series", 99, NOW)
    assert ok["basic_data"] == {"last_modified": "5"} and ok["episodes_fetched"] is True
    marker = ok["probe"]
    assert marker["status"] == "ok" and marker["episodes"] == 20 and marker["seasons"] == 2
    assert marker["last_modified"] == "5" and marker["sampled_from"] == 99 and "attempts" not in marker
    pending = series_marker(ok, 5, 20, 2, "first_of_series", None, NOW)["probe"]
    assert pending["status"] == "pending" and pending["attempts"] == 1
    assert series_marker({}, 5, 0, 0, "first_of_series", None, NOW)["probe"]["status"] == "pending"


def test_series_work():
    from contract import series_marker, series_work

    fresh = {"episodes_fetched": True, "basic_data": {"last_modified": "5"}}
    assert series_work({"episodes_fetched": False}, 0, "first_of_series") == {"reason": "load", "reload": True}
    assert series_work(fresh, 20, "first_of_series") == {"reason": "unmarked", "reload": False}
    done = series_marker(fresh, 5, 20, 2, "first_of_series", 99, NOW)
    assert series_work(done, 20, "first_of_series") is None
    assert series_work(done, 21, "first_of_series") == {"reason": "count", "reload": False}
    moved = {**done, "basic_data": {"last_modified": "6"}}
    assert series_work(moved, 20, "first_of_series") == {"reason": "changed", "reload": True}
    assert series_work(done, 20, "all") == {"reason": "mode", "reload": False}
    pending = series_marker(fresh, 5, 20, 2, "first_of_series", None, NOW)
    assert series_work(pending, 20, "first_of_series") is None  # left alone until Retry Errors
    from contract import flag_retry

    assert series_work(flag_retry(pending), 20, "first_of_series") == {"reason": "retry", "reload": False}


def test_a_reload_by_dispatcharr_after_our_summary_forces_reprocessing():
    from contract import series_marker, series_work

    fresh = {"episodes_fetched": True, "basic_data": {"last_modified": "5"}}
    done = series_marker(fresh, 5, 20, 2, "first_of_series", 99, NOW)
    assert series_work(done, 20, "first_of_series", last_episode_refresh=NOW - timedelta(hours=1)) is None
    # our own reload, a fraction of a second before the summary's truncated time
    assert series_work(done, 20, "first_of_series", last_episode_refresh=NOW + timedelta(seconds=1)) is None
    assert series_work(done, 20, "first_of_series", last_episode_refresh=NOW + timedelta(minutes=5)) == {
        "reason": "reloaded", "reload": False,
    }


def test_a_more_thorough_mode_redoes_a_series_a_lighter_one_does_not():
    from contract import series_marker, series_work

    fresh = {"episodes_fetched": True, "basic_data": {"last_modified": "5"}}
    done = series_marker(fresh, 5, 20, 2, "first_of_season", 99, NOW)
    assert series_work(done, 20, "first_of_series") is None
    assert series_work(done, 20, "first_of_season") is None
    assert series_work(done, 20, "all") == {"reason": "mode", "reload": False}
    light = series_marker(fresh, 5, 20, 2, "first_of_series", 99, NOW)
    assert series_work(light, 20, "first_of_season") == {"reason": "mode", "reload": False}


def test_a_series_loaded_with_no_episode_is_asked_for_again_a_few_times():
    from contract import MAX_EMPTY_RELOADS, series_marker, series_work

    fetched = {"episodes_fetched": True, "basic_data": {"last_modified": "5"}}
    assert series_work(fetched, 0, "first_of_series") == {"reason": "empty", "reload": True}
    marker = fetched
    for attempt in range(MAX_EMPTY_RELOADS):
        assert series_work(marker, 0, "first_of_series") == {"reason": "empty", "reload": True}
        marker = series_marker(marker, 5, 0, 0, "first_of_series", None, NOW)
        assert marker["probe"]["attempts"] == attempt + 1
    assert series_work(marker, 0, "first_of_series") is None  # left alone


def test_retry_errors_reloads_a_series_with_no_episode_but_not_one_that_has_some():
    from contract import flag_retry, series_marker, series_work

    fetched = {"episodes_fetched": True, "basic_data": {"last_modified": "5"}}
    empty = flag_retry(series_marker(series_marker(series_marker(fetched, 5, 0, 0, "first_of_series", None, NOW), 5, 0, 0, "first_of_series", None, NOW), 5, 0, 0, "first_of_series", None, NOW))
    assert series_work(empty, 0, "first_of_series") == {"reason": "retry", "reload": True}
    pending = flag_retry(series_marker(fetched, 5, 20, 2, "first_of_series", None, NOW))
    assert series_work(pending, 20, "first_of_series") == {"reason": "retry", "reload": False}


def test_flag_reload_makes_the_next_run_ask_the_provider_again():
    from contract import flag_reload, series_marker, series_work

    fetched = {"episodes_fetched": True, "basic_data": {"last_modified": "5"}}
    done = series_marker(fetched, 5, 8, 1, "first_of_series", 99, NOW)
    assert series_work(done, 8, "first_of_series") is None
    flagged = flag_reload(done, NOW)
    assert flagged["probe"]["episodes"] == 8 and flagged["probe"]["reload"] is True
    assert series_work(flagged, 8, "first_of_series") == {"reason": "incomplete", "reload": True}
    assert series_work(flag_reload(fetched, NOW), 8, "first_of_series") == {"reason": "incomplete", "reload": True}
    assert "reload" not in series_marker(flagged, 5, 20, 1, "first_of_series", 99, NOW)["probe"]  # a new summary clears it


def test_an_all_mode_series_with_failed_episodes_is_pending_not_ok():
    from contract import series_marker, series_work

    fetched = {"episodes_fetched": True, "basic_data": {"last_modified": "5"}}
    failed = series_marker(fetched, 5, 40, 3, "all", None, NOW)["probe"]   # _run_series passes no sample when an episode failed
    assert failed["status"] == "pending" and failed["attempts"] == 1
    done = series_marker(fetched, 5, 40, 3, "all", 7, NOW)["probe"]
    assert done["status"] == "ok" and "attempts" not in done
    assert series_work({**fetched, "probe": failed}, 40, "all") is None  # left alone until Retry Errors


def test_a_failed_episode_makes_its_series_visited_again():
    from contract import flag_series_retry, series_marker, series_work

    fetched = {"episodes_fetched": True, "basic_data": {"last_modified": "5"}}
    done = series_marker(fetched, 5, 40, 3, "all", 7, NOW)
    assert series_work(done, 40, "all") is None
    flagged = flag_series_retry(done)
    assert flagged["probe"]["retry"] is True and "retry" not in done["probe"]
    assert series_work(flagged, 40, "all") == {"reason": "retry", "reload": False}
    assert "retry" not in series_marker(flagged, 5, 40, 3, "all", 7, NOW)["probe"]  # visiting the series clears it
    assert flag_series_retry(fetched) == fetched  # no summary: it is processed anyway
