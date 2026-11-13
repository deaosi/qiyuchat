"""Scheduled dashboard credentials. The configured permanent key stays outside this store."""

from contextlib import closing, contextmanager
from hashlib import sha256
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import time
from uuid import uuid4


_TOKEN = re.compile(r"yvoc_[A-Za-z0-9_-]{43}")
_ID = re.compile(r"[0-9a-f]{32}")
_MAX_TIME = 253402300799  # 9999-12-31 23:59:59 UTC
_PUBLIC = ("id", "label", "starts_at", "expires_at", "created_at", "revoked_at", "preview")
_SELECT_PUBLIC = ",".join(_PUBLIC)
_SCHEDULE_FIELDS = {"label", "starts_at", "expires_at"}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS admin_key (
    id TEXT PRIMARY KEY,
    digest BLOB NOT NULL UNIQUE,
    label TEXT NOT NULL,
    starts_at INTEGER NOT NULL,
    expires_at INTEGER,
    created_at INTEGER NOT NULL,
    revoked_at INTEGER,
    preview TEXT NOT NULL,
    CHECK (expires_at IS NULL OR expires_at > starts_at)
)
"""


class MissingKey(ValueError):
    """The requested managed key is unknown."""


class InactiveKey(ValueError):
    """The requested managed key can no longer be edited."""


def _status(row, now):
    if row["revoked_at"] is not None:
        return "revoked"
    if row["expires_at"] is not None and row["expires_at"] <= now:
        return "expired"
    if now < row["starts_at"]:
        return "scheduled"
    return "active"


def _public(row, now):
    return {**{field: row[field] for field in _PUBLIC}, "status": _status(row, now)}


def _validate_action(body, action, fields):
    if not isinstance(body, dict) or set(body) - (fields | {"action"}) or body.get("action", action) != action:
        raise ValueError("密钥操作参数无效。")


def _key_id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("密钥编号无效。")
    return value


def _schedule(body):
    label = body.get("label")
    if not isinstance(label, str):
        raise ValueError("请输入密钥名称。")
    label = label.strip()
    if not label or len(label) > 80 or any(ord(char) < 32 or 127 <= ord(char) <= 159
                                          or 0xD800 <= ord(char) <= 0xDFFF for char in label):
        raise ValueError("密钥名称须为 1–80 个字符，且不能含控制字符。")
    start, expiry = body.get("starts_at"), body.get("expires_at")
    if type(start) is not int or not 0 <= start <= _MAX_TIME:
        raise ValueError("生效时间无效。")
    if expiry is not None and (type(expiry) is not int or not start < expiry <= _MAX_TIME):
        raise ValueError("过期时间须晚于生效时间。")
    return label, start, expiry


class KeyStore:
    def __init__(self, path: Path):
        self.path = Path(path).absolute()

    def _check_file(self):
        mode = self.path.lstat().st_mode
        if not stat.S_ISREG(mode) or (os.name != "nt" and mode & 0o077):
            raise OSError("密钥存储文件权限无效。")

    def _connect(self, *, writable=False):
        mode = "rw" if writable else "ro"
        db = sqlite3.connect(self.path.as_uri() + "?mode=" + mode, uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    @contextmanager
    def _write(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        self._check_file()
        with closing(self._connect(writable=True)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(_SCHEMA)
            yield db

    def list(self) -> list[dict]:
        try:
            self._check_file()
        except FileNotFoundError:
            return []
        with closing(self._connect()) as db:
            rows = db.execute(f"SELECT {_SELECT_PUBLIC} FROM admin_key ORDER BY created_at DESC, id DESC").fetchall()
        now = int(time.time())
        return [_public(row, now) for row in rows]

    def _active(self, column, value) -> str | None:
        try:
            self._check_file()
            with closing(self._connect()) as db:
                row = db.execute(f"SELECT id,starts_at,expires_at,revoked_at FROM admin_key WHERE {column}=?",
                                 (value,)).fetchone()
            if row is not None and _status(row, int(time.time())) == "active":
                return row["id"]
        except (OSError, sqlite3.Error):
            pass
        return None

    def identify(self, value: str) -> str | None:
        if isinstance(value, str) and _TOKEN.fullmatch(value):
            return self._active("digest", sha256(value.encode("ascii")).digest())
        return None

    def authenticate(self, value: str) -> bool:
        return self.identify(value) is not None

    def active_id(self, key_id: str) -> bool:
        return bool(isinstance(key_id, str) and _ID.fullmatch(key_id) and self._active("id", key_id))

    @staticmethod
    def _record(db, key_id):
        row = db.execute(f"SELECT {_SELECT_PUBLIC} FROM admin_key WHERE id=?", (key_id,)).fetchone()
        if row is None:
            raise MissingKey("未找到该管理员密钥。")
        return row

    def create(self, body: dict) -> dict:
        _validate_action(body, "create", _SCHEDULE_FIELDS)
        label, start, expiry = _schedule(body)
        value = "yvoc_" + secrets.token_urlsafe(32)
        digest = sha256(value.encode("ascii")).digest()
        key_id = uuid4().hex
        with self._write() as db:
            now = int(time.time())
            if expiry is not None and expiry <= now:
                raise ValueError("新密钥的过期时间须晚于当前时间。")
            db.execute("""INSERT INTO admin_key(id,digest,label,starts_at,expires_at,created_at,preview)
                          VALUES (?,?,?,?,?,?,?)""", (key_id, digest, label, start, expiry, now, "yvoc_…" + value[-6:]))
            record = _public(self._record(db, key_id), now)
        return {"key": value, "record": record}

    def update(self, body: dict) -> dict:
        _validate_action(body, "update", _SCHEDULE_FIELDS | {"id"})
        key_id = _key_id(body.get("id"))
        label, start, expiry = _schedule(body)
        with self._write() as db:
            now = int(time.time())
            if _status(self._record(db, key_id), now) in ("revoked", "expired"):
                raise InactiveKey("已撤销或已过期的密钥无法修改。")
            db.execute("UPDATE admin_key SET label=?,starts_at=?,expires_at=? WHERE id=?", (label, start, expiry, key_id))
            record = _public(self._record(db, key_id), now)
        return {"record": record}

    def revoke(self, body: dict) -> dict:
        _validate_action(body, "revoke", {"id"})
        key_id = _key_id(body.get("id"))
        with self._write() as db:
            now = int(time.time())
            self._record(db, key_id)
            db.execute("UPDATE admin_key SET revoked_at=? WHERE id=? AND revoked_at IS NULL", (now, key_id))
            record = _public(self._record(db, key_id), now)
        return {"record": record}
