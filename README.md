# VOD Probe

A small Dispatcharr plugin that probes each VOD relation (movie or episode) once with `ffprobe` and writes the result into the relation's `custom_properties`, so every tool that reads Dispatcharr's API can use it instead of probing on its own.

It only writes `quality`, `resolution` and a `probe` block. It never deletes, merges or renames anything.

## Status

Version 0.6.0, tested on a Dispatcharr test instance only: about 630 movies and 450 series versions so far; the full catalogue pass is not finished.

Actions:

- **Probe Run** runs in a Celery background task (never inside the HTTP request), probes a random sample of due movie relations and series versions (or the ids listed in the settings) and, with **Dry run** off, writes `quality`, `resolution` and a `probe` block into each relation's `custom_properties`. Dry run is on by default.
- **Run Status**, **Pause** and **Resume** follow and stop a run; a circuit breaker pauses it when too many probes fail.
- **Scan** counts the relations due for a probe and **Coverage Stats** reports how many are already probed.

Only relations of active accounts in enabled categories are considered. `quality_info` (the field Dispatcharr's API and its source labels already read) then shows the probed quality. The rest of the block is for API clients; see [DESIGN.md](DESIGN.md) for the contract and the open questions.

Series: each version of a series (standard, 4K...) is its own relation and is handled on its own. Its episodes are loaded first if Dispatcharr has not (one provider request per version, about 0.3 to 0.5 s), then either one episode is probed and its result copied to all the others (marked `status: inferred`, `inferred_from`) or every episode is probed, depending on the **Episodes** setting. Every episode gets an answer either way. Each series relation also gets a small summary in its `custom_properties.probe` (the provider's `last_modified`, the episode and season counts).

Incremental by design: the daily scan reads series relations only, never the episodes. A series is due when it was never loaded, has no summary, the provider's `last_modified` moved (its episode list is then requested again), the number of episodes differs, or Dispatcharr reloaded it since our summary. That last case matters: **a reload by Dispatcharr (which the UI triggers when a series is opened after 24 hours) replaces the whole `custom_properties` of every episode relation of that series, so our data on them is erased** until the next scan puts it back (one probe and copies). Movies are due when they have no current `probe` block.

A daily schedule is available (Apply Schedule); its timer firing has not been observed yet. Not done: the slow re-probe rotation, and a test against a concurrent list sync.

## Install

Clone this repo into Dispatcharr's plugins directory (folder name `vod_probe`), enable **VOD Probe** in Dispatcharr → Plugins, and restart Dispatcharr once. `ffprobe` must be on `PATH` in the container.

## Tests

```
python3 -m pytest
```

The tests are pure Python and run outside Dispatcharr.

## License

MIT
