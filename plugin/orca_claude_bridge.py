# /// script
# requires-python = ">=3.12"
# dependencies = []
#
# [tool.orcaslicer.plugin]
# name = "Claude Bridge"
# description = "Local HTTP bridge on 127.0.0.1 that lets Claude Code read the open project and edit the active presets (never saved to disk)."
# author = "WPlus"
# version = "0.1.2"
# ///
"""Claude Bridge: a local HTTP/JSON server inside OrcaSlicer 2.5.0.

Run "Claude Bridge" once from the Plugins dialog. The Script capability's
``async def execute`` never returns: it serves 127.0.0.1 on the background
Script worker's asyncio loop until the plugin is disabled, unloaded or Orca
quits (all of which cancel the task at its next ``await``).

Endpoints (all JSON, all need ``Authorization: Bearer <token>``):

  GET  /health
  GET  /app_info
  GET  /project_path
  GET  /presets                         active process/printer/filament presets
  GET  /preset/<category>?key=K[&slot=N] edited preset value (filament by 1-based slot)
  GET  /preset/<category>/keys[?slot=N]
  GET  /config?key=K                    full (effective) config value
  GET  /config/keys
  GET  /model                           objects on the plate
  POST /preset/<category>               {"values": {...}, "preset_name": "", "slot": 1}

The token and port are written to ``bridge.json`` in the plugin's storage
folder, where the MCP server reads them.

GUI state is read on the UI thread through a small dock panel: the worker
queues the read, posts "pump" to the panel, the page posts back, and
``on_message`` (which Orca calls on the UI thread) runs the queued reads.
``app_info()`` and ``project_path()`` refuse to run anywhere else. With the
panel closed, reads that do not require the UI thread run on the worker.

Writes go through ``orca.host.preset.<category>.begin_update()`` -> ``set()``
-> ``commit()``, the same path as editing the field in the GUI. That API never
saves; the preset stays dirty until the user saves or discards it in Orca.
"""

import asyncio
import collections
import json
import os
import time
import urllib.parse

import orca

HOST = "127.0.0.1"
# The first free port in this range is used; the CLI reads the actual one from bridge.json.
FIRST_PORT = int(os.environ.get("ORCA_CLAUDE_BRIDGE_PORT", "47651"))
PORT_ATTEMPTS = 10
UI_TIMEOUT_S = 5.0
CATEGORIES = ("process", "printer", "filament")

PANEL_HTML = """
<style>
  body { margin: 0; padding: 10px; font-size: 12px; }
  h3 { margin: 0 0 6px; font-size: 13px; }
  #status, #log { color: var(--orca-muted); }
  #log { margin-top: 8px; white-space: pre-wrap; font-family: ui-monospace, monospace; font-size: 11px; }
</style>
<h3>Claude Bridge</h3>
<div id="status">Starting...</div>
<div id="log"></div>
<script>
(function () {
  var log = document.getElementById("log");
  orca.onMessage(function (message) {
    if (!message) return;
    if (message.command === "pump") {
      orca.postMessage({ command: "pump" });
    } else if (message.command === "status") {
      document.getElementById("status").textContent = message.text;
    } else if (message.command === "log") {
      log.textContent = (message.text + "\\n" + log.textContent).split("\\n").slice(0, 12).join("\\n");
    }
  });
  // Fallback in case a "pump" post is dropped (e.g. sent before the page loaded).
  setInterval(function () { orca.postMessage({ command: "pump" }); }, 1000);
  orca.postMessage({ command: "ready" });
})();
</script>
"""


class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class UiPump:
    """Runs callables on Orca's UI thread via the dock panel's on_message."""

    def __init__(self):
        self.panel = None
        self.loop = None
        self.jobs = collections.deque()
        self.status_text = "Starting..."

    def is_open(self):
        try:
            return self.panel is not None and self.panel.is_open()
        except Exception:
            return False

    # UI thread.
    def on_message(self, message):
        command = (message or {}).get("command")
        if command == "ready" and self.panel is not None:
            # A post made before the page loaded can be dropped, so resend the status on load.
            self.panel.post({"command": "status", "text": self.status_text})
        if command in ("pump", "ready"):
            self.drain()

    def on_close(self):
        self.panel = None
        self.drain(error=RuntimeError("Claude Bridge panel was closed"))

    def drain(self, error=None):
        while self.jobs:
            fn, future = self.jobs.popleft()
            if error is None:
                try:
                    outcome = (True, fn())
                except Exception as exc:  # delivered to the waiting request
                    outcome = (False, exc)
            else:
                outcome = (False, error)
            self.loop.call_soon_threadsafe(_settle, future, outcome)

    # Script worker.
    async def run(self, fn):
        if not self.is_open():
            return fn()
        future = self.loop.create_future()
        self.jobs.append((fn, future))
        self.panel.post({"command": "pump"})
        return await asyncio.wait_for(future, UI_TIMEOUT_S)

    def post(self, payload):
        if self.is_open():
            try:
                self.panel.post(payload)
            except Exception:
                pass


def _settle(future, outcome):
    if future.done():
        return
    ok, value = outcome
    if ok:
        future.set_result(value)
    else:
        future.set_exception(value)


# ---------------------------------------------------------------------------
# Reads. Each returns plain JSON-able data and must run on the UI thread when
# the panel is open (UiPump.run decides).
# ---------------------------------------------------------------------------

def _preset_summary(preset):
    if preset is None:
        return None
    return {
        "name": preset.name,
        "alias": preset.alias,
        "label": preset.label(),
        "is_dirty": preset.is_dirty,
        "is_system": preset.is_system,
        "is_user": preset.is_user(),
        "is_default": preset.is_default,
        "is_compatible": preset.is_compatible,
        "is_project_embedded": preset.is_project_embedded,
    }


def read_presets():
    bundle = orca.host.preset_bundle()
    plater = orca.host.plater()
    return {
        "process": {
            "selected": bundle.prints.selected_preset_name(),
            "edited": _preset_summary(bundle.current_process_preset()),
        },
        "printer": {
            "selected": bundle.printers.selected_preset_name(),
            "edited": _preset_summary(bundle.current_printer_preset()),
        },
        "filament": {
            "edited": _preset_summary(bundle.filaments.edited_preset()),
            "slots": [
                {"slot": index + 1, "name": name, "preset": _preset_summary(preset)}
                for index, (name, preset) in enumerate(
                    zip(bundle.current_filament_preset_names(), bundle.current_filament_presets())
                )
            ],
        },
        "is_presets_dirty": plater.is_presets_dirty(),
        "is_project_dirty": plater.is_project_dirty(),
    }


def _category_preset(category, slot):
    bundle = orca.host.preset_bundle()
    if category == "process":
        return bundle.current_process_preset()
    if category == "printer":
        return bundle.current_printer_preset()
    if slot is None:
        return bundle.filaments.edited_preset()
    presets = bundle.current_filament_presets()
    if not 1 <= slot <= len(presets) or presets[slot - 1] is None:
        raise HttpError(404, f"no filament preset in slot {slot}")
    return presets[slot - 1]


def read_preset_value(category, key, slot):
    preset = _category_preset(category, slot)
    value = preset.config_value(key)
    if value is None:
        raise HttpError(404, f"{category} preset {preset.name!r} has no setting {key!r}")
    return {"category": category, "preset": preset.name, "is_dirty": preset.is_dirty, "key": key, "value": value}


def read_preset_keys(category, slot):
    preset = _category_preset(category, slot)
    return {"category": category, "preset": preset.name, "keys": sorted(preset.config_keys())}


def read_config_value(key):
    value = orca.host.preset_bundle().full_config_value(key)
    if value is None:
        raise HttpError(404, f"no setting {key!r} in the full config")
    return {"key": key, "value": value}


def read_config_keys():
    return {"keys": sorted(orca.host.preset_bundle().full_config_keys())}


def _bbox(box):
    if not box.defined:
        return None
    return {"min": box.min, "max": box.max, "size": box.size}


def read_model():
    model = orca.host.model()
    objects = []
    for index, obj in enumerate(model.objects()):
        objects.append({
            "index": index,
            "id": obj.id(),
            "name": obj.name,
            "input_file": obj.input_file,
            "instances": obj.instance_count(),
            "volumes": [
                {"name": v.name, "type": str(v.type()).split(".")[-1], "extruder_id": v.extruder_id(),
                 "facets": v.facets_count(), "config": {k: v.config_value(k) for k in v.config_keys()}}
                for v in obj.volumes()
            ],
            "bounding_box": _bbox(obj.bounding_box()),
            "max_z": obj.max_z(),
            "config": {k: obj.config_value(k) for k in obj.config_keys()},
            "is_mm_painted": obj.is_mm_painted(),
            "is_fdm_support_painted": obj.is_fdm_support_painted(),
        })
    return {"object_count": model.object_count(), "current_plate_index": model.current_plate_index(), "objects": objects}


def read_app_info():
    return dict(orca.host.app_info())


def read_project_path():
    path = orca.host.plater().project_path()
    return {"project_path": path, "saved": bool(path)}


# ---------------------------------------------------------------------------
# Write. Runs on the Script worker: begin_update() must not run on the UI
# thread. It blocks this worker (and so the HTTP loop) while Orca shows any
# unsaved-changes dialog, which only happens when preset_name switches preset.
# ---------------------------------------------------------------------------

def write_preset(category, payload):
    values = payload.get("values")
    if not isinstance(values, dict) or not values:
        raise HttpError(400, 'body needs "values": {"key": "value", ...}')
    preset_name = payload.get("preset_name") or ""
    if not isinstance(preset_name, str):
        raise HttpError(400, "preset_name must be a string")

    editor = getattr(orca.host.preset, category)
    if category == "filament":
        slot = payload.get("slot")
        if not isinstance(slot, int):
            raise HttpError(400, 'filament writes need "slot": <1-based int>')
        transaction = editor.begin_update(slot, preset_name)
    else:
        transaction = editor.begin_update(preset_name)

    for key, value in values.items():
        transaction.set(str(key), _serialize(value))
    result = transaction.commit()
    return {
        "category": result.category,
        "target_name": result.target_name,
        "selection_changed": result.selection_changed,
        "slot_number": result.slot_number,
        "shared_slots": list(result.shared_slots),
        "keys": list(result.keys),
        "dirty": result.dirty,
        "saved_to_disk": False,
    }


def _serialize(value):
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (list, tuple)):
        return ";".join(_serialize(v) for v in value)
    return str(value)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Bridge:
    def __init__(self, token):
        self.token = token
        self.port = None
        self.pump = UiPump()

    def allowed_hosts(self):
        return {f"127.0.0.1:{self.port}", f"localhost:{self.port}", "127.0.0.1", "localhost"}

    async def handle(self, reader, writer):
        status, body = 500, {"error": "internal error"}
        try:
            method, path, query, headers, raw = await asyncio.wait_for(_read_request(reader), 10)
            self._authorize(headers)
            data = json.loads(raw) if raw else {}
            status, body = 200, await self.route(method, path, query, data)
        except HttpError as exc:
            status, body = exc.status, {"error": exc.message}
        except ValueError as exc:  # invalid key/value from commit(), bad JSON
            status, body = 400, {"error": f"{type(exc).__name__}: {exc}"}
        except asyncio.TimeoutError:
            status, body = 504, {"error": "timed out waiting for the request or the UI thread"}
        except Exception as exc:
            status, body = 500, {"error": f"{type(exc).__name__}: {exc}"}
        try:
            payload = json.dumps(body, default=str).encode()
            writer.write(
                f"HTTP/1.1 {status} {'OK' if status == 200 else 'Error'}\r\n"
                f"Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n"
                "Connection: close\r\n\r\n".encode() + payload
            )
            await writer.drain()
        finally:
            writer.close()

    def _authorize(self, headers):
        # A Host check stops DNS-rebinding pages; the bearer token stops any other local page or process.
        if headers.get("host") not in self.allowed_hosts():
            raise HttpError(403, "bad Host header")
        if headers.get("authorization") != f"Bearer {self.token}":
            raise HttpError(401, "missing or wrong bearer token")

    async def route(self, method, path, query, data):
        parts = [p for p in path.split("/") if p]
        run = self.pump.run
        slot = int(query["slot"]) if "slot" in query else None

        if method == "GET":
            if parts == ["health"]:
                return {"ok": True, "panel_open": self.pump.is_open(), "port": self.port}
            if parts == ["app_info"]:
                return await run(read_app_info)
            if parts == ["project_path"]:
                return await run(read_project_path)
            if parts == ["presets"]:
                return await run(read_presets)
            if parts == ["model"]:
                return await run(read_model)
            if parts == ["config"]:
                return await run(lambda: read_config_value(_required(query, "key")))
            if parts == ["config", "keys"]:
                return await run(read_config_keys)
            if len(parts) >= 2 and parts[0] == "preset" and parts[1] in CATEGORIES:
                category = parts[1]
                if parts[2:] == ["keys"]:
                    return await run(lambda: read_preset_keys(category, slot))
                if len(parts) == 2:
                    return await run(lambda: read_preset_value(category, _required(query, "key"), slot))
        elif method == "POST" and len(parts) == 2 and parts[0] == "preset" and parts[1] in CATEGORIES:
            result = write_preset(parts[1], data)
            self.pump.post({"command": "log", "text": f"{time.strftime('%H:%M:%S')} set {result['category']}: {', '.join(result['keys'])}"})
            return result
        raise HttpError(404, f"no route {method} {path}")


def _required(query, name):
    if not query.get(name):
        raise HttpError(400, f"missing ?{name}=")
    return query[name]


async def _read_request(reader):
    head = await reader.readuntil(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    method, target, _ = lines[0].split(" ", 2)
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()
    length = int(headers.get("content-length") or 0)
    if length > 1_000_000:
        raise HttpError(413, "body too large")
    raw = await reader.readexactly(length) if length else b""
    url = urllib.parse.urlsplit(target)
    query = dict(urllib.parse.parse_qsl(url.query))
    return method.upper(), url.path, query, headers, raw


async def _start_server(bridge):
    last_error = None
    for port in range(FIRST_PORT, FIRST_PORT + PORT_ATTEMPTS):
        try:
            server = await asyncio.start_server(bridge.handle, HOST, port)
        except OSError as error:  # address in use
            last_error = error
            continue
        bridge.port = port
        return server
    raise last_error


class ClaudeBridge(orca.script.ScriptPluginCapabilityBase):
    running = False

    def get_name(self):
        return "Claude Bridge"

    async def execute(self):
        if ClaudeBridge.running:
            return orca.ExecutionResult.success("Claude Bridge is already running")

        token = os.urandom(24).hex()
        bridge = Bridge(token)
        bridge.pump.loop = asyncio.get_running_loop()
        info_path = os.path.join(orca.host.plugin.storage(), "bridge.json")

        server = await _start_server(bridge)
        PORT = bridge.port
        ClaudeBridge.running = True
        try:
            with open(info_path, "w", encoding="utf-8") as handle:
                json.dump({"host": HOST, "port": PORT, "token": token, "pid": os.getpid()}, handle)
            bridge.pump.status_text = f"Serving http://{HOST}:{PORT}"
            bridge.pump.panel = orca.host.ui.create_dock_panel(
                html=PANEL_HTML, title="Claude Bridge", width=260, height=200,
                on_message=bridge.pump.on_message, on_close=bridge.pump.on_close, dock="right",
            )
            bridge.pump.post({"command": "status", "text": f"Serving http://{HOST}:{PORT}"})
            print(f"[claude-bridge] serving http://{HOST}:{PORT}, token in {info_path}", flush=True)
            async with server:
                await server.serve_forever()
        finally:
            ClaudeBridge.running = False
            try:
                os.replace(info_path, info_path + ".stopped")
            except OSError:
                pass
            if bridge.pump.is_open():
                bridge.pump.panel.close()
            print("[claude-bridge] stopped", flush=True)
        return orca.ExecutionResult.success("Claude Bridge stopped")


@orca.plugin
class ClaudeBridgePlugin(orca.base):
    def register_capabilities(self):
        # A canonical scheme://host grant covers raw sockets to that host on every port.
        orca.request_permissions(network=["http://127.0.0.1"])
        orca.register_capability(ClaudeBridge)
