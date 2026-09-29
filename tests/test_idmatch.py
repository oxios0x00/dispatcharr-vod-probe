import os
import sys
from collections import namedtuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from idmatch import extract_movie_ids, extract_series_ids, resolve_targets

Row = namedtuple("Row", ["id"])


def test_extract_movie_ids_prefers_the_long_key():
    assert extract_movie_ids({"tmdb_id": 1917, "tmdb": 999, "imdb_id": "tt123"}) == (1917, "tt123")
    assert extract_movie_ids({"tmdb": 2012}) == (2012, None)


def test_extract_series_ids_prefers_the_short_key():
    # Confirmed live on Dispatcharr-test: series use `tmdb`, not `tmdb_id`.
    assert extract_series_ids({"tmdb": 456, "tmdb_id": 999}) == (456, None)
    assert extract_series_ids({"tmdb_id": 456}) == (456, None)  # fallback, in case a provider differs


def test_extract_ids_treats_zero_and_blank_as_missing():
    # Real payload from a "strong" series still lacking an id: {"tmdb": "0", ...}.
    assert extract_series_ids({"tmdb": "0"}) == (None, None)
    assert extract_movie_ids({"tmdb_id": 0, "imdb_id": ""}) == (None, None)


def test_extract_ids_on_a_provider_with_nothing_at_all():
    # Real payload from Cabo's series detail: no tmdb/imdb key of any kind.
    assert extract_series_ids({"name": "x", "plot": "y", "category_id": 665}) == (None, None)


def test_neither_id_taken_means_nothing_to_merge():
    assert resolve_targets(None, None) == (None, [])


def test_only_tmdb_taken_survives_alone():
    a = Row(id=5)
    assert resolve_targets(a, None) == (a, [])


def test_only_imdb_taken_survives_alone():
    b = Row(id=7)
    assert resolve_targets(None, b) == (b, [])


def test_both_ids_point_to_the_same_row_is_the_ordinary_case():
    a = Row(id=5)
    assert resolve_targets(a, a) == (a, [])


def test_three_way_conflict_the_older_row_survives_whichever_id_it_matched():
    older, younger = Row(id=5), Row(id=9)
    assert resolve_targets(older, younger) == (older, [younger])
    assert resolve_targets(younger, older) == (older, [younger])
