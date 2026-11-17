"""Offline tests for claude_config.py (no Windows needed): python -m pytest -q"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import claude_config as cc  # noqa: E402

W = r"C:\Users\x\Documents\Techne\solidworks_mcp-0.21.0-py3-none-any.whl"
UVX = r"C:\Users\x\.local\bin\uvx.exe"


def test_install_adds_entry_to_empty_config():
    data, names = cc.apply_install({}, W, UVX)
    assert names == ["solidworks"]
    assert data["mcpServers"]["solidworks"] == {"command": UVX, "args": ["--from", W, "solidworks-mcp"]}


def test_install_keeps_other_servers_and_settings():
    before = {"mcpServers": {"Roblox_Studio": {"command": "x", "args": ["--y"]}}, "globalShortcut": "Ctrl+Space"}
    data, _ = cc.apply_install(before, W, UVX)
    assert data["mcpServers"]["Roblox_Studio"] == {"command": "x", "args": ["--y"]}
    assert data["globalShortcut"] == "Ctrl+Space"
    assert before["mcpServers"].keys() == {"Roblox_Studio"}  # input untouched


def test_install_replaces_old_entry_in_another_folder_keeping_its_name():
    old = {"mcpServers": {"SolidWorks": {"command": "uvx", "args": [
        "--from", r"D:\old\solidworks_mcp-0.20.0.post1-py3-none-any.whl", "solidworks-mcp"]}}}
    data, names = cc.apply_install(old, W, UVX)
    assert names == ["SolidWorks"]
    assert data["mcpServers"]["SolidWorks"]["args"][1] == W  # the whole path, not just the file name
    assert "solidworks" not in data["mcpServers"]


def test_uninstall_restores_previous_entry_or_removes_ours():
    prev = {"mcpServers": {"solidworks": {"command": "uvx", "args": ["--from", r"C:\a\solidworks_mcp-0.20.0.post1-py3-none-any.whl", "solidworks-mcp"]}}}
    now, _ = cc.apply_install(prev, W, UVX)
    back, changed = cc.apply_uninstall(now, prev)
    assert back["mcpServers"]["solidworks"] == prev["mcpServers"]["solidworks"] and changed == ["solidworks: restored"]
    fresh, _ = cc.apply_install({"mcpServers": {"other": {"command": "o"}}}, W, UVX)
    gone, changed = cc.apply_uninstall(fresh, None)
    assert "solidworks" not in gone["mcpServers"] and gone["mcpServers"]["other"] == {"command": "o"}
    assert changed == ["solidworks: removed"]
    untouched, changed = cc.apply_uninstall(prev, None)  # not a Techne entry: left alone
    assert untouched == prev and changed == []


def test_files_round_trip_with_backup_bom_and_creation():
    with tempfile.TemporaryDirectory() as root:
        local, roaming = os.path.join(root, "Local"), os.path.join(root, "Roaming")
        os.environ["LOCALAPPDATA"], os.environ["APPDATA"] = local, roaming
        acct = os.path.join(local, "Claude-Data", "claude_abc")
        os.makedirs(acct)
        wheel = os.path.join(root, "solidworks_mcp-0.21.0-py3-none-any.whl")
        open(wheel, "w").close()
        # no config anywhere yet: created in the account folder and the standard place
        assert cc.main(["install", "--wheel", wheel, "--command", UVX]) == 0
        made = [os.path.join(acct, "claude_desktop_config.json"), os.path.join(roaming, "Claude", "claude_desktop_config.json")]
        for p in made:
            assert cc.read(p)["mcpServers"]["solidworks"]["args"][1] == wheel
        # an existing config with a BOM and another server: updated in place, backup kept
        p = made[0]
        with open(p, "w", encoding="utf-8-sig") as fh:
            json.dump({"mcpServers": {"other": {"command": "o"}}}, fh)
        assert cc.main(["install", "--wheel", wheel, "--command", UVX]) == 0
        data = cc.read(p)
        assert set(data["mcpServers"]) == {"other", "solidworks"}
        assert any(f.startswith("claude_desktop_config.json.techne-backup-") for f in os.listdir(acct))
        with open(p, "rb") as fh:
            assert not fh.read(3).startswith(b"\xef\xbb\xbf")  # written without a BOM
        # uninstall: back to the oldest backup (which had no solidworks entry) -> removed
        assert cc.main(["uninstall"]) == 0
        assert set(cc.read(p)["mcpServers"]) == {"other"}


def test_bad_json_is_skipped_not_overwritten(capsys):
    with tempfile.TemporaryDirectory() as root:
        os.environ["LOCALAPPDATA"], os.environ["APPDATA"] = os.path.join(root, "L"), os.path.join(root, "R")
        p = os.path.join(root, "R", "Claude", "claude_desktop_config.json")
        os.makedirs(os.path.dirname(p))
        with open(p, "w") as fh:
            fh.write("{not json")
        wheel = os.path.join(root, "w.whl")
        open(wheel, "w").close()
        cc.main(["install", "--wheel", wheel])
        assert open(p).read() == "{not json"
        assert "Skipped" in capsys.readouterr().out
