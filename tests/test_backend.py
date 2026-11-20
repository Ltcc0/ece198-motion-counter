"""Protocol/HTTP tests with fake ports only; never open a physical serial device."""
from __future__ import annotations

import csv
from datetime import datetime
import http.client
import io
import json
from pathlib import Path
import sqlite3
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

    def start_training(self, profile, mode, target, record_id):
        training = dict(user_id=profile["id"], training_mode=mode, target=target)
        self.assertTrue(self.bridge.command("START", training)[0])
        self.bridge._pump_command()
        self.assertEqual(self.opened[-1].writes[-1], b"START\n")
        self.bridge.feed_line("STARTED id=%d" % record_id)

    def test_count_goal_binds_profile_to_confirmed_record_and_survives_restart(self):
        self.connect()
        user = self.bridge.add_profile("  Lee  ")
        with self.assertRaisesRegex(ValueError, "in use"):
            self.bridge.add_profile("lee")
        self.start_training(user, "count", 3, 1)
        self.bridge.feed_line("REP=2 EVENT=PASS_COUNTED")
        self.assertIsNone(self.bridge.pending)
        self.bridge.feed_line("REP=3 EVENT=PASS_COUNTED")
        self.assertEqual(self.bridge.pending["command"], "STOP")
        self.assertFalse(self.bridge.command("START", {})[0])
        self.bridge._pump_command()
        self.assertEqual(self.opened[-1].writes[-1], b"STOP\n")
        self.bridge.feed_line(IDLE)
        self.assertEqual(self.bridge.records(user["id"])["history"], [])
        self.bridge.feed_line("SAVED id=1 count=3 seconds=15")
        self.clock.advance(1)
        self.bridge.feed_line("LOG_BEGIN")
        self.bridge.feed_line("1,3,15")
        self.bridge.feed_line("LOG_END")
        loaded = Bridge(self.path, lambda: [], auto_connect=False)
        try:
            self.assertEqual(loaded.snapshot()["profiles"], [user])
            archive = loaded.records(user["id"])
            self.assertEqual(len(archive["history"]), 1)
            record = archive["history"][0]
            self.assertEqual((record["user_id"], record["training_mode"], record["target"]),
                             (user["id"], "count", 3))
            day = archive["reports"][0]["days"][0]
            self.assertEqual((day["goals_met"], day["pace"]), (1, 12))
            self.assertIsNone(archive["reports"][0]["delta"])
        finally:
            loaded.close()

    def test_timed_goal_uses_board_seconds_and_handles_failed_saves(self):
        self.connect()
        user = self.bridge.add_profile("A")
        self.start_training(user, "timed", 30, 1)
        self.clock.advance(60)
        self.bridge.feed_line("STATE=RUNNING COUNT=40 SEC=29")
        self.assertIsNone(self.bridge.pending)
        self.bridge.feed_line("STATE=RUNNING COUNT=40 SEC=30")
        self.assertEqual(self.bridge.pending["command"], "STOP")
        self.bridge._pump_command()
        self.bridge.feed_line("SAVE_FAILED id=1 count=40 seconds=30")
        self.assertIsNone(self.bridge.snapshot()["active_training"])
        self.assertFalse(self.bridge.records()["history"])

    def test_trends_isolate_profiles_goals_days_and_zero_duration(self):
        self.connect()
        a = self.bridge.add_profile("A")
        b = self.bridge.add_profile("B")
        cases = [(a, 1, "free", None, 3, 60), (a, 1, "free", None, 6, 30),
                 (a, 2, "free", None, 2, 0), (a, 2, "free", None, 15, 60),
                 (a, 2, "count", 20, 7, 12), (a, 2, "count", 10, 10, 10),
                 (b, 2, "free", None, 50, 15), (b, 3, "free", None, 0, 0)]
        for record_id, (user, day, mode, target, count, seconds) in enumerate(cases, 1):
            self.clock.now = datetime(2026, 6, day, 12).timestamp()
            self.bridge.feed_line(IDLE)
            self.start_training(user, mode, target, record_id)
            self.assertTrue(self.bridge.command("STOP")[0])
            self.bridge._pump_command()
            self.bridge.feed_line("SAVED id=%d count=%d seconds=%d" % (record_id, count, seconds))
        reports = self.bridge.records(a["id"])["reports"]
        self.assertEqual(len(reports), 3)
        self.assertEqual([r["target"] for r in reports if r["training_mode"] == "count"], [10, 20])
        free = next(r for r in reports if r["training_mode"] == "free")
        self.assertEqual([d["day"] for d in free["days"]], ["2026-06-01", "2026-06-02"])
        self.assertEqual([d["count"] for d in free["days"]], [9, 17])
        self.assertEqual([d["pace"] for d in free["days"]], [6, 15])
        self.assertEqual(free["delta"], 9)
        self.assertEqual(reports[1]["days"][0]["goals_met"], 0)
        other = self.bridge.records(b["id"])["reports"][0]
        self.assertIsNone(other["days"][-1]["pace"])
        self.assertIsNone(other["delta"])

    def test_invalid_or_interrupted_start_does_not_attribute_board_only_records(self):
        self.connect()
        user = self.bridge.add_profile("A")
        for mode, target in [("free", 1), ("timed", True), ("count", 0), ("invalid", 2), ("count", 1.2)]:
            self.assertFalse(self.bridge.command("START", dict(user_id=user["id"], training_mode=mode,
                                                             target=target))[0])
        self.start_training(user, "count", 5, 1)
        self.bridge._drop_connection("interrupted")
        self.bridge.feed_line("LOG_BEGIN")
        self.bridge.feed_line("1,5,15")
        self.bridge.feed_line("LOG_END")
        self.assertEqual(self.bridge.records(user["id"])["history"], [])
        orphan = self.bridge.records(None)["history"][0]
        self.assertIsNone(orphan["training_day"])
        self.assertIsNone(orphan["user_id"])

    def test_legacy_import_is_idempotent_and_unassigned(self):
        raw = json.dumps(dict(version=1, sessions=[dict(device_id="old", id=8, count=2, seconds=4,
                                                       mode="CYCLE", saved_at=10)]))
        self.path.write_text(raw)
        for _ in range(2):
            loaded = Bridge(self.path, lambda: [], auto_connect=False)
            try:
                row, = loaded.records(None)["history"]
                self.assertEqual((row["mode"], row["saved_at"]), ("CYCLE", 10))
                self.assertIsNone(row["training_day"])
                self.assertIsNone(row["user_id"])
            finally:
                loaded.close()
        self.assertEqual(self.path.read_text(), raw)

    def test_lost_stop_ack_recovers_profile_only_after_complete_board_log(self):
        self.connect()
        user = self.bridge.add_profile("A")
        self.start_training(user, "count", 2, 1)
        self.bridge.feed_line("REP=2 EVENT=PASS_COUNTED")
        self.bridge._pump_command()
        self.bridge.feed_line(IDLE)
        self.clock.advance(5.1)
        self.bridge._pump_command()
        self.assertEqual(self.opened[-1].writes[-1], b"LOG\n")
        self.bridge.feed_line("LOG_BEGIN")
        self.bridge.feed_line("1,2,10")
        self.assertEqual(self.bridge.records()["history"], [])
        self.bridge.feed_line("LOG_END")
        row, = self.bridge.records(user["id"])["history"]
        self.assertEqual(row["training_mode"], "count")
        self.assertIsNone(row["saved_at"])
        self.assertIsNone(self.bridge.active_training)

    def test_sqlite_retry_retains_training_metadata(self):
        self.connect()
        user = self.bridge.add_profile("A")
        self.start_training(user, "free", None, 1)
        save = self.bridge.store.save
        def busy(rows):
            raise sqlite3.OperationalError("database is locked")
        self.bridge.store.save = busy
        self.bridge.feed_line("SAVED id=1 count=2 seconds=3")
        self.assertIsNotNone(self.bridge.storage_error)
        self.bridge.store.save = save
        self.bridge.feed_line("LOG_BEGIN")
        self.bridge.feed_line("1,2,3")
        self.bridge.feed_line("LOG_END")
        self.assertIsNone(self.bridge.storage_error)
        self.assertEqual(self.bridge.store.load()[0]["user_id"], user["id"])

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
        self.cookie = ""
        self.user = self.register("alice@example.test")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.bridge.close()
        self.directory.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            headers = dict(headers or {})
            if self.cookie:
                headers.setdefault("Cookie", self.cookie)
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def post(self, path, payload, origin=None):
        return self.request("POST", path, json.dumps(payload), {
            "Content-Type": "application/json", "Origin": self.origin if origin is None else origin})

    def register(self, email):
        status, headers, body = self.post("/api/register", {"email": email, "password": "test-password"})
        self.assertEqual(status, 200)
        self.cookie = headers["Set-Cookie"].split(";")[0]
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        return json.loads(body)["account"]

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

    def test_login_logout_expiry_and_persistence(self):
        credentials = {"email": "ALICE@example.test", "password": "test-password"}
        self.assertEqual(self.post("/api/register", credentials)[0], 400)
        self.assertEqual(self.post("/api/register", dict(credentials, email="invalid"))[0], 400)
        self.assertEqual(self.post("/api/register", dict(credentials, password=""))[0], 400)
        self.assertEqual(self.post("/api/logout", {})[0], 200)
        for path in ("/api/state", "/api/state?live=1", "/api/records", "/api/export.csv", "/api/ports"):
            self.assertEqual(self.request("GET", path)[0], 401)
        self.assertEqual(self.request("GET", "/")[1]["Location"], "/login.html")
        self.assertEqual(self.post("/api/command", {"command": "START"})[0], 401)
        self.assertEqual(self.post("/api/login", dict(credentials, password="wrong"))[0], 401)
        status, headers, body = self.post("/api/login", credentials)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["account"], self.user)
        self.cookie = headers["Set-Cookie"].split(";")[0]
        token = self.cookie.split("=")[1]
        reopened = Bridge(self.bridge.history_path, lambda: [], auto_connect=False)
        try:
            self.assertEqual(reopened.accounts.current(token), self.user)
            with reopened.store.db:
                reopened.store.db.execute("UPDATE logins SET expires_at=0")
        finally:
            reopened.close()
        self.assertEqual(self.request("GET", "/api/records")[0], 401)

    def test_account_ownership_and_dated_reports(self):
        alice_cookie = self.cookie
        bob = self.register("bob@example.test")
        bob_cookie = self.cookie
        self.cookie = alice_cookie
        self.bridge.auto_connect = True
        self.bridge._attempt_connect()
        self.bridge.feed_line(BANNER)
        self.bridge.feed_line(IDLE)
        cases = [(alice_cookie, self.user, 1, 5), (alice_cookie, self.user, 3, 8), (bob_cookie, bob, 3, 11)]
        for session_id, (cookie, account, day, count) in enumerate(cases, 1):
            self.cookie = cookie
            now = datetime(2026, 6, day, 12).timestamp()
            self.bridge.clock = lambda: now
            self.bridge.feed_line(IDLE)
            if session_id == 1:
                self.assertEqual(self.post("/api/command", {"command": "START", "training":
                                 {"user_id": bob["id"], "training_mode": "free"}})[0], 400)
            self.assertEqual(self.post("/api/command", {"command": "START"})[0], 200)
            self.bridge._pump_command()
            self.bridge.feed_line("STARTED id=%d" % session_id)
            self.bridge.feed_line("STATE=RUNNING COUNT=4 SEC=10")
            self.assertEqual(self.bridge.active_training["user_id"], account["id"])
            if session_id == 1:
                self.cookie = bob_cookie
                state = json.loads(self.request("GET", "/api/state")[2])
                self.assertTrue(state["device_busy"])
                self.assertIsNone(state["active_training"])
                self.assertIsNone(state["device"]["count"])
                self.assertEqual(state["samples"], [])
                self.assertEqual(state["events"], [])
                self.assertEqual(self.post("/api/command", {"command": "STOP"})[0], 409)
                self.assertEqual(self.post("/api/disconnect", {})[0], 409)
                self.cookie = cookie
            self.bridge.feed_line("SAVED id=%d count=%d seconds=60" % (session_id, count))
        self.cookie = alice_cookie
        result = json.loads(self.request("GET", "/api/records")[2])
        self.assertEqual([row["training_day"] for row in result["history"]], ["2026-06-03", "2026-06-01"])
        self.assertEqual(result["reports"][0]["delta"], 3)
        state = json.loads(self.request("GET", "/api/state")[2])
        self.assertEqual(len(state["history"]), 2)
        self.assertNotIn("profiles", state)
        self.assertIsNone(state["device"]["seconds"])
        self.assertEqual(self.request("GET", "/api/records?user=all")[0], 403)
        self.assertEqual(self.request("GET", "/api/export.csv?user=%d" % bob["id"])[0], 403)
        self.assertEqual(self.request("GET", "/api/records?from=2026-02-29")[0], 400)
        self.assertEqual(self.request("GET", "/api/records?from=2026-06-03&to=2026-06-01")[0], 400)
        interval = "?from=2026-06-03&to=2026-06-03"
        one_day = json.loads(self.request("GET", "/api/records" + interval)[2])
        self.assertEqual([row["count"] for row in one_day["history"]], [8])
        self.assertIsNone(one_day["reports"][0]["delta"])
        exported = self.request("GET", "/api/export.csv" + interval)[2]
        row, = csv.DictReader(io.StringIO(exported.decode("utf-8-sig")))
        self.assertEqual((row["profile"], row["training_day"], row["count"]), ("alice@example.test", "2026-06-03", "8"))
        self.assertEqual(json.loads(self.request("GET", "/api/records?to=2025-01-01")[2])["history"], [])

    def test_old_nickname_is_not_claimed_by_email_registration(self):
        old = self.bridge.add_profile("legacy@example.test")
        account = self.register("legacy@example.test")
        self.assertNotEqual(account["id"], old["id"])
        self.assertEqual(self.post("/api/profiles", {"name": "alias"})[0], 404)

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
