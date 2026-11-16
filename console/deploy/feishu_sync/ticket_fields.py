"""Keep ticket writes bound to the original Feishu columns."""

from dataclasses import dataclass
import re

from src import voc_feishu_sync as sync


_FIELD_ID = re.compile(r"fld[A-Za-z0-9]{4,60}")


@dataclass(frozen=True)
class Target(sync.Target):
    approved: dict[str, tuple[str, int]]
    primary: str
    skipped: tuple[str, ...]
    feedback_attachment: str | None = None
    attachment_id: str | None = None


def schema(target):
    fields = {}
    identifiers = set()
    for field in sync._paginated(target, "GET", "/fields"):
        if not isinstance(field, dict):
            raise sync.SyncError("飞书工单表返回了无法核验的列。", 409)
        name, identifier, kind = (field.get(key) for key in ("field_name", "field_id", "type"))
        if (not isinstance(name, str) or not name or not isinstance(identifier, str)
                or not _FIELD_ID.fullmatch(identifier) or type(kind) is not int
                or name in fields or identifier in identifiers):
            raise sync.SyncError("飞书工单表返回了无法核验的列。", 409)
        identifiers.add(identifier)
        fields[name] = ((identifier, kind), field.get("is_primary") is True)
    return fields


def select(target, fields):
    if isinstance(target, Target):
        return {name: value for name, value in fields.items() if name in target.approved}
    return fields


def require(target, names, *, refresh=False):
    if not isinstance(target, Target):
        return
    names = set(names)
    readable = target.approved
    if target.feedback_attachment and target.attachment_id:
        readable = {**readable, target.feedback_attachment: (target.attachment_id, 17)}
    missing = sorted(names - readable.keys())
    if missing:
        raise sync.SyncError(f"原有工单列已变更，暂不能使用：{'、'.join(missing)}。", 409)
    if refresh:
        current = schema(target)
        for name in sorted(names | {target.primary}):
            if current.get(name) != (readable[name], name == target.primary):
                raise sync.SyncError(f"飞书同步期间原有工单列已变更：{name}。", 409)


def batch(target, operation, records, *, required=()):
    if records:
        names = {name for row in records for name in row["fields"]}
        if isinstance(target, Target) and names - target.approved.keys():
            raise sync.SyncError("飞书工单更新包含只读或未绑定的列。", 409)
        require(target, names | set(required), refresh=True)
    sync._batch(target, operation, records)
