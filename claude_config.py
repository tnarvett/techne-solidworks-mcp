"""Point Claude Desktop at the Techne SolidWorks MCP server, or undo that. Run by install.ps1 and
uninstall.ps1 (through uv, which brings its own Python), so it works on a PC that has never run
the server before.

    python claude_config.py install --wheel <path to the .whl> --command <path to uvx.exe>
    python claude_config.py uninstall

install:   every Claude Desktop config found gets a server entry that runs the wheel. An entry that
           already runs solidworks-mcp (any version, any folder) is replaced in place, keeping its
           name; otherwise one named "solidworks" is added. Other servers and settings are kept.
           Each file is backed up first (<file>.techne-backup-<time>).
uninstall: the entries go back to what the oldest backup had (the server before Techne), or are
           removed when there was none.
"""

import argparse
import glob
import json
import os
import re
import shutil
import sys
import time

MARK = re.compile(r"solidworks[-_]mcp", re.I)
TECHNE = re.compile(r"solidworks_mcp-0\.2[1-9]", re.I)
BACKUP = ".techne-backup-"


# ------------------------------------------------------------------ pure parts (unit-tested)

def entry(wheel: str, command: str) -> dict:
    return {"command": command, "args": ["--from", wheel, "solidworks-mcp"]}


def solidworks_names(servers: dict) -> list:
    """Names of the entries that run solidworks-mcp (the wheel or the package)."""
    return [name for name, e in servers.items() if isinstance(e, dict) and MARK.search(json.dumps(e))]


def apply_install(data: dict, wheel: str, command: str) -> tuple:
    data = dict(data or {})
    servers = dict(data.get("mcpServers") or {})
    names = solidworks_names(servers) or ["solidworks"]
    for name in names:
        servers[name] = entry(wheel, command)
    data["mcpServers"] = servers
    return data, names


def apply_uninstall(data: dict, before: dict | None) -> tuple:
    """Put the solidworks entries back as `before` (the oldest backup) had them; drop the ones
    Techne added."""
    data = dict(data or {})
    servers = dict(data.get("mcpServers") or {})
    old = dict((before or {}).get("mcpServers") or {})
    changed = []
    for name in solidworks_names(servers):
        if not TECHNE.search(json.dumps(servers[name])):
            continue  # not ours
        if name in old:
            servers[name] = old[name]
            changed.append(f"{name}: restored")
        else:
            del servers[name]
            changed.append(f"{name}: removed")
    data["mcpServers"] = servers
    return data, changed


# ------------------------------------------------------------------ files

def config_paths(create: bool) -> list:
    local = os.environ.get("LOCALAPPDATA", "")
    roaming = os.environ.get("APPDATA", "")
    found = glob.glob(os.path.join(local, "Claude-Data", "*", "claude_desktop_config.json"))
    for folder in ("Claude", "Claude-3p"):
        path = os.path.join(roaming, folder, "claude_desktop_config.json")
        if os.path.exists(path):
            found.append(path)
    if found or not create:
        return found
    # no config yet: one in each Claude data folder there is, and the standard place
    targets = [os.path.join(d, "claude_desktop_config.json")
               for d in glob.glob(os.path.join(local, "Claude-Data", "*")) if os.path.isdir(d)]
    targets.append(os.path.join(roaming, "Claude", "claude_desktop_config.json"))
    return targets


def read(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8-sig") as fh:
        text = fh.read()
    return json.loads(text) if text.strip() else {}


def write(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        shutil.copy2(path, path + BACKUP + time.strftime("%Y%m%d-%H%M%S"))
    tmp = path + ".techne-tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def oldest_backup(path: str) -> dict | None:
    backups = sorted(glob.glob(glob.escape(path) + BACKUP + "*"))
    for b in backups:
        try:
            return read(b)
        except (OSError, ValueError):
            continue
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("action", choices=["install", "uninstall"])
    ap.add_argument("--wheel")
    ap.add_argument("--command", default="uvx")
    a = ap.parse_args(argv)
    if a.action == "install":
        if not a.wheel or not os.path.exists(a.wheel):
            print(f"No wheel at {a.wheel}", file=sys.stderr)
            return 2
        for path in config_paths(create=True):
            try:
                data, names = apply_install(read(path), os.path.abspath(a.wheel), a.command)
            except ValueError as exc:
                print(f"Skipped {path}: not valid JSON ({exc}). Fix or delete it and run again.")
                continue
            write(path, data)
            print(f"Updated {path}: {', '.join(names)}")
        return 0
    for path in config_paths(create=False):
        try:
            data, changed = apply_uninstall(read(path), oldest_backup(path))
        except ValueError as exc:
            print(f"Skipped {path}: not valid JSON ({exc}).")
            continue
        if changed:
            write(path, data)
            print(f"Updated {path}: {', '.join(changed)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
