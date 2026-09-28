"""Build the plugin ZIP attached to each GitHub release.

The archive holds a single top-level folder, vod_probe/, with the plugin's
files. Dispatcharr names an imported plugin after that folder, so the plugin
keeps the key vod_probe whether it comes from this ZIP, from the plugin
registry or from a git clone into /data/plugins/vod_probe. Without the folder,
Dispatcharr would name it after the download instead, and an existing
vod_probe install would end up installed twice.

Only the files git tracks at the repository root go in (the plugin has no
subpackage): no tests, no scripts, no runtime data.

    python3 scripts/build_zip.py                  # writes dist/vod_probe.zip
    python3 scripts/build_zip.py --version 1.0.0  # also checks the version
"""
import argparse
import json
import os
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FOLDER = "vod_probe"
# A fixed date, so the same sources always give the same archive.
TIMESTAMP = (2026, 1, 1, 0, 0, 0)


def plugin_files():
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.split()
    return sorted(name for name in tracked if "/" not in name and not name.startswith("."))


def version():
    """plugin.json is the only place the version is stated; plugin.py and
    contract.py derive theirs from it at import time (see manifest.py)."""
    with open(os.path.join(ROOT, "plugin.json"), encoding="utf-8") as f:
        return json.load(f)["version"]


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--version", help="fail unless plugin.json states this version (e.g. the release tag without its v)")
    parser.add_argument("--output", default=os.path.join(ROOT, "dist", f"{FOLDER}.zip"))
    args = parser.parse_args()

    found_version = version()
    if args.version and args.version != found_version:
        sys.exit(f"plugin.json states version {found_version}, not {args.version}")

    files = plugin_files()
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with zipfile.ZipFile(args.output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in files:
            info = zipfile.ZipInfo(f"{FOLDER}/{name}", date_time=TIMESTAMP)
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            with open(os.path.join(ROOT, name), "rb") as f:
                archive.writestr(info, f.read())
    print(f"{args.output}: version {found_version}, {len(files)} files: {', '.join(files)}")


if __name__ == "__main__":
    main()
