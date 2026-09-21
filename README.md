# VOD Probe

A small Dispatcharr plugin that probes each VOD relation (movie or episode) once with `ffprobe` and writes the result into the relation's `custom_properties`, so every tool that reads Dispatcharr's API can use it instead of probing on its own.

It only writes `quality`, `resolution` and a `probe` block. It never deletes, merges or renames anything.

## Status

Version 0.3.0, tested on a Dispatcharr test instance only, on movies. It has not been run on series yet.

Actions:

- **Probe Run** runs in a Celery background task (never inside the HTTP request), probes a random sample of due relations, or the ids listed in the settings, and, with **Dry run** off, writes `quality`, `resolution` and a `probe` block into each relation's `custom_properties`. Dry run is on by default.
- **Run Status**, **Pause** and **Resume** follow and stop a run; a circuit breaker pauses it when too many probes fail.
- **Scan** counts the relations due for a probe and **Coverage Stats** reports how many are already probed.

Only relations of active accounts in enabled categories are considered. `quality_info` (the field Dispatcharr's API and its source labels already read) then shows the probed quality. The rest of the block is for API clients; see [DESIGN.md](DESIGN.md) for the contract and the open questions.

Not done yet: episodes (Dispatcharr loads them on demand), the scheduled re-probe rotation, and a locked write test against a concurrent list sync.

## Install

Clone this repo into Dispatcharr's plugins directory (folder name `vod_probe`), enable **VOD Probe** in Dispatcharr → Plugins, and restart Dispatcharr once. `ffprobe` must be on `PATH` in the container.

## Tests

```
python3 -m pytest
```

The tests are pure Python and run outside Dispatcharr.

## License

MIT
