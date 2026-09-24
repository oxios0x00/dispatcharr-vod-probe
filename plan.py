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


def provider_episode_count(series_info):
    """Number of episodes in an Xtream get_series_info() payload: usually a
    dict keyed by season, each value a list of episodes; some panels return a
    plain list of lists."""
    episodes = (series_info or {}).get("episodes") or {}
    seasons = episodes.values() if isinstance(episodes, dict) else episodes
    return sum(len(season) for season in seasons if isinstance(season, list))


def short_versions(counts_by_series):
    """counts_by_series: {series id: [(series relation id, episodes held)]}.
    The relations holding fewer episodes than another version of the same
    series, and at least one: those with none are handled on their own. Only
    the provider can say whether the difference is real."""
    short = []
    for versions in counts_by_series.values():
        most = max((n for _, n in versions), default=0)
        short += [rid for rid, n in versions if 0 < n < most]
    return short


def next_batch(due, done, size):
    """The next batch of a run, and how many units are left to do.

    due: the units due now, as (kind, id, forced); done: the (kind, id) pairs
    this run already handled. A run goes through everything due, size units at
    a time (0 = all at once), and reads what is due again between batches, so
    it also picks up what became due meanwhile. What it already handled is
    never taken again in the same run: a failure, or any unit in a dry run,
    stays due and would otherwise come back forever."""
    remaining = [unit for unit in due if (unit[0], unit[1]) not in done]
    batch = remaining[:size] if size > 0 else remaining
    return batch, len(remaining)
