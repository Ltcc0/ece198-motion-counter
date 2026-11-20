"""Local training profiles, archive and within-protocol daily comparisons."""
from collections import defaultdict
import sqlite3
import unicodedata

MODES = {"free", "count", "timed"}
SESSION_COLUMNS = ("device_id", "id", "count", "seconds", "saved_at", "mode",
                   "user_id", "training_mode", "target", "started_at", "training_day")


class TrainingStore:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        # The bridge lock serializes access from HTTP and USB worker threads.
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, name_key TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS sessions (
                device_id TEXT NOT NULL, id INTEGER NOT NULL, count INTEGER NOT NULL,
                seconds INTEGER NOT NULL, saved_at REAL, mode TEXT,
                user_id INTEGER REFERENCES users(id), training_mode TEXT, target INTEGER,
                started_at REAL, training_day TEXT, PRIMARY KEY(device_id, id)
            );
        """)

    def profiles(self):
        return [dict(row) for row in self.db.execute("SELECT id, name FROM users ORDER BY name_key")]

    def profile(self, name):
        if not isinstance(name, str):
            raise ValueError("Enter a profile name.")
        name = unicodedata.normalize("NFC", name.strip())
        if not 1 <= len(name) <= 60 or not name.isprintable():
            raise ValueError("Use a profile name with 1–60 printable characters.")
        key = name.casefold()
        try:
            with self.db:
                cursor = self.db.execute("INSERT INTO users(name, name_key) VALUES (?, ?)", (name, key))
                return dict(id=cursor.lastrowid, name=name)
        except sqlite3.IntegrityError as error:
            raise ValueError("That profile name is in use. Select it or enter a different alias.") from error

    def load(self):
        return [dict(row, source="local") for row in self.db.execute("SELECT * FROM sessions")]

    def save(self, rows, import_only=False):
        columns = ", ".join(SESSION_COLUMNS)
        params = ", ".join(":" + key for key in SESSION_COLUMNS)
        updates = ", ".join(f"{key}=excluded.{key}" for key in SESSION_COLUMNS[2:])
        conflict = "DO NOTHING" if import_only else "DO UPDATE SET " + updates
        with self.db:
            self.db.executemany(f"INSERT INTO sessions ({columns}) VALUES ({params}) "
                                f"ON CONFLICT(device_id, id) {conflict}", rows)


def training_plan(user_id, mode, target, profiles):
    if type(user_id) is not int or not any(p["id"] == user_id for p in profiles):
        raise ValueError("Select an existing profile.")
    if not isinstance(mode, str) or mode not in MODES:
        raise ValueError("Choose free practice, count goal or timed practice.")
    if mode == "free":
        if target is not None:
            raise ValueError("Free practice takes no target.")
    elif type(target) is not int or not 1 <= target <= 4294967295:
        raise ValueError("Enter a positive whole-number target within the board counter range.")
    return dict(user_id=user_id, training_mode=mode, target=target)


def daily_reports(rows):
    """Caller supplies one profile. Compare like targets and weight pace by time."""
    protocols = defaultdict(dict)
    for row in rows:
        if not row["training_day"] or row["mode"] != "PASSAGE" or row["training_mode"] not in MODES:
            continue
        days = protocols[row["training_mode"], row["target"]]
        day = days.setdefault(row["training_day"], dict(day=row["training_day"], sessions=0,
                              count=0, seconds=0, timed_count=0, goals_met=0))
        day["sessions"] += 1
        day["count"] += row["count"]
        day["seconds"] += row["seconds"]
        if row["seconds"] > 0:
            day["timed_count"] += row["count"]
        if row["target"] is not None:
            value = row["seconds"] if row["training_mode"] == "timed" else row["count"]
            day["goals_met"] += int(value >= row["target"])

    reports = []
    for (mode, target), days in sorted(protocols.items()):
        ordered = [days[date] for date in sorted(days)]
        for day in ordered:
            count = day.pop("timed_count")
            day["pace"] = count * 60 / day["seconds"] if day["seconds"] else None
        baseline = next((day for day in ordered if day["pace"] is not None), None)
        latest = ordered[-1]
        delta = None
        if baseline and baseline["day"] != latest["day"] and latest["pace"] is not None:
            delta = latest["pace"] - baseline["pace"]
        reports.append(dict(training_mode=mode, target=target, days=ordered, delta=delta,
                            baseline_day=baseline["day"] if baseline else None))
    return reports
