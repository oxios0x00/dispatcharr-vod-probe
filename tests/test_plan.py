import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contract import merge_failure, merge_inferred, merge_success
from plan import MODE_ALL, MODE_FIRST, episodes_to_infer, plan_season

NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)
RETRY = timedelta(hours=24)
RESULT = {"ok": True, "width": 1920, "height": 1080, "quality_label": "1080p", "video_codec": "h264",
          "hdr_type": "sdr", "summary": {}}
MEASURED = merge_success({}, RESULT, NOW)
INFERRED = merge_inferred({}, MEASURED, 1, NOW)


def plan(entries, mode=MODE_FIRST):
    return plan_season(entries, mode, NOW, RETRY, 3)


def test_first_of_season_with_nothing_probed_tries_the_first_episodes_in_order():
    result = plan([(1, {}), (2, {}), (3, {}), (4, {}), (5, {})])
    assert result["candidates"] == [1, 2, 3] and result["representative"] is None


def test_first_of_season_with_a_measured_episode_infers_the_rest_without_probing():
    result = plan([(1, MEASURED), (2, {}), (3, INFERRED), (4, merge_failure({}, "x", NOW))])
    assert result["candidates"] == [] and result["representative"] == 1
    assert result["infer"] == [2, 4]


def test_a_new_episode_of_a_known_season_is_inferred_not_probed():
    result = plan([(1, MEASURED), (2, INFERRED), (3, {})])
    assert result["candidates"] == [] and result["infer"] == [3]


def test_first_episode_failing_falls_to_the_next_one():
    failed = merge_failure({}, "boom", NOW)  # too recent to retry
    result = plan([(1, failed), (2, {}), (3, {})])
    assert result["candidates"] == [2, 3]


def test_all_mode_probes_every_episode_including_inferred_ones():
    result = plan([(1, MEASURED), (2, INFERRED), (3, {})], MODE_ALL)
    assert result["candidates"] == [2, 3] and result["infer"] == []


def test_episodes_to_infer_after_a_successful_sample():
    assert episodes_to_infer([(1, {}), (2, {}), (3, MEASURED)], 3) == [1, 2]
