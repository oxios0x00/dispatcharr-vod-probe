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
