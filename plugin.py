"""VOD Probe — writes each VOD relation's real quality into Dispatcharr's
catalogue. See DESIGN.md for the contract and the roadmap.

Probe Run probes for real. With Dry run on (the default) it only reports what
it would write; with it off it writes quality, resolution and probe into the
relations' custom_properties.

Movies are probed one relation at a time. For series, each series relation (one
version of a series) has its episodes loaded if needed, then one episode is
probed and its result copied to all the others (marked "inferred"), or every
episode is probed, depending on the Episodes setting. Every episode ends up
with an answer either way. A small summary written on the series relation
(the provider's last_modified, the episode and season counts) lets the daily
scan read only series relations and open only the ones that changed.

Probe Run runs in a Celery worker, not in the request that clicked it: a pass
takes minutes, longer than the browser or nginx will wait, so a synchronous
click ended in a 504 while the work carried on unseen (the same lesson as
vod-manager). The click only queues the task; Run Status shows how far it got
and Pause stops it after the relation in progress."""
import json
import os
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

# Sibling modules are imported inside methods, not at module top level, to
# avoid stale references across a plugin reload cycle (same pattern as
# vod-manager).


def _release_db_connections():
    """Hands this thread's database connection back to Dispatcharr's pool.
    Under its gevent pool every worker thread checks a connection out on its
    first query and nobody returns it; batch after batch this filled the pool
    and froze Dispatcharr (vod-manager, 2026-09-19)."""
    from django.db import connections

    connections.close_all()


class _RateLimiter:
    """Minimum interval between probe starts, shared by the worker threads."""

    def __init__(self, max_per_second):
        self._min_interval = 1.0 / max_per_second if max_per_second > 0 else 0
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def wait(self):
        if self._min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            start_at = max(now, self._next_allowed)
            self._next_allowed = start_at + self._min_interval
        sleep_for = start_at - time.monotonic()
        if sleep_for > 0:
            time.sleep(sleep_for)


class Plugin:
    name = "VOD Probe"
    version = "0.6.0"
    description = (
        "Probes the real quality of each VOD relation with ffprobe and writes it "
        "into the relation's custom_properties, so every tool reading "
        "Dispatcharr's API can use it."
    )
    author = "oxios0x00"
    help_url = "https://github.com/oxios0x00/vod-probe"

    SCHEDULED_TASK_CELERY_NAME = "vod_probe.run"
    SCHEDULE_TASK_NAME = "vod_probe.auto_run"
    _BACKGROUND_ACTIONS = {"probe_run"}
    _LOCK = "probe_run"
    # A run renews its lock after every relation; a lock silent this long
    # belongs to a run that died with its worker.
    _LOCK_STALE_SECONDS = 900
    _NOTIFICATION_KEY_PREFIX = "vod-probe-run-"

    def __init__(self):
        from .state import State

        data_dir = os.environ.get(
            "VOD_PROBE_DATA_DIR", os.path.join(os.path.dirname(__file__), "data")
        )
        self.state = State(data_dir)

    # --- dispatch ---------------------------------------------------------

    def run(self, action_id, params, context):
        settings = context.get("settings", {})
        try:
            if action_id in self._BACKGROUND_ACTIONS and not context.get("background"):
                return self._start_background(action_id, settings)
            if action_id == "probe_run":
                return self._probe_run(settings)
            if action_id == "run_status":
                return self._run_status()
            if action_id == "pause":
                self.state.set_paused(True)
                return {"status": "ok", "message": "Paused: the run stops after the relation in progress."}
            if action_id == "resume":
                self.state.set_paused(False)
                return {"status": "ok", "message": "Resumed. Start Probe Run again."}
            if action_id == "apply_schedule":
                return self._apply_schedule(settings)
            if action_id == "remove_schedule":
                return self._remove_schedule()
            if action_id == "schedule_status":
                return self._schedule_status()
            if action_id == "test_fire_schedule":
                return self._test_fire_schedule(settings)
            if action_id == "scan":
                return self._scan(settings)
            if action_id == "coverage":
                return self._coverage()
            return {"status": "error", "message": f"Unknown action '{action_id}'"}
        finally:
            _release_db_connections()

    # --- background ---------------------------------------------------------

    def _start_background(self, action_id, settings):
        held_since = self.state.lock_held_since(self._LOCK, self._LOCK_STALE_SECONDS)
        if held_since:
            return self._busy_message(held_since)
        if self.state.is_paused():
            return {"status": "error", "message": "Paused: use Resume first."}
        task_fn = globals().get("_vod_probe_task")
        if task_fn is None:
            return {
                "status": "error",
                "message": "Background task failed to register at plugin load — check server logs.",
            }
        try:
            task_fn.apply_async(
                kwargs={"action": action_id, "settings": dict(settings or {}), "scheduled": False}, queue="dvr"
            )
        except Exception as exc:  # noqa: BLE001 - reported to the user
            return {"status": "error", "message": f"Failed to queue the background run: {exc}"}
        return {
            "status": "ok",
            "message": "Probe Run started in the background. Follow it with Run Status, stop it with Pause.",
        }

    def _busy_message(self, held_since):
        elapsed = int(time.time() - held_since)
        minutes = max(1, self._LOCK_STALE_SECONDS // 60)
        return {
            "status": "error",
            "message": (
                f"A run is already going (last activity {elapsed}s ago) — check Run Status. If "
                f"Dispatcharr restarted while it ran, this clears itself after {minutes} minutes "
                "without activity."
            ),
        }

    # --- schedule --------------------------------------------------------------
    #
    # Dispatcharr has no scheduling API for plugins: like vod-manager, the plugin
    # registers its own django-celery-beat PeriodicTask, which queues the same
    # Celery task as the Probe Run button. The settings are copied when Apply is
    # clicked, so changing a setting afterwards needs Apply again. A scheduled
    # run always handles everything due (no per-run limit) and never a fixed list
    # of ids: it is the small daily run that picks up what is new.

    def _schedule_snapshot(self, settings):
        snapshot = {k: v for k, v in (settings or {}).items() if not k.startswith("schedule_")}
        snapshot.update(batch_limit=0, only_relation_ids="", only_series_relation_ids="")
        return snapshot

    def _apply_schedule(self, settings):
        cron_expr = (settings.get("schedule_cron") or "").strip()
        if not cron_expr:
            # Empty means no schedule: Apply then also removes one that exists.
            removed = self._remove_schedule()
            return {"status": "ok", "message": f"Schedule is empty, so there is none. {removed['message']}"}
        tz_str = (settings.get("schedule_timezone") or "").strip() or "UTC"
        fields = cron_expr.split()
        if len(fields) != 5:
            return {
                "status": "error",
                "message": f"Cron expression must have 5 fields (minute hour day-of-month month day-of-week), got: {cron_expr!r}",
            }
        try:
            import pytz

            if tz_str not in pytz.all_timezones_set:
                return {"status": "error", "message": f"Unknown timezone: {tz_str}"}
        except ImportError:
            pass  # let CrontabSchedule validate it
        try:
            from django_celery_beat.models import CrontabSchedule, PeriodicTask
        except ImportError as exc:
            return {"status": "error", "message": f"django-celery-beat not available ({exc}); scheduling requires it."}
        minute, hour, dom, month, dow = fields
        try:
            schedule, _ = CrontabSchedule.objects.get_or_create(
                minute=minute, hour=hour, day_of_month=dom, month_of_year=month, day_of_week=dow, timezone=tz_str,
            )
        except Exception as exc:  # noqa: BLE001 - reported to the user
            return {"status": "error", "message": f"Invalid cron expression: {exc}"}
        _, created = PeriodicTask.objects.update_or_create(
            name=self.SCHEDULE_TASK_NAME,
            defaults={
                "crontab": schedule,
                "task": self.SCHEDULED_TASK_CELERY_NAME,
                "queue": "dvr",  # the queue Dispatcharr reserves for long-running background work
                "kwargs": json.dumps({"action": "probe_run", "settings": self._schedule_snapshot(settings)}),
                "enabled": True,
                "description": f"Scheduled run for {self.name} v{self.version}",
            },
        )
        dry = " Dry run is ON: it will only report." if settings.get("dry_run", True) else ""
        return {
            "status": "ok",
            "message": f"{'Created' if created else 'Updated'} schedule: '{cron_expr}' ({tz_str}) -> Probe Run.{dry}",
        }

    def _remove_schedule(self):
        try:
            from django_celery_beat.models import PeriodicTask
        except ImportError:
            return {"status": "ok", "message": "django-celery-beat not installed; nothing to remove."}
        deleted, _ = PeriodicTask.objects.filter(name=self.SCHEDULE_TASK_NAME).delete()
        return {"status": "ok", "message": f"Removed {deleted} scheduled task(s)."}

    def _schedule_status(self):
        try:
            from django_celery_beat.models import PeriodicTask
        except ImportError:
            return {"status": "ok", "message": "django-celery-beat not installed."}
        task = PeriodicTask.objects.filter(name=self.SCHEDULE_TASK_NAME).first()
        if not task:
            return {"status": "ok", "message": "No schedule registered."}
        last_run = task.last_run_at.isoformat() if task.last_run_at else "never"
        try:
            dry = json.loads(task.kwargs or "{}").get("settings", {}).get("dry_run", True)
        except ValueError:
            dry = True
        return {
            "status": "ok",
            "message": (
                f"Schedule: {task.crontab} | enabled={task.enabled} | dry run={dry} | "
                f"last run: {last_run} | total runs: {task.total_run_count}"
            ),
        }

    def _test_fire_schedule(self, settings):
        held_since = self.state.lock_held_since(self._LOCK, self._LOCK_STALE_SECONDS)
        if held_since:
            return self._busy_message(held_since)
        task_fn = globals().get("_vod_probe_task")
        if task_fn is None:
            return {"status": "error", "message": "Background task failed to register at plugin load — check server logs."}
        try:
            async_result = task_fn.apply_async(
                kwargs={"action": "probe_run", "settings": self._schedule_snapshot(settings), "scheduled": True}, queue="dvr"
            )
        except Exception as exc:  # noqa: BLE001 - reported to the user
            return {"status": "error", "message": f"Failed to queue the run: {exc}"}
        return {"status": "ok", "message": f"Test fire queued (task {async_result.id}). Follow it with Run Status."}

    # --- selection ---------------------------------------------------------

    @staticmethod
    def _relation_models():
        from apps.vod.models import M3UEpisodeRelation, M3UMovieRelation

        return (("movies", M3UMovieRelation), ("episodes", M3UEpisodeRelation))

    @staticmethod
    def _active_relations(model):
        """Relations of active accounts whose category is enabled for that
        account. Dispatcharr keeps the relations of a category after it is
        disabled, so filtering on the account alone counts them too."""
        from apps.vod.models import M3UVODCategoryRelation
        from django.db.models import Exists, OuterRef

        # An episode relation has no category of its own: it takes the one of
        # its series relation.
        category_field = (
            "series_relation__category_id"
            if "series_relation" in {f.name for f in model._meta.get_fields()}
            else "category_id"
        )
        enabled = M3UVODCategoryRelation.objects.filter(
            m3u_account_id=OuterRef("m3u_account_id"),
            category_id=OuterRef(category_field),
            enabled=True,
        )
        return model.objects.filter(m3u_account__is_active=True).filter(Exists(enabled))

    @staticmethod
    def _series_model():
        from apps.vod.models import M3USeriesRelation

        return M3USeriesRelation

    @staticmethod
    def _mode(settings):
        from .plan import MODE_ALL, MODE_FIRST

        return MODE_ALL if settings.get("episode_probing") == MODE_ALL else MODE_FIRST

    @staticmethod
    def _thresholds(settings):
        retry_after = timedelta(hours=float(settings.get("retry_after_hours", 24) or 24))
        return retry_after, int(settings.get("max_attempts", 3) or 3)

    def _movies_due(self, settings, now):
        """Ids of the movie relations due for a probe, or, when
        only_relation_ids is set, exactly those (forced, even if already
        probed)."""
        from .contract import needs_probe

        _, movie_model = self._relation_models()[0]
        only = [int(x) for x in str(settings.get("only_relation_ids") or "").replace(" ", "").split(",") if x.isdigit()]
        if only:
            found = set(self._active_relations(movie_model).filter(id__in=only).values_list("id", flat=True))
            return [rid for rid in only if rid in found]
        retry_after, max_attempts = self._thresholds(settings)
        return [
            rid
            for rid, properties in self._active_relations(movie_model).values_list("id", "custom_properties").iterator()
            if needs_probe(properties, now, retry_after, max_attempts)
        ]

    def _series_due(self, settings, now):
        """{series relation id: {"reason", "reload", "episodes"}} for every
        series relation with something to do. Reads series relations only, plus
        one grouped count of their episodes: never the episodes themselves."""
        from django.db.models import Count

        from .contract import series_work

        mode = self._mode(settings)
        retry_after, max_attempts = self._thresholds(settings)
        series_model = self._series_model()
        _, episode_model = self._relation_models()[1]
        counts = {
            row["series_relation_id"]: row["n"]
            for row in self._active_relations(episode_model).values("series_relation_id").annotate(n=Count("id"))
        }
        due = {}
        for srid, properties, refreshed in self._active_relations(series_model).values_list(
            "id", "custom_properties", "last_episode_refresh"
        ).iterator():
            work = series_work(properties, counts.get(srid, 0), mode, now, retry_after, max_attempts, refreshed)
            if work:
                due[srid] = {**work, "episodes": counts.get(srid, 0)}
        return due

    def _units(self, settings, now):
        """[("movies", id) | ("series", id)]: what a run has to do. With
        only_relation_ids (movies) or only_series_relation_ids (series
        versions) set, only those, and nothing else."""
        only_movies = str(settings.get("only_relation_ids") or "").strip()
        only_series = [
            int(x) for x in str(settings.get("only_series_relation_ids") or "").replace(" ", "").split(",") if x.isdigit()
        ]
        if only_movies or only_series:
            units = [("movies", rid) for rid in self._movies_due(settings, now)] if only_movies else []
            if only_series:
                plan = self._series_due(settings, now)
                units += [("series", srid) for srid in only_series if srid in plan]
            return units
        units = [("movies", rid) for rid in self._movies_due(settings, now)]
        return units + [("series", srid) for srid in self._series_due(settings, now)]

    # --- the run ------------------------------------------------------------

    def _probe_run(self, settings):
        import logging

        logger = logging.getLogger("vod_probe")
        acquired, held_since = self.state.try_acquire_lock(self._LOCK, self._LOCK_STALE_SECONDS)
        if not acquired:
            return self._busy_message(held_since)
        try:
            return self._run_pass(settings, logger)
        finally:
            self.state.release_lock(self._LOCK)

    def _run_pass(self, settings, logger):
        limit = int(settings.get("batch_limit", 25) or 0)
        max_concurrent = max(1, int(settings.get("max_concurrent_probes", 1) or 1))
        limiter = _RateLimiter(float(settings.get("max_probes_per_second", 2) or 0))
        breaker_ratio = float(settings.get("circuit_breaker_error_ratio", 0.5) or 0.5)
        timeout = int(settings.get("probe_timeout_seconds", 25) or 25)
        dry_run = bool(settings.get("dry_run", True))
        now = datetime.now(timezone.utc)

        units = self._units(settings, now)
        available = len(units)
        if limit > 0 and available > limit:
            import random

            units = random.sample(units, limit)
        progress = {
            "started": time.time(), "finished": None, "dry_run": dry_run, "available": available,
            "total": len(units), "processed": 0, "errors": 0, "probed": 0, "inferred": 0, "loaded": 0,
            "tiers": {}, "error_examples": [], "seconds": 0.0, "note": "",
        }
        self.state.set("run", progress)
        tiers = Counter()
        stopped = False

        with ThreadPoolExecutor(max_workers=max_concurrent) as pool:
            futures = {
                pool.submit(self._run_unit, kind, uid, settings, timeout, limiter, now, logger, dry_run): (kind, uid)
                for kind, uid in units
            }
            for future in as_completed(futures):
                if future.cancelled():
                    continue
                kind, uid = futures[future]
                try:
                    outcome = future.result()
                except Exception as exc:  # noqa: BLE001 - counted, never fatal for the pass
                    outcome = _outcome(errors=1, error=f"{type(exc).__name__}: {exc}")
                progress["processed"] += 1
                for key in ("errors", "probed", "inferred", "loaded"):
                    progress[key] += outcome[key]
                progress["seconds"] += outcome["seconds"]
                tiers.update(outcome["tiers"])
                if outcome["error"] and len(progress["error_examples"]) < 3:
                    progress["error_examples"].append(f"{kind} {uid}: {outcome['error'][:120]}")
                progress["tiers"] = dict(tiers)
                self.state.set("run", progress)
                self.state.renew_lock(self._LOCK)

                attempts = progress["probed"] + progress["errors"]
                tripped = attempts >= 5 and progress["errors"] / attempts > breaker_ratio
                if (self.state.is_paused() or tripped) and not stopped:
                    stopped = True
                    if tripped:
                        self.state.set_paused(True)
                        progress["note"] = "circuit breaker tripped, paused"
                    for pending in futures:
                        pending.cancel()

        progress["finished"] = time.time()
        if stopped and not progress["note"]:
            progress["note"] = "paused"
        self.state.set("run", progress)
        message = self._describe(progress)
        logger.info("Probe Run finished: %s", message)
        self._notify(message, stopped)
        return {"status": "ok", "message": message}

    def _run_unit(self, kind, unit_id, settings, timeout, limiter, now, logger, dry_run):
        """One movie relation or one series relation. One retry on the
        transient gevent scheduling error seen in vod-manager ("This
        operation would block forever")."""
        try:
            try:
                return self._run_unit_once(kind, unit_id, settings, timeout, limiter, now, logger, dry_run)
            except Exception as exc:
                if "would block forever" not in str(exc):
                    raise
                return self._run_unit_once(kind, unit_id, settings, timeout, limiter, now, logger, dry_run)
        finally:
            _release_db_connections()

    def _run_unit_once(self, kind, unit_id, settings, timeout, limiter, now, logger, dry_run):
        if kind == "movies":
            _, model = self._relation_models()[0]
            return self._probe_relation(model, unit_id, timeout, limiter, now, logger, dry_run)
        return self._run_series(unit_id, settings, timeout, limiter, now, logger, dry_run)

    def _probe_relation(self, model, relation_id, timeout, limiter, now, logger, dry_run):
        """Probe one relation and, unless dry_run, write the result. Returns an
        outcome, with the block it produced under "props" on success."""
        from .contract import merge_failure, merge_success
        from .probe import probe_stream

        relation = model.objects.filter(id=relation_id).first()
        if relation is None:
            return _outcome(errors=1, error="relation no longer exists")
        url = relation.get_stream_url()
        if not url:
            return _outcome(errors=1, error="no stream URL")
        limiter.wait()
        started = time.monotonic()
        result = probe_stream(url, timeout_seconds=timeout)
        seconds = time.monotonic() - started
        verb = "would get" if dry_run else "got"
        if not result.get("ok"):
            failed = merge_failure(relation.custom_properties, result.get("error"), now)
            logger.info("vod-probe, relation %s %s: %s", relation_id, verb,
                        json.dumps({"probe": failed["probe"]}, ensure_ascii=False))
            if not dry_run:
                self._write(model, relation_id, lambda current: merge_failure(current, result.get("error"), now))
            return _outcome(errors=1, error=failed["probe"]["error"], seconds=seconds)
        merged = merge_success(relation.custom_properties, result, now)
        logger.info("vod-probe, relation %s %s: %s", relation_id, verb, json.dumps(
            {k: merged[k] for k in ("quality", "resolution", "probe") if k in merged}, ensure_ascii=False))
        if not dry_run:
            self._write(model, relation_id, lambda current: merge_success(current, result, now))
        return _outcome(probed=1, tiers={merged["probe"]["tier"]: 1}, seconds=seconds, props=merged)

    def _run_series(self, series_relation_id, settings, timeout, limiter, now, logger, dry_run):
        """One version of a series: load its episodes if Dispatcharr has not or
        if the provider's last_modified moved, probe the episodes the plan asks
        for, copy the measured result to the others, and write the summary on
        the series relation."""
        from apps.vod.tasks import refresh_series_episodes

        from .contract import merge_inferred, series_marker, series_work
        from .plan import MODE_FIRST, episodes_to_infer, plan_series

        mode = self._mode(settings)
        retry_after, max_attempts = self._thresholds(settings)
        series_model = self._series_model()
        _, episode_model = self._relation_models()[1]
        out = _outcome()

        relation = series_model.objects.select_related("series", "m3u_account").filter(id=series_relation_id).first()
        if relation is None:
            return _outcome(errors=1, error="series relation no longer exists")
        episode_count = episode_model.objects.filter(series_relation_id=series_relation_id).count()
        work = series_work(
            relation.custom_properties, episode_count, mode, now, retry_after, max_attempts, relation.last_episode_refresh
        )
        if work is None:
            return out
        if work["reload"]:
            if dry_run:
                return out  # loading creates or removes episodes in Dispatcharr: not done in a dry run
            limiter.wait()
            refresh_series_episodes(relation.m3u_account, relation.series, relation.external_series_id)
            relation = series_model.objects.select_related("series", "m3u_account").get(id=series_relation_id)
            # Dispatcharr's function catches its own errors and returns nothing: the
            # flag it sets on success is the only way to tell that the load worked.
            if not (relation.custom_properties or {}).get("episodes_fetched"):
                return _outcome(errors=1, error="could not load the episodes (see Dispatcharr's log)")
            out["loaded"] = 1

        entries = list(
            episode_model.objects.filter(series_relation_id=series_relation_id)
            .order_by("episode__season_number", "episode__episode_number")
            .values_list("id", "custom_properties")
        )
        seasons = len(set(
            episode_model.objects.filter(series_relation_id=series_relation_id).values_list("episode__season_number", flat=True)
        ))
        series_plan = plan_series(entries, mode, now, retry_after, max_attempts)
        representative = series_plan["representative"]
        representative_props = dict(entries)[representative] if representative is not None else None
        infer = series_plan["infer"]
        for rid in series_plan["candidates"]:
            result = self._probe_relation(episode_model, rid, timeout, limiter, now, logger, dry_run)
            _add_outcome(out, result)
            if mode == MODE_FIRST and result["props"] is not None:
                representative, representative_props = rid, result["props"]
                infer = episodes_to_infer(entries, rid)
                break
        if mode == MODE_FIRST and representative is not None:
            for rid in infer:
                if dry_run or self._write(
                    episode_model, rid,
                    lambda current: merge_inferred(current, representative_props, representative, now),
                ):
                    out["inferred"] += 1

        if not dry_run:
            last_modified = ((relation.custom_properties or {}).get("basic_data") or {}).get("last_modified")
            complete_sample = representative if mode == MODE_FIRST else (entries[0][0] if entries and not out["errors"] else None)
            self._write(
                series_model, series_relation_id,
                # The current time, not the start of the pass: the summary must be newer than
                # the reload done above, or the series would look reloaded after it.
                lambda current: series_marker(
                    current, last_modified, len(entries), seasons, mode, complete_sample, datetime.now(timezone.utc)
                ),
            )
        return out

    @staticmethod
    def _write(model, relation_id, merge):
        """Merge into the relation's custom_properties under a row lock, so a
        list sync (which reads then rewrites this dictionary) cannot slip in
        between our read and our write. update(), not save(): a relation
        deleted meanwhile (vod-manager prunes) is simply skipped, and
        updated_at, which Dispatcharr uses, is left alone. Returns whether a
        row was written."""
        from django.db import transaction

        with transaction.atomic():
            current = (
                model.objects.select_for_update().filter(id=relation_id).values_list("custom_properties", flat=True).first()
            )
            if current is None and not model.objects.filter(id=relation_id).exists():
                return False
            return bool(model.objects.filter(id=relation_id).update(custom_properties=merge(current)))

    @staticmethod
    def _describe(progress):
        tiers = ", ".join(f"{t} {n}" for t, n in sorted(progress["tiers"].items())) or "none"
        probes = progress["probed"] + progress["errors"]
        seconds = probes and progress["seconds"] / probes
        head = "Dry run, nothing written." if progress["dry_run"] else "WROTE to the catalogue."
        text = (
            f"{head} Handled {progress['processed']} of {progress['total']} movie/series relations "
            f"({progress['available']} due): {progress['probed']} probed, {progress['inferred']} episodes "
            f"{'would be ' if progress['dry_run'] else ''}inferred, {progress['loaded']} series loaded, "
            f"{progress['errors']} errors, {seconds:.1f}s per probe on average. Tiers measured: {tiers}."
        )
        if progress["error_examples"]:
            text += " Errors: " + "; ".join(progress["error_examples"]) + "."
        if progress["note"]:
            text += f" Stopped: {progress['note']}."
        return text

    def _run_status(self):
        progress = self.state.get("run")
        held_since = self.state.lock_held_since(self._LOCK, self._LOCK_STALE_SECONDS)
        paused = " [PAUSED]" if self.state.is_paused() else ""
        if not progress:
            return {"status": "ok", "message": f"No run yet.{paused}"}
        if held_since:
            return {
                "status": "ok",
                "message": (
                    f"[RUNNING, last activity {int(time.time() - held_since)}s ago]{paused} "
                    f"{progress['processed']}/{progress['total']} probed, {progress['errors']} errors."
                ),
            }
        return {"status": "ok", "message": f"Last run{paused}: {self._describe(progress)}"}

    def _notify(self, message, stopped):
        import logging

        try:
            from core.models import SystemNotification
            from core.utils import send_websocket_notification

            SystemNotification.objects.filter(notification_key__startswith=self._NOTIFICATION_KEY_PREFIX).delete()
            kind = SystemNotification.NotificationType
            notification = SystemNotification.objects.create(
                notification_key=f"{self._NOTIFICATION_KEY_PREFIX}{int(time.time())}",
                notification_type=kind.WARNING if stopped else kind.INFO,
                priority=SystemNotification.Priority.HIGH,
                title=f"VOD Probe: run {'stopped' if stopped else 'done'}",
                message=message,
                is_active=True,
                admin_only=True,
            )
            send_websocket_notification(notification)
        except Exception as exc:  # noqa: BLE001 - a notification must never fail the run
            logging.getLogger("vod_probe").warning("Could not send the completion notification: %s", exc)

    # --- read-only reports (short, fine inside a request) ---------------------

    def _scan(self, settings):
        now = datetime.now(timezone.utc)
        seconds_per_probe = float(settings.get("seconds_per_probe", 1) or 1)
        seconds_per_load = float(settings.get("seconds_per_episode_load", 1.5) or 1.5)
        movies = len(self._movies_due(settings, now))
        due = self._series_due(settings, now)
        reasons = Counter(item["reason"] for item in due.values())
        reloads = sum(1 for item in due.values() if item["reload"])
        all_mode = self._mode(settings) == "all"
        # One probe for each series to load; "all" mode probes every episode of the others.
        probes = reasons["load"] + (sum(item["episodes"] for item in due.values() if not item["reload"]) if all_mode else 0)
        total_seconds = (movies + probes) * seconds_per_probe + reloads * seconds_per_load
        labels = {"load": "never loaded", "unmarked": "no summary yet", "changed": "provider changed it",
                  "count": "episode count differs", "retry": "retry", "mode": "mode changed",
                  "reloaded": "reloaded by Dispatcharr since"}
        detail = ", ".join(f"{labels[k]} {v}" for k, v in reasons.most_common()) or "none"
        return {
            "status": "ok",
            "message": (
                f"Movies to probe: {movies}. Series versions to handle: {len(due)} ({detail}); "
                f"{reloads} need their episode list requested from the provider. "
                f"Estimated: about {total_seconds / 3600:.1f} h ({'every episode' if all_mode else 'one probe per series'}, "
                "one at a time; series loaded now add their probes once known). Nothing was written."
            ),
        }

    def _coverage(self):
        from .contract import coverage_bucket

        parts = []
        for label, model in self._relation_models():
            buckets = Counter()
            tiers = Counter()
            for properties in self._active_relations(model).values_list("custom_properties", flat=True).iterator():
                buckets[coverage_bucket(properties)] += 1
                block = (properties or {}).get("probe")
                if isinstance(block, dict) and block.get("status") in ("ok", "inferred"):
                    tiers[block.get("tier", "unknown")] += 1
            total = sum(buckets.values())
            answered = buckets["ok"] + buckets["inferred"]
            pct = f"{100 * answered / total:.0f}%" if total else "n/a"
            tier_text = ", ".join(f"{t} {n}" for t, n in tiers.most_common()) or "none yet"
            parts.append(
                f"{label}: {answered}/{total} with an answer ({pct}: {buckets['ok']} measured, "
                f"{buckets['inferred']} inferred), {buckets['error']} in error, {buckets['never']} never probed; "
                f"tiers: {tier_text}"
            )
        return {"status": "ok", "message": " | ".join(parts)}


def _outcome(errors=0, error=None, probed=0, inferred=0, loaded=0, tiers=None, seconds=0.0, props=None):
    return {
        "errors": errors, "error": error, "probed": probed, "inferred": inferred, "loaded": loaded,
        "tiers": dict(tiers or {}), "seconds": seconds, "props": props,
    }


def _add_outcome(total, part):
    for key in ("errors", "probed", "inferred", "loaded", "seconds"):
        total[key] += part[key]
    for tier, count in part["tiers"].items():
        total["tiers"][tier] = total["tiers"].get(tier, 0) + count
    total["error"] = total["error"] or part["error"]


# Registered at import, like vod-manager's: the click only queues this task on
# the Celery worker's "dvr" queue.
try:
    from celery import shared_task as _vod_probe_shared_task

    @_vod_probe_shared_task(name=Plugin.SCHEDULED_TASK_CELERY_NAME)
    def _vod_probe_task(action="probe_run", settings=None, scheduled=True):
        import logging

        logger = logging.getLogger("vod_probe")
        result = Plugin().run(action, {}, {"settings": settings or {}, "background": True})
        if result.get("status") == "error":
            logger.error("Background action '%s' failed: %s", action, result.get("message"))
        try:
            from django.utils import timezone
            from django_celery_beat.models import PeriodicTask

            PeriodicTask.objects.filter(name=Plugin.SCHEDULE_TASK_NAME).update(last_run_at=timezone.now())
        except Exception as exc:  # noqa: BLE001 - only bookkeeping
            logger.warning("Could not update the schedule's last run time: %s", exc)
        return result
except Exception as _celery_register_err:  # pragma: no cover - environment-dependent
    import logging

    logging.getLogger("vod_probe").error(
        "Could not register the background task (Probe Run will be unavailable): %s", _celery_register_err
    )
