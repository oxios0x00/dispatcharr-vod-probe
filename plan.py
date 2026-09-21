"""What to probe and what to copy inside one version of a series.

Pure Python, no Django. The sampling key is one series relation: the standard
and the 4K version of a series are separate relations (often in the same
account), each with its own files, so they never share a sample. Within a
version, all episodes are assumed to have similar characteristics, whatever the
season."""
try:
    from .contract import is_measured, needs_inference, needs_probe
except ImportError:  # imported as a top-level module by the unit tests
    from contract import is_measured, needs_inference, needs_probe

MODE_FIRST = "first_of_series"
MODE_ALL = "all"

# How many episodes are tried, one after the other, to find one that can be
# probed, before the series is left for the next pass.
MAX_SAMPLE_TRIES = 3


def plan_series(entries, mode, now, retry_after, max_attempts):
    """entries: [(relation_id, custom_properties)] for every episode of one
    series relation, in season and episode order.

    Returns {"candidates": [...], "representative": id or None, "infer": [...]}:
    - mode "all": candidates are every episode due for a probe; nothing is copied.
    - mode "first_of_series": if a measured episode exists it is the
      representative and the others still lacking a block are to be
      inferred. Otherwise the due episodes, in order, are the candidates to
      try; the caller infers the rest once one of them succeeds."""
    if mode == MODE_ALL:
        return {
            "candidates": [rid for rid, cp in entries if needs_probe(cp, now, retry_after, max_attempts, inferred_due=True)],
            "representative": None,
            "infer": [],
        }
    representative = next((rid for rid, cp in entries if is_measured(cp)), None)
    if representative is not None:
        return {
            "candidates": [],
            "representative": representative,
            "infer": [rid for rid, cp in entries if rid != representative and needs_inference(cp)],
        }
    return {
        "candidates": [rid for rid, cp in entries if needs_probe(cp, now, retry_after, max_attempts)][:MAX_SAMPLE_TRIES],
        "representative": None,
        "infer": [],
    }


def episodes_to_infer(entries, representative):
    """After a sample succeeded: the other episodes that still lack a block."""
    return [rid for rid, cp in entries if rid != representative and needs_inference(cp)]
