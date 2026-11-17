# Techne SolidWorks MCP

Let Claude (or any MCP client) model, measure, simulate and check parts in **your own SolidWorks**.

Techne is an extension of [hjbaard/SolidWorks-MCP](https://github.com/hjbaard/SolidWorks-MCP). It keeps all 88 of the original tools and adds 53 more, plus a small SolidWorks add-in. Most of the new tools reach native SolidWorks features: free-form sketches with relations and dimensions, extrude, revolve, sweep and helix, any feature by name, Simulation static and frequency studies, topology optimization, and checkpoints and rollback.

<!-- mcp-name: io.github.tnarvett/techne-solidworks-mcp -->

**141 tools total:** 88 from the original server and 53 from Techne.

## Requirements

- Windows with **SolidWorks** (tested on SolidWorks 2025, API 33.5). The sim and topology tools need SOLIDWORKS Simulation; topology needs Simulation Professional or Premium.
- **Claude Desktop** (any plan) or another MCP client.
- Nothing else. The installer gets [uv](https://docs.astral.sh/uv/), which brings its own Python, and compiles the add-in with the C# compiler that ships with Windows.

## Install

1. Quit SolidWorks and Claude Desktop (tray icon > Quit).
2. Clone or download this repo somewhere permanent, e.g. `Documents\techne-solidworks-mcp`. Claude Desktop runs the server from this folder, so don't move it afterwards.
3. In PowerShell, from that folder:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\install.ps1
   ```

   Click **Yes** on the admin prompt (registering the add-in needs it). The installer:
   - builds and registers the **Techne** add-in
   - installs uv if missing
   - builds the server wheel from `src\`
   - adds a `solidworks` server to every Claude Desktop config it finds, or replaces an older one. A backup of each config is kept.
4. Open SolidWorks and Claude Desktop again. In SolidWorks, **Tools > Add-Ins** should list **Techne**. Turn on **SOLIDWORKS Simulation** there if you want the sim/topology tools.

Options: `-SkipAddin` (server only), `-SkipMcp` (add-in only), `-NoPause`. Everything the installer does is logged to `install.log`.

**Uninstall:** `uninstall.ps1` unregisters the add-in and puts the Claude Desktop config back the way it was.

**Server only, without the add-in:** add this to `claude_desktop_config.json` (Settings > Developer > Edit Config), with the path to your clone:

```json
{
  "mcpServers": {
    "solidworks": {
      "command": "uvx",
      "args": ["--from", "C:\\path\\to\\techne-solidworks-mcp", "solidworks-mcp"]
    }
  }
}
```

Without the add-in, `watch_changes` has no journal of your manual edits. Every other tool works.

## What Techne adds

| Area | Tools |
| --- | --- |
| Any SolidWorks API | `run_api_script`, `call_api`, `api_lookup`, `create_feature`, `get_feature`, `modify_feature`, `select_entities`, `name_entity` |
| Sketching | `sketch_entities`, `sketch_relation`, `sketch_dimension`, `sketch_fully_define`, `sketch_info`, `sketch_from_edges`, `add_3d_sketch`, `sketch_picture`, `curve_xyz` |
| Features | `extrude`, `revolve`, `sweep`, `add_helix`, `add_draft`, `add_axis`, `add_coordinate_system`, `scale_body`, `split_line` |
| Safe, fast work | `checkpoint`, `restore_checkpoint`, `rollback`, `rename_feature`, `rebuild_report`, `feature_stats`, `run_recipe`, `job_status`, `watch_changes`, `telemetry_summary` |
| Document | `doc_settings`, `custom_properties`, `configurations` |
| Measuring | `measure`, `section_properties`, `mass_properties_about`, `check_geometry` |
| Simulation | `sim_static`, `sim_frequency`, `sim_list`, `sim_delete` |
| Topology | `topology_study`, `topology_sweep`, `topology_run`, `topology_views`, `topology_verify` |
| Mesh | `import_mesh` |

The server's built-in guide (`solidworks://guide`) explains the conventions to the model. `src/solidworks_mcp/guide.md` has the Techne part at the end.

## Files and data on your PC

- **Add-in journal:** the add-in has no UI, timers or threads. It only appends one line per document event to `%LOCALAPPDATA%\Techne\journal`, which `watch_changes` reads.
- **Telemetry:** every tool call is logged locally to `%LOCALAPPDATA%\Techne\telemetry.jsonl` (`telemetry_summary`). Nothing is sent anywhere.
- **Checkpoints:** saved to `Documents\Techne\snapshots`, because SolidWorks won't save into AppData.
- **Topology runs:** a run takes about a minute or more. It runs as a background job (`job_status`), and other tools wait until it finishes. Each setup is recorded in `%LOCALAPPDATA%\Techne\topology` so `topology_verify` can re-run it on the part you model from the result.

## Tips

- **Materials:** SolidWorks' `6061 Alloy` is annealed (55 MPa yield). Use `6061-T6 (SS)` for T6 stock.
- **Units:** the tools work in mm whatever the part's units. `doc_settings(units="ips")` or `"mmgs"` only changes how the part displays.
- **Don't let anything close the last document out from under the server.** If the server reports "The object invoked has disconnected from its clients", restart Claude Desktop so it reconnects to SolidWorks.

## Development

```powershell
uv run --no-project python build_wheel.py      # build solidworks_mcp-<version>-py3-none-any.whl from src\
uv run --extra dev pytest -m "not solidworks"  # unit tests (no SolidWorks needed)
```

- **Source:** `src/solidworks_mcp/`. The Techne modules are `ext_*.py` and `api_script.py`; the rest is the original server.
- **Tests:** `tests/` are the original server's tests. `techne_tests/offline/` covers the Techne tools without SolidWorks. `techne_tests/live/live_check.py` runs staged checks against a live SolidWorks; see its docstring.
- **Add-in:** `addin/Techne/TechneAddin.cs`. `install.ps1` compiles it with `csc.exe` against SolidWorks' interop DLLs. Those DLLs are Dassault's and are not in this repo.
- **Updates from the original:** `git fetch upstream && git merge upstream/main`. This repo keeps the original history, so merges work.

Issues and pull requests are welcome.

## Credits and license

MIT. The original SolidWorks MCP server is by **Henk Jan Baard** ([hjbaard/SolidWorks-MCP](https://github.com/hjbaard/SolidWorks-MCP)); its README is kept at [Docs/UPSTREAM_README.md](Docs/UPSTREAM_README.md). The Techne extension and add-in are by **Thomas Narvett**. See [LICENSE](LICENSE).

SolidWorks is a trademark of Dassault Systèmes. This project is not affiliated with or endorsed by Dassault Systèmes.
