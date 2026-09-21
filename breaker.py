"""When a run stops itself because too many probes fail.

Pure Python, no Django. The point is to stop hammering a provider that is
failing (an outage, a ban), so a probe that was asked for again after it had
already failed (Retry Errors, or the switch) is left out of the count: those
relations are known to be dead links, and retrying them must not stop a run."""

MIN_ATTEMPTS = 5


def tripped(probed, errors, retried, retried_errors, ratio):
    """probed: probes that succeeded, errors: probes that failed, retried: the
    attempts among them that were retries of an earlier failure, retried_errors:
    the failures among those retries."""
    attempts = (probed + errors) - retried
    failures = errors - retried_errors
    return attempts >= MIN_ATTEMPTS and failures / attempts > ratio
