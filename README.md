# VOD Probe

A small Dispatcharr plugin that probes each VOD relation (movie or episode) once with `ffprobe` and writes the result into the relation's `custom_properties`, so every tool that reads Dispatcharr's API can use it instead of probing on its own.

It only writes `quality`, `resolution` and a `probe` block. It never deletes, merges or renames anything.

## Status

Version 0.4.0, tested on a Dispatcharr test instance only: movies, and 6 series versions (54 episodes). The full catalogue has not been run yet.

Actions:

- **Probe Run** runs in a Celery background task (never inside the HTTP request), probes a random sample of due movie relations and series versions (or the ids listed in the settings) and, with **Dry run** off, writes `quality`, `resolution` and a `probe` block into each relation's `custom_properties`. Dry run is on by default.
- **Run Status**, **Pause** and **Resume** follow and stop a run; a circuit breaker pauses it when too many probes fail.
- **Scan** counts the relations due for a probe and **Coverage Stats** reports how many are already probed.

Only relations of active accounts in enabled categories are considered. `quality_info` (the field Dispatcharr's API and its source labels already read) then shows the probed quality. The rest of the block is for API clients; see [DESIGN.md](DESIGN.md) for the contract and the open questions.

Series: each version of a series (standard, 4K...) is its own relation and is handled on its own. Its episodes are loaded first if Dispatcharr has not (one provider request per version), then, per season, either one episode is probed and copied to the others (marked `status: inferred`, `inferred_from`) or every episode is probed, depending on the **Episodes** setting. Every episode gets an answer either way.

Incremental by design: only relations without a current `probe` block are due, so a big first pass (Relations per run = 0) is followed by small runs that only pick up new titles, new episodes and retries. A new episode of a known season is copied from the measured one without any probe.

Not done yet: a schedule for the daily run, the slow re-probe rotation, and a locked write test against a concurrent list sync.

## Install

Clone this repo into Dispatcharr's plugins directory (folder name `vod_probe`), enable **VOD Probe** in Dispatcharr → Plugins, and restart Dispatcharr once. `ffprobe` must be on `PATH` in the container.

## Tests

```
python3 -m pytest
```

The tests are pure Python and run outside Dispatcharr.

## License

MIT
