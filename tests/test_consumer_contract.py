"""The data contract, read the way a consumer reads it.

README.md tells tools that build on this data what they can rely on. These
tests read the blocks with a small independent reader, and fail when a change
would break such a tool: a documented field gone, a status renamed, a summary
that no longer says when a series is fully answered."""
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contract import flag_reload, flag_retry, merge_failure, merge_inferred, merge_success, series_marker
from probe import PROBE_SCHEMA_VERSION

NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)
ROOT = os.path.join(os.path.dirname(__file__), "..")

RESULT = {
    "ok": True, "width": 3840, "height": 2160, "quality_label": "2160p", "video_codec": "hevc",
    "video_bitrate": 13_954_901, "hdr_type": "hdr10", "duration_secs": 6776.0,
    "summary": {
        "format": {"format_name": "matroska,webm", "bit_rate": "17034152"},
        "video": [{"profile": "Main 10", "pix_fmt": "yuv420p10le", "avg_frame_rate": "24/1"}],
        "audio": [
            {"codec_name": "eac3", "channels": 6, "language": "eng"},
            {"codec_name": "eac3", "channels": 6, "language": "fre"},
            {"codec_name": "aac", "channels": 2, "language": "fre", "flags": ["visual_impaired"]},
        ],
    },
}


# --- the reader a consumer would write from the README ------------------------------

def block(properties):
    found = (properties or {}).get("probe")
    return found if isinstance(found, dict) else None


def usable_tier(properties):
    found = block(properties)
    return found.get("tier") if found and found.get("status") in ("ok", "inferred") else None


def failed(properties):
    found = block(properties)
    return bool(found) and found.get("status") in ("error", "unreachable")


def languages(properties):
    tracks = (block(properties) or {}).get("audio") or []
    return [t.get("language", "und") for t in tracks if not t.get("audio_description")]


def series_answered(properties):
    found = block(properties)
    return bool(found) and found.get("status") == "ok"


# --- what the README documents ---------------------------------------------------------

DOCUMENTED_RELATION_FIELDS = {
    "schema_version", "status", "probed_at", "tier", "hdr", "video", "bit_rate",
    "audio_languages", "audio", "duration_secs", "container", "source",
}
DOCUMENTED_SERIES_FIELDS = {
    "schema_version", "status", "probed_at", "last_modified", "episodes", "seasons", "mode", "sampled_from", "source",
}


def test_a_measured_relation_carries_every_documented_field():
    props = merge_success({"basic_data": {}}, RESULT, NOW)
    assert DOCUMENTED_RELATION_FIELDS <= set(props["probe"])
    assert props["quality"] == "4K" and props["resolution"] == "3840x2160"
    assert usable_tier(props) == "2160p" and not failed(props)
    assert languages(props) == ["eng", "fre"]  # the audio-description track is not a language
    assert props["probe"]["audio_languages"] == ["eng", "fre"]
    assert props["probe"]["bit_rate"] == 17_034_152 and props["probe"]["video"]["bit_rate"] == 13_954_901


def test_an_inferred_copy_is_usable_and_says_where_it_comes_from():
    source = merge_success({}, RESULT, NOW)
    copy = merge_inferred({}, source, 42, NOW)
    assert usable_tier(copy) == "2160p" and copy["probe"]["status"] == "inferred" and copy["probe"]["inferred_from"] == 42
    assert languages(copy) == ["eng", "fre"] and copy["quality"] == "4K"
    assert "duration_secs" not in copy["probe"]


def test_a_failure_is_readable_and_keeps_the_last_known_quality():
    good = merge_success({}, RESULT, NOW)
    for unreachable, status in ((False, "error"), (True, "unreachable")):
        props = merge_failure(good, "no video stream found", NOW, unreachable=unreachable)
        assert failed(props) and block(props)["status"] == status
        assert props["quality"] == "4K" and props["resolution"] == "3840x2160"
        assert usable_tier(props) is None
    assert block({"basic_data": {}}) is None  # never handled: no block


def test_a_series_is_answered_only_when_its_summary_says_ok():
    fetched = {"episodes_fetched": True, "basic_data": {"last_modified": "5"}}
    done = series_marker(fetched, 5, 8, 1, "first_of_series", 99, NOW, "ok")
    assert series_answered(done) and DOCUMENTED_SERIES_FIELDS <= set(done["probe"])
    assert done["probe"]["last_modified"] == "5" and done["probe"]["episodes"] == 8 and done["probe"]["mode"] == "first_of_series"
    pending = series_marker(fetched, 5, 8, 1, "first_of_series", None, NOW, "pending")
    assert not series_answered(pending) and pending["probe"]["status"] == "pending"
    assert not series_answered(fetched)


def test_internal_flags_do_not_change_what_a_consumer_reads():
    done = series_marker({"episodes_fetched": True}, 5, 8, 1, "first_of_series", 99, NOW, "ok")
    assert series_answered(flag_reload(done, NOW)) and series_answered(done)
    failed_props = merge_failure({}, "boom", NOW)
    assert failed(flag_retry(failed_props))


def test_the_readme_states_the_current_schema_version():
    readme = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
    assert re.search(r"It is currently \*\*%d\*\*" % PROBE_SCHEMA_VERSION, readme), (
        "README.md says another schema_version than probe.PROBE_SCHEMA_VERSION"
    )
    assert f'"schema_version": {PROBE_SCHEMA_VERSION}' in readme
