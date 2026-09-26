"""A dry run must never write anything, and must never leave a relation in a
state where the next real run treats it differently than a fresh one would.

plugin.py uses relative imports (it is a package once installed), so it is
loaded here as one, with the Django pieces it reaches into stood in for."""
import contextlib
import importlib.util
import os
import sys
import tempfile
import types

ROOT = os.path.join(os.path.dirname(__file__), "..")


def load_plugin_module():
    spec = importlib.util.spec_from_file_location(
        "vod_probe_pkg", os.path.join(ROOT, "__init__.py"), submodule_search_locations=[ROOT]
    )
    package = importlib.util.module_from_spec(spec)
    sys.modules["vod_probe_pkg"] = package
    spec.loader.exec_module(package)
    return importlib.import_module("vod_probe_pkg.plugin")


plugin_module = load_plugin_module()
probe_module = importlib.import_module("vod_probe_pkg.probe")
from vod_probe_pkg.contract import needs_probe  # noqa: E402


def _get_path(row, path):
    value = row
    for part in path.split("__"):
        value = getattr(value, part)
    return value


class ValuesList(list):
    def first(self):
        return self[0] if self else None


class FakeQuerySet:
    def __init__(self, manager, rows):
        self.manager, self.rows = manager, rows

    def filter(self, **kwargs):
        rows = self.rows
        for key, wanted in kwargs.items():
            if key.endswith("__in"):
                rows = [r for r in rows if _get_path(r, key[: -len("__in")]) in wanted]
            else:
                rows = [r for r in rows if _get_path(r, key) == wanted]
        return FakeQuerySet(self.manager, rows)

    def select_related(self, *_a, **_k):
        return self

    def select_for_update(self):
        return self

    def order_by(self, *fields):
        rows = list(self.rows)
        for field in reversed(fields):
            rows.sort(key=lambda r: _get_path(r, field))
        return FakeQuerySet(self.manager, rows)

    def values_list(self, *fields, flat=False):
        if flat:
            return ValuesList(_get_path(r, fields[0]) for r in self.rows)
        return ValuesList(tuple(_get_path(r, f) for f in fields) for r in self.rows)

    def __iter__(self):
        return iter(self.rows)

    def first(self):
        return self.rows[0] if self.rows else None

    def exists(self):
        return bool(self.rows)

    def update(self, **kwargs):
        for row in self.rows:
            for key, value in kwargs.items():
                setattr(row, key, value)
        return len(self.rows)

    def count(self):
        return len(self.rows)

    def get(self, **kwargs):
        matches = self.filter(**kwargs).rows
        if len(matches) != 1:
            raise LookupError(f"expected exactly 1 row, found {len(matches)}")
        return matches[0]


class FakeManager(FakeQuerySet):
    """Both the manager (model.objects) and a queryset: real Django's manager
    is one too, and plugin.py chains straight off model.objects."""

    def __init__(self, rows):
        self.rows = rows  # the master, mutable list; a filtered FakeQuerySet only ever views it
        self.manager = self

    def bulk_update(self, objs, fields):
        by_id = {r.id: r for r in self.rows}
        for obj in objs:
            target = by_id.get(obj.id)
            if target is not None:
                for field in fields:
                    setattr(target, field, getattr(obj, field))


class Row:
    def __init__(self, id, custom_properties=None, **extra):
        self.id = id
        self.custom_properties = dict(custom_properties or {})
        for key, value in extra.items():
            setattr(self, key, value)


def make_model(name, rows):
    model = type(name, (Row,), {})
    model.objects = FakeManager(list(rows))
    return model


@contextlib.contextmanager
def fake_django(movies=(), episodes=(), series=(), refresh_series_episodes_calls=None):
    names = ("django", "django.db", "apps", "apps.vod", "apps.vod.models", "apps.vod.tasks")
    saved = {name: sys.modules.get(name) for name in names}

    django_module, db_module = types.ModuleType("django"), types.ModuleType("django.db")
    db_module.transaction = types.SimpleNamespace(atomic=contextlib.nullcontext)
    db_module.connections = types.SimpleNamespace(close_all=lambda: None)
    django_module.db = db_module

    models_module = types.ModuleType("apps.vod.models")
    models_module.M3UMovieRelation = make_model("M3UMovieRelation", movies)
    models_module.M3USeriesRelation = make_model("M3USeriesRelation", series)
    models_module.M3UEpisodeRelation = make_model("M3UEpisodeRelation", episodes)

    calls = refresh_series_episodes_calls if refresh_series_episodes_calls is not None else []
    tasks_module = types.ModuleType("apps.vod.tasks")
    tasks_module.refresh_series_episodes = lambda *args: calls.append(args)

    apps_module, vod_module = types.ModuleType("apps"), types.ModuleType("apps.vod")
    apps_module.vod, vod_module.models, vod_module.tasks = vod_module, models_module, tasks_module

    sys.modules.update({
        "django": django_module, "django.db": db_module,
        "apps": apps_module, "apps.vod": vod_module,
        "apps.vod.models": models_module, "apps.vod.tasks": tasks_module,
    })
    try:
        yield models_module
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


@contextlib.contextmanager
def with_plugin():
    tmp = tempfile.mkdtemp()
    os.environ["VOD_PROBE_DATA_DIR"] = tmp
    try:
        plugin = plugin_module.Plugin()
        # The selection query (_active_relations) needs a category model and
        # Exists/OuterRef this test does not stand in for: the units a run
        # works on are given directly instead, which is what these tests are
        # about — not which relations are selected, already covered by
        # test_contract.py and test_plan.py.
        yield plugin
    finally:
        os.environ.pop("VOD_PROBE_DATA_DIR", None)


def run(plugin, units, settings, ok=True, error="ffprobe: connection refused"):
    """One Probe Run, with the ffprobe call stood in for."""
    plugin._units = lambda _settings: units
    result = {"ok": ok}
    if ok:
        result.update(width=1920, height=1080, quality_label="1080p", video_codec="h264", hdr_type="sdr", summary={})
    else:
        result["error"] = error
    probe_module.probe_stream = lambda *_a, **_k: dict(result)
    return plugin._probe_run({**settings, "batch_limit": 0, "max_probes_per_second": 0}, scheduled=False)


BASE_SETTINGS = {"episode_probing": "first_of_series"}


def test_a_successful_dry_run_writes_nothing_and_the_relation_stays_due():
    with fake_django() as models:
        movie = Row(id=1, custom_properties={})
        movie.get_stream_url = lambda: "http://provider/movie/1"
        models.M3UMovieRelation.objects.rows.append(movie)
        with with_plugin() as plugin:
            result = run(plugin, [("movies", 1, False)], {**BASE_SETTINGS, "dry_run": True})
            assert "1 probed" in result["message"]
            assert movie.custom_properties == {}
            assert needs_probe(movie.custom_properties) is True


def test_a_failed_dry_run_writes_nothing_and_the_relation_stays_due():
    with fake_django() as models:
        movie = Row(id=1, custom_properties={})
        movie.get_stream_url = lambda: "http://provider/movie/1"
        models.M3UMovieRelation.objects.rows.append(movie)
        with with_plugin() as plugin:
            run(plugin, [("movies", 1, False)], {**BASE_SETTINGS, "dry_run": True}, ok=False)
            assert movie.custom_properties == {}
            assert needs_probe(movie.custom_properties) is True


def test_dry_run_never_loads_a_series_episode_list():
    """A series never loaded (no episodes_fetched) needs a reload; the loader
    itself must never run in a dry run, not just its result stay unwritten —
    checked by a call counter, not by relying on an exception to surface: a
    unit's exception is caught and counted as an ordinary probe error, so a
    raising stand-in would pass silently instead of failing the test."""
    calls = []
    with fake_django(refresh_series_episodes_calls=calls) as models:
        # Set so that, without the dry-run guard, the call reaches the counter
        # instead of failing earlier on a missing attribute (still silently
        # counted as an ordinary probe error either way).
        series = Row(id=10, custom_properties={}, last_episode_refresh=None, m3u_account=object(), series=object(), external_series_id="1")
        models.M3USeriesRelation.objects.rows.append(series)
        with with_plugin() as plugin:
            run(plugin, [("series", 10, False)], {**BASE_SETTINGS, "dry_run": True})
            assert series.custom_properties == {}
            assert calls == []


def test_dry_run_does_not_copy_results_to_other_episodes_or_write_the_series_summary():
    with fake_django() as models:
        series = Row(id=10, custom_properties={"episodes_fetched": True}, last_episode_refresh=None)
        episodes = [
            Row(id=100 + n, custom_properties={}, series_relation_id=10, episode=types.SimpleNamespace(season_number=1, episode_number=n))
            for n in range(1, 4)
        ]
        for ep in episodes:
            ep.get_stream_url = lambda: "http://provider/episode"
        models.M3USeriesRelation.objects.rows.append(series)
        models.M3UEpisodeRelation.objects.rows.extend(episodes)
        with with_plugin() as plugin:
            run(plugin, [("series", 10, False)], {**BASE_SETTINGS, "dry_run": True})
            assert series.custom_properties == {"episodes_fetched": True}
            assert all(ep.custom_properties == {} for ep in episodes)
            assert all(needs_probe(ep.custom_properties) for ep in episodes)


def test_a_real_run_after_a_dry_run_behaves_exactly_like_a_fresh_real_run():
    with fake_django() as models:
        movie = Row(id=1, custom_properties={})
        movie.get_stream_url = lambda: "http://provider/movie/1"
        models.M3UMovieRelation.objects.rows.append(movie)
        with with_plugin() as plugin:
            run(plugin, [("movies", 1, False)], {**BASE_SETTINGS, "dry_run": True})
            assert movie.custom_properties == {}  # confirmed above too, kept as a precondition here

            result = run(plugin, [("movies", 1, False)], {**BASE_SETTINGS, "dry_run": False})
            assert "1 probed" in result["message"]
            assert movie.custom_properties["quality"] == "1080p"
            assert movie.custom_properties["probe"]["status"] == "ok"
            assert needs_probe(movie.custom_properties) is False
