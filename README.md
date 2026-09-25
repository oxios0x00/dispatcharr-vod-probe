# VOD Probe

**Measure the real quality of your VOD catalogue once, and let every tool read it.**

VOD Probe is a [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr) plugin. It runs `ffprobe` against the movies and episodes Dispatcharr imported from your Xtream Codes providers, and writes the result into each relation's `custom_properties`. Anything that reads Dispatcharr's API (a media-server companion, another plugin, a script) can then use the real resolution, HDR type, codec and audio languages instead of guessing from a category name.

It does not delete, merge or rename anything, and it does not write `.strm` files. It only adds information.

## Why

Dispatcharr never probes a stream. The quality it shows comes from a keyword in the title or from a field the provider may or may not fill in, and provider category names are unreliable: in a "4K Dolby Vision" group, a large share of titles turn out to be plain SDR. If several tools each probe the catalogue on their own, they multiply the load on your provider's connections and each keeps its own private copy of the answer.

VOD Probe does the measuring in one place, in the background, and stores the answer where Dispatcharr's API already exposes it.

## What you get

For every probed relation, `quality` and `resolution` are set, using the vocabulary Dispatcharr's own `quality_info` already understands, so existing clients pick them up without any change:

```json
{
  "id": 8135,
  "movie": "Some Movie (2026)",
  "quality_info": { "quality": "4K" },
  "custom_properties": {
    "quality": "4K",
    "resolution": "3840x2160",
    "probe": {
      "schema_version": 5,
      "status": "ok",
      "probed_at": "2026-09-21T14:26:21Z",
      "tier": "2160p",
      "hdr": "hdr10",
      "video": { "codec": "hevc", "profile": "Main 10", "bit_depth": 10, "bit_rate": 13954901, "frame_rate": 24.0 },
      "bit_rate": 17034152,
      "audio_languages": ["eng", "ger", "fre"],
      "audio": [
        { "codec": "eac3", "channels": 6, "language": "eng" },
        { "codec": "eac3", "channels": 6, "language": "ger" },
        { "codec": "eac3", "channels": 6, "language": "fre" }
      ],
      "duration_secs": 6776.0,
      "container": "matroska,webm",
      "source": { "plugin": "vod-probe", "version": "0.8.0" }
    }
  }
}
```

A relation that was never probed simply has no `probe` block, and `quality_info` stays `null`.

### The `probe` block

| Field | Meaning |
| --- | --- |
| `schema_version` | Format version. A block from an older format is probed again. |
| `status` | `ok` (measured), `inferred` (copied from a probed sibling, see below), `error` or `unreachable`. |
| `probed_at` | When it was measured (UTC). |
| `tier` | `2160p`, `1080p`, `720p`, `480p`, `sd` or `unknown`. Width **or** height decides, so a 3840x1608 cinemascope encode is still 2160p. |
| `hdr` | `sdr`, `hdr10`, `hlg` or `dolby_vision`. |
| `video` | Codec, profile, bit depth, video bitrate (bits/s) and frame rate, when the file reports them. |
| `bit_rate` | Bitrate of the whole file (video and audio), in bits/s. |
| `audio_languages` | The languages present, de-duplicated: the field to filter on. |
| `audio` | Every audio track: codec, channels, language, and `audio_description: true` when it is an audio-description track. |
| `duration_secs`, `container` | Duration and container format. |
| `source` | Which plugin and version wrote the block. |

`quality` maps the tier to `4K`, `1080p`, `720p`, `480p` or `SD`. A failed probe never overwrites the last known `quality` and `resolution`.

Error messages are stripped of URLs before they are stored: a provider URL carries your account's credentials, and `custom_properties` is readable through the API.

### The series summary

Each series relation (one version of a series) also carries its own `custom_properties.probe`, a summary of what the plugin did with its episodes:

```json
"probe": {
  "schema_version": 5,
  "status": "ok",
  "probed_at": "2026-09-21T14:30:02Z",
  "last_modified": "1789592978",
  "episodes": 8,
  "seasons": 1,
  "mode": "first_of_series",
  "sampled_from": 12349,
  "source": { "plugin": "vod-probe", "version": "0.9.0" }
}
```

| Field | Meaning |
| --- | --- |
| `schema_version` | Format version, as for a relation. |
| `status` | `ok`: **every episode has an answer**, measured or inferred. `partial`: some do, and the rest are confirmed dead — settled, nothing more will happen without **Retry Errors**. `error`: none do — every one it could try failed, settled too. `pending`: genuinely not sampled yet, or (in `first_of_season`) a season the sample cap has not finished trying — this one keeps going on its own, no Retry Errors needed. A series with no summary has not been handled yet. |
| `probed_at` | When the summary was written (UTC). |
| `last_modified` | The provider's own "last modified" value for the series at that time. |
| `episodes`, `seasons` | How many the plugin saw. |
| `mode` | The **Episodes** setting used: `first_of_series`, `first_of_season` or `all`. |
| `sampled_from` | The episode relation that was probed and copied from. In *Every episode* mode it is simply the first episode. Present when `status` is `ok` or `partial`. |
| `attempts` | How many times the series ended up short of `ok`: `pending`, `partial` or `error`. |
| `retry`, `reload` | Internal flags set by Retry Errors and Reload Incomplete Series. Ignore them. |

### What you can rely on

If you build on this data, these are the rules:

- **A relation has a usable result** when `probe.status` is `ok` or `inferred` and `probe.tier` is set. `error` and `unreachable` mean the probe was tried and failed. No `probe` block means the relation has not been handled yet. A series is fully answered when its summary's `status` is `ok`. `partial` and `error` are real answers too — sampled, some or none usable — not "still working on it"; only `pending` means that.
- **Documented fields keep their name and meaning** as long as `schema_version` does not change. It is currently **5**.
- **Fields may be added without a version change**, so ignore the ones you do not know. The same goes for a new value of an existing field that only narrows a state already treated as "not answered" — `error` (1.0.2) and `partial` (1.0.3) for a series summary's `status` are ones: a consumer already treating anything but `ok` as "not ready" needs no change to stay correct, and can start treating them as a definitive answer whenever it does.
- **A rename, a removal or a change of meaning raises `schema_version`.** Treat a block whose version you do not know as not measured.
- **Not part of the contract:** the text of `error`, `attempts`, `retry`, `reload` and `source`. They are informational and may change.
- **`quality`** (next to `probe`) uses Dispatcharr's own vocabulary, so `quality_info` keeps working.
- **Data can be missing for a while.** When Dispatcharr reloads a series, it erases what was written on its episodes until the next run (see [below](#dispatcharr-can-erase-what-was-written-on-episodes)). A consumer should treat a missing block as "not measured yet", not as an error.

## How it works

### Movies

Each movie relation is probed once. Dispatcharr builds the relation's stream URL, the same way it does for playback: the provider's own URL, with that relation's account and stream id, so each version is measured at its own provider. `ffprobe` reads only the few megabytes it needs from it; a probe typically takes around a second. The probe goes straight to the provider, not through Dispatcharr's proxy, so Dispatcharr does not count it as a connection: if an account allows a single connection, a probe can clash with someone watching on that account.

### Series

Each *version* of a series (the standard one, the 4K one, and so on) is a separate relation with its own files and its own episode list, so each is handled on its own:

1. **Load the episode list** if Dispatcharr has not. This is one request to the provider per version, the same call the Dispatcharr UI makes when you open a series, and it takes a fraction of a second.
2. **Probe**, according to the **Episodes** setting:

   | Setting | What is probed | Cost |
   | --- | --- | --- |
   | *One episode per series version* (default) | The first probeable episode. Its result is copied to every other episode of that version. | One probe per version, whatever the number of seasons. |
   | *One episode per season* | The first episode of each season, copied within its season. | One probe per season. Catches a season encoded differently from the others. |
   | *Every episode individually* | Each episode. | One probe per episode: hours on a big catalogue. |

3. **Every episode ends up with an answer** in all three cases. A copy carries `status: "inferred"` and `inferred_from` (the id of the relation it was copied from) and no `duration_secs`, so a client can always tell a measurement from a deduction.

Choosing a more thorough setting redoes the series already handled; choosing a lighter one leaves them as they are.

Each series relation also gets a small summary in its own `custom_properties.probe`: the provider's `last_modified` value, the number of episodes and seasons, and which episode was sampled. It is what lets the daily run stay cheap.

### Incremental by design

Only relations without a current `probe` block are due, so the first run is a long one and the following ones only pick up what is new:

- **Movies:** no `probe` block, or a block in an older format.
- **Series versions:** never loaded, loaded with no episode, flagged by **Reload Incomplete Series**, no summary yet, the provider's `last_modified` moved (the episode list is then requested again), the number of episodes differs, Dispatcharr reloaded the series since the summary, or a more thorough **Episodes** setting was chosen.
- **New episodes** of a series already sampled are copied from the measured episode without any new probe.

The daily run reads series relations only, never their episodes, and opens just the ones that changed.

### It runs in the background

Probe Run is a Celery task on Dispatcharr's `dvr` queue, not part of the web request. The button returns at once and you follow the work with **Run Status**. Long runs inside a request end in gateway timeouts, which is exactly what this design avoids.

## Install

1. Download `vod_probe.zip` from the [latest release](https://github.com/oxios0x00/dispatcharr-vod-probe/releases/latest). In Dispatcharr, open *Plugins*, click **Import Plugin** and drop the ZIP. It installs into Dispatcharr's plugins directory (`/data/plugins` by default), in a folder named `vod_probe`.

   Or clone the repository there yourself, in a folder of the same name:

   ```bash
   git clone https://github.com/oxios0x00/dispatcharr-vod-probe.git /data/plugins/vod_probe
   ```

2. Make sure `ffprobe` is on the container's `PATH` (Dispatcharr's image ships with it).
3. Restart Dispatcharr once, so that its background worker picks the plugin up, then enable **VOD Probe** in *Plugins*.
4. The plugin creates a `vod_probe_data` folder next to its own, in the same plugins directory (set `VOD_PROBE_DATA_DIR` to put it elsewhere). It keeps a small SQLite file there: the run lock, the pause flag, and the progress of the current run and the last report. No probe result is stored in it. The plugins directory must be writable by the user Dispatcharr runs as, which it is by default.

To upgrade, import the new ZIP the same way and accept to replace the installed plugin, then restart Dispatcharr. Dispatcharr replaces the whole plugin folder, which is why the plugin's data lives next to it: the settings, the schedule, the probe results (in the catalogue) and `vod_probe_data` are all kept. Up to 0.10.0 that state lived in `vod_probe/data/`, inside the plugin folder: upgrading from such a version by importing the ZIP loses it once (**Run Status** starts empty, a pause is lifted). An install updated in place, with `git pull`, keeps it, and the plugin moves it to `vod_probe_data` the first time it starts.

To uninstall, click **[SCHEDULE] Remove** first (otherwise Celery keeps a nightly task for a plugin that is gone), delete the plugin in Dispatcharr, then the `vod_probe_data` folder, which Dispatcharr does not know about. What the plugin wrote into the catalogue stays there.

## Quick start

Keep **Dry run** on for the first step.

1. **Scan** counts what is due: movies to probe, and series versions to handle with the reason for each. Nothing is written.
2. **Probe Run** with Dry run on. It goes through everything due and probes for real, but writes nothing and loads no episode lists: Dispatcharr's log shows, for each relation, what it would write. Click **Pause** once you have seen enough, or put a few ids in *Only these movie relation ids* to try only those.
3. Turn **Dry run** off and click **Probe Run** again: this is the first pass, which writes everything due, batch after batch. Follow it with **Run Status**; **Pause** stops it after the relation in progress, **Resume** lifts the pause and the next run carries on. Check a few relations through the API; in the UI, the quality also shows next to each source in a movie's details.
4. From then on, run it on a schedule (below): each run only handles what is new.

## Settings

| Setting | Default | What it does |
| --- | --- | --- |
| Dry run | on | Probe for real but write nothing, and load no episode lists. |
| Batch size | 25 | A run goes through everything due, this many movie relations and series versions at a time (one series version counts as one, all its seasons included). Between batches it reads what is due again, so it also takes what became due meanwhile, and it does not stop until nothing is left, you click **Pause**, or the circuit breaker trips. `0` means a single batch. |
| Only these movie relation ids / series relation ids | empty | Comma-separated. When set, a run handles exactly these and nothing else, and probes them again even if they are already done. Handy to redo one title. |
| Episodes: what to probe | one per series version | See [Series](#series). |
| Max concurrent probes | 1 | Each probe opens a real connection to your provider. Stay below its connection limit. |
| Max probes started per second | 2 | Caps the rate independently of the concurrency. `0` means no limit. |
| Stop when the error ratio exceeds | 0.8 | The circuit breaker: after 5 probes, a run whose failures exceed this share pauses itself instead of hammering a failing provider. See [Failures](#failures). |
| Retry errors at the next scheduled run | off | One-shot: the next scheduled run tries the failed relations again, then the switch turns itself off. |
| Probe timeout (seconds) | 25 | `ffprobe` gives up after this delay. |
| Schedule (5-field cron) | empty | Empty means no schedule. See [Scheduling](#scheduling). |

## Actions

| Action | What it does |
| --- | --- |
| **Probe Run** | Starts the background run described above. |
| **Run Status** | Progress of the run in flight, or the result of the last one. |
| **Pause** / **Resume** | Stop a run after the relation in progress and block new ones until resumed. The circuit breaker pauses the same way. |
| **Retry Errors** | Flags every failed relation (and series version left pending) so the next run tries it again. |
| **Reload Incomplete Series** | Finds series versions holding fewer episodes than another version of the same series, asks the provider how many it lists, and flags those where it lists more, so the next run asks for their episode list again. Background task; a dry run only reports. See [Incomplete episode lists](#incomplete-episode-lists). |
| **Scan** | Counts what is due and why. Writes nothing. |
| **Coverage Stats** | How many relations have an answer, split into measured and inferred, with the tiers found. |
| **[SCHEDULE] Apply / Remove / Status** | Manage the periodic run. |

**Scan**, **Coverage Stats**, **Retry Errors** and **Reload Incomplete Series** also run in the background, because they read every relation. The click returns at once; their result appears in Dispatcharr's notification centre. A notification is also sent when a run ends; **Run Status** gives the details of that run, or the progress of the one in flight.

## Scheduling

Dispatcharr has no scheduling API for plugins, so VOD Probe registers a `django-celery-beat` periodic task, the same way other plugins do.

- Set **Schedule** to a cron expression (`0 4 * * *` is every day at 04:00), then click **[SCHEDULE] Apply**. The time is read in the time zone set in Dispatcharr's System Settings, like Dispatcharr's own schedules; notification titles show the time in the same zone. An empty schedule means no schedule: Apply then removes any existing one.
- The settings are **copied when you click Apply**, so click it again after changing one. The exceptions are the retry switch, which is read live when the run starts, and the id lists, which a scheduled run ignores: like any run, it handles everything due.

## Good to know

### Failures

A failed probe is never retried on its own, so a dead link is not probed again every day. Use **Retry Errors** (immediately) or the **Retry errors at the next scheduled run** switch. Retried probes do not count towards the circuit breaker, because they are known failures; a new, real outage still trips it.

A relation whose probe fails keeps its last known `quality` and `resolution`; only `probe.status`, the short error and an attempt counter change.

The same is true of a series version once it has fully sampled and is left with nothing more to try: its summary settles on `status: "error"` (nothing worked) or `"partial"` (some seasons did, one or more are confirmed dead) and is left alone, exactly like a failed movie — **Retry Errors** flags it (and its failed episodes) for the next run too. If the provider is still down, it settles back where it was, not `pending`. A season with more episodes than the sample cap tries a few more each pass on its own, without Retry Errors — a `pending` summary means real work is still left, not a settled answer.

### Incomplete episode lists

Dispatcharr marks a series' episodes as loaded as soon as the provider answers without an error, even when the answer was empty or partial, and never checks again. An episode Dispatcharr does not hold has no information at all, so two cases are handled:

- **A series loaded with no episode** is asked for again automatically, up to 3 times, then left alone (some series are genuinely empty at the provider).
- **A version holding fewer episodes than another version of the same series** may have had a truncated load, or the provider may simply have less of it. Only the provider can tell, so **Reload Incomplete Series** asks it (one request per suspicious version) and flags only those where it lists more. A real difference is left alone. Run it when you suspect gaps, then run Probe Run.

### Dispatcharr can erase what was written on episodes

When Dispatcharr reloads a series' episodes, it replaces the whole `custom_properties` of every episode relation of that series. That wipes `quality`, `resolution` and `probe` on them. Dispatcharr does this, for instance, when a series is opened in the UI more than 24 hours after its last refresh.

The plugin notices (the series' `last_episode_refresh` is newer than its summary) and redoes the series on its next run, which costs one probe and some copies in the default mode. Until then, those episodes have no quality in the API.

### Disabling and re-enabling a group

Disabling a category in Dispatcharr and syncing makes Dispatcharr remove that group's relations and the movies and series left without any. There is nothing to clean up on the plugin's side. Re-enabling the group brings its relations back as new ones, without the plugin's data, and the next run handles them.

### Dry run and episode lists

Loading an episode list makes Dispatcharr create episodes in its database. A dry run therefore never loads one, and series whose episodes are not loaded yet are only counted.

### Logs

The plugin logs under `apps.plugins.vod_probe`, so it follows Dispatcharr's `DISPATCHARR_LOG_LEVEL` like the rest of Dispatcharr. At the default level you get the summary of each run and, in a **dry run**, one line per relation with the block it *would* write, which is how you inspect a dry run in detail. A run that writes logs those per-relation lines at debug level only, to keep the log short.

### Use of your provider

Each probe is a real stream connection, and each series version costs one metadata request. Keep concurrency low, and do not run this at the same time as another tool that probes the same relations.

## Limitations

- **Xtream Codes accounts only**, like Dispatcharr's own VOD support.
- **A file replaced by the provider under the same id is not detected.** Nothing cheap can tell that a file changed; only probing it again would.
- **Episodes are sampled by default.** "One per series version" assumes the files of a version are alike, and a series whose seasons differ in quality is caught only with "One per season" or "Every episode". Copies are always marked `inferred`.
- **The provider's own technical fields are not used.** For episodes, some providers send video, audio and bitrate data with the episode list. VOD Probe does not read it: it measures the stream itself.
- **Detecting a new episode relies on `last_modified`.** It is the provider's own timestamp on each series, and whether a provider bumps it when an episode is added depends on that provider.
- **The UI shows less than the API.** From reading Dispatcharr's front-end code, its interface uses `quality_info` only in the label of each source of a movie, and its "Technical Details" panel reads the provider's own data, not the plugin's. Cards and lists show no quality.

## Compatibility

Developed and tested against **Dispatcharr 0.31.0** (image `ghcr.io/dispatcharr/dispatcharr:latest`, September 2026) with Python 3.13.

The plugin relies on parts of Dispatcharr that are not a public API, so an update can break it:

- the models `M3UMovieRelation`, `M3USeriesRelation`, `M3UEpisodeRelation` and `M3UVODCategoryRelation` (`apps.vod.models`), including the fields `custom_properties`, `last_episode_refresh` and `series_relation`;
- `refresh_series_episodes` (`apps.vod.tasks`), the function that loads a series' episodes;
- the Xtream client (`core.xtream_codes`), `SystemNotification` and `CoreSettings.get_system_time_zone()` (`core.models`);
- `django-celery-beat` and the `dvr` Celery queue.

If a run fails after a Dispatcharr update, look at these first.

## Status

Version 1.0.4. In daily use since 2026-09-21 on a Dispatcharr 0.31.0 instance that serves a Jellyfin library, with one Xtream Codes provider and a catalogue of about 600 movies, 900 series and 22,000 episodes. That covers a full first pass on movies and series, writes to the catalogue, disabling and re-enabling groups, failed probes and retries, Dispatcharr's own scheduled refreshes, and the plugin's scheduled runs with the retry switch on. Not tested with several providers or with a concurrency above 1.

## Development

The pure logic (the data contract, the season and series planning, the circuit breaker, the run state) has no Django dependency and is unit-tested. `tests/test_consumer_contract.py` reads the blocks the way a consumer would and checks that the documented fields are there, so a change that would break a tool built on this data fails a test:

```bash
python3 -m pytest
```

Modules: `plugin.py` (actions and the run), `contract.py` (what is written and when a relation is due), `plan.py` (what to probe in a series), `breaker.py`, `state.py`, `probe.py` and `probe_summary.py` (the `ffprobe` call, adapted from [dispatcharr-vod-manager](https://github.com/oxios0x00/dispatcharr-vod-manager)).

`python3 scripts/build_zip.py` builds `dist/vod_probe.zip`, the archive attached to each release, with everything in a `vod_probe/` folder so that Dispatcharr installs it under that name. The *Release ZIP* workflow runs the tests, builds it and attaches it when a release is published; it fails if `plugin.json`, `plugin.py` and `contract.py` do not all state the release's version.

## License

MIT
