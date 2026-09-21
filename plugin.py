"""VOD Probe — writes each VOD relation's real quality into Dispatcharr's
catalogue. See DESIGN.md for the contract and the roadmap.

Probe Run probes for real. With Dry run on (the default) it only reports what
it would write; with it off it writes quality, resolution and probe into the
relations' custom_properties.

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
    version = "0.3.0"
    description = (
        "Probes the real quality of each VOD relation with ffprobe and writes it "
        "into the relation's custom_properties, so every tool reading "
        "Dispatcharr's API can use it."
    )
    author = "oxios0x00"
    help_url = "https://github.com/oxios0x00/vod-probe"

    SCHEDULED_TASK_CELERY_NAME = "vod_probe.run"
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
            task_fn.apply_async(kwargs={"action": action_id, "settings": dict(settings or {})}, queue="dvr")
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

    def _due(self, settings, now):
        """[(label, relation id)] of every relation due for a probe, or, when
        only_relation_ids is set, exactly those movie relations (forced, even
        if already probed)."""
        from .contract import needs_probe

        only = [int(x) for x in str(settings.get("only_relation_ids") or "").replace(" ", "").split(",") if x.isdigit()]
        if only:
            _, movie_model = self._relation_models()[0]
            found = set(self._active_relations(movie_model).filter(id__in=only).values_list("id", flat=True))
            return [("movies", rid) for rid in only if rid in found]

        retry_after = timedelta(hours=float(settings.get("retry_after_hours", 24) or 24))
        max_attempts = int(settings.get("max_attempts", 3) or 3)
        due = []
        for label, model in self._relation_models():
            for rid, properties in self._active_relations(model).values_list("id", "custom_properties").iterator():
                if needs_probe(properties, now, retry_after, max_attempts):
                    due.append((label, rid))
        return due

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

        due = self._due(settings, now)
        available = len(due)
        if limit > 0 and available > limit:
            import random

            due = random.sample(due, limit)
        progress = {
            "started": time.time(), "finished": None, "dry_run": dry_run, "available": available,
            "total": len(due), "processed": 0, "errors": 0, "tiers": {}, "error_examples": [],
            "seconds": 0.0, "note": "",
        }
        self.state.set("run", progress)
        models = dict(self._relation_models())
        tiers = Counter()
        stopped = False

        with ThreadPoolExecutor(max_workers=max_concurrent) as pool:
            futures = {
                pool.submit(self._probe_one, models[label], rid, timeout, limiter, now, logger, dry_run): (label, rid)
                for label, rid in due
            }
            for future in as_completed(futures):
                if future.cancelled():
                    continue
                label, rid = futures[future]
                try:
                    outcome = future.result()
                except Exception as exc:  # noqa: BLE001 - counted, never fatal for the pass
                    outcome = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "seconds": 0.0}
                progress["processed"] += 1
                progress["seconds"] += outcome.get("seconds", 0.0)
                if outcome["ok"]:
                    tiers[outcome["tier"]] += 1
                else:
                    progress["errors"] += 1
                    if len(progress["error_examples"]) < 3:
                        progress["error_examples"].append(f"{label} {rid}: {outcome['error'][:120]}")
                progress["tiers"] = dict(tiers)
                self.state.set("run", progress)
                self.state.renew_lock(self._LOCK)

                paused = self.state.is_paused()
                tripped = (
                    progress["processed"] >= 5 and progress["errors"] / progress["processed"] > breaker_ratio
                )
                if (paused or tripped) and not stopped:
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

    def _probe_one(self, model, relation_id, timeout, limiter, now, logger, dry_run):
        """Probe one relation and report what would be written. Nothing is
        written. One retry on the transient gevent scheduling error seen in
        vod-manager ("This operation would block forever")."""
        try:
            try:
                return self._probe_one_once(model, relation_id, timeout, limiter, now, logger, dry_run)
            except Exception as exc:
                if "would block forever" not in str(exc):
                    raise
                return self._probe_one_once(model, relation_id, timeout, limiter, now, logger, dry_run)
        finally:
            _release_db_connections()

    def _probe_one_once(self, model, relation_id, timeout, limiter, now, logger, dry_run):
        from .contract import merge_failure, merge_success
        from .probe import probe_stream

        relation = model.objects.filter(id=relation_id).first()
        if relation is None:
            return {"ok": False, "error": "relation no longer exists", "seconds": 0.0}
        url = relation.get_stream_url()
        if not url:
            return {"ok": False, "error": "no stream URL", "seconds": 0.0}
        limiter.wait()
        started = time.monotonic()
        result = probe_stream(url, timeout_seconds=timeout)
        seconds = time.monotonic() - started
        if not result.get("ok"):
            failed = merge_failure(relation.custom_properties, result.get("error"), now)
            verb = "would get" if dry_run else "got"
            logger.info("vod-probe, relation %s %s: %s", relation_id, verb,
                        json.dumps({"probe": failed["probe"]}, ensure_ascii=False))
            if not dry_run:
                self._write(model, relation_id, lambda current: merge_failure(current, result.get("error"), now))
            return {"ok": False, "error": failed["probe"]["error"], "seconds": seconds}
        merged = merge_success(relation.custom_properties, result, now)
        verb = "would get" if dry_run else "got"
        logger.info("vod-probe, relation %s %s: %s", relation_id, verb, json.dumps(
            {k: merged[k] for k in ("quality", "resolution", "probe") if k in merged}, ensure_ascii=False))
        if not dry_run:
            self._write(model, relation_id, lambda current: merge_success(current, result, now))
        return {"ok": True, "tier": merged["probe"]["tier"], "seconds": seconds}

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
        seconds = progress["processed"] and progress["seconds"] / progress["processed"]
        text = (
            f"{'Dry run, nothing written.' if progress['dry_run'] else 'WROTE to the catalogue.'} Probed {progress['processed']} of {progress['total']} "
            f"({progress['available']} due), {progress['errors']} errors, {seconds:.1f}s per probe on average. "
            f"Tiers: {tiers}."
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
        seconds_per_probe = float(settings.get("seconds_per_probe", 1) or 1)
        due = Counter(label for label, _ in self._due(settings, datetime.now(timezone.utc)))
        total = sum(due.values())
        hours = total * seconds_per_probe / 3600
        return {
            "status": "ok",
            "message": (
                f"{total} relations to probe (movies {due['movies']}, episodes {due['episodes']}). "
                f"At {seconds_per_probe:g}s each and one at a time: about {hours:.1f} h. Nothing was written."
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
                if isinstance(block, dict) and block.get("status") == "ok":
                    tiers[block.get("tier", "unknown")] += 1
            total = sum(buckets.values())
            done = buckets["ok"]
            pct = f"{100 * done / total:.0f}%" if total else "n/a"
            tier_text = ", ".join(f"{t} {n}" for t, n in tiers.most_common()) or "none yet"
            parts.append(
                f"{label}: {done}/{total} probed ({pct}), {buckets['error']} in error, "
                f"{buckets['never']} never probed; tiers: {tier_text}"
            )
        return {"status": "ok", "message": " | ".join(parts)}


# Registered at import, like vod-manager's: the click only queues this task on
# the Celery worker's "dvr" queue.
try:
    from celery import shared_task as _vod_probe_shared_task

    @_vod_probe_shared_task(name=Plugin.SCHEDULED_TASK_CELERY_NAME)
    def _vod_probe_task(action="probe_run", settings=None):
        import logging

        logger = logging.getLogger("vod_probe")
        result = Plugin().run(action, {}, {"settings": settings or {}, "background": True})
        if result.get("status") == "error":
            logger.error("Background action '%s' failed: %s", action, result.get("message"))
        return result
except Exception as _celery_register_err:  # pragma: no cover - environment-dependent
    import logging

    logging.getLogger("vod_probe").error(
        "Could not register the background task (Probe Run will be unavailable): %s", _celery_register_err
    )
