import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contract import merge_failure, merge_inferred, merge_success
from plan import MODE_ALL, MODE_FIRST, episodes_to_infer, plan_series

NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)
RESULT = {"ok": True, "width": 1920, "height": 1080, "quality_label": "1080p", "video_codec": "h264",
          "hdr_type": "sdr", "summary": {}}
MEASURED = merge_success({}, RESULT, NOW)
INFERRED = merge_inferred({}, MEASURED, 1, NOW)


def plan(entries, mode=MODE_FIRST):
    return plan_series(entries, mode)


def test_first_of_series_with_nothing_probed_tries_the_first_episodes_in_order():
    result = plan([(1, {}), (2, {}), (3, {}), (4, {}), (5, {})])
    assert result["candidates"] == [1, 2, 3] and result["representative"] is None


def test_first_of_series_with_a_measured_episode_infers_the_rest_without_probing():
    result = plan([(1, MEASURED), (2, {}), (3, INFERRED), (4, merge_failure({}, "x", NOW))])
    assert result["candidates"] == [] and result["representative"] == 1
    assert result["infer"] == [2, 4]


def test_a_new_episode_of_a_known_season_is_inferred_not_probed():
    result = plan([(1, MEASURED), (2, INFERRED), (3, {})])
    assert result["candidates"] == [] and result["infer"] == [3]


def test_first_episode_failing_falls_to_the_next_one():
    failed = merge_failure({}, "boom", NOW)  # a failure is not retried on its own
    result = plan([(1, failed), (2, {}), (3, {})])
    assert result["candidates"] == [2, 3]


def test_a_failed_episode_flagged_for_retry_is_tried_again():
    from contract import flag_retry

    failed = merge_failure({}, "boom", NOW)
    result = plan([(1, flag_retry(failed)), (2, {}), (3, {})])
    assert result["candidates"] == [1, 2, 3]


def test_all_mode_probes_every_episode_including_inferred_ones():
    result = plan([(1, MEASURED), (2, INFERRED), (3, {})], MODE_ALL)
    assert result["candidates"] == [2, 3] and result["infer"] == []


def test_episodes_to_infer_after_a_successful_sample():
    assert episodes_to_infer([(1, {}), (2, {}), (3, MEASURED)], 3) == [1, 2]


def test_split_groups_by_mode():
    from plan import MODE_SEASON, split_groups

    rows = [(1, {}, 1), (2, {}, 1), (3, {}, 2), (4, {}, 2), (5, {}, 3)]
    assert split_groups(rows, MODE_FIRST) == [[(1, {}), (2, {}), (3, {}), (4, {}), (5, {})]]
    assert split_groups(rows, MODE_SEASON) == [[(1, {}), (2, {})], [(3, {}), (4, {})], [(5, {})]]


def test_copies_from_outside_the_group_are_redone_when_sampling_per_season():
    from plan import MODE_SEASON

    copied_from_season_one = merge_inferred({}, MEASURED, 1, NOW)
    season_two = [(3, copied_from_season_one), (4, copied_from_season_one)]
    result = plan(season_two, MODE_SEASON)
    assert result["candidates"] == [3, 4] and result["representative"] is None


def test_copies_from_a_relation_that_no_longer_exists_are_redone():
    orphan = merge_inferred({}, MEASURED, 999, NOW)
    result = plan([(1, orphan), (2, orphan)])
    assert result["candidates"] == [1, 2]


def test_provider_episode_count_reads_both_shapes():
    from plan import provider_episode_count

    assert provider_episode_count({"episodes": {"1": [1, 2, 3], "2": [1, 2]}}) == 5
    assert provider_episode_count({"episodes": [[1, 2], [3]]}) == 3
    assert provider_episode_count({}) == 0 and provider_episode_count(None) == 0


def test_short_versions_are_those_below_a_sibling_and_above_zero():
    from plan import short_versions

    counts = {1: [(10, 32), (11, 16)], 2: [(20, 8), (21, 8)], 3: [(30, 12), (31, 0)], 4: [(40, 5)]}
    assert short_versions(counts) == [11]
