"""Email/password accounts and expiring login sessions for the local dashboard."""
import hashlib
import hmac
import re
import secrets
import sqlite3
import time

SESSION_AGE = 7 * 24 * 60 * 60
ROUNDS = 600_000


class InvalidLogin(ValueError):
    pass


class Accounts:
    def __init__(self, db, lock):
        self.db = db
        self.lock = lock
        # Old nickname profiles have no account row. Registration always creates a new identity.
        db.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
                user_id INTEGER PRIMARY KEY REFERENCES users(id),
                email TEXT NOT NULL UNIQUE, salt BLOB NOT NULL,
                password_hash BLOB NOT NULL, rounds INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS logins (
                token_hash BLOB PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES accounts(user_id),
                expires_at INTEGER NOT NULL
            );
        """)

    @staticmethod
    def credentials(email, password):
        email = email.strip().casefold() if isinstance(email, str) else ""
        if not (email.isprintable() and len(email) <= 254 and re.fullmatch(r"[^@\s]+@[^@\s]+", email)):
            raise ValueError("Enter a valid email address.")
        if not isinstance(password, str) or not 1 <= len(password) <= 256:
            raise ValueError("Enter a password of up to 256 characters.")
        return email, password.encode("utf-8")

    def register(self, email, password):
        email, password = self.credentials(email, password)
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", password, salt, ROUNDS)
        try:
            with self.lock, self.db:
                user_id = self.db.execute("INSERT INTO users(name, name_key) VALUES (?, ?)",
                                          (email, "\0account:" + email)).lastrowid
                self.db.execute("INSERT INTO accounts VALUES (?, ?, ?, ?, ?)",
                                (user_id, email, salt, digest, ROUNDS))
                token = self._issue(user_id)
        except sqlite3.IntegrityError as error:
            raise ValueError("This email already has an account. Sign in to continue.") from error
        return token, dict(id=user_id, email=email)

    def login(self, email, password):
        email, password = self.credentials(email, password)
        with self.lock:
            row = self.db.execute("SELECT * FROM accounts WHERE email=?", (email,)).fetchone()
        digest = hashlib.pbkdf2_hmac("sha256", password, row["salt"] if row else bytes(16),
                                     row["rounds"] if row else ROUNDS)
        if row is None or not hmac.compare_digest(digest, row["password_hash"]):
            raise InvalidLogin("Email or password is incorrect.")
        with self.lock, self.db:
            token = self._issue(row["user_id"])
        return token, dict(id=row["user_id"], email=email)

    def _issue(self, user_id):
        now = int(time.time())
        token = secrets.token_urlsafe(32)
        self.db.execute("DELETE FROM logins WHERE expires_at <= ?", (now,))
        self.db.execute("INSERT INTO logins VALUES (?, ?, ?)",
                        (self._token_hash(token), user_id, now + SESSION_AGE))
        return token

    @staticmethod
    def _token_hash(token):
        return hashlib.sha256(token.encode("ascii")).digest()

    def current(self, token):
        if not token:
            return None
        with self.lock:
            row = self.db.execute("""
                SELECT a.user_id AS id, a.email FROM accounts a JOIN logins l ON l.user_id=a.user_id
                WHERE l.token_hash=? AND l.expires_at>?
            """, (self._token_hash(token), int(time.time()))).fetchone()
        return dict(row) if row else None

    def logout(self, token):
        if token:
            with self.lock, self.db:
                self.db.execute("DELETE FROM logins WHERE token_hash=?", (self._token_hash(token),))
