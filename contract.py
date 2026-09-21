"""The data contract: what vod-probe writes into a relation's custom_properties,
and when a relation is due for (re)probing. See DESIGN.md.

Pure Python, no Django, so it is unit-tested outside Dispatcharr.

Only three keys are ever written: `quality`, `resolution` and `probe`.
Everything else in the dictionary (basic_data, detailed_info, the flags
Dispatcharr keeps) is carried over untouched."""
import re
from datetime import datetime, timedelta, timezone

try:
    from .probe import PROBE_SCHEMA_VERSION, _AD_TITLE_HINTS
except ImportError:  # imported as a top-level module by the unit tests
    from probe import PROBE_SCHEMA_VERSION, _AD_TITLE_HINTS

PLUGIN_SOURCE = "vod-probe"
PLUGIN_VERSION = "0.4.0"

STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_UNREACHABLE = "unreachable"
STATUS_INFERRED = "inferred"

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


def scrub_error(text):
    """ffprobe quotes the stream URL in its errors, and the provider's URL
    carries the account's username and password. The error ends up in
    custom_properties, which every API client can read, so URLs are removed."""
    return _URL.sub("<url>", str(text))


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


def needs_probe(custom_properties, now, retry_after=timedelta(hours=24), max_attempts=3, inferred_due=False):
    """Whether a relation is due for a probe: no block yet, a block written
    under an older schema, or a failure old enough to try again (and not yet
    tried max_attempts times). A block copied from a sibling episode
    (status "inferred") counts as done, unless inferred_due says every
    episode must be measured individually."""
    block = (custom_properties or {}).get("probe")
    if not isinstance(block, dict):
        return True
    if block.get("schema_version") != PROBE_SCHEMA_VERSION:
        return True
    if block.get("status") == STATUS_OK:
        return False
    if block.get("status") == STATUS_INFERRED:
        return inferred_due
    if int(block.get("attempts") or 0) >= max_attempts:
        return False
    last = _parse_iso(block.get("probed_at"))
    return last is None or now - last >= retry_after


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


def coverage_bucket(custom_properties):
    """One of 'ok', 'inferred', 'error', 'never' for the coverage statistics."""
    block = (custom_properties or {}).get("probe")
    if not isinstance(block, dict):
        return "never"
    if block.get("status") == STATUS_OK:
        return "ok"
    return "inferred" if block.get("status") == STATUS_INFERRED else "error"
