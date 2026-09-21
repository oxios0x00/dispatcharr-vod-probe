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
    assert needs_probe({}, NOW)
    assert needs_probe(None, NOW)
    ok = merge_success({}, RESULT, NOW)
    assert not needs_probe(ok, NOW + timedelta(days=90))
    stale = {"probe": {**ok["probe"], "schema_version": PROBE_SCHEMA_VERSION - 1}}
    assert needs_probe(stale, NOW)


def test_failed_probe_waits_then_stops_after_max_attempts():
    failed = merge_failure({}, "boom", NOW)
    assert not needs_probe(failed, NOW + timedelta(hours=1))
    assert needs_probe(failed, NOW + timedelta(hours=25))
    third = merge_failure(merge_failure(failed, "boom", NOW), "boom", NOW)
    assert not needs_probe(third, NOW + timedelta(days=30), max_attempts=3)


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
    assert not needs_probe(inferred, NOW)
    assert needs_probe(inferred, NOW, inferred_due=True)
    assert coverage_bucket(inferred) == "inferred"


def test_needs_inference():
    from contract import needs_inference

    ok = merge_success({}, RESULT, NOW)
    assert needs_inference({}) and needs_inference(merge_failure({}, "x", NOW))
    assert not needs_inference(ok)
    assert not needs_inference(ok["probe"] and {"probe": {**ok["probe"], "status": "inferred"}})
    assert needs_inference({"probe": {**ok["probe"], "schema_version": 1}})
