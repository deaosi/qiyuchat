"""Display sender metadata for authenticated conversation context."""
from __future__ import annotations

from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import re
from unicodedata import normalize

from ..clean.rules import mask_pii
from ..push_sync import VERSION
from ..qiyu.message import source_auto_reply, source_integer


_FUKE_CHANNEL = re.compile(r"^福客\s*AI(?:$|[\s_·—-])", re.I)
_ACCOUNT = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{1,63}")
_EXTERNAL_CONTACT = re.compile(r"w[mo][A-Za-z0-9_-]{16,}")


def _object(text: str) -> dict:
    try:
        value = json.loads(text)
    except (TypeError, ValueError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


def fuke_channel(metadata: dict) -> bool:
    subtype = metadata.get("fromSubType")
    return (metadata.get("fromType") == "wx_cs" and isinstance(subtype, str)
            and bool(_FUKE_CHANNEL.match(normalize("NFKC", subtype).strip())))


def public_name(value: object) -> str:
    return mask_pii(" ".join(value.split())[:300]) if isinstance(value, str) else ""


def _directory(state_dir: str) -> dict:
    # The operations directory supplies confirmed enterprise account-to-name
    # mappings. It is local to the installation and contains no session data.
    path = Path(state_dir) / "fuke-staff-directory.json"
    try:
        return _object(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError):
        return {}


def _label(row: dict, company: bool, directory: dict) -> dict:
    if row["from"] == 0:
        name = public_name(row.get("staffName"))
        return {"sender_name": name} if name else {}
    account = row.get("externalUserId")
    account = account.strip() if isinstance(account, str) else ""
    name = public_name(row.get("userName"))
    if company:
        full_name = public_name(directory.get(account)) if account else ""
        if full_name:
            return {"sender_kind": "fuke_staff", "sender_name": full_name}
        if _EXTERNAL_CONTACT.fullmatch(account):
            return {"sender_name": name or "来源访客"}
        label = {"sender_kind": "unidentified"}
        if name:
            label["sender_name"] = name
        elif _ACCOUNT.fullmatch(account) and mask_pii(account) == account:
            label["sender_account"] = account
        return label
    return {"sender_name": name} if name else {}


def _key(message: dict) -> tuple:
    return message.get("role"), message.get("ts"), message.get("content")


def _source_matches(metadata: dict, source: list, retained: list[dict], *, legacy: bool = False):
    """Match exact originals to the already scoped, retained transcript rows."""
    by_id, by_content = {}, {}
    sid = metadata.get("id")
    for row in source:
        if (not isinstance(row, dict) or row.get("sessionId") != sid
                or not source_integer(row.get("id"), positive=True)
                or not source_integer(row.get("time"), positive=True)
                or row.get("status") != 1 or row.get("mType") not in (1, 2)
                or not isinstance(row.get("msg"), str)):
            continue
        if row.get("from") == 1:
            role = "user"
        elif row.get("from") == 0 and source_integer(row.get("staffId"), positive=True):
            role = "staff"
        else:
            continue
        key = (role, row["time"], row["msg"])
        by_id[f"source:{sid}:{row['id']}"] = (key, row)
        if legacy:
            by_content[key] = row if key not in by_content else None
    retained_counts = Counter(_key(message) for message in retained) if legacy else {}
    for message in retained:
        seq, key = message.get("context_seq"), _key(message)
        if type(seq) is not int or seq < 0:
            continue
        mid = message.get("source_message_id")
        if mid:
            exact = by_id.get(mid)
            row = exact[1] if exact and exact[0] == key else None
        else:
            row = by_content.get(key) if legacy and retained_counts[key] == 1 else None
        if row:
            yield seq, row


def labels(state_dir: str, metadata: dict, source: list, retained: list[dict], *, legacy: bool = False) -> list[dict]:
    company = fuke_channel(metadata)
    directory = _directory(state_dir) if company else {}
    return [{"context_seq": seq, **label}
            for seq, row in _source_matches(metadata, source, retained, legacy=legacy)
            if (label := _label(row, company, directory))]


def _saved_snapshot(db, sid: str, retained: list[dict], tables: set[str]) -> tuple[dict, list] | None:
    if "push_sync_state" not in tables or not any(m.get("source_message_id") for m in retained):
        return None
    row = db.execute(
        "SELECT source_json FROM push_sync_state WHERE session_id=? AND status='completed' "
        "AND source_version=version AND archive_reason=''", (sid,),
    ).fetchone()
    snapshot = _object(row["source_json"]) if row else {}
    metadata, source = snapshot.get("session"), snapshot.get("messages")
    if (snapshot.get("projection_version") != VERSION or not isinstance(metadata, dict)
            or not source_integer(metadata.get("id"), positive=True) or str(metadata["id"]) != sid
            or not isinstance(source, list)):
        return None
    return metadata, source


def saved_labels(db, state_dir: str, sid: str, retained: list[dict], tables: set[str]) -> dict[int, dict]:
    session = db.execute("SELECT payload FROM raw_session WHERE session_id=?", (sid,)).fetchone()
    company = bool(session and fuke_channel(_object(session["payload"])))
    result = {m["context_seq"]: {"sender_kind": "unidentified"}
              for m in retained if company and m["role"] == "user"}
    if snapshot := _saved_snapshot(db, sid, retained, tables):
        for label in labels(state_dir, *snapshot, retained):
            result[label.pop("context_seq")] = label
    else:
        result.update(saved_legacy_labels(state_dir, sid, retained))
    return result


def saved_source_nicknames(db, sid: str, retained: list[dict], tables: set[str]) -> dict[int, str]:
    """Read recorded nicknames without loading local account or staff directories."""
    snapshot = _saved_snapshot(db, sid, retained, tables)
    if snapshot is None:
        return {}
    company = fuke_channel(snapshot[0])
    return {seq: name for seq, row in _source_matches(*snapshot, retained)
            if (name := _source_nickname(row, company))}


def _external_contact(row: dict) -> bool:
    account = row.get("externalUserId")
    return isinstance(account, str) and bool(_EXTERNAL_CONTACT.fullmatch(account.strip()))


def _source_nickname(row: dict, company: bool) -> str:
    if row["from"] == 1:
        if company and not _external_contact(row):
            return ""
        name = row.get("userName")
        identifiers = (row.get("externalUserId"), row.get("userId"))
    elif not source_auto_reply(row):
        name = row.get("staffName")
        identifiers = (row.get("staffAccount"), row.get("staffId"))
    else:
        return ""
    if isinstance(name, str) and name.strip() and all(name.strip().casefold() != str(value).strip().casefold()
                                                     for value in identifiers if value is not None):
        return name.strip()[:300]
    return ""


def _legacy_digest(message: dict) -> str:
    return sha256(json.dumps(_key(message), ensure_ascii=True).encode()).hexdigest()


def legacy_source_file(state_dir: str, sid: str) -> Path | None:
    if not re.fullmatch(r"[1-9][0-9]{0,18}", sid):
        return None
    return Path(state_dir) / "voc-legacy-senders" / f"{sid}.json"


def legacy_source_records(metadata: dict, source: list, retained: list[dict]) -> dict:
    """Keep only labels bound to exact retained rows from a verified group export."""
    if not fuke_channel(metadata):
        return {}
    by_sequence = {m["context_seq"]: m for m in retained}
    records = {}
    for seq, row in _source_matches(metadata, source, retained, legacy=True):
        if row["from"] == 0 and source_auto_reply(row):
            continue
        records[str(seq)] = {
            "digest": _legacy_digest(by_sequence[seq]),
            "unidentified": row["from"] == 1 and not _external_contact(row),
            "name": _source_nickname(row, True),
        }
    return records


def saved_legacy_labels(state_dir: str, sid: str, retained: list[dict]) -> dict[int, dict]:
    if not retained or not (path := legacy_source_file(state_dir, sid)):
        return {}
    try:
        records = _object(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        return {}
    result = {}
    for message in retained:
        seq = message["context_seq"]
        record = records.get(str(seq))
        if not isinstance(record, dict) or record.get("digest") != _legacy_digest(message):
            continue
        if record.get("unidentified") is True and message["role"] == "user":
            result[seq] = {"sender_kind": "unidentified"}
        else:
            name = public_name(record.get("name"))
            result[seq] = {"sender_name": name} if name else (
                {"sender_name": "来源访客"} if message["role"] == "user" else {})
    return result
