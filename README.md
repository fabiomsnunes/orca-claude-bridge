# orca-claude-bridge

Proof of concept: Claude Code reads and changes the live settings of OrcaSlicer 2.5 on a
Mac (tested with an Elegoo Centauri Carbon 2 + Canvas) while the GUI is open, through the
Python plugin API that 2.5.0 introduced.

> **Status:** it works, and I use it day to day. OrcaSlicer is expected to ship its own MCP
> server; once it does, use that instead. This repo stays as a working example of the 2.5.0
> plugin API.

```
Claude Code ──Bash──> ./orca-bridge (CLI, stdlib) ──HTTP 127.0.0.1:47651 + token──> plugin inside Orca
```

- `plugin/orca_claude_bridge.py`: Orca Script plugin ("Claude Bridge"). It starts on its
  own at the first project or preset event after Orca opens and serves HTTP/JSON on
  127.0.0.1. Its dock panel has an **Optimise** button that starts an optimiser you provide.
- `orca-bridge`: the CLI Claude calls from Bash. Python 3.9+, standard library only.

## Why this works (checked in the v2.5.0 source)

- `ScriptAsyncRuntime::run_invocation` (`src/slic3r/plugin/PluginManager.cpp`) runs every
  Script invocation on its own detached thread. An `async def execute` gets an event loop for
  that invocation (`new_event_loop` → `run_until_complete`), so a coroutine that awaits
  `server.serve_forever()` never blocks the UI. While it waits in `select()` it releases the
  GIL, so the UI thread can still run Python callbacks.
- Disabling or unloading the plugin, or quitting Orca, calls `cancel_plugin`, which cancels
  the task at its next `await`. The `finally` then closes the server and renames
  `bridge.json` to `bridge.json.stopped`.
- Network access is declared with `orca.request_permissions(network=["http://127.0.0.1"])`.
  A canonical `scheme://host` grant covers raw sockets to that host on every port, which
  includes `socket.bind`. Nothing else is requested.

## Autostart (2.5.2)

Orca 2.5.2 calls `on_lifecycle_event` on every loaded capability, on the UI thread for project
and preset events. The first `NewProject`, `ProjectOpened`, `PresetSelected`, `ObjectAdded` or
`PlateSelected` after Orca opens creates the dock panel (it must be created on the UI thread)
and starts the server on a thread of its own with its own asyncio loop. `on_unload` cancels it.
This needs the `threading` permission. Clicking Run still works and does the same by hand.

## Optimise button

The panel's **Optimise** button starts `~/.local/bin/orca-optimise`, an executable you write.
The plugin only starts it (one run at a time, nothing runs until the button is pressed) and
passes `ORCA_BRIDGE_INFO` in the environment. A typical optimiser runs a headless agent such as
`claude -p` that reads the model through the bridge, exports a copy with `export-3mf`, slices
variants headless, applies the best settings with `set`, and reports with `log`.

## Using it

1. In Orca: Plugins → Install local plugin → `plugin/orca_claude_bridge.py`, accept the
   permission prompts, enable it, and restart Orca (or click Run on "Claude Bridge"). A small
   "Claude Bridge" panel docks on the right while it is serving.
2. From Bash:

```bash
./orca-bridge status
./orca-bridge presets                       # active process / printer / filament per slot, dirty flags
./orca-bridge get process layer_height      # live (edited) preset value
./orca-bridge get filament nozzle_temperature --slot 2
./orca-bridge config layer_height           # effective merged value Orca slices with
./orca-bridge model                         # objects, parts/modifiers, extruders, bboxes, per-object overrides
./orca-bridge app-info
./orca-bridge project-path
./orca-bridge set process layer_height=0.16 wall_loops=3
./orca-bridge set filament --slot 1 nozzle_temperature=225
./orca-bridge export-3mf ~/Library/Caches/orca-claude-bridge/copy.3mf   # copy of the open project
./orca-bridge log "text"                    # show a line in the panel
```

Output is JSON. The exit code is 1 when there is an error.

Values use Orca's serialized form: plain numbers, `15%`, booleans `1`/`0`, enums by their
internal key, and lists separated by `;`. `./orca-bridge keys <category>` lists the valid keys.

## What it can do

| Area | How |
|---|---|
| App version, build, language | `orca.host.app_info()` (UI thread) |
| Open project path | `plater().project_path()` (UI thread) |
| Active presets, dirty state | `preset_bundle()` collections, `Preset.is_dirty`, `plater().is_presets_dirty()` |
| One setting of the live preset | `Preset.config_value(key)` on the edited preset (or the filament slot's preset) |
| Effective merged config | `preset_bundle().full_config_value(key)` |
| Plate contents | `orca.host.model()`: objects, instances, volumes (type, extruder), bboxes, per-object/volume config overrides |
| Copy of the open project | `plater().export_3mf_copy(path)` on the UI thread, unsaved changes included; only into `~/Library/Caches/orca-claude-bridge` |
| Change settings | `orca.host.preset.{process,printer,filament}.begin_update()` → `set()` → `commit()`, which is the same path as editing the field in the GUI (`Tab::apply_preset_config_values`). This runs validation and `on_change` and sets the dirty marker. |

**Writes never save to disk.** The preset API has no save call. The edited preset stays
marked as modified until you save or discard it in Orca. An invalid key or value fails at
`commit()` and nothing in the batch is applied.

Passing `--preset NAME` to `set` switches preset first, through Orca's normal selection flow.
If the current preset has unsaved edits, Orca shows its usual dialog (transfer, discard or
cancel) and the call waits for the answer. Without `--preset`, it edits the active preset and
no dialog appears.

## What it cannot do (2.5.0 bindings)

- **No load model or slice from a Script.** The bindings have no load-model or slice call,
  so an optimiser slices the exported copy with the OrcaSlicer CLI.
- **No saving, creating or deleting presets**, and no per-object setting changes (the model
  bindings are read-only).
- **No setting changes on objects/modifiers**, no arranging and no moving objects.
- **One target per transaction.** Changing process and filament takes two `set` calls.
- Reads go to the UI thread through the dock panel. `app_info` and `project_path` refuse any
  other thread. If you close the panel, preset and model reads fall back to the worker, which
  works but is not synchronised with the GUI, and `app-info` and `project-path` then fail.
  To get the panel back, untick Activate, tick it again and click Run.
- While a `set` that switches preset waits on Orca's dialog, the server answers nothing else
  (it is one thread).
- Autostart needs 2.5.2's lifecycle events; on 2.5.0 click Run after each Orca start.

## Security

- Binds 127.0.0.1 only, on the first free port from 47651 to 47660 (8765 is already taken on
  the Mac it was built on by another local Python service). The port it actually used is in `bridge.json`.
- Requests need `Authorization: Bearer <token>`. The token is random on
  every Run and written to `bridge.json` in the plugin's storage folder under
  `~/Library/Application Support/OrcaSlicer/orca_plugins/`.
- Rejects any `Host` header other than `127.0.0.1`/`localhost`, which stops DNS-rebinding
  from a web page.
- The CLI refuses a `bridge.json` that names a host other than 127.0.0.1.
- Orca's audit hook is a Python policy, not an OS sandbox. The plugin asks for: the 127.0.0.1
  network grant, starting `~/.local/bin/orca-optimise` only, writing only inside
  `~/Library/Caches/orca-claude-bridge`, and threading (for autostart).
- The optimiser runs with your user rights. Keep it narrow: the plugin never passes it any
  input from the network.

## Notes for the next change

- Install the plugin by copying `plugin/orca_claude_bridge.py` into
  `~/Library/Application Support/OrcaSlicer/orca_plugins/claude-bridge/`. Then go to
  File → Plugins → Refresh, tick Activate, and click Run (▷) on the "Claude Bridge" row.
- To ship a new version, raise `version` in the header, copy the file, then untick Activate,
  click Refresh, tick it again and click Run. Unticking stops the server cleanly. The network
  grant stays in `.install_state.json`, so Orca does not ask again.
- Only stderr goes to `~/Library/Application Support/OrcaSlicer/log/python_*.log`, which is
  where tracebacks end up. `print()` output is not logged.

## Evidence (2026-11-07, OrcaSlicer 2.5.0 build 40ecc66f, live GUI with the ornament project)

| File | What it shows |
|---|---|
| `evidence/01-presets-before.json` | Process `0.20mm Standard @Elegoo CC2 0.4 nozzle`, `is_dirty: false`. Printer `Elegoo Centauri Carbon 2 0.4 nozzle - Copy`. Four filament slots |
| `evidence/02-layer-height-before.json` | `layer_height` = `0.2` |
| `evidence/03-gui-before.jpg` | GUI: Layer height 0.2 and no modified marker on the process preset |
| `evidence/04-set-layer-height.json` | `orca-bridge set process layer_height=0.16` returned `dirty: true`, `saved_to_disk: false` |
| `evidence/05-layer-height-after.json` | Read back: `0.16`, `is_dirty: true`. `config layer_height` (merged) also showed `0.16` |
| `evidence/06-presets-after.json` | `is_presets_dirty: true` |
| `evidence/07-gui-after.jpg` | GUI: Layer height 0.16 in orange with the reset arrow on both the field and the preset combo. The plate thumbnail went back to "Slice", because the old slice was invalidated |
| `evidence/08-invalid-value.json` | `layer_height=abc` was rejected (`ValueError: Invalid value provided ...`) and the value stayed 0.16. `not_a_key=1` was also rejected (`Unknown process preset setting`) |
| `evidence/09-revert.json` | `set process layer_height=0.2` returned `dirty: false`, so the preset matched its saved state again |
| `evidence/10-model.json` | `orca-bridge model`: 1 object, `ornamento-3-laco.stl` |
| `evidence/11-gui-restored.jpg` | GUI back at 0.2 with no marker. The panel shows `Serving http://127.0.0.1:47651` |

The test wrote no file under `~/Library/Application Support/OrcaSlicer/user/`; nothing was
saved. Unticking Activate stopped the server: the port was released and `bridge.json` was
renamed to `bridge.json.stopped`. `app-info` and `project-path` worked through the panel; both
refuse to run off the UI thread.
