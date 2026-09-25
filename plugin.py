"""VOD Probe — writes each VOD relation's real quality into Dispatcharr's
catalogue. See README.md for the data contract.

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
from datetime import datetime, timezone

# Sibling modules are imported inside methods, not at module top level, to
# avoid stale references across a plugin reload cycle (same pattern as
# vod-manager).


# Dispatcharr sets the level and the handler of a few loggers only (apps, core.*,
# celery...). A logger outside them falls back on a stricter default in the Celery
# worker, where only warnings and errors get through: the plugin's own lines
# (what a dry run would write, the summary of a run) were missing from the log.
# Under "apps" it follows Dispatcharr's LOG_LEVEL like the rest.
LOGGER_NAME = "apps.plugins.vod_probe"


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
    version = "1.0.3"
    description = (
        "Probes the real quality of each VOD relation with ffprobe and writes it "
        "into the relation's custom_properties, so every tool reading "
        "Dispatcharr's API can use it."
    )
    author = "oxios0x00"
    help_url = "https://github.com/oxios0x00/dispatcharr-vod-probe"

    SCHEDULED_TASK_CELERY_NAME = "vod_probe.run"
    SCHEDULE_TASK_NAME = "vod_probe.auto_run"
    _BACKGROUND_ACTIONS = {"probe_run", "reload_incomplete"}
    # Reports read every relation's JSON, so they also run in the background, with
    # no lock and no pause check: they change nothing but a few flags. The result
    # goes to the notification centre.
    _REPORT_ACTIONS = {"scan": "Scan", "coverage": "Coverage Stats", "retry_errors": "Retry Errors"}
    _REPORT_KEY_PREFIX = "vod-probe-report-"
    _LOCK = "probe_run"
    # A run renews its lock after every relation; a lock silent this long
    # belongs to a run that died with its worker.
    _LOCK_STALE_SECONDS = 900
    _NOTIFICATION_KEY_PREFIX = "vod-probe-run-"

    def __init__(self):
        import logging

        from .state import State, data_dir_for, move_legacy_state

        # Next to the plugin's folder, not inside it: an update replaces the folder.
        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        data_dir = data_dir_for(plugin_dir)
        try:
            if move_legacy_state(plugin_dir, data_dir):
                logging.getLogger(LOGGER_NAME).info("Moved the plugin state from %s/data to %s", plugin_dir, data_dir)
        except OSError as exc:  # the old state is only history and a pause flag
            logging.getLogger(LOGGER_NAME).warning("Could not move the old plugin state to %s: %s", data_dir, exc)
        self.state = State(data_dir)

    # --- dispatch ---------------------------------------------------------

    def run(self, action_id, params, context):
        settings = context.get("settings", {})
        try:
            if action_id in self._BACKGROUND_ACTIONS and not context.get("background"):
                return self._start_background(action_id, settings)
            if action_id == "probe_run":
                return self._probe_run(settings, scheduled=bool(context.get("scheduled")))
            if action_id == "reload_incomplete":
                result = self._reload_incomplete(settings)
                self._notify(
                    result["message"], result.get("status") == "error", title="VOD Probe: Reload Incomplete Series",
                    prefix=f"{self._REPORT_KEY_PREFIX}reload_incomplete-",
                )
                return result
            if action_id in self._REPORT_ACTIONS:
                if not context.get("background"):
                    return self._start_report(action_id, settings)
                return self._run_report(action_id, settings)
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
        name = {"probe_run": "Probe Run", "reload_incomplete": "Reload Incomplete Series"}[action_id]
        return {
            "status": "ok",
            "message": f"{name} started in the background. Follow it with Run Status, stop it with Pause.",
        }

    def _start_report(self, action_id, settings):
        task_fn = globals().get("_vod_probe_task")
        if task_fn is None:
            return {"status": "error", "message": "Background task failed to register at plugin load — check server logs."}
        try:
            task_fn.apply_async(
                kwargs={"action": action_id, "settings": dict(settings or {}), "scheduled": False}, queue="dvr"
            )
        except Exception as exc:  # noqa: BLE001 - reported to the user
            return {"status": "error", "message": f"Failed to queue the report: {exc}"}
        return {
            "status": "ok",
            "message": f"{self._REPORT_ACTIONS[action_id]} started in the background. The result appears in the notification centre.",
        }

    def _run_report(self, action_id, settings):
        handler = {"scan": lambda: self._scan(settings), "coverage": self._coverage, "retry_errors": self._retry_errors}[action_id]
        result = handler()
        self._notify(
            result["message"], False, title=f"VOD Probe: {self._REPORT_ACTIONS[action_id]}",
            prefix=f"{self._REPORT_KEY_PREFIX}{action_id}-",
        )
        return result

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
    # run, like any run, handles everything due, and never a fixed list of ids:
    # it is the small daily run that picks up what is new.

    def _schedule_snapshot(self, settings):
        snapshot = {k: v for k, v in (settings or {}).items() if not k.startswith("schedule_")}
        snapshot.update(only_relation_ids="", only_series_relation_ids="")
        return snapshot

    def _apply_schedule(self, settings):
        cron_expr = (settings.get("schedule_cron") or "").strip()
        if not cron_expr:
            # Empty means no schedule: Apply then also removes one that exists.
            removed = self._remove_schedule()
            return {"status": "ok", "message": f"Schedule is empty, so there is none. {removed['message']}"}
        # Like Dispatcharr's own cron schedules (core.scheduling), the time is read
        # in the system time zone set in Dispatcharr's settings.
        tz_str = self._system_timezone()
        fields = cron_expr.split()
        if len(fields) != 5:
            return {
                "status": "error",
                "message": f"Cron expression must have 5 fields (minute hour day-of-month month day-of-week), got: {cron_expr!r}",
            }
        try:
            from django_celery_beat.models import CrontabSchedule, PeriodicTask
        except ImportError as exc:
            return {"status": "error", "message": f"django-celery-beat not available ({exc}); scheduling requires it."}
        minute, hour, dom, month, dow = fields
        try:
            # Celery parses the fields when it builds the schedule: a value such as
            # "99 99 * * *" would otherwise be saved and only fail later, in Beat.
            from celery.schedules import crontab

            crontab(minute=minute, hour=hour, day_of_month=dom, month_of_year=month, day_of_week=dow)
        except ImportError:
            pass  # let CrontabSchedule validate it
        except Exception as exc:  # noqa: BLE001 - reported to the user
            return {"status": "error", "message": f"Invalid cron expression {cron_expr!r}: {exc}"}
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
        from .plan import MODE_FIRST, MODES

        value = settings.get("episode_probing")
        return value if value in MODES else MODE_FIRST

    def _movies_due(self, settings):
        """Ids of the movie relations due for a probe, or, when
        only_relation_ids is set, exactly those (forced, even if already
        probed)."""
        from .contract import needs_probe

        _, movie_model = self._relation_models()[0]
        only = [int(x) for x in str(settings.get("only_relation_ids") or "").replace(" ", "").split(",") if x.isdigit()]
        if only:
            found = set(self._active_relations(movie_model).filter(id__in=only).values_list("id", flat=True))
            return [rid for rid in only if rid in found]
        return [
            rid
            for rid, properties in self._active_relations(movie_model).values_list("id", "custom_properties").iterator()
            if needs_probe(properties)
        ]

    def _series_due(self, settings):
        """{series relation id: {"reason", "reload", "episodes"}} for every
        series relation with something to do. Reads series relations only, plus
        one grouped count of their episodes: never the episodes themselves."""
        from django.db.models import Count

        from .contract import series_work

        mode = self._mode(settings)
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
            work = series_work(properties, counts.get(srid, 0), mode, refreshed)
            if work:
                due[srid] = {**work, "episodes": counts.get(srid, 0)}
        return due

    def _units(self, settings):
        """[("movies" | "series", id, forced)]: what a run has to do. With
        only_relation_ids (movies) or only_series_relation_ids (series
        versions) set, only those, and nothing else."""
        only_movies = str(settings.get("only_relation_ids") or "").strip()
        only_series = [
            int(x) for x in str(settings.get("only_series_relation_ids") or "").replace(" ", "").split(",") if x.isdigit()
        ]
        if only_movies or only_series:
            units = [("movies", rid, False) for rid in self._movies_due(settings)] if only_movies else []
            if only_series:
                # Forced, like the movie ids: probed again even if already done.
                known = set(
                    self._active_relations(self._series_model()).filter(id__in=only_series).values_list("id", flat=True)
                )
                units += [("series", srid, True) for srid in dict.fromkeys(only_series) if srid in known]
            return units
        units = [("movies", rid, False) for rid in self._movies_due(settings)]
        return units + [("series", srid, False) for srid in self._series_due(settings)]

    # --- the run ------------------------------------------------------------

    def _probe_run(self, settings, scheduled=False):
        import logging

        logger = logging.getLogger(LOGGER_NAME)
        if self.state.is_paused():
            # A scheduled run does not go through the button's own check.
            logger.info("Probe Run not started: the plugin is paused.")
            return {"status": "ok", "message": "Paused: nothing was run. Use Resume."}
        acquired, held_since = self.state.try_acquire_lock(self._LOCK, self._LOCK_STALE_SECONDS)
        if not acquired:
            return self._busy_message(held_since)
        try:
            if scheduled:
                self._apply_retry_switch(settings, logger)
            return self._run_pass(settings, logger)
        finally:
            self.state.release_lock(self._LOCK)

    def _run_pass(self, settings, logger):
        """Everything due, batch after batch, like vod-manager's Process: the
        run only stops when nothing is left, on Pause, or when the circuit
        breaker trips. Between batches it hands its database connection back
        and reads what is due again."""
        from .breaker import tripped as breaker_tripped
        from .contract import scrub_error
        from .plan import next_batch

        batch_size = int(settings.get("batch_limit", 25) or 0)
        max_concurrent = max(1, int(settings.get("max_concurrent_probes", 1) or 1))
        limiter = _RateLimiter(float(settings.get("max_probes_per_second", 2) or 0))
        breaker_ratio = float(settings.get("circuit_breaker_error_ratio", 0.8) or 0.8)
        timeout = int(settings.get("probe_timeout_seconds", 25) or 25)
        dry_run = bool(settings.get("dry_run", True))
        now = datetime.now(timezone.utc)

        done = set()
        batch, remaining = next_batch(self._units(settings), done, batch_size)
        progress = {
            "started": time.time(), "finished": None, "dry_run": dry_run, "available": remaining,
            "total": remaining, "batches": 0, "processed": 0, "errors": 0, "probed": 0, "inferred": 0, "loaded": 0,
            "retried": 0, "retried_errors": 0, "tiers": {}, "error_examples": [], "seconds": 0.0, "note": "",
        }
        self.state.set("run", progress)
        tiers = Counter()
        stopped = False

        with ThreadPoolExecutor(max_workers=max_concurrent) as pool:
            while batch and not stopped:
                if self.state.is_paused():
                    stopped = True
                    break
                progress["batches"] += 1
                futures = {
                    pool.submit(self._run_unit, kind, uid, settings, timeout, limiter, now, logger, dry_run, force): (kind, uid)
                    for kind, uid, force in batch
                }
                for future in as_completed(futures):
                    if future.cancelled():
                        continue
                    kind, uid = futures[future]
                    try:
                        outcome = future.result()
                    except Exception as exc:  # noqa: BLE001 - counted, never fatal for the pass
                        outcome = _outcome(errors=1, error=scrub_error(f"{type(exc).__name__}: {exc}"))
                    progress["processed"] += 1
                    for key in ("errors", "probed", "inferred", "loaded", "retried", "retried_errors"):
                        progress[key] += outcome[key]
                    progress["seconds"] += outcome["seconds"]
                    tiers.update(outcome["tiers"])
                    if outcome["error"] and len(progress["error_examples"]) < 3:
                        progress["error_examples"].append(f"{kind} {uid}: {outcome['error'][:120]}")
                    progress["tiers"] = dict(tiers)
                    self.state.set("run", progress)
                    self.state.renew_lock(self._LOCK)

                    tripped = breaker_tripped(
                        progress["probed"], progress["errors"], progress["retried"], progress["retried_errors"], breaker_ratio
                    )
                    if (self.state.is_paused() or tripped) and not stopped:
                        stopped = True
                        if tripped:
                            self.state.set_paused(True)
                            progress["note"] = "circuit breaker tripped, paused"
                        for pending in futures:
                            pending.cancel()
                done.update((kind, uid) for kind, uid, _ in batch)
                if stopped:
                    break
                _release_db_connections()
                batch, remaining = next_batch(self._units(settings), done, batch_size)
                progress["total"] = progress["processed"] + remaining
                self.state.set("run", progress)
                self.state.renew_lock(self._LOCK)

        progress["finished"] = time.time()
        if stopped and not progress["note"]:
            progress["note"] = "paused"
        self.state.set("run", progress)
        message = self._describe(progress)
        logger.info("Probe Run finished: %s", message)
        self._notify(self._describe(progress, short=True), stopped)
        return {"status": "ok", "message": message}

    def _run_unit(self, kind, unit_id, settings, timeout, limiter, now, logger, dry_run, force=False):
        """One movie relation or one series relation. One retry on the
        transient gevent scheduling error seen in vod-manager ("This
        operation would block forever")."""
        try:
            try:
                return self._run_unit_once(kind, unit_id, settings, timeout, limiter, now, logger, dry_run, force)
            except Exception as exc:
                if "would block forever" not in str(exc):
                    raise
                return self._run_unit_once(kind, unit_id, settings, timeout, limiter, now, logger, dry_run, force)
        finally:
            _release_db_connections()

    def _run_unit_once(self, kind, unit_id, settings, timeout, limiter, now, logger, dry_run, force=False):
        if kind == "movies":
            _, model = self._relation_models()[0]
            return self._probe_relation(model, unit_id, timeout, limiter, now, logger, dry_run)
        return self._run_series(unit_id, settings, timeout, limiter, now, logger, dry_run, force)

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
        was_retry = 1 if ((relation.custom_properties or {}).get("probe") or {}).get("retry") else 0
        limiter.wait()
        started = time.monotonic()
        result = probe_stream(url, timeout_seconds=timeout)
        seconds = time.monotonic() - started
        verb = "would get" if dry_run else "got"
        if not result.get("ok"):
            failed = merge_failure(relation.custom_properties, result.get("error"), now)
            (logger.info if dry_run else logger.debug)("vod-probe, relation %s %s: %s", relation_id, verb,
                        json.dumps({"probe": failed["probe"]}, ensure_ascii=False))
            if not dry_run:
                self._write(model, relation_id, lambda current: merge_failure(current, result.get("error"), now))
            return _outcome(errors=1, error=failed["probe"]["error"], seconds=seconds, retried=was_retry, retried_errors=was_retry)
        merged = merge_success(relation.custom_properties, result, now)
        (logger.info if dry_run else logger.debug)("vod-probe, relation %s %s: %s", relation_id, verb, json.dumps(
            {k: merged[k] for k in ("quality", "resolution", "probe") if k in merged}, ensure_ascii=False))
        if not dry_run:
            self._write(model, relation_id, lambda current: merge_success(current, result, now))
        return _outcome(probed=1, tiers={merged["probe"]["tier"]: 1}, seconds=seconds, props=merged, retried=was_retry)

    def _run_series(self, series_relation_id, settings, timeout, limiter, now, logger, dry_run, force=False):
        """One version of a series: load its episodes if Dispatcharr has not or
        if the provider's last_modified moved, probe the episodes the plan asks
        for, copy the measured result to the others, and write the summary on
        the series relation."""
        from apps.vod.tasks import refresh_series_episodes

        from .contract import is_measured, merge_inferred, series_marker, series_status, series_work
        from .plan import MODE_ALL, episodes_to_infer, plan_series, split_groups

        mode = self._mode(settings)
        series_model = self._series_model()
        _, episode_model = self._relation_models()[1]
        out = _outcome()

        relation = series_model.objects.select_related("series", "m3u_account").filter(id=series_relation_id).first()
        if relation is None:
            return _outcome(errors=1, error="series relation no longer exists")
        episode_count = episode_model.objects.filter(series_relation_id=series_relation_id).count()
        work = series_work(relation.custom_properties, episode_count, mode, relation.last_episode_refresh)
        if work is None:
            if not force:
                return out
            work = {"reason": "forced", "reload": False}  # asked for by id: probed again even if done
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

        rows = list(
            episode_model.objects.filter(series_relation_id=series_relation_id)
            .order_by("episode__season_number", "episode__episode_number")
            .values_list("id", "custom_properties", "episode__season_number")
        )
        seasons = len({season for _, _, season in rows})
        answered = 0          # groups that ended with a measured episode
        exhausted = True      # no group left an episode untried without answering
        first_sample = None
        groups = split_groups(rows, mode)
        for entries in groups:
            if force:  # ignore what was written before, so the sample is probed again
                entries = [(rid, {k: v for k, v in (cp or {}).items() if k != "probe"}) for rid, cp in entries]
            group_plan = plan_series(entries, mode)
            representative = group_plan["representative"]
            representative_props = dict(entries)[representative] if representative is not None else None
            infer = group_plan["infer"]
            for rid in group_plan["candidates"]:
                result = self._probe_relation(episode_model, rid, timeout, limiter, now, logger, dry_run)
                _add_outcome(out, result)
                if mode != MODE_ALL and result["props"] is not None:
                    representative, representative_props = rid, result["props"]
                    infer = episodes_to_infer(entries, rid)
                    break
            if mode != MODE_ALL:
                if representative is not None:
                    answered += 1
                    first_sample = first_sample or representative
                    if dry_run:
                        out["inferred"] += len(infer)
                    else:
                        out["inferred"] += self._write_many(
                            episode_model, infer,
                            lambda current: merge_inferred(current, representative_props, representative, now),
                        )
                elif group_plan["more_due"]:
                    # The MAX_SAMPLE_TRIES cap left episodes of this group untried: not
                    # this group's final answer yet, whatever the tried ones came back as.
                    exhausted = False

        if not dry_run:
            last_modified = ((relation.custom_properties or {}).get("basic_data") or {}).get("last_modified")
            if mode == MODE_ALL:
                # No representative here: every due episode is tried every pass (no cap),
                # so each is its own one-episode "group" — always exhausted after a pass.
                total, sample_count = len(rows), sum(1 for _, cp, _ in rows if is_measured(cp)) + out["probed"]
                complete_sample = rows[0][0] if rows and sample_count > 0 else None
            else:
                total, sample_count = len(groups), answered
                complete_sample = first_sample
            status = series_status(len(rows), sample_count, total, exhausted)
            self._write(
                series_model, series_relation_id,
                # The current time, not the start of the pass: the summary must be newer than
                # the reload done above, or the series would look reloaded after it.
                lambda current: series_marker(
                    current, last_modified, len(rows), seasons, mode, complete_sample, datetime.now(timezone.utc), status
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
    def _write_many(model, relation_ids, merge, chunk=200):
        """_write for many relations at once: each chunk is one transaction that
        locks its rows (in id order, so two writers cannot deadlock), merges each
        dictionary and saves them with one bulk update instead of three queries
        per relation. Relations deleted meanwhile are skipped. Returns how many
        rows were written."""
        from django.db import transaction

        ids = list(relation_ids)
        written = 0
        for start in range(0, len(ids), chunk):
            with transaction.atomic():
                rows = list(
                    model.objects.select_for_update().filter(id__in=ids[start:start + chunk]).order_by("id")
                    .values_list("id", "custom_properties")
                )
                objects = [model(id=rid, custom_properties=merge(current)) for rid, current in rows]
                if objects:
                    model.objects.bulk_update(objects, ["custom_properties"])
                written += len(objects)
        return written

    _TIER_ORDER = ("2160p", "1080p", "720p", "480p", "sd", "unknown")

    @classmethod
    def _tier_text(cls, counts):
        """Tier counts from the best to the worst: "2160p 204, 1080p 375"."""
        rank = {tier: i for i, tier in enumerate(cls._TIER_ORDER)}
        ordered = sorted(counts.items(), key=lambda item: rank.get(item[0], len(rank)))
        return ", ".join(f"{tier} {n}" for tier, n in ordered if n)

    @classmethod
    def _describe(cls, progress, short=False):
        """`short` is for the pop-up notification (one line, no room for
        detail); the full version is for Run Status, read on demand."""
        probes = progress["probed"] + progress["errors"]
        seconds = probes and progress["seconds"] / probes
        head = "Dry run" if progress["dry_run"] else "Wrote"
        parts = [
            f"{head}: {progress['probed']} probed, {progress['errors']} errors, {progress['inferred']} inferred "
            f"({progress['processed']}/{progress['total']} handled), {seconds:.1f}s/probe"
        ]
        if progress["note"]:
            parts.append(f"Stopped: {progress['note']}")
        if short:
            if not progress["dry_run"] or progress["note"]:
                parts.append("See Run Status for details")
            return " | ".join(parts)
        parts.append(f"Tiers: {cls._tier_text(progress['tiers']) or 'none'}")
        if progress.get("retried"):
            parts.append(f"{progress['retried']} retries ({progress['retried_errors']} failed again)")
        if progress["error_examples"]:
            parts.append("e.g. " + progress["error_examples"][0])
        return " | ".join(parts)

    def _run_status(self):
        """The run in flight, or the last one. The reports (Scan, Coverage Stats,
        Retry Errors, Reload Incomplete Series) are in the notification centre."""
        progress = self.state.get("run")
        held_since = self.state.lock_held_since(self._LOCK, self._LOCK_STALE_SECONDS)
        paused = self.state.is_paused()
        if held_since:
            idle = int(time.time() - held_since)
            if progress and progress.get("finished") is None:
                state = "Pausing" if paused else "Running"
                return {"status": "ok", "message": (
                    f"{state}: {progress['processed']}/{progress['total']} handled, "
                    f"{progress['errors']} errors (last activity {idle}s ago)"
                )}
            # The same lock covers the other long job.
            return {"status": "ok", "message": f"Reload Incomplete Series is running (last activity {idle}s ago)"}
        suffix = " | Paused: use Resume" if paused else ""
        if not progress:
            return {"status": "ok", "message": f"No run yet{suffix}"}
        when = self._local_time(progress.get("finished") or progress["started"], "%Y-%m-%d %H:%M")
        return {"status": "ok", "message": f"Last run {when} · {self._describe(progress)}{suffix}"}

    @staticmethod
    def _system_timezone():
        """The time zone set in Dispatcharr's System Settings, UTC when unknown."""
        try:
            from core.models import CoreSettings

            return CoreSettings.get_system_time_zone() or "UTC"
        except Exception:  # noqa: BLE001 - older Dispatcharr, or settings unreadable
            return "UTC"

    @classmethod
    def _local_time(cls, timestamp, fmt):
        """A timestamp formatted in Dispatcharr's system time zone, for texts
        shown to the user. When that zone is UTC (or unknown), it says so."""
        from zoneinfo import ZoneInfo

        tz_str = cls._system_timezone()
        if tz_str != "UTC":
            try:
                return datetime.fromtimestamp(timestamp, ZoneInfo(tz_str)).strftime(fmt)
            except Exception:  # noqa: BLE001 - an unknown time zone falls back to UTC
                pass
        return datetime.fromtimestamp(timestamp, timezone.utc).strftime(fmt) + " UTC"

    @classmethod
    def _clock(cls):
        """Current time for a notification title: Dispatcharr's notification
        centre shows only the date."""
        return cls._local_time(time.time(), "%H:%M")

    def _notify(self, message, stopped, title=None, prefix=None):
        import logging

        try:
            from core.models import SystemNotification
            from core.utils import send_websocket_notification

            prefix = prefix or self._NOTIFICATION_KEY_PREFIX
            SystemNotification.objects.filter(notification_key__startswith=prefix).delete()
            kind = SystemNotification.NotificationType
            clock = self._clock()
            title = title or f"VOD Probe: run {'stopped' if stopped else 'done'}"
            notification = SystemNotification.objects.create(
                notification_key=f"{prefix}{int(time.time())}",
                notification_type=kind.WARNING if stopped else kind.INFO,
                priority=SystemNotification.Priority.HIGH,
                title=f"{title} ({clock})",
                message=message,
                is_active=True,
                admin_only=True,
            )
            send_websocket_notification(notification)
        except Exception as exc:  # noqa: BLE001 - a notification must never fail the run
            logging.getLogger(LOGGER_NAME).warning("Could not send the completion notification: %s", exc)

    # --- read-only reports (short, fine inside a request) ---------------------

    def _scan(self, settings):
        movies = len(self._movies_due(settings))
        due = self._series_due(settings)
        reasons = Counter(item["reason"] for item in due.values())
        reloads = sum(1 for item in due.values() if item["reload"])
        labels = {"load": "never loaded", "unmarked": "no summary yet", "changed": "provider changed it",
                  "count": "episode count differs", "retry": "flagged for retry", "mode": "more thorough setting",
                  "reloaded": "reloaded by Dispatcharr since", "empty": "loaded with no episode",
                  "incomplete": "provider lists more episodes"}
        detail = ", ".join(f"{labels[k]} {v}" for k, v in reasons.most_common()) or "none"
        return {
            "status": "ok",
            "message": f"{movies} movies, {len(due)} series versions due ({detail}), {reloads} need a reload.",
        }

    def _apply_retry_switch(self, settings, logger):
        """The "Retry errors at the next scheduled run" switch. It is read live,
        not from the settings copied when the schedule was applied, so that
        turning it on needs no new Apply, and it turns itself off once used. A
        dry run changes nothing, so it leaves the switch on."""
        from apps.plugins.loader import PluginManager
        from apps.plugins.models import PluginConfig

        key = os.path.basename(os.path.dirname(os.path.abspath(__file__)))
        config = PluginConfig.objects.filter(key=key).first()
        live = dict(config.settings or {}) if config else {}
        if not live.get("retry_errors_next_schedule"):
            return
        if settings.get("dry_run", True):
            logger.info("Retry errors switch is on; kept for a run that writes (this one is a dry run).")
            return
        result = self._retry_errors()
        logger.info("Retry errors switch: %s", result["message"])
        PluginManager.get().update_settings(key, {**live, "retry_errors_next_schedule": False})

    def _reload_incomplete(self, settings):
        """Series versions holding fewer episodes than another version of the same
        series: ask the provider how many it lists, and flag the ones where it
        lists more, so the next run asks for their episode list again.
        Dispatcharr marks a load as done even when the provider's answer was
        partial, and never checks it again. A real difference between versions
        (the provider simply has less of one) is left alone."""
        import logging

        from django.db.models import Count

        from .contract import flag_reload
        from .plan import provider_episode_count, short_versions

        logger = logging.getLogger(LOGGER_NAME)
        acquired, held_since = self.state.try_acquire_lock(self._LOCK, self._LOCK_STALE_SECONDS)
        if not acquired:
            return self._busy_message(held_since)
        try:
            from core.xtream_codes import Client

            dry_run = bool(settings.get("dry_run", True))
            series_model = self._series_model()
            _, episode_model = self._relation_models()[1]
            held = {
                row["series_relation_id"]: row["n"]
                for row in self._active_relations(episode_model).values("series_relation_id").annotate(n=Count("id"))
            }
            by_series = {}
            for rid, series_id, properties in self._active_relations(series_model).values_list("id", "series_id", "custom_properties"):
                if (properties or {}).get("episodes_fetched"):
                    by_series.setdefault(series_id, []).append((rid, held.get(rid, 0)))
            suspects = short_versions(by_series)
            limiter = _RateLimiter(float(settings.get("max_probes_per_second", 2) or 0))
            flagged = same = errors = 0
            for rid in suspects:
                relation = series_model.objects.select_related("m3u_account").get(id=rid)
                account = relation.m3u_account
                limiter.wait()
                try:
                    with Client(account.server_url, account.username, account.password, account.get_user_agent_string()) as client:
                        provider = provider_episode_count(client.get_series_info(relation.external_series_id))
                except Exception as exc:  # noqa: BLE001 - counted, the others are still checked
                    errors += 1
                    logger.warning("Could not ask the provider about series relation %s: %s", rid, type(exc).__name__)
                    continue
                if provider > held.get(rid, 0):
                    flagged += 1
                    if not dry_run:
                        self._write(series_model, rid, lambda current: flag_reload(current, datetime.now(timezone.utc)))
                else:
                    same += 1
                self.state.renew_lock(self._LOCK)
            verb = "would flag" if dry_run else "flagged"
            message = f"Checked {len(suspects)} suspect series versions: {verb} {flagged}, {same} confirmed real, {errors} errors."
            logger.info("Reload Incomplete Series: %s", message)
            return {"status": "ok", "message": message}
        finally:
            self.state.release_lock(self._LOCK)

    def _retry_errors(self):
        """Flag the failed relations so the next run (scheduled or manual) tries
        them again. A failure is never retried on its own. A failed episode is
        retried by visiting its series, so its series relation is flagged too."""
        from .contract import flag_retry, flag_series_retry

        counts = Counter()
        series_to_visit = set()
        for label, model in self._relation_models():
            failed = model.objects.filter(custom_properties__probe__status__in=["error", "unreachable"])
            if label == "episodes":
                rows = list(failed.values_list("id", "series_relation_id"))
                series_to_visit.update(srid for _, srid in rows if srid)
            else:
                rows = [(rid, None) for rid in failed.values_list("id", flat=True)]
            flagged = self._write_many(model, [rid for rid, _ in rows], flag_retry)
            if flagged:
                counts[label] += flagged
        series_model = self._series_model()
        series_to_visit.update(
            series_model.objects.filter(custom_properties__probe__status__in=["pending", "error"]).values_list("id", flat=True)
        )
        flagged = self._write_many(series_model, sorted(series_to_visit), flag_series_retry)
        if flagged:
            counts["series versions"] += flagged
        if not counts:
            return {"status": "ok", "message": "Nothing failed, nothing to retry."}
        text = ", ".join(f"{n} {label}" for label, n in counts.items())
        return {"status": "ok", "message": f"Flagged for retry at the next run: {text}."}

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
            counts = [f"{buckets['ok']} measured"]
            counts += [f"{buckets[key]} {name}" for key, name in (("inferred", "inferred"), ("error", "errors"), ("never", "never probed")) if buckets[key]]
            tier_text = self._tier_text(tiers)
            parts.append(f"{label.capitalize()} {pct} answered: {', '.join(counts)}" + (f" · {tier_text}" if tier_text else ""))
        return {"status": "ok", "message": " | ".join(parts)}


def _outcome(errors=0, error=None, probed=0, inferred=0, loaded=0, tiers=None, seconds=0.0, props=None, retried=0, retried_errors=0):
    return {
        "errors": errors, "error": error, "probed": probed, "inferred": inferred, "loaded": loaded,
        "tiers": dict(tiers or {}), "seconds": seconds, "props": props,
        "retried": retried, "retried_errors": retried_errors,
    }


def _add_outcome(total, part):
    for key in ("errors", "probed", "inferred", "loaded", "seconds", "retried", "retried_errors"):
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

        logger = logging.getLogger(LOGGER_NAME)
        result = Plugin().run(action, {}, {"settings": settings or {}, "background": True, "scheduled": scheduled})
        if result.get("status") == "error":
            logger.error("Background action '%s' failed: %s", action, result.get("message"))
        if scheduled:
            try:
                from django.utils import timezone
                from django_celery_beat.models import PeriodicTask

                PeriodicTask.objects.filter(name=Plugin.SCHEDULE_TASK_NAME).update(last_run_at=timezone.now())
            except Exception as exc:  # noqa: BLE001 - only bookkeeping
                logger.warning("Could not update the schedule's last run time: %s", exc)
        return result
except Exception as _celery_register_err:  # pragma: no cover - environment-dependent
    import logging

    logging.getLogger(LOGGER_NAME).error(
        "Could not register the background task (Probe Run will be unavailable): %s", _celery_register_err
    )
