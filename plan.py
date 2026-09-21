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

MODE_FIRST = "first_of_series"   # one probe per series version, copied to every episode
MODE_SEASON = "first_of_season"  # one probe per season of a series version
MODE_ALL = "all"                 # every episode probed individually
MODES = (MODE_FIRST, MODE_SEASON, MODE_ALL)

# How many episodes are tried, one after the other, to find one that can be
# probed, before the series is left for the next pass.
MAX_SAMPLE_TRIES = 3


def split_groups(entries, mode):
    """entries: [(relation_id, custom_properties, season)] in episode order.
    Returns the groups that are sampled on their own: one group for the whole
    series version, or one per season in "first_of_season" mode. Each group is
    [(relation_id, custom_properties)]."""
    if mode != MODE_SEASON:
        return [[(rid, cp) for rid, cp, _ in entries]]
    groups = {}
    for rid, cp, season in entries:
        groups.setdefault(season, []).append((rid, cp))
    return list(groups.values())


def _detach_foreign_copies(entries):
    """An episode copied from a relation outside this group (another season
    when sampling moved to one per season, or a sample that no longer exists)
    is treated as having no answer: it gets one from its own group."""
    ids = {rid for rid, _ in entries}
    cleaned = []
    for rid, cp in entries:
        block = (cp or {}).get("probe")
        if isinstance(block, dict) and block.get("status") == "inferred" and block.get("inferred_from") not in ids:
            cp = {k: v for k, v in cp.items() if k != "probe"}
        cleaned.append((rid, cp))
    return cleaned


def plan_series(entries, mode):
    """entries: [(relation_id, custom_properties)] for every episode of one
    series relation, in season and episode order.

    Returns {"candidates": [...], "representative": id or None, "infer": [...]}:
    - mode "all": candidates are every episode due for a probe; nothing is copied.
    - the other modes: if a measured episode exists it is the
      representative and the others still lacking a block are to be
      inferred. Otherwise the due episodes, in order, are the candidates to
      try; the caller infers the rest once one of them succeeds."""
    entries = _detach_foreign_copies(entries)
    if mode == MODE_ALL:
        return {
            "candidates": [rid for rid, cp in entries if needs_probe(cp, inferred_due=True)],
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
        "candidates": [rid for rid, cp in entries if needs_probe(cp)][:MAX_SAMPLE_TRIES],
        "representative": None,
        "infer": [],
    }


def episodes_to_infer(entries, representative):
    """After a sample succeeded: the other episodes that still lack a block."""
    entries = _detach_foreign_copies(entries)
    return [rid for rid, cp in entries if rid != representative and needs_inference(cp)]
