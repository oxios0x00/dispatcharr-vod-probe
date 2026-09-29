"""The data contract: what vod-probe writes into a relation's custom_properties,
and when a relation is due for (re)probing. See README.md.

Pure Python, no Django, so it is unit-tested outside Dispatcharr.

Only four keys are ever written: `quality`, `resolution`, `probe` and
`id_lookup`. Everything else in the dictionary (basic_data, detailed_info,
the flags Dispatcharr keeps) is carried over untouched."""
import re
from datetime import datetime, timedelta, timezone

try:
    from .probe import PROBE_SCHEMA_VERSION, _AD_TITLE_HINTS
    from .manifest import VERSION as PLUGIN_VERSION
except ImportError:  # imported as a top-level module by the unit tests
    from probe import PROBE_SCHEMA_VERSION, _AD_TITLE_HINTS
    from manifest import VERSION as PLUGIN_VERSION

PLUGIN_SOURCE = "vod-probe"

STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_UNREACHABLE = "unreachable"
STATUS_INFERRED = "inferred"
STATUS_PARTIAL = "partial"

ID_LOOKUP_SCHEMA_VERSION = 1
ID_STATUS_NOT_FOUND = "not_found"
ID_STATUS_ERROR = "error"

RELOAD_TOLERANCE = timedelta(seconds=2)
# How many times a series loaded with no episode is asked for again before it is left alone.
MAX_EMPTY_RELOADS = 3

# probe.tier -> the vocabulary Dispatcharr's own quality_info uses, so any
# client that already reads quality_info needs no change.
_QUALITY_BY_TIER = {
    "2160p": "4K",
    "1080p": "1080p",
    "720p": "720p",
    "480p": "480p",
    "sd": "SD",
}

_ERROR_MAX_CHARS = 200
_UNREACHABLE_HINTS = ("timeout", "connection", "unreachable", "404", "403", "server returned")


_URL = re.compile(r"[a-z][a-z0-9+.-]*://\S+?(?=:?(?:\s|$))", re.IGNORECASE)
# What is left of a URL once its scheme and host are gone (requests reports "with url:
# /player_api.php?username=...&password=..."): credential parameters, and the
# /movie|series|live/<user>/<password>/ form of a stream path.
_CREDENTIAL_PARAM = re.compile(r"\b(username|password|user|pass|token)=[^&\s)\"']*", re.IGNORECASE)
_STREAM_PATH = re.compile(r"/(movie|series|live)/[^/\s]+/[^/\s]+/", re.IGNORECASE)


def scrub_error(text):
    """ffprobe and HTTP client errors quote the URL they were given, and the
    provider's URL carries the account's username and password. The text ends
    up in custom_properties, in the run state and in notifications, which
    others can read, so URLs and credentials are removed."""
    text = _URL.sub("<url>", str(text))
    text = _CREDENTIAL_PARAM.sub(lambda m: f"{m.group(1)}=<hidden>", text)
    return _STREAM_PATH.sub(lambda m: f"/{m.group(1)}/<hidden>/<hidden>/", text)


def _iso(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(text):
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _bit_depth(video):
    raw = video.get("bits_per_raw_sample")
    try:
        if raw:
            return int(raw)
    except (TypeError, ValueError):
        pass
    pix_fmt = video.get("pix_fmt") or ""
    for depth in (12, 10):
        if f"p{depth}" in pix_fmt or f"{depth}le" in pix_fmt or f"{depth}be" in pix_fmt:
            return depth
    return 8 if pix_fmt else None


def _frame_rate(video):
    rate = video.get("avg_frame_rate") or video.get("r_frame_rate") or ""
    try:
        num, _, den = rate.partition("/")
        value = float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return None
    return round(value, 3) if value else None


def _container_bit_rate(summary):
    """Overall bitrate of the file (video + audio), in bits per second: the
    figure comparable to the `bitrate` a provider announces, and present even
    when the video stream carries no bitrate of its own (many MKVs)."""
    try:
        return int((summary.get("format") or {}).get("bit_rate")) or None
    except (TypeError, ValueError):
        return None


def _audio_tracks(summary):
    tracks = []
    for stream in summary.get("audio") or []:
        title = str(stream.get("title", "")).lower()
        description = "visual_impaired" in (stream.get("flags") or []) or any(
            hint in title for hint in _AD_TITLE_HINTS
        )
        track = {
            "codec": stream.get("codec_name"),
            "channels": stream.get("channels"),
            "language": stream.get("language", "und"),
        }
        if description:
            track["audio_description"] = True
        tracks.append({k: v for k, v in track.items() if v is not None})
    return tracks


def _video_block(result, summary):
    video = (summary.get("video") or [{}])[0]
    block = {
        "codec": result.get("video_codec"),
        "profile": video.get("profile"),
        "bit_depth": _bit_depth(video),
        "bit_rate": result.get("video_bitrate"),
        "frame_rate": _frame_rate(video),
    }
    return {k: v for k, v in block.items() if v is not None}


def build_probe_block(result, now):
    """The `probe` block for a successful probe_stream() result."""
    summary = result.get("summary") or {}
    audio = _audio_tracks(summary)
    block = {
        "schema_version": PROBE_SCHEMA_VERSION,
        "probed_at": _iso(now),
        "status": STATUS_OK,
        "tier": result.get("quality_label") or "unknown",
        "hdr": result.get("hdr_type") or "sdr",
        "video": _video_block(result, summary),
        # `audio_languages` is the flat, de-duplicated list a client filters on;
        # `audio` keeps every track (a missing audio_description means false).
        "audio_languages": list(dict.fromkeys(track["language"] for track in audio)),
        "audio": audio,
        "duration_secs": result.get("duration_secs"),
        "container": (summary.get("format") or {}).get("format_name"),
        "bit_rate": _container_bit_rate(summary),
        "source": {"plugin": PLUGIN_SOURCE, "version": PLUGIN_VERSION},
    }
    return {k: v for k, v in block.items() if v is not None}


def quality_fields(result):
    """`quality` and `resolution` for a successful result; keys are omitted
    when unknown, so a merge never overwrites a good value with a blank."""
    fields = {}
    quality = _QUALITY_BY_TIER.get(result.get("quality_label"))
    if quality:
        fields["quality"] = quality
    if result.get("width") and result.get("height"):
        fields["resolution"] = f"{result['width']}x{result['height']}"
    return fields


def merge_success(existing, result, now):
    """New custom_properties after a successful probe."""
    merged = dict(existing or {})
    merged.update(quality_fields(result))
    merged["probe"] = build_probe_block(result, now)
    return merged


def merge_failure(existing, error, now, unreachable=None):
    """New custom_properties after a failed probe. `quality` and `resolution`
    keep their last known value; the previous probe details are kept too,
    with the status, the short error and the attempt count updated."""
    merged = dict(existing or {})
    previous = dict(merged.get("probe") or {})
    text = scrub_error(error or "unknown error")
    if unreachable is None:
        unreachable = any(hint in text.lower() for hint in _UNREACHABLE_HINTS)
    previous.pop("retry", None)  # this is the retry the flag asked for
    previous.update(
        schema_version=PROBE_SCHEMA_VERSION,
        probed_at=_iso(now),
        status=STATUS_UNREACHABLE if unreachable else STATUS_ERROR,
        error=text[:_ERROR_MAX_CHARS],
        attempts=int(previous.get("attempts") or 0) + 1,
        source={"plugin": PLUGIN_SOURCE, "version": PLUGIN_VERSION},
    )
    merged["probe"] = previous
    return merged


def needs_probe(custom_properties, inferred_due=False):
    """Whether a relation is due for a probe: no block yet, a block written
    under an older schema, or a failed one that Retry Errors flagged. A failure
    is never retried on its own. A block copied from a sibling episode (status
    "inferred") counts as done, unless inferred_due says every episode must be
    measured individually."""
    block = (custom_properties or {}).get("probe")
    if not isinstance(block, dict):
        return True
    if block.get("schema_version") != PROBE_SCHEMA_VERSION:
        return True
    if block.get("status") == STATUS_OK:
        return False
    if block.get("status") == STATUS_INFERRED:
        return inferred_due
    return bool(block.get("retry"))


def flag_retry(custom_properties):
    """custom_properties with the probe block (or series summary) of a failed
    relation marked to be tried again at the next run."""
    merged = dict(custom_properties or {})
    block = merged.get("probe")
    if isinstance(block, dict) and block.get("status") not in (STATUS_OK, STATUS_INFERRED):
        merged["probe"] = {**block, "retry": True}
    return merged


def is_measured(custom_properties):
    """A current-schema block that comes from a real probe of this relation."""
    block = (custom_properties or {}).get("probe")
    return (
        isinstance(block, dict)
        and block.get("status") == STATUS_OK
        and block.get("schema_version") == PROBE_SCHEMA_VERSION
    )


def needs_inference(custom_properties):
    """Whether an episode still lacks a usable block, so it should receive a
    copy of its season's measured one: none yet, an old schema, or a failed
    probe. Measured and already inferred blocks are left alone."""
    block = (custom_properties or {}).get("probe")
    if not isinstance(block, dict) or block.get("schema_version") != PROBE_SCHEMA_VERSION:
        return True
    return block.get("status") not in (STATUS_OK, STATUS_INFERRED)


def merge_inferred(existing, source_properties, source_relation_id, now):
    """New custom_properties for an episode that is given the result measured
    on a sibling of the same season and version. The block is a copy marked
    status "inferred" with inferred_from, minus what belongs to one episode
    (its duration), so a client can tell measured from deduced."""
    merged = dict(existing or {})
    for key in ("quality", "resolution"):
        if key in (source_properties or {}):
            merged[key] = source_properties[key]
    block = {
        k: v for k, v in dict((source_properties or {}).get("probe") or {}).items()
        if k not in ("duration_secs", "attempts", "error")
    }
    block.update(
        status=STATUS_INFERRED,
        inferred_from=source_relation_id,
        schema_version=PROBE_SCHEMA_VERSION,
        source={"plugin": PLUGIN_SOURCE, "version": PLUGIN_VERSION},
    )
    merged["probe"] = block
    return merged


def series_status(episodes, answered, total, exhausted):
    """The series summary's status once a pass over it is done.

    `answered`/`total`: how many independent samples came back usable, out of
    how many exist — a season in "one per season" mode, one episode in "every
    episode" mode, or the one series-wide sample otherwise. `exhausted`: True
    when nothing is left untried among the ones that did not answer (the
    MAX_SAMPLE_TRIES cap can leave some of a season's episodes untried, in
    which case it is not).

    "ok" needs every sample to answer. Short of that: "pending" while
    something up there is still untried, so a next pass (or Retry Errors) can
    still make progress; once nothing is left to try, "error" if none of them
    answered, "partial" if at least one did — both real answers, not "still
    working on it", the same way a failed relation is never retried on its
    own. A series a pass never got to (no episode found yet, or its episode
    list could not be loaded) is "pending" too, via `episodes <= 0`."""
    if episodes <= 0:
        return "pending"
    if total > 0 and answered == total:
        return STATUS_OK
    if not exhausted:
        return "pending"
    return STATUS_ERROR if answered == 0 else STATUS_PARTIAL


def series_marker(existing, last_modified, episodes, seasons, mode, sampled_from, now, status):
    """New custom_properties for a series relation once its episodes have been
    handled. The marker records what was seen (the provider's last_modified,
    the episode and season counts), so the daily scan only has to read series
    relations, and only opens the series whose last_modified moved. `status`
    comes from series_status(); anything but "ok" stays so until Retry Errors
    flags it, since series_work only revisits a series whose status is not
    "ok"."""
    merged = dict(existing or {})
    previous = merged.get("probe") if isinstance(merged.get("probe"), dict) else {}
    marker = {
        "schema_version": PROBE_SCHEMA_VERSION,
        "status": status,
        "probed_at": _iso(now),
        "last_modified": None if last_modified is None else str(last_modified),
        "episodes": episodes,
        "seasons": seasons,
        "mode": mode,
        "source": {"plugin": PLUGIN_SOURCE, "version": PLUGIN_VERSION},
    }
    if sampled_from is not None:
        marker["sampled_from"] = sampled_from
    if status != STATUS_OK:
        marker["attempts"] = int(previous.get("attempts") or 0) + 1
    merged["probe"] = marker
    return merged


# How thorough each way of sampling is: moving to a higher one redoes a series, a lower one leaves it.
_MODE_RANK = {"first_of_series": 0, "first_of_season": 1, "all": 2}


def flag_series_retry(custom_properties):
    """custom_properties of a series relation whose summary is marked so that
    the next run visits it again, whatever its status: one of its episodes
    failed, and only a visit to the series can retry it. A series without a
    summary needs no flag, it is processed anyway."""
    merged = dict(custom_properties or {})
    marker = merged.get("probe")
    if isinstance(marker, dict):
        merged["probe"] = {**marker, "retry": True}
    return merged


def flag_reload(custom_properties, now):
    """custom_properties of a series relation marked so that the next run asks
    the provider for its episode list again (its list looks incomplete)."""
    merged = dict(custom_properties or {})
    marker = merged.get("probe")
    if isinstance(marker, dict):
        merged["probe"] = {**marker, "reload": True}
    else:
        merged["probe"] = {"schema_version": PROBE_SCHEMA_VERSION, "status": "pending", "reload": True, "probed_at": _iso(now)}
    return merged


def series_work(custom_properties, episode_count, mode, last_episode_refresh=None):
    """What a series relation needs, from its own properties and the number of
    episodes Dispatcharr holds for it: None, or {"reason": ..., "reload": bool}.
    Reload means asking the provider for the episode list again: the series has
    never been loaded, it was loaded with no episode (Dispatcharr marks a load
    as done even when the answer was empty), Reload Incomplete Series flagged
    it, or the provider's last_modified moved since we last looked. A series
    whose episodes Dispatcharr reloaded after our summary (last_episode_refresh
    is newer) is reprocessed too: a reload, which the UI triggers when a series
    is opened after 24 hours, replaces the whole custom_properties of every
    episode relation and so erases what we wrote.

    A "pending" summary is also revisited on its own, unlike "error" and
    "partial": it means a season's sample is genuinely still short of
    episodes to try (the MAX_SAMPLE_TRIES cap), not a settled answer, so each
    pass keeps sampling more of what is left without needing Retry Errors."""
    properties = custom_properties or {}
    if not properties.get("episodes_fetched"):
        return {"reason": "load", "reload": True}
    marker = properties.get("probe") if isinstance(properties.get("probe"), dict) else None
    if marker and marker.get("reload"):
        return {"reason": "incomplete", "reload": True}
    if marker and marker.get("retry"):
        return {"reason": "retry", "reload": episode_count == 0}
    if episode_count == 0:
        attempts = int(marker.get("attempts") or 0) if marker else 0
        return {"reason": "empty", "reload": True} if attempts < MAX_EMPTY_RELOADS else None
    if marker is None or marker.get("schema_version") != PROBE_SCHEMA_VERSION:
        return {"reason": "unmarked", "reload": False}
    summarised_at = _parse_iso(marker.get("probed_at"))
    # The summary's time is truncated to the second and written just after our own
    # reload, so a small tolerance keeps that reload from being taken for another one.
    if last_episode_refresh is not None and summarised_at is not None and last_episode_refresh > summarised_at + RELOAD_TOLERANCE:
        return {"reason": "reloaded", "reload": False}
    current = (properties.get("basic_data") or {}).get("last_modified")
    if str(current) != str(marker.get("last_modified")) and not (current is None and marker.get("last_modified") is None):
        return {"reason": "changed", "reload": True}
    if marker.get("status") == "pending":
        # Genuinely unfinished (the MAX_SAMPLE_TRIES cap left episodes of a season
        # untried), not a settled answer: keeps sampling on its own, no Retry Errors
        # needed — unlike "error"/"partial", which are done and need a human to ask
        # again. Each pass tries more of what is left, so this converges on its own.
        return {"reason": "sampling", "reload": False}
    if marker.get("status") != STATUS_OK:
        return None
    if _MODE_RANK.get(mode, 0) > _MODE_RANK.get(marker.get("mode"), 0):
        return {"reason": "mode", "reload": False}  # a more thorough mode than the one it was done with
    if episode_count != marker.get("episodes"):
        return {"reason": "count", "reload": False}
    return None


def coverage_bucket(custom_properties):
    """One of 'ok', 'inferred', 'error', 'never' for the coverage statistics."""
    block = (custom_properties or {}).get("probe")
    if not isinstance(block, dict):
        return "never"
    if block.get("status") == STATUS_OK:
        return "ok"
    return "inferred" if block.get("status") == STATUS_INFERRED else "error"


def needs_id_lookup(has_id, custom_properties):
    """Whether a relation is due for a tmdb_id/imdb_id lookup: it has neither
    field set yet (`has_id` is the caller's own `bool(tmdb_id or imdb_id)`),
    and the last lookup (if any) was not a settled not_found/error outcome
    — or was, but Retry Errors flagged it since. A relation that
    already has an id needs no lookup and no block; the real field is the
    only signal that matters, the same way `probe` never gets a second
    concept of "measured" alongside its own status."""
    if has_id:
        return False
    block = (custom_properties or {}).get("id_lookup")
    if not isinstance(block, dict):
        return True
    if block.get("schema_version") != ID_LOOKUP_SCHEMA_VERSION:
        return True
    return bool(block.get("retry"))


def merge_id_found(existing):
    """custom_properties once a lookup has resolved an id onto the real
    field: any previous id_lookup marker is cleared, since the field itself
    is now the only source of truth and a stale marker would otherwise keep
    describing a problem that no longer exists."""
    merged = dict(existing or {})
    merged.pop("id_lookup", None)
    return merged


def merge_id_not_found(existing, now):
    """custom_properties after a lookup whose provider genuinely has no
    tmdb_id/imdb_id for this title. A terminal outcome, like a failed probe:
    never retried on its own, only via an explicit Retry Errors action."""
    merged = dict(existing or {})
    previous = dict(merged.get("id_lookup") or {})
    previous.pop("retry", None)
    previous.pop("error", None)
    previous.update(
        schema_version=ID_LOOKUP_SCHEMA_VERSION,
        looked_up_at=_iso(now),
        status=ID_STATUS_NOT_FOUND,
        attempts=int(previous.get("attempts") or 0) + 1,
        source={"plugin": PLUGIN_SOURCE, "version": PLUGIN_VERSION},
    )
    merged["id_lookup"] = previous
    return merged


def merge_id_error(existing, error, now):
    """custom_properties after a lookup that failed to even get an answer
    from the provider (network/HTTP error, not "no id"). Same terminal,
    Retry-Errors-only idiom as merge_id_not_found, plus the scrubbed error
    text."""
    merged = dict(existing or {})
    previous = dict(merged.get("id_lookup") or {})
    text = scrub_error(error or "unknown error")
    previous.pop("retry", None)
    previous.update(
        schema_version=ID_LOOKUP_SCHEMA_VERSION,
        looked_up_at=_iso(now),
        status=ID_STATUS_ERROR,
        error=text[:_ERROR_MAX_CHARS],
        attempts=int(previous.get("attempts") or 0) + 1,
        source={"plugin": PLUGIN_SOURCE, "version": PLUGIN_VERSION},
    )
    merged["id_lookup"] = previous
    return merged


def flag_id_lookup_retry(custom_properties):
    """custom_properties with the id_lookup block (if any) marked so the next
    run tries this relation again, whatever its status. A relation with no
    block yet needs no flag, it is tried anyway."""
    merged = dict(custom_properties or {})
    block = merged.get("id_lookup")
    if isinstance(block, dict):
        merged["id_lookup"] = {**block, "retry": True}
    return merged
