#!/usr/bin/env python3
"""Local-only HTTP dashboard and USB serial bridge. No simulated device data."""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import mimetypes
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit
import webbrowser

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    serial = None
    list_ports = None

ROOT = Path(__file__).resolve().parents[1]
COMMANDS = {"START", "STOP", "LOG", "STATUS", "HELP"}
STALE_SECONDS = 3.0
ACK_TIMEOUT = 5.0
MAX_JSON_BYTES = 4096
UINT32_MAX = 4294967295


def empty_device():
    return dict(firmware=None, mode=None, state="UNKNOWN", distance_cm=None,
                sensor=None, count=None, seconds=None, near_cm=None,
                far_cm=None, phase=None, session_id=None)


def discover_ports():
    return list(list_ports.comports()) if list_ports else []


def identity(port):
    number = getattr(port, "serial_number", None)
    vid = getattr(port, "vid", None)
    if number:
        return "usb:%s:%s" % (format(vid, "04x") if vid is not None else "unknown", number)
    hwid = getattr(port, "hwid", None)
    return "port:" + (hwid if hwid and hwid != "n/a" else port.device)


def port_label(port):
    description = getattr(port, "description", None) or "Serial device"
    return "%s — %s" % (description, port.device)


def port_is_busy(path):
    """Catch an IDE monitor that does not cooperate with POSIX flock locks."""
    executable = shutil.which("lsof") or ("/usr/sbin/lsof" if Path("/usr/sbin/lsof").exists() else None)
    if not executable:
        return False
    try:
        result = subprocess.run([executable, "-t", "--", path], capture_output=True,
                                text=True, timeout=2, check=False)
        return any(value.isdigit() and int(value) != os.getpid()
                   for value in result.stdout.split())
    except (OSError, subprocess.TimeoutExpired):
        return False


def uint32(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,10}", value):
        return None
    number = int(value)
    return number if number <= UINT32_MAX else None


def finite_number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


class Bridge:
    def __init__(self, history_path=None, port_provider=None, serial_factory=None,
                 busy_checker=None, clock=None, auto_connect=True):
        self.lock = threading.RLock()
        self.clock = clock or time.time
        self.port_provider = port_provider or discover_ports
        self.serial_factory = serial_factory or (serial.Serial if serial else None)
        self.busy_checker = busy_checker or port_is_busy
        self.history_path = Path(history_path or ROOT / "runtime" / "sessions.json")
        self.device = empty_device()
        self.connection = dict(connected=False, port=None, device=None, error=None,
                               last_received_at=None)
        self.device_id = None
        self.history = {}
        self.samples = deque(maxlen=1200)
        self.events = deque(maxlen=200)
        self.pending = None
        self.auto_queue = deque()
        self.initial_log_requested = False
        self.log_rows = None
        self.storage_error = None
        self.auto_connect = auto_connect
        self.target = None
        self.handle = None
        self.rx = bytearray()
        self.stop_event = threading.Event()
        self.worker = None
        self.last_attempt = float("-inf")
        self.generation = 0
        self._load_history()

    def _event(self, text, kind="info"):
        self.events.append(dict(at=self.clock(), text=str(text)[:1000], kind=kind))

    def _set_error(self, message):
        if message != self.connection["error"]:
            self.connection["error"] = message
            if message:
                self._event(message, "error")

    def available_ports(self):
        try:
            return sorted(self.port_provider(), key=lambda port: port.device)
        except Exception as error:
            with self.lock:
                self._set_error("Cannot list serial ports: %s" % error)
            return []

    def ports(self):
        return [dict(path=port.device, label=port_label(port)) for port in self.available_ports()]

    def _load_history(self):
        if not self.history_path.exists():
            return
        try:
            content = json.loads(self.history_path.read_text(encoding="utf-8"))
            if content.get("version") != 1 or not isinstance(content.get("sessions"), list):
                raise ValueError("Unrecognized history format")
            for row in content["sessions"]:
                if not isinstance(row, dict) or not isinstance(row.get("device_id"), str):
                    continue
                if not all(type(row.get(key)) is int and 0 <= row[key] <= UINT32_MAX
                           for key in ("id", "count", "seconds")) or row["id"] == 0:
                    continue
                saved_at = finite_number(row.get("saved_at"))
                mode = row.get("mode") if isinstance(row.get("mode"), str) else None
                record = dict(device_id=row["device_id"], id=row["id"], count=row["count"],
                              seconds=row["seconds"], saved_at=saved_at, mode=mode, source="local")
                self.history[(record["device_id"], record["id"])] = record
        except (OSError, ValueError, TypeError, AttributeError) as error:
            self.storage_error = "Local history could not be read; original file is preserved: %s" % error
            self._event(self.storage_error, "error")

    def _persist(self):
        # Never silently replace an unreadable history file with partial data.
        if self.storage_error and "could not be read" in self.storage_error:
            return
        temporary = None
        try:
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                             dir=str(self.history_path.parent),
                                             prefix=".sessions-", suffix=".tmp", delete=False) as stream:
                temporary = stream.name
                json.dump(dict(version=1, sessions=list(self.history.values())), stream,
                          ensure_ascii=False, allow_nan=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.history_path)
            self.storage_error = None
        except (OSError, ValueError) as error:
            self.storage_error = "Board data received, but local history could not be saved: %s" % error
            self._event(self.storage_error, "error")
        finally:
            if temporary and os.path.exists(temporary):
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def _merge_record(self, record_id, count, seconds, acknowledged=False):
        if not self.device_id or not record_id:
            return
        key = (self.device_id, record_id)
        old = self.history.get(key, {})
        # LOG contains no mode or date: preserve known metadata, never infer old
        # records from the currently installed firmware (old id=1 may be CYCLE).
        row = dict(device_id=self.device_id, id=record_id, count=count, seconds=seconds,
                   saved_at=old.get("saved_at"), source="board", mode=old.get("mode"))
        if acknowledged:
            row["saved_at"] = old.get("saved_at") or self.clock()
            row["mode"] = self.device.get("mode")
        self.history[key] = row

    def snapshot(self):
        ports = self.ports()
        with self.lock:
            connection = dict(self.connection)
            last = connection["last_received_at"]
            connection["stale"] = (not connection["connected"] or last is None or
                                   self.clock() - last > STALE_SECONDS)
            history = sorted(self.history.values(), key=lambda row: (row["device_id"], row["id"]), reverse=True)
            return dict(connection=connection, device=dict(self.device),
                        history=[dict(row) for row in history], samples=list(self.samples),
                        events=list(self.events), ports=ports,
                        pending_command=self.pending["command"] if self.pending else None,
                        storage_error=self.storage_error)

    def export_csv(self):
        output = io.StringIO(newline="")
        fields = ["device_id", "id", "count", "seconds", "saved_at", "source", "mode"]
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for row in self.snapshot()["history"]:
            # Device identifiers are external strings. Avoid spreadsheet formulas.
            escaped = {key: ("'" + value if isinstance(value, str) and value[:1] in "=+-@" and value else value)
                       for key, value in row.items() if key in fields}
            writer.writerow(escaped)
        return output.getvalue()

    def connect(self, path):
        match = next((port for port in self.available_ports() if port.device == path), None)
        if not match:
            return False, "Select a serial port from the current device list."
        self._drop_connection(None)
        with self.lock:
            self.auto_connect = False
            self.target = (path, identity(match))
            self.last_attempt = float("-inf")
            self.generation += 1
            self._set_error(None)
            self._event("Connection requested: %s" % path)
        return True, "Connection requested; waiting for the board."

    def disconnect(self):
        with self.lock:
            self.auto_connect = False
            self.target = None
            self.generation += 1
        self._drop_connection(None)
        with self.lock:
            self._set_error(None)
            self._event("Disconnected from the board.")
        return True, "Disconnected."

    def command(self, command):
        if not isinstance(command, str) or command.strip().upper() not in COMMANDS:
            return False, "Allowed commands: START, STOP, LOG, STATUS, HELP."
        command = command.strip().upper()
        with self.lock:
            if not self.handle or not self.connection["connected"]:
                return False, "Connect the Arduino first."
            if self.pending:
                return False, "Waiting for the board to acknowledge %s." % self.pending["command"]
            last = self.connection["last_received_at"]
            if command not in {"HELP", "STATUS"} and (last is None or self.clock() - last > STALE_SECONDS):
                return False, "Board data is stale. Check the USB connection or request STATUS."
            state = self.device["state"]
            if command in {"START", "LOG"} and state != "IDLE":
                return False, "The board must report IDLE before %s." % command
            if command == "STOP" and state != "RUNNING":
                return False, "The board has not reported a running session."
            if command == "STOP":
                # An IDLE telemetry line cannot stand in for a SAVED acknowledgement.
                self.initial_log_requested = True
            self.pending = dict(command=command, sent=False, at=None)
        return True, "%s queued; waiting for board confirmation." % command

    def _queue_auto(self, command):
        if command not in self.auto_queue and not (self.pending and self.pending["command"] == command):
            self.auto_queue.append(command)

    def _ack(self, command):
        if self.pending and self.pending["command"] == command:
            self.pending = None

    def feed_line(self, line):
        """Process only a real serial line; tests supply recorded protocol lines."""
        line = line.strip()
        if not line or len(line) > 2048:
            return
        fields = dict(re.findall(r"([A-Za-z_]+)=([^\s]+)", line))
        with self.lock:
            self.connection["last_received_at"] = self.clock()
            self._set_error(None)
            if line.startswith("READY"):
                self.device["firmware"] = fields.get("FW", "").rstrip(":") or None
                self.device["mode"] = fields.get("MODE", "").rstrip(":") or None
                self._ack("HELP")
                self._event(line)
            elif line.startswith("STATE="):
                state = fields.get("STATE")
                if state not in {"IDLE", "RUNNING"}:
                    return
                self.device["state"] = state
                self.device["sensor"] = fields.get("SENSOR")
                self.device["phase"] = fields.get("PHASE")
                self.device["distance_cm"] = finite_number(fields.get("DIST_CM"))
                for source, target in (("COUNT", "count"), ("SEC", "seconds")):
                    value = uint32(fields.get(source))
                    if value is not None:
                        self.device[target] = value
                for source, target in (("NEAR_CM", "near_cm"), ("FAR_CM", "far_cm")):
                    self.device[target] = finite_number(fields.get(source))
                self.samples.append(dict(at=self.clock(), distance_cm=self.device["distance_cm"],
                                         count=self.device["count"]))
                self._ack("STATUS")
                if state == "IDLE" and not self.initial_log_requested and not (
                        self.pending and self.pending["command"] == "STOP"):
                    self.initial_log_requested = True
                    self._queue_auto("LOG")
            elif line.startswith("STARTED "):
                record_id = uint32(fields.get("id"))
                if record_id:
                    self.device.update(state="RUNNING", session_id=record_id)
                    self._ack("START")
                    self._event(line, "success")
            elif line.startswith("SAVED "):
                values = [uint32(fields.get(key)) for key in ("id", "count", "seconds")]
                if all(value is not None for value in values) and values[0]:
                    self.device.update(state="IDLE", session_id=values[0], count=values[1], seconds=values[2])
                    self._merge_record(*values, acknowledged=True)
                    self._persist()
                    self._ack("STOP")
                    self._event(line, "success")
                    self.initial_log_requested = True
                    self._queue_auto("LOG")
            elif line.startswith("SAVE_FAILED "):
                self.device["state"] = "IDLE"
                self._ack("STOP")
                self._event(line + " — the board did not confirm a saved record.", "error")
            elif line.startswith("LOG_BEGIN"):
                self.log_rows = []
            elif line.startswith("LOG_END"):
                if self.log_rows is not None:
                    for values in self.log_rows:
                        self._merge_record(*values)
                    self._persist()
                    self._event("Read %d saved board records." % len(self.log_rows), "success")
                    self.log_rows = None
                    self._ack("LOG")
            elif self.log_rows is not None and re.fullmatch(r"\d+,\d+,\d+", line):
                values = [uint32(value) for value in line.split(",")]
                if len(self.log_rows) < 16 and all(value is not None for value in values) and values[0]:
                    self.log_rows.append(values)
            elif line.startswith("REP="):
                value = uint32(fields.get("REP"))
                if value is not None:
                    self.device["count"] = value
                    self._event(line, "count")
            elif line.startswith("EVENT="):
                self._event(line)
            elif line.startswith(("Already stopped", "Already running")):
                self.pending = None
                self._event(line)
            elif line.startswith(("Unknown command", "Command too long", "STOP before", "Session ID limit")):
                self.pending = None
                self._event(line, "error")

    def _drop_connection(self, reason, expected=None):
        with self.lock:
            if expected is not None and self.handle is not expected:
                return
            old, self.handle = self.handle, None
            self.connection["connected"] = False
            if self.pending:
                self._event("No confirmation for %s: connection ended." % self.pending["command"], "error")
            self.pending = None
            self.auto_queue.clear()
            self.rx.clear()
            self.log_rows = None
            self.initial_log_requested = False
            if reason:
                self._set_error(reason)
        if old:
            try:
                old.close()
            except Exception:
                pass

    def _attempt_connect(self):
        with self.lock:
            now = self.clock()
            if self.handle or now - self.last_attempt < 1.0:
                return
            self.last_attempt = now
            target, automatic, generation = self.target, self.auto_connect, self.generation
        ports = self.available_ports()
        if target:
            candidates = [port for port in ports if identity(port) == target[1]]
            if not candidates:
                with self.lock:
                    self._set_error("Selected board is unplugged; waiting for it to reconnect.")
                return
        elif automatic:
            candidates = [port for port in ports if getattr(port, "vid", None) == 0x2341]
            if len(candidates) != 1:
                if len(candidates) > 1:
                    with self.lock:
                        self._set_error("Multiple Arduino ports found. Select one explicitly.")
                return
        else:
            return
        port = next((item for item in candidates if target and item.device == target[0]), candidates[0])
        handle = None
        try:
            if self.busy_checker(port.device):
                raise OSError("Port is in use. Close Arduino IDE Serial Monitor/Plotter, then reconnect.")
            if not self.serial_factory:
                raise OSError("pyserial is not installed. Install requirements.txt first.")
            kwargs = dict(port=port.device, baudrate=115200, timeout=0.1, write_timeout=0.5)
            if os.name == "posix":
                kwargs["exclusive"] = True
            handle = self.serial_factory(**kwargs)
            if os.name == "posix" and hasattr(handle, "fileno"):
                import fcntl
                import termios
                if hasattr(termios, "TIOCEXCL"):
                    fcntl.ioctl(handle.fileno(), termios.TIOCEXCL)
            with self.lock:
                if generation != self.generation or self.stop_event.is_set():
                    handle.close()
                    return
                self.handle = handle
                self.device_id = identity(port)
                self.device = empty_device()
                self.samples.clear()
                self.connection.update(connected=True, port=port.device, device=port_label(port),
                                       error=None, last_received_at=None)
                self._event("Connected: %s. Waiting for live data." % port.device)
                self._queue_auto("HELP")
                self._queue_auto("STATUS")
        except Exception as error:
            if handle:
                try:
                    handle.close()
                except Exception:
                    pass
            with self.lock:
                self._set_error("Cannot open %s: %s" % (port.device, error))

    def _pump_command(self):
        with self.lock:
            if not self.handle:
                return
            now = self.clock()
            if self.pending and self.pending["sent"] and now - self.pending["at"] > ACK_TIMEOUT:
                self._event("No board acknowledgement for %s. State is unconfirmed." % self.pending["command"], "error")
                if self.pending["command"] == "LOG":
                    self.log_rows = None
                self.pending = None
            if self.pending is None and self.auto_queue:
                command = self.auto_queue.popleft()
                if command == "LOG" and self.device["state"] != "IDLE":
                    return
                self.pending = dict(command=command, sent=False, at=None)
            if not self.pending or self.pending["sent"]:
                return
            handle = self.handle
            data = (self.pending["command"] + "\n").encode("ascii")
            try:
                written = handle.write(data)
                if written != len(data):
                    raise OSError("Incomplete serial write")
                self.pending.update(sent=True, at=now)
            except Exception as error:
                self._drop_connection("Serial write failed: %s" % error, expected=handle)

    def _tick(self):
        with self.lock:
            handle = self.handle
        if handle is None:
            self._attempt_connect()
            return
        self._pump_command()
        try:
            data = handle.read(256)
            with self.lock:
                if self.handle is not handle:
                    return
                self.rx.extend(data)
                while b"\n" in self.rx:
                    line, _, rest = self.rx.partition(b"\n")
                    self.rx = bytearray(rest)
                    self.feed_line(line.decode("utf-8", errors="replace"))
                if len(self.rx) > 4096:
                    self.rx.clear()
                    self._event("Discarded an overlong serial line.", "error")
        except Exception as error:
            self._drop_connection("USB connection lost: %s. Waiting to reconnect." % error, expected=handle)

    def start(self):
        if self.worker:
            return
        def run():
            while not self.stop_event.is_set():
                self._tick()
                self.stop_event.wait(0.03)
        self.worker = threading.Thread(target=run, name="arduino-serial", daemon=True)
        self.worker.start()

    def close(self):
        self.stop_event.set()
        self._drop_connection(None)
        if self.worker and self.worker is not threading.current_thread():
            self.worker.join(timeout=2)


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, bridge, frontend=None):
        if address[0] != "127.0.0.1":
            raise ValueError("Dashboard may bind only to 127.0.0.1")
        self.bridge = bridge
        self.frontend = Path(frontend or ROOT / "frontend").resolve()
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "ECE198Local/1.0"

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, format, *args):
        pass

    def _send(self, status, body, content_type="application/json; charset=utf-8", extra=None):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _host(self):
        values = self.headers.get_all("Host", [])
        if len(values) != 1:
            return None
        try:
            parsed = urlsplit("http://" + values[0])
            if parsed.hostname not in {"localhost", "127.0.0.1"} or parsed.username or parsed.password:
                return None
            if parsed.path or parsed.query or parsed.fragment:
                return None
            if (parsed.port or 80) != self.server.server_address[1]:
                return None
            return "http://" + values[0].lower()
        except ValueError:
            return None

    def do_GET(self):
        if not self._host():
            self._send(403, dict(ok=False, message="Only a local Host header is allowed."))
            return
        path = urlsplit(self.path).path
        if path == "/api/health":
            self._send(200, dict(ok=True, application="ece198-motion-counter", version="1.0.0"))
        elif path == "/api/state":
            self._send(200, self.server.bridge.snapshot())
        elif path == "/api/ports":
            self._send(200, dict(ports=self.server.bridge.ports()))
        elif path == "/api/export.csv":
            self._send(200, self.server.bridge.export_csv().encode("utf-8-sig"), "text/csv; charset=utf-8",
                       {"Content-Disposition": 'attachment; filename="exercise-sessions.csv"'})
        elif path.startswith("/api/"):
            self._send(404, dict(ok=False, message="Unknown API route."))
        else:
            try:
                relative = unquote(path).lstrip("/") or "index.html"
                file = (self.server.frontend / relative).resolve()
                file.relative_to(self.server.frontend)
                if not file.is_file():
                    raise FileNotFoundError
                content_type = mimetypes.guess_type(str(file))[0] or "application/octet-stream"
                self._send(200, file.read_bytes(), content_type)
            except (OSError, ValueError):
                self._send(404, dict(ok=False, message="File not found."))

    def do_POST(self):
        origin = self._host()
        if not origin or self.headers.get("Origin", "").lower() != origin:
            self._send(403, dict(ok=False, message="Same-origin local requests only."))
            return
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            self._send(415, dict(ok=False, message="Send application/json."))
            return
        try:
            if self.headers.get("Transfer-Encoding"):
                raise ValueError
            length = int(self.headers.get("Content-Length", "-1"))
            if length < 1 or length > MAX_JSON_BYTES:
                self._send(413, dict(ok=False, message="JSON body must be between 1 and 4096 bytes."))
                return
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError
        except (ValueError, UnicodeError, OSError):
            self._send(400, dict(ok=False, message="Invalid JSON object."))
            return
        path = urlsplit(self.path).path
        if path == "/api/connect":
            ok, message = self.server.bridge.connect(data.get("port"))
        elif path == "/api/disconnect":
            ok, message = self.server.bridge.disconnect()
        elif path == "/api/command":
            ok, message = self.server.bridge.command(data.get("command"))
        else:
            self._send(404, dict(ok=False, message="Unknown API route."))
            return
        self._send(200 if ok else 409, dict(ok=ok, message=message))


def main():
    parser = argparse.ArgumentParser(description="Local Arduino dashboard")
    parser.add_argument("--port", type=int, default=8765, help="Local HTTP port (default: 8765)")
    parser.add_argument("--serial", help="Explicit serial path from the currently enumerated ports")
    parser.add_argument("--no-auto-connect", action="store_true")
    parser.add_argument("--open-browser", action="store_true")
    args = parser.parse_args()
    if serial is None:
        parser.error("pyserial 3.5 is required. Install requirements.txt first.")
    bridge = Bridge(auto_connect=not args.no_auto_connect)
    if args.serial:
        ok, message = bridge.connect(args.serial)
        if not ok:
            parser.error(message)
    try:
        server = DashboardServer(("127.0.0.1", args.port), bridge)
    except OSError as error:
        parser.error("Cannot start local dashboard: %s" % error)
    bridge.start()
    url = "http://127.0.0.1:%d" % server.server_address[1]
    print("Local dashboard: " + url, flush=True)
    if args.open_browser:
        timer = threading.Timer(0.2, webbrowser.open, args=(url,))
        timer.daemon = True
        timer.start()
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        bridge.close()


if __name__ == "__main__":
    main()
