"""Deciding what to do with a tmdb_id/imdb_id resolved for a relation that had
neither, once checked against the catalogue's existing Movie/Series rows.

Kept pure (no Django, no Dispatcharr import) so the decision itself — which
row survives, which get absorbed — is unit-tested without a database. The
actual DB reads/writes (Movie.objects.filter(...), merge_movie_data, delete)
live in plugin.py; this module only takes plain objects with an `.id`."""


def _clean_id(value):
    """None for a genuinely empty external id. Dispatcharr's own bulk import
    treats '', 0 and '0' as "no id" (apps/vod/tasks.py) — the same
    normalization applied here so a detailed per-title fetch and the bulk
    listing agree on what counts as missing."""
    if value in (None, "", 0, "0"):
        return None
    return value


def extract_movie_ids(info):
    """(tmdb_id, imdb_id) from a movie's get_vod_info() 'info' block, using
    the same key priority as Dispatcharr's own bulk import for movies
    (tmdb_id checked before the shorter tmdb)."""
    return _clean_id(info.get("tmdb_id") or info.get("tmdb")), _clean_id(info.get("imdb_id") or info.get("imdb"))


def extract_series_ids(info):
    """(tmdb_id, imdb_id) from a series' get_series_info() 'info' block.

    Confirmed live on Dispatcharr-test (2026-09-28): the key there is `tmdb`,
    not `tmdb_id` — the same priority Dispatcharr's own bulk import already
    uses for series (reversed from movies). Checked on the 2 "strong" series
    still lacking a bulk id: both carry `"tmdb": "0"` (genuinely no id there
    either, not a Dispatcharr extraction gap). No imdb-shaped key has been
    observed for any provider tested (9 Cabo series: no id-like key of any
    kind; 2 strong series: `tmdb` only) — both spellings are still checked,
    the same tolerance the movie side has, in case another provider does
    include one."""
    return _clean_id(info.get("tmdb") or info.get("tmdb_id")), _clean_id(info.get("imdb") or info.get("imdb_id"))


def resolve_targets(tmdb_match, imdb_match):
    """tmdb_match/imdb_match: the existing Movie/Series row that already
    carries the freshly-resolved tmdb_id/imdb_id, or None when nothing has it
    yet. Returns (survivor, absorbed):

    - (None, []) — neither id is already taken: nothing to merge, just write
      both ids on the relation's own row.
    - (row, []) — exactly one existing row matches (or both ids point to the
      same row): the ordinary case, merge the relation's own row into it.
    - (row, [other]) — tmdb_id and imdb_id point to two DIFFERENT existing
      rows (a title that reached the catalogue twice, each copy identified by
      only one of the two ids, independently, at different times — see
      README/Journal for how this happens for real). Both `other` and the
      relation's own row are absorbed into `row`.

    The older of the two rows (the lower `id`, i.e. created first) is always
    the survivor — a fixed fact about the catalogue's history, not about
    which relation this particular pass happens to reach first, so the
    outcome never depends on processing order."""
    if tmdb_match is None and imdb_match is None:
        return None, []
    if tmdb_match is None or imdb_match is None or tmdb_match.id == imdb_match.id:
        return (tmdb_match or imdb_match), []
    survivor, loser = (tmdb_match, imdb_match) if tmdb_match.id < imdb_match.id else (imdb_match, tmdb_match)
    return survivor, [loser]
