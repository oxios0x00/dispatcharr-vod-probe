"""The plugin's identity (name, version, description, author, help_url), read
once from plugin.json: the single place these are stated.

Pure Python, no Django, so it works the same way whether Dispatcharr is
running or this is imported standalone by the tests. Safe to import at module
top level, unlike the Django-touching sibling modules (see plugin.py): a
static JSON file has nothing to go stale across a plugin reload cycle."""
import json
import os

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugin.json"), encoding="utf-8") as _f:
    _manifest = json.load(_f)

NAME = _manifest["name"]
VERSION = _manifest["version"]
DESCRIPTION = _manifest["description"]
AUTHOR = _manifest["author"]
HELP_URL = _manifest.get("help_url", "")
