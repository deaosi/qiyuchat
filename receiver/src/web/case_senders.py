"""Display sender metadata for authenticated conversation context."""
from __future__ import annotations

from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import re
from unicodedata import normalize

from ..clean.rules import mask_pii
from ..push_sync import VERSION, _numeric_identity
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
        if source_auto_reply(row):
            return {}
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
        if nickname := public_name(_participant_nickname(row)):
            return {"sender_kind": "group_participant", "sender_name": nickname}
        label = {"sender_kind": "unidentified"}
        if _ACCOUNT.fullmatch(account) and mask_pii(account) == account:
            label["sender_account"] = account
        return label
    return {"sender_name": name} if name else {}


def _key(message: dict) -> tuple:
    return message.get("role"), message.get("ts"), message.get("content")


def _trimmed_key(key: tuple) -> tuple:
    role, stamp, text = key
    return role, stamp, text.strip() if isinstance(text, str) else text


def _source_matches(metadata: dict, source: list, retained: list[dict], *, legacy: bool = False):
    """Match exact originals to the already scoped, retained transcript rows."""
    by_id, by_content, by_trimmed = {}, {}, {}
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
            trimmed = _trimmed_key(key)
            by_trimmed[trimmed] = row if trimmed not in by_trimmed else None
    retained_counts = Counter(_key(message) for message in retained) if legacy else {}
    trimmed_counts = Counter(_trimmed_key(_key(message)) for message in retained) if legacy else {}
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
            if row is None and legacy and key not in by_content and isinstance(key[2], str) and key[2].strip():
                trimmed = _trimmed_key(key)
                if trimmed_counts[trimmed] == 1:
                    row = by_trimmed.get(trimmed)
        if row:
            yield seq, row


def labels(state_dir: str, metadata: dict, source: list, retained: list[dict], *, legacy: bool = False) -> list[dict]:
    company = fuke_channel(metadata)
    directory = _directory(state_dir) if company else {}
    result, aliases = [], {}
    for seq, row in _source_matches(metadata, source, retained, legacy=legacy):
        label = _label(row, company, directory)
        if label == {"sender_kind": "unidentified"} and (identity := _unnamed_key(metadata, row)):
            label["sender_name"] = _source_alias(aliases, identity)
        if label:
            result.append({"context_seq": seq, **label})
    return result


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
        result.update(saved_legacy_labels(state_dir, sid, retained, admin=True))
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


def _nickname(value: object, identifiers: tuple) -> str:
    if isinstance(value, str) and value.strip() and all(value.strip().casefold() != str(identity).strip().casefold()
                                                       for identity in identifiers if identity is not None):
        return value.strip()[:300]
    return ""


def _participant_nickname(row: dict) -> str:
    return _nickname(row.get("userName"), (row.get("externalUserId"), row.get("userId")))


def _source_nickname(row: dict, company: bool) -> str:
    if row["from"] == 1:
        if company and not _external_contact(row):
            return ""
        return _participant_nickname(row)
    if not source_auto_reply(row):
        return _nickname(row.get("staffName"), (row.get("staffAccount"), row.get("staffId")))
    return ""


def _legacy_digest(message: dict) -> str:
    return sha256(json.dumps(_key(message), ensure_ascii=True).encode()).hexdigest()


def _source_user_identity(metadata: dict, row: dict) -> int | None:
    value = row.get("userId")
    default = _numeric_identity(metadata.get("userId"))
    if source_integer(value, positive=True) and value != default:
        return value
    return None


def _unnamed_key(metadata: dict, row: dict) -> str:
    account = row.get("externalUserId")
    if (row["from"] == 1 and not _participant_nickname(row)
            and not (isinstance(account, str) and account.strip())
            and _source_user_identity(metadata, row) is not None):
        return _source_sender_key(metadata, row)
    return ""


def _source_alias(aliases: dict, key: str) -> str:
    return aliases.setdefault(key, f"来源账号 {len(aliases) + 1}")


def _source_sender_key(metadata: dict, row: dict) -> str:
    if row["from"] != 1:
        return ""
    account = row.get("externalUserId")
    if isinstance(account, str) and account.strip():
        identity = ("external", account.strip())
    elif (visitor := _source_user_identity(metadata, row)) is not None:
        identity = ("visitor", visitor)
    elif _participant_nickname(row):
        # An unlinked participant's nickname identifies this message for
        # display, but may also be shared by someone else in the group.
        identity = ("message", row["id"])
    else:
        return ""
    return sha256(json.dumps([str(metadata["id"]), *identity], ensure_ascii=True).encode()).hexdigest()


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
        record = {
            "digest": _legacy_digest(by_sequence[seq]),
            "unidentified": row["from"] == 1 and not _external_contact(row),
            "name": _source_nickname(row, True),
        }
        if key := _source_sender_key(metadata, row):
            record["sender_key"] = key
        if _unnamed_key(metadata, row):
            record["unnamed_source"] = True
        if record["unidentified"]:
            if name := _participant_nickname(row):
                record["participant_name"] = name
            account = row.get("externalUserId")
            if isinstance(account, str) and account.strip():
                record["participant_account"] = account.strip()
        records[str(seq)] = record
    return records


def _legacy_records(state_dir: str, sid: str, retained: list[dict]):
    if not retained or not (path := legacy_source_file(state_dir, sid)):
        return
    try:
        records = _object(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        return
    for message in retained:
        seq = message["context_seq"]
        record = records.get(str(seq))
        if not isinstance(record, dict) or record.get("digest") != _legacy_digest(message):
            continue
        yield message, record


def saved_legacy_labels(state_dir: str, sid: str, retained: list[dict], *, admin: bool = False) -> dict[int, dict]:
    directory = _directory(state_dir) if admin else {}
    result, aliases = {}, {}
    for message, record in _legacy_records(state_dir, sid, retained):
        seq = message["context_seq"]
        if record.get("unidentified") is True and message["role"] == "user":
            result[seq] = {"sender_kind": "unidentified"}
            if admin:
                account = record.get("participant_account")
                full_name = public_name(directory.get(account)) if isinstance(account, str) else ""
                if full_name:
                    result[seq] = {"sender_kind": "fuke_staff", "sender_name": full_name}
                elif name := public_name(record.get("participant_name")):
                    result[seq] = {"sender_kind": "group_participant", "sender_name": name}
                elif isinstance(account, str) and _ACCOUNT.fullmatch(account) and mask_pii(account) == account:
                    result[seq]["sender_account"] = account
                elif record.get("unnamed_source") is True and (key := _cached_sender_key(record)):
                    result[seq]["sender_name"] = _source_alias(aliases, key)
        else:
            name = public_name(record.get("name"))
            result[seq] = {"sender_name": name} if name else (
                {"sender_name": "来源访客"} if message["role"] == "user" else {})
    return result


def _cached_sender_key(record: dict) -> str:
    key = record.get("sender_key")
    return key if isinstance(key, str) and re.fullmatch(r"[0-9a-f]{64}", key) else ""


def saved_guest_groups(db, state_dir: str, sid: str, retained: list[dict], tables: set[str]) -> dict[int, str]:
    """Internal sender groups for exact visible rows. Never return keys to clients."""
    if snapshot := _saved_snapshot(db, sid, retained, tables):
        if not fuke_channel(snapshot[0]):
            return {}
        return {seq: key for seq, row in _source_matches(*snapshot, retained)
                if (key := _source_sender_key(snapshot[0], row))}
    return {message["context_seq"]: key for message, record in _legacy_records(state_dir, sid, retained)
            if message["role"] == "user" and (key := _cached_sender_key(record))}
