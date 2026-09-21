import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from breaker import tripped


def test_a_run_of_failures_trips_after_five_attempts():
    assert not tripped(0, 4, 0, 0, 0.8)
    assert tripped(0, 5, 0, 0, 0.8)


def test_the_ratio_is_a_threshold_not_a_count():
    assert not tripped(2, 8, 0, 0, 0.8)   # exactly 0.8 does not trip
    assert tripped(1, 9, 0, 0, 0.8)


def test_retried_failures_are_left_out_of_the_count():
    assert not tripped(0, 26, 26, 26, 0.8)  # the known dead links, retried
    assert not tripped(5, 26, 26, 26, 0.8)  # five new probes worked, the 26 retries failed
    assert tripped(0, 31, 26, 26, 0.8)      # five new probes failed as well: a real outage
