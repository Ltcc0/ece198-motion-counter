"""Protocol/HTTP tests with fake ports only; never open a physical serial device."""
from __future__ import annotations

import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.server import Bridge, DashboardServer, uint32


class Clock:
    def __init__(self):
        self.now = 1000.0
    def __call__(self):
        return self.now
    def advance(self, seconds):
        self.now += seconds


class FakeSerial:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.writes = []
        self.closed = False
        self.incoming = b""
        self.error = None
    def write(self, data):
        if self.error:
            raise self.error
        self.writes.append(data)
        return len(data)
    def read(self, size):
        if self.error:
            raise self.error
        result, self.incoming = self.incoming[:size], self.incoming[size:]
        return result
    def close(self):
        self.closed = True


def port(path="/dev/cu.test", serial_number="TEST123", vid=0x2341):
    return SimpleNamespace(device=path, description="Arduino UNO R4 Minima", vid=vid,
                           pid=0x0069, serial_number=serial_number, hwid="TEST-HWID")


IDLE = "STATE=IDLE DIST_CM=40.0 SENSOR=OK COUNT=0 SEC=0 NEAR_CM=25.0 FAR_CM=35.0 PHASE=-"
RUNNING = "STATE=RUNNING DIST_CM=17.2 SENSOR=OK COUNT=3 SEC=12 NEAR_CM=25.0 FAR_CM=35.0 PHASE=OCCUPIED"
BANNER = "READY FW=3 MODE=PASSAGE: UNO R4 Minima + HC-SR04"


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "runtime" / "sessions.json"
        self.clock = Clock()
        self.available = [port()]
        self.opened = []
        def factory(**kwargs):
            handle = FakeSerial(**kwargs)
            self.opened.append(handle)
            return handle
        self.bridge = Bridge(self.path, lambda: self.available, factory,
                             lambda path: False, self.clock, auto_connect=True)

    def tearDown(self):
        self.bridge.close()
        self.directory.cleanup()

    def connect(self, idle=True):
        self.bridge._attempt_connect()
        self.bridge._pump_command()
        self.assertEqual(self.opened[-1].writes[-1], b"HELP\n")
        self.bridge.feed_line(BANNER)
        self.bridge._pump_command()
        self.assertEqual(self.opened[-1].writes[-1], b"STATUS\n")
        self.bridge.feed_line(IDLE if idle else RUNNING)
        if idle:
            self.bridge._pump_command()
            self.assertEqual(self.opened[-1].writes[-1], b"LOG\n")
            self.bridge.feed_line("LOG_BEGIN id,count,seconds")
            self.bridge.feed_line("LOG_END (up to 16 saved sessions)")

    def test_initial_state_has_no_fake_readings(self):
        state = self.bridge.snapshot()
        self.assertFalse(state["connection"]["connected"])
        self.assertTrue(state["connection"]["stale"])
        self.assertIsNone(state["device"]["count"])
        self.assertIsNone(state["device"]["distance_cm"])
        self.assertEqual(state["samples"], [])
        self.assertFalse(self.opened)

    def test_unique_arduino_connect_handshake_and_stale(self):
        self.connect()
        state = self.bridge.snapshot()
        self.assertEqual(state["device"]["firmware"], "3")
        self.assertEqual(state["device"]["mode"], "PASSAGE")
        self.assertFalse(state["connection"]["stale"])
        self.assertEqual(self.opened[-1].kwargs["baudrate"], 115200)
        self.assertTrue(self.opened[-1].kwargs["exclusive"])
        self.clock.advance(3.01)
        self.assertTrue(self.bridge.snapshot()["connection"]["stale"])
        self.assertFalse(self.bridge.command("START")[0])
        self.assertTrue(self.bridge.command("STATUS")[0])
        self.bridge.feed_line(IDLE)
        self.assertFalse(self.bridge.snapshot()["connection"]["stale"])

    def test_auto_selection_is_unique_and_manual_path_is_enumerated(self):
        self.available.append(port("/dev/cu.second", "TEST456"))
        self.bridge._attempt_connect()
        self.assertFalse(self.opened)
        self.assertIn("Multiple", self.bridge.snapshot()["connection"]["error"])
        self.assertFalse(self.bridge.connect("/etc/passwd")[0])
        self.assertTrue(self.bridge.connect("/dev/cu.second")[0])
        self.bridge._attempt_connect()
        self.assertEqual(self.opened[-1].kwargs["port"], "/dev/cu.second")

    def test_busy_port_is_not_opened(self):
        self.bridge.busy_checker = lambda path: True
        self.bridge._attempt_connect()
        self.assertFalse(self.opened)
        self.assertIn("Serial Monitor", self.bridge.snapshot()["connection"]["error"])

    def test_telemetry_events_invalid_distances_and_corrupt_numbers(self):
        self.connect()
        self.bridge.feed_line(RUNNING)
        self.bridge.feed_line("REP=4 EVENT=PASS_COUNTED next=CLEAR")
        self.bridge.feed_line("EVENT=CLEARED next=ARMED")
        self.assertEqual(self.bridge.snapshot()["device"]["count"], 4)
        self.assertEqual(self.bridge.snapshot()["events"][-2]["kind"], "count")
        self.bridge.feed_line("STATE=RUNNING DIST_CM=NO_TARGET SENSOR=NO_ECHO COUNT=4 SEC=13 NEAR_CM=25 FAR_CM=35 PHASE=ARMED")
        state = self.bridge.snapshot()
        self.assertIsNone(state["device"]["distance_cm"])
        self.assertIsNone(state["samples"][-1]["distance_cm"])
        self.assertEqual(state["device"]["sensor"], "NO_ECHO")
        self.bridge.feed_line("REP=² EVENT=PASS_COUNTED")
        self.assertEqual(self.bridge.snapshot()["device"]["count"], 4)
        self.assertIsNone(uint32("4294967296"))
        self.assertIsNone(uint32("²"))

    def test_command_acknowledgement_and_successful_stop_only(self):
        self.connect()
        self.assertFalse(self.bridge.command("START\nSTOP")[0])
        self.assertFalse(self.bridge.command("CLEAR")[0])
        self.assertTrue(self.bridge.command("start")[0])
        self.assertEqual(self.bridge.snapshot()["pending_command"], "START")
        self.assertEqual(self.bridge.snapshot()["device"]["state"], "IDLE")
        self.bridge._pump_command()
        self.bridge.feed_line("STARTED id=2")
        self.assertEqual(self.bridge.snapshot()["device"]["session_id"], 2)
        self.assertIsNone(self.bridge.snapshot()["pending_command"])
        self.bridge.feed_line(RUNNING)
        self.assertFalse(self.bridge.command("LOG")[0])
        self.assertTrue(self.bridge.command("STOP")[0])
        self.bridge._pump_command()
        before = len(self.opened[-1].writes)
        self.bridge.feed_line(IDLE)
        self.bridge._pump_command()
        self.assertEqual(len(self.opened[-1].writes), before)
        self.assertEqual(self.bridge.snapshot()["history"], [])
        self.assertEqual(self.bridge.snapshot()["pending_command"], "STOP")
        self.bridge.feed_line("SAVED id=2 count=3 seconds=12")
        self.assertEqual(self.bridge.snapshot()["history"][0]["count"], 3)
        self.assertEqual(self.bridge.snapshot()["history"][0]["mode"], "PASSAGE")
        self.bridge._pump_command()
        self.assertEqual(self.opened[-1].writes[-1], b"LOG\n")

    def test_failed_save_and_ack_timeout_are_not_success(self):
        self.connect(idle=False)
        self.assertTrue(self.bridge.command("STOP")[0])
        self.bridge._pump_command()
        self.bridge.feed_line("SAVE_FAILED id=2 count=3 seconds=12")
        self.assertEqual(self.bridge.snapshot()["history"], [])
        self.assertIsNone(self.bridge.snapshot()["pending_command"])
        self.assertEqual(self.bridge.snapshot()["events"][-1]["kind"], "error")
        self.assertFalse(self.bridge.auto_queue)
        self.assertTrue(self.bridge.command("STATUS")[0])
        self.bridge._pump_command()
        self.clock.advance(5.01)
        self.bridge._pump_command()
        self.assertIsNone(self.bridge.snapshot()["pending_command"])
        self.assertIn("unconfirmed", self.bridge.snapshot()["events"][-1]["text"])

    def test_old_record_mode_unknown_dedup_and_atomic_reload(self):
        self.connect()
        self.bridge.feed_line("LOG_BEGIN id,count,seconds")
        self.bridge.feed_line("1,3,373")
        self.bridge.feed_line("LOG_END (up to 16 saved sessions)")
        self.assertIsNone(self.bridge.snapshot()["history"][0]["mode"])
        self.assertIsNone(self.bridge.snapshot()["history"][0]["saved_at"])
        self.bridge.feed_line("SAVED id=2 count=7 seconds=22")
        saved_at = self.bridge.snapshot()["history"][0]["saved_at"]
        self.clock.advance(2)
        self.bridge.feed_line("LOG_BEGIN id,count,seconds")
        self.bridge.feed_line("1,3,373")
        self.bridge.feed_line("2,7,22")
        self.bridge.feed_line("LOG_END (up to 16 saved sessions)")
        rows = self.bridge.snapshot()["history"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["mode"], "PASSAGE")
        self.assertEqual(rows[0]["saved_at"], saved_at)
        self.assertIsNone(rows[1]["mode"])
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])
        loaded = Bridge(self.path, lambda: [], clock=self.clock, auto_connect=False)
        try:
            self.assertEqual(len(loaded.snapshot()["history"]), 2)
            self.assertEqual(loaded.snapshot()["history"][0]["source"], "local")
        finally:
            loaded.close()
        self.bridge.device_id = "usb:2341:ANOTHER"
        self.bridge.feed_line("SAVED id=2 count=9 seconds=42")
        self.assertEqual(len(self.bridge.snapshot()["history"]), 3)

    def test_partial_log_is_not_imported_and_bad_history_is_preserved(self):
        self.connect()
        self.bridge.feed_line("LOG_BEGIN id,count,seconds")
        self.bridge.feed_line("1,3,373")
        self.bridge._drop_connection("test disconnect")
        self.assertEqual(self.bridge.snapshot()["history"], [])
        self.path.write_text("broken data", encoding="utf-8")
        other = Bridge(self.path, lambda: [], auto_connect=False)
        try:
            other.device_id = "test"
            other.feed_line("SAVED id=1 count=2 seconds=3")
            self.assertEqual(self.path.read_text(encoding="utf-8"), "broken data")
            self.assertIsNotNone(other.snapshot()["storage_error"])
        finally:
            other.close()

    def test_unplug_reconnect_and_manual_disconnect(self):
        self.connect()
        old = self.opened[-1]
        old.error = OSError("device unplugged")
        self.available = []
        self.bridge._tick()
        self.assertFalse(self.bridge.snapshot()["connection"]["connected"])
        self.assertTrue(old.closed)
        self.available = [port("/dev/cu.changed")]
        self.clock.advance(1.1)
        self.bridge._tick()
        self.assertTrue(self.bridge.snapshot()["connection"]["connected"])
        self.assertEqual(self.opened[-1].kwargs["port"], "/dev/cu.changed")
        self.bridge.disconnect()
        count = len(self.opened)
        self.clock.advance(2)
        self.bridge._tick()
        self.assertEqual(len(self.opened), count)

    def test_partial_serial_frames_are_reassembled(self):
        self.bridge._attempt_connect()
        handle = self.opened[-1]
        handle.incoming = b"READY FW=3 MODE=PAS"
        self.bridge._tick()
        self.assertIsNone(self.bridge.snapshot()["device"]["firmware"])
        handle.incoming = b"SAGE: UNO R4 Minima\r\n" + IDLE.encode() + b"\r\n"
        self.bridge._tick()
        self.assertEqual(self.bridge.snapshot()["device"]["mode"], "PASSAGE")
        self.assertEqual(self.bridge.snapshot()["device"]["distance_cm"], 40.0)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.frontend = root / "frontend"
        self.frontend.mkdir()
        (self.frontend / "index.html").write_text("<h1>Test fixture</h1>", encoding="utf-8")
        (root / "secret.txt").write_text("must not be served", encoding="utf-8")
        (self.frontend / "escape").symlink_to(root / "secret.txt")
        self.bridge = Bridge(root / "sessions.json", lambda: [port()], lambda **kw: FakeSerial(**kw),
                             lambda path: False, auto_connect=False)
        self.server = DashboardServer(("127.0.0.1", 0), self.bridge, self.frontend)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.origin = "http://127.0.0.1:%d" % self.port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.bridge.close()
        self.directory.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def post(self, path, payload, origin=None):
        return self.request("POST", path, json.dumps(payload), {
            "Content-Type": "application/json", "Origin": self.origin if origin is None else origin})

    def test_health_state_static_and_csv(self):
        status, headers, body = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["application"], "ece198-motion-counter")
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(self.request("GET", "/")[0], 200)
        self.assertEqual(self.request("GET", "/api/state")[0], 200)
        self.assertEqual(json.loads(self.request("GET", "/api/ports")[2])["ports"][0]["path"], "/dev/cu.test")
        self.assertEqual(self.request("GET", "/api/export.csv")[0], 200)

    def test_host_origin_json_size_and_traversal_guards(self):
        self.assertEqual(self.request("GET", "/api/state", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.post("/api/connect", {"port": "/dev/cu.test"}, "https://evil.example")[0], 403)
        self.assertEqual(self.request("POST", "/api/disconnect", "{}", {"Content-Type": "application/json"})[0], 403)
        self.assertEqual(self.request("POST", "/api/disconnect", "{}", {"Origin": self.origin})[0], 415)
        self.assertEqual(self.post("/api/disconnect", {"padding": "x" * 5000})[0], 413)
        self.assertEqual(self.post("/api/disconnect", [1, 2])[0], 400)
        self.assertEqual(self.request("GET", "/%2e%2e/secret.txt")[0], 404)
        self.assertEqual(self.request("GET", "/escape")[0], 404)
        self.assertEqual(self.post("/api/connect", {"port": "/etc/passwd"})[0], 409)
        self.assertEqual(self.post("/api/command", {"command": "START\nSTOP"})[0], 409)

    def test_connect_is_pending_not_fake_device_success(self):
        status, _, body = self.post("/api/connect", {"port": "/dev/cu.test"})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        state = json.loads(self.request("GET", "/api/state")[2])
        self.assertFalse(state["connection"]["connected"])
        self.assertIsNone(state["device"]["count"])
        self.assertEqual(self.post("/api/disconnect", {})[0], 200)


if __name__ == "__main__":
    unittest.main()
