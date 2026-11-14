"""Publish the full rolling VOC ranking and its Top 100 through Feishu CLI."""

from __future__ import annotations

import argparse
from datetime import date, timedelta
from hashlib import sha256
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from deploy.feishu_sync import dashboard, source, support, top as top_table
from src import voc_feishu_sync as sync

BATCH_SIZE = 100
REGISTER_LIMIT = 1500
UPDATED_FIELD = "最近更新时间"
DISPLAY_LINK_FIELD = "语海详情"
TOP_DISPLAY_LINK_FIELD = "点击查看详情"
_SYNC_ERRORS = (ValueError, TypeError, KeyError, OSError, sync.SyncError)


def _validate(snapshot):
    topics, sops = (snapshot["documents"][kind] for kind in ("topics", "sops"))
    start, end = map(date.fromisoformat, (topics["start"], topics["end"]))
    if (snapshot["filters"] != {"days": 7} or not support._valid_marker(snapshot["marker"])
            or end - start != timedelta(days=6) or snapshot["marker"][:10] != end.isoformat()
            or (sops["start"], sops["end"]) != (topics["start"], topics["end"])
            or not topics["records"] or topics["total"] != topics["count"]):
        raise ValueError("全量成果的统计范围无法核对。")
    seen_keys, seen_urls = set(), set()
    for kind, document in (("topics", topics), ("sops", sops)):
        if document["count"] != len(document["records"]):
            raise ValueError("成果数量与发布清单不一致。")
        previous = float("inf")
        for item in document["records"]:
            if (not sync._SOURCE_ID[kind].fullmatch(item["key"]) or item["key"] in seen_keys
                    or not sync._GUEST_URL.fullmatch(item["url"]) or item["url"] in seen_urls
                    or not all(isinstance(item[name], str) and item[name].strip()
                               for name in ("title", "summary", "detail"))):
                raise ValueError("成果存在重复或无效的编号、字段或访客链接。")
            seen_keys.add(item["key"])
            seen_urls.add(item["url"])
            if kind == "topics":
                count = item["consultations"]
                if type(count) is not int or not 0 < count <= previous:
                    raise ValueError("全量高频榜的咨询次数或排序无效。")
                previous = count
    return snapshot


def _read_rows(target, *, creation_times=None):
    rows, links, tokens = {}, {}, set()
    params = {"page_size": 500}
    data = {"automatic_fields": True} if creation_times is not None else {}
    while True:
        page = sync._call(target, "POST", "/records/search", params=params, data=data)
        if not isinstance(page.get("items"), list) or type(page.get("has_more")) is not bool:
            raise ValueError("飞书记录分页结果不完整。")
        for item in page["items"]:
            record_id, fields = item["record_id"], item["fields"]
            if not sync._RECORD_ID.fullmatch(record_id) or record_id in rows or not isinstance(fields, dict):
                raise ValueError("飞书记录无法唯一核对。")
            if creation_times is not None:
                created = item.get("created_time")
                if type(created) is not int or created <= 0:
                    raise ValueError("飞书未返回可核对的首次登记时间。")
                creation_times[record_id] = created
            fields = {name: value for name, value in fields.items() if name != UPDATED_FIELD}
            rows[record_id] = fields
            url = support._source_link(target, fields)
            if isinstance(url, str) and url:
                links.setdefault(url, []).append(record_id)
        if not page["has_more"]:
            return rows, links
        token = page.get("page_token")
        if not isinstance(token, str) or not token or token in tokens:
            raise ValueError("飞书记录分页游标无效。")
        tokens.add(token)
        params["page_token"] = token


def _display_link(key, url):
    return {"text": "查看 SOP" if key.startswith("sop_") else "查看问题", "link": url}


def _project(target, snapshot, *, top=False, display_field=DISPLAY_LINK_FIELD):
    doc = snapshot["documents"]["topics"]
    records = doc["records"][:100] if top else doc["records"]
    fields = target.fields
    desired = {}
    for rank, item in enumerate(records, start=1):
        _, _, questions = item["detail"].partition("\n具体问法\n")
        question = item.get("representative_question", "")
        detail = questions or (question if question != item["title"] else "") or "具体咨询与会话上下文见访客页。"
        row = {fields["title"]: item["title"], fields["detail"]: detail,
               fields["summary"]: item["summary"], fields["source"]: item["url"]}
        if top:
            row[fields["summary"]] = item["summary"].partition(" · ")[2]
            row.update({fields["rank"]: rank, fields["consultations"]: item["consultations"],
                        fields["period"]: f'{doc["start"]} 至 {doc["end"]}'})
        key = item.get("register_key", item["key"])
        if not sync._SOURCE_ID["topics"].fullmatch(key) or key in desired:
            raise ValueError("高频问题登记编号重复或无效。")
        if display_field:
            row[display_field] = _display_link(key, item["url"])
        desired[key] = row
    if not top:
        for item in snapshot["documents"]["sops"]["records"]:
            desired[item["key"]] = {fields["title"]: item["title"], fields["detail"]: item["detail"],
                                    fields["summary"]: item["summary"], fields["source"]: item["url"]}
            if display_field:
                desired[item["key"]][display_field] = _display_link(item["key"], item["url"])
    return desired


def _match_url(target, fields, links):
    found = links.get(support._source_link(target, fields), [])
    if len(found) > 1:
        raise ValueError("飞书已有重复的访客链接，无法确认记录归属。")
    return found[0] if found else None


def _adopt_legacy(target, rows, links, previous):
    schema = support._schema(target)
    if not support._compact(target, schema):
        raise ValueError("原飞书表的四列同步结构已变化。")
    all_fields = {**support._mapped(previous, "topics", target, schema),
                  **support._published_sops(previous, target, schema)}
    known = {}
    for key, fields in all_fields.items():
        record_id = _match_url(target, fields, links)
        if not record_id or not support._compact_matches(target, fields, rows[record_id],
                                                        legacy="topic_row_ids" not in previous):
            raise ValueError("原飞书表未能与已确认的同步记录匹配。")
        short_key = key.split(":")[1]
        known[short_key] = {"id": record_id, "fields": {name: rows[record_id].get(name) for name in fields}}
    return known


def _load_table_state(path, target):
    value = support._read_state(path)
    if value["table"] != target.table_id or not isinstance(value["records"], dict):
        raise ValueError("飞书发布状态与目标表不一致。")
    ids = [entry["id"] for entry in value["records"].values()]
    if len(set(ids)) != len(ids) or any(not sync._RECORD_ID.fullmatch(record_id) for record_id in ids):
        raise ValueError("飞书发布状态包含重复或无效的记录 ID。")
    return value["records"]


def _topic_members(snapshot):
    result = {}
    for item in snapshot["documents"]["topics"]["records"]:
        members = item.get("member_keys")
        if members is None:
            continue
        if (not isinstance(members, list) or not members or item["key"] not in members
                or any(not sync._SOURCE_ID["topics"].fullmatch(key) for key in members)):
            raise ValueError("高频问题的聚合来源无法核对。")
        result[item.get("register_key", item["key"])] = set(members)
    return result


def _reconcile_topics(known, previous_members, current_members, incoming, *, age):
    by_member = {}
    for key, members in current_members.items():
        for member in members:
            by_member.setdefault(member, set()).add(key)
    replacements = {}
    for old in known.keys() - current_members.keys():
        members = previous_members.get(old)
        if not members:
            continue
        destinations = set.intersection(*(by_member.get(member, set()) for member in members))
        if len(destinations) == 1:
            new = next(iter(destinations))
            if new in incoming and current_members[new] >= members:
                replacements.setdefault(new, []).append(old)
    merged = set()
    for new, originals in replacements.items():
        survivor = new if new in known else min(originals, key=age)
        if survivor != new:
            known[new] = known.pop(survivor)
        merged.update(old for old in originals if old != survivor)
    return merged


def _register_plan(target, path, snapshot, current, times, known, incoming, merged):
    state = support._read_state(path)
    retired = set(state.get("retired", []))
    if any(not sync._SOURCE_ID["topics"].fullmatch(key) for key in retired):
        raise ValueError("登记表淘汰记录无法核对。")
    intent_path = path.with_suffix(".fifo.json")
    if intent_path.exists():
        intent = support._read_state(intent_path)
        if intent["table"] != target.table_id or intent["marker"] != snapshot["marker"]:
            raise ValueError("登记表的待淘汰清单与当前同步不一致。")
        excluded = set(intent["excluded"])
        evict = intent["evict"]
        merged = set(intent.get("merged", []))
        if any(key not in known or known[key]["id"] != record_id for key, record_id in evict.items()):
            # The state can already be committed if interruption happened during journal cleanup.
            if not (state.get("marker") == snapshot["marker"] and set(evict) <= retired | merged):
                raise ValueError("登记表的待淘汰记录与已确认记录不一致。")
            evict = {}
        retired.update(excluded, evict.keys() - merged)
    else:
        owned = {entry["id"] for entry in known.values()}
        if not owned <= current.keys():
            raise ValueError("登记表有已同步记录被移除，无法核对淘汰顺序。")
        manual = len(current) - len(owned)
        protected = sum(not key.startswith("t_") for key in known)
        new = [key for key in incoming if key not in known and key not in retired and key.startswith("t_")]
        new_sops = sum(key.startswith("sop_") and key not in known for key in incoming)
        capacity = REGISTER_LIMIT - manual - protected - new_sops
        if capacity < 0:
            raise ValueError("登记表已有的非高频记录占满容量。")
        excluded = set(new[:max(0, len(new) - capacity)])
        overflow = max(0, len(current) - len(merged) + len(new) - len(excluded) + new_sops - REGISTER_LIMIT)
        oldest = sorted((key for key in known if key.startswith("t_") and key not in merged),
                        key=lambda key: (times[known[key]["id"]], known[key]["id"]))
        if overflow > len(oldest):
            raise ValueError("登记表没有足够的可裁剪记录。")
        evict = {key: known[key]["id"] for key in (*sorted(merged), *oldest[:overflow])}
        retired.update(excluded, evict.keys() - merged)
    desired = {key: incoming.get(key, entry["fields"]) for key, entry in known.items()
               if key not in retired and key not in merged}
    desired.update({key: fields for key, fields in incoming.items() if key not in retired})
    for key, record_id in evict.items():
        if record_id in current and (not support._matches(target, known[key]["fields"], current[record_id])
                                     or set(current[record_id]) - set(known[key]["fields"])):
            raise ValueError("最早登记的记录含手动修改或额外字段，已保留。")
    return desired, retired, evict, excluded, merged, intent_path


def _batches(items):
    for start in range(0, len(items), BATCH_SIZE):
        yield items[start:start + BATCH_SIZE]


def _plan(target, current, links, known, desired, pending_create):
    owned_ids = {entry["id"] for entry in known.values()}
    reserved = {key: entry["id"] for key, entry in known.items() if key in desired}
    available = []
    for key, entry in known.items():
        record_id = entry["id"]
        if key not in desired and record_id in current:
            available.append(record_id)
    fresh = [key for key in desired if key not in reserved]
    # Recycling displaced rows preserves Feishu record IDs and keeps the ranking compact.
    for key, record_id in zip((key for key in fresh if key not in pending_create), available):
        reserved[key] = record_id
    before = {entry["id"]: entry["fields"] for entry in known.values()}
    updates, creates = [], []
    for key, fields in desired.items():
        record_id = reserved.get(key)
        linked = _match_url(target, fields, links)
        if not record_id and linked and linked not in owned_ids:
            if (key not in pending_create or set(current[linked]) - set(fields)
                    or not support._matches(target, fields, current[linked])):
                raise ValueError("飞书同链接记录的内容与发布计划冲突。")
            reserved[key] = linked
            record_id = linked
        if linked and linked != record_id:
            raise ValueError("飞书访客链接与预定记录 ID 冲突。")
        if not record_id:
            creates.append(key)
            continue
        if record_id not in current:
            raise ValueError("飞书已有同步记录已被移除，无法确认原位更新。")
        row = current[record_id]
        if not support._matches(target, fields, row):
            if (record_id not in before or not support._matches(target, before[record_id], row)
                    or (known.get(key, {}).get("id") != record_id and set(row) - set(fields))):
                raise ValueError("飞书现有同步记录有外部编辑或附加数据，本轮暂停。")
            changes = {name: value for name, value in fields.items()
                       if not support._same(target, name, row.get(name), value)}
            updates.append({"record_id": record_id, "fields": changes})
    remove = []
    for record_id in sorted(owned_ids - set(reserved.values())):
        row = current.get(record_id)
        if row is None:
            continue
        if not support._matches(target, before[record_id], row) or set(row) - set(before[record_id]):
            raise ValueError("掉榜记录含手动修改或额外内容，本轮保留并暂停。")
        remove.append(record_id)
    protected = {record_id: fields for record_id, fields in current.items()
                 if record_id not in owned_ids and record_id not in reserved.values()}
    return reserved, updates, creates, remove, protected


def _confirm(target, desired, reserved, protected, *, retain=(), original=None):
    rows, links = _read_rows(target)
    support.confirm_unchanged(rows, protected)
    state = {}
    for key, fields in desired.items():
        record_id = _match_url(target, fields, links)
        if (not record_id or record_id in protected or (key in reserved and reserved[key] != record_id)
                or not support._matches(target, fields, rows[record_id])):
            raise ValueError("飞书回读的记录 ID 或内容与本轮不一致。")
        if original is not None and record_id in original:
            extra = set(rows[record_id]) | set(original[record_id])
            if any(rows[record_id].get(name) != original[record_id].get(name) for name in extra - set(fields)):
                raise ValueError("飞书记录的非同步字段在同步期间发生变化。")
        state[key] = {"id": record_id, "fields": fields}
    if set(rows) != set(protected) | {entry["id"] for entry in state.values()} | set(retain):
        raise ValueError("飞书回读存在超出本次计划的记录。")
    return state


def _create_body(keys, desired):
    return [{"fields": desired[key]} for key in keys]


def _create_digest(keys, desired):
    body = json.dumps(_create_body(keys, desired), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(body.encode("utf-8")).hexdigest()


def _load_create_intent(path, target, marker, desired):
    if not path.exists():
        return set(), [], False
    intent = support._read_state(path)
    if intent["table"] != target.table_id or intent["marker"] != marker:
        raise ValueError("飞书待写记录与当前发布计划不一致。")
    if "batches" not in intent:
        keys = intent["creates"]
        if not isinstance(keys, list) or not set(keys) <= desired.keys():
            raise ValueError("飞书待写记录与当前发布计划不一致。")
        return set(keys), [], True
    batches = intent["batches"]
    keys_seen = set()
    if not isinstance(batches, list):
        raise ValueError("飞书新增批次清单无效。")
    for batch in batches:
        keys = batch["keys"]
        if (not isinstance(keys, list) or not 1 <= len(keys) <= BATCH_SIZE
                or len(set(keys)) != len(keys) or not set(keys) <= desired.keys() or keys_seen.intersection(keys)
                or batch["digest"] != _create_digest(keys, desired)):
            raise ValueError("飞书新增批次清单与当前发布计划不一致。")
        keys_seen.update(keys)
    return keys_seen, batches, False


def _sync_table(target, path, snapshot, previous, *, top=False, allow_writes=True, display_field=DISPLAY_LINK_FIELD):
    times = {} if not top else None
    current, links = _read_rows(target, creation_times=times) if not top else _read_rows(target)
    if path.exists():
        known = _load_table_state(path, target)
        previous_state = support._read_state(path)
    elif top:
        known = {}
        previous_state = {}
    else:
        known = _adopt_legacy(target, current, links, previous)
        previous_state = {"table": target.table_id, "records": known}
        support._write_state(path, previous_state)
    intent_path = path.with_suffix(".pending.json")
    pending_field = support._read_state(intent_path).get("display_field", DISPLAY_LINK_FIELD) if intent_path.exists() else None
    if display_field and pending_field and display_field != pending_field:
        raise ValueError("飞书超链接列在新增批次确认前改名，请先核对待写记录。")
    _match_display_field(known, current, display_field)
    desired = _project(target, snapshot, top=top, display_field=display_field)
    if not previous_state.get("stable_keys") and (top or not previous_state.get("fifo")):
        identities = {item["key"]: item.get("register_key", item["key"])
                      for data in (snapshot, previous) for item in data["documents"]["topics"]["records"]}
        mapped = {identities.get(key, key): entry for key, entry in known.items()}
        if len(mapped) != len(known):
            raise ValueError("历史登记编号无法一一对应。")
        known = mapped
    current_members = _topic_members(snapshot)
    old_members = {**_topic_members(previous),
                   **{key: set(members) for key, members in previous_state.get("lineage", {}).items()}}
    if top:
        _reconcile_topics(known, old_members, current_members, desired, age=lambda key: known[key]["id"])
    else:
        candidates = desired.keys() - set(previous_state.get("retired", []))
        merged = _reconcile_topics(known, old_members, current_members, candidates,
                                   age=lambda key: (times.get(known[key]["id"], 0), known[key]["id"]))
    evict = {}
    if not top:
        desired, retired, evict, excluded, merged, fifo_path = _register_plan(
            target, path, snapshot, current, times, known, desired, merged)
        known = {key: entry for key, entry in known.items() if key not in evict}
        evict_ids = set(evict.values())
        plan_rows = {record_id: fields for record_id, fields in current.items() if record_id not in evict_ids}
        plan_links = {url: [record_id for record_id in ids if record_id not in evict_ids] for url, ids in links.items()}
        if len(plan_rows) + len(desired) - len(known) > REGISTER_LIMIT and not path.with_suffix(".pending.json").exists():
            raise ValueError("登记表没有足够的新增容量。")
    else:
        plan_rows, plan_links = current, links
    pending_create, create_batches, old_intent = _load_create_intent(intent_path, target, snapshot["marker"], desired)
    reserved, updates, creates, remove, protected = _plan(target, plan_rows, plan_links, known, desired, pending_create)
    fifo_remove = sorted({record_id for key, record_id in evict.items() if key not in merged} & current.keys()) if not top else []
    merge_remove = sorted({record_id for key, record_id in evict.items() if key in merged} & current.keys()) if not top else []
    if not allow_writes and (updates or creates or remove or fifo_remove or merge_remove):
        raise ValueError("跨日发布记录尚未完整回读，已保留待恢复的同步清单。")
    missing = set(creates)
    if old_intent and missing & pending_create:
        raise ValueError("旧新增清单尚未完整回读，已保留以核对原批次。")
    new_creates = [key for key in creates if key not in pending_create]
    if not top and len(plan_rows) + len(creates) > REGISTER_LIMIT:
        raise ValueError("登记表没有足够的新增容量。")
    if not top and (evict or excluded or creates) and not fifo_path.exists():
        support._write_state(fifo_path, {"table": target.table_id, "marker": snapshot["marker"],
                                       "evict": evict, "excluded": sorted(excluded), "merged": sorted(merged)})
    if new_creates:
        create_batches.extend({"keys": keys, "digest": _create_digest(keys, desired)}
                              for keys in _batches(new_creates))
        support._write_state(intent_path, {"table": target.table_id, "marker": snapshot["marker"],
                                         "batches": create_batches, "display_links": bool(display_field),
                                         "display_field": display_field})
    for batch in _batches(updates):
        sync._batch(target, "update", batch)
    if merge_remove:
        latest, _ = _read_rows(target)
        if (any(latest.get(record_id) != current[record_id] for record_id in merge_remove)
                or any(record_id not in latest or not support._matches(target, desired[key], latest[record_id])
                       for key, record_id in reserved.items())):
            raise ValueError("合并后的记录尚未完成回读，保留原登记记录。")
        fifo_remove += merge_remove
    if fifo_remove:
        latest, _ = _read_rows(target)
        if any(latest.get(record_id) != current[record_id] for record_id in fifo_remove):
            raise ValueError("待淘汰记录发生变化，已保留。")
        _delete(target, fifo_remove)
        remaining, _ = _read_rows(target)
        if set(fifo_remove).intersection(remaining) or len(remaining) + len(creates) > REGISTER_LIMIT:
            raise ValueError("登记表尚未腾出足够空间，等待回读后续跑。")
    for batch in create_batches:
        keys = batch["keys"]
        if missing.intersection(keys):
            sync._batch(target, "create", _create_body(keys, desired))
    state = _confirm(target, desired, reserved, protected, retain=remove, original=current)
    if remove:
        current, _ = _read_rows(target)
        prior_by_id = {entry["id"]: entry["fields"] for entry in known.values()}
        if any(record_id not in current or not support._matches(target, prior_by_id[record_id], current[record_id])
               or set(current[record_id]) - set(prior_by_id[record_id]) for record_id in remove):
            raise ValueError("掉榜记录在同步期间被修改，已保留。")
        _delete(target, remove)
        state = _confirm(target, desired, reserved, protected, original=current)
    lineage = {key: sorted(current_members.get(key) or old_members[key]) for key in state
               if key in current_members or key in old_members}
    committed = {"table": target.table_id, "records": state, "stable_keys": True, "lineage": lineage,
                 "display_links": bool(display_field)}
    if not top:
        committed.update(fifo=True, retired=sorted(retired), marker=snapshot["marker"])
    support._write_state(path, committed)
    intent_path.unlink(missing_ok=True)
    if not top:
        fifo_path.unlink(missing_ok=True)
    return {"topics": sum(key.startswith("t_") for key in state), "sops": sum(key.startswith("sop_") for key in state),
            "updated": len(updates), "created": len(creates), "removed": len(remove) + len(fifo_remove), "other": len(protected)}


def _delete(target, record_ids):
    for batch in _batches(record_ids):
        result = sync._call(target, "POST", "/records/batch_delete", data={"records": batch})
        if {item["record_id"] for item in result.get("records", []) if item.get("deleted") is True} != set(batch):
            raise ValueError("飞书未确认移除全部计划记录。")


def _ensure_updated_field(target):
    args = ("--table-id", target.table_id)

    def get_field():
        fields = sync._paginated(target, "GET", "/fields")
        return next((item for item in fields if item.get("field_name") == UPDATED_FIELD), None)

    field = get_field()
    if field is None:
        definition = {"type": "updated_at", "name": UPDATED_FIELD, "style": {"format": "yyyy/MM/dd HH:mm"}}
        top_table._cli(target, "+field-create", *args, "--json", json.dumps(definition, ensure_ascii=False))
        field = get_field()
    if field is None or field.get("type") != 1002:
        raise ValueError("登记表最近更新时间未能确认为飞书系统字段。")


def _ensure_display_field(target, *, top=False):
    def get_field():
        fields = {row.get("field_name"): row for row in sync._paginated(target, "GET", "/fields")}
        return (fields.get(TOP_DISPLAY_LINK_FIELD) if top else None) or fields.get(DISPLAY_LINK_FIELD)

    field = get_field()
    if field is None:
        definition = {"name": DISPLAY_LINK_FIELD, "type": "text", "style": {"type": "url"}}
        top_table._cli(target, "+field-create", "--table-id", target.table_id,
                       "--json", json.dumps(definition, ensure_ascii=False))
        field = get_field()
    if field is None or field.get("type") != 15:
        raise ValueError("飞书「语海详情」列须使用超链接类型。")
    return field["field_name"]


def _match_display_field(known, current, display_field):
    if display_field != TOP_DISPLAY_LINK_FIELD:
        return
    for entry in known.values():
        fields = entry["fields"]
        if DISPLAY_LINK_FIELD not in fields:
            continue
        row = current.get(entry["id"], {})
        value = fields[DISPLAY_LINK_FIELD]
        if (display_field in fields or sync._value(row.get(display_field)) != sync._value(value)
                or sync._value(row.get(DISPLAY_LINK_FIELD))):
            raise ValueError("飞书排名表超链接列改名后内容无法核对。")
        fields[display_field] = fields.pop(DISPLAY_LINK_FIELD)


def _backfill_display_links(target, path, display_field=DISPLAY_LINK_FIELD):
    """Format known guest URLs in place and keep the record ownership journal current."""
    known = _load_table_state(path, target)
    state = support._read_state(path)
    current, _ = _read_rows(target)
    _match_display_field(known, current, display_field)
    desired, updates = {}, []
    for key, entry in known.items():
        record_id, fields = entry["id"], entry["fields"]
        actual = current.get(record_id)
        url = support._source_link(target, fields)
        if actual is None or not url or not sync._GUEST_URL.fullmatch(url) or url != support._source_link(target, actual):
            raise ValueError("飞书历史记录的访客链接已变化，无法安全迁移。")
        value = _display_link(key, url)
        desired[record_id] = value
        if sync._value(actual.get(display_field)) != value:
            updates.append({"record_id": record_id, "fields": {display_field: value}})

    for batch in _batches(updates):
        sync._batch(target, "update", batch)
    latest, _ = _read_rows(target) if updates else (current, None)
    if set(latest) != set(current) or any(
        {name: value for name, value in row.items() if name != display_field}
        != {name: value for name, value in current[record_id].items() if name != display_field}
        or (record_id in desired and sync._value(row.get(display_field)) != desired[record_id])
        or (record_id not in desired and row.get(display_field) != current[record_id].get(display_field))
        for record_id, row in latest.items()
    ):
        raise ValueError("飞书历史链接未完整回读，请重试核对。")
    for entry in known.values():
        entry["fields"][display_field] = desired[entry["id"]]
    updated_state = {**state, "records": known, "display_links": True}
    if updated_state != state:
        support._write_state(path, updated_state)
    return {"verified": len(desired), "updated": len(updates),
            "topics": sum(key.startswith("t_") for key in known), "sops": sum(key.startswith("sop_") for key in known)}


def _links_only(state_dir):
    support._safe_state(state_dir)
    target = support.target_table(state_dir)
    with sync._write_lock(target.lock_path):
        if any((state_dir / name).exists() for name in (
            "full-pending.json", "full-table.pending.json", "full-table.fifo.json", "top-table.pending.json",
        )):
            raise ValueError("飞书已有待完成的发布批次，请在其完成后再转换链接展示。")
        top_target = top_table.ensure_top(target, apply=False)
        if top_target is None:
            raise ValueError("飞书高频问题 Top 100 表尚未创建。")
        paths = ((target, state_dir / "full-table.json"), (top_target, state_dir / "top-table.json"))
        for table, path in paths:
            _load_table_state(path, table)
        fields = (_ensure_display_field(target), _ensure_display_field(top_target, top=True))
        return {"status": "links_updated", "all": _backfill_display_links(*paths[0], fields[0]),
                "top": _backfill_display_links(*paths[1], fields[1])}


def run(state_dir, apply, *, refresh=False, dashboard_only=False):
    support._safe_state(state_dir)
    target = support.target_table(state_dir)
    original_roles = ("title", "detail", "summary", "source")
    previous_path = state_dir / "published.json"
    published = state_dir / "full-published.json"
    pending = state_dir / "full-pending.json"
    full_state = state_dir / "full-table.json"
    top_state = state_dir / "top-table.json"

    def update_dashboard(snapshot):
        job = source.fetch({}, job_only=True)["job"]
        timestamp = snapshot.get("published_at") or published.stat().st_mtime
        return dashboard.update(target, top_table._cli, snapshot, timestamp, job)

    def previous_snapshot():
        if published.exists():
            return _validate(support._read_state(published))
        return support._publication(support._read_state(previous_path))

    if not apply:
        preview = source.fetch(previous_snapshot())
        if "status" in preview:
            return preview
        document = preview["documents"]["topics"]
        return {"status": "ready", "period": [document["start"], document["end"]],
                "topics": document["count"], "top": min(100, document["count"]), "source": preview["source_metrics"]}

    with sync._write_lock(target.lock_path):
        previous = previous_snapshot()
        if dashboard_only:
            if not published.exists():
                raise ValueError("暂无已回读确认的飞书同步记录。")
            return {"status": "dashboard_updated", "dashboard_url": update_dashboard(previous)}
        _ensure_updated_field(target)
        ready = source.fetch({}, probe=True)
        if ready.get("status") != "ready":
            raise ValueError("语海Voc暂不可同步，稍后重试。")
        marker = ready["marker"]
        if published.exists() and support._slot_checked(previous, marker, refresh) and not pending.exists():
            count = previous.get("register_count", previous["documents"]["topics"]["count"])
            return {"status": "already_checked", "all_topics": count,
                    "top_topics": min(100, previous["documents"]["topics"]["count"]),
                    "dashboard_url": update_dashboard(previous)}
        schema = support._schema(target)
        if not support._compact(target, schema) or any(target.fields[role] not in schema for role in original_roles):
            raise ValueError("原飞书表的四列同步结构已变化。")
        top_target = top_table.ensure_top(target, apply=True)
        _ensure_updated_field(top_target)

        def publish(stage, *, allow_writes=True):
            active_states = (full_state, top_state)
            old_intent = any(path.with_suffix(".pending.json").exists()
                             and not support._read_state(path.with_suffix(".pending.json")).get("display_links")
                             for path in active_states)
            original_field = None if old_intent else _ensure_display_field(target)
            top_field = None if old_intent else _ensure_display_field(top_target, top=True)
            original_result = _sync_table(target, full_state, stage, previous, allow_writes=allow_writes,
                                          display_field=original_field)
            top_result = _sync_table(top_target, top_state, stage, previous, top=True,
                                     allow_writes=allow_writes, display_field=top_field)
            stage = {**stage, "published_at": int(time.time()), "register_count": original_result["topics"],
                     "published_sops": support._sop_inventory(previous, stage)}
            support._write_state(published, stage)
            pending.unlink()
            return {"status": "published", "period": list(support._period(stage)), "slot": stage["marker"],
                    "all": original_result, "top": top_result, "top_url": top_target.destination,
                    "dashboard_url": update_dashboard(stage)}

        if pending.exists():
            staged = _validate(support._read_state(pending))
            if staged["marker"] > marker:
                raise ValueError("飞书待恢复清单的时间晚于当前同步时间。")
            result = publish(staged, allow_writes=staged["marker"][:10] == marker[:10])
            previous = previous_snapshot()
            if staged["marker"] == marker:
                return result
        stage = source.fetch(previous, True, marker=marker)
        if stage.get("status"):
            raise ValueError("生产侧全量快照暂不可发布，请重试。")
        _validate(stage)
        doc = stage["documents"]["topics"]
        if (stage["source_metrics"]["topics"]["with_case"] != doc["count"] or stage["marker"] != marker
                or support._period(stage)[1] < support._period(previous)[1]):
            raise ValueError("生产侧全量快照的会话依据或日期无法核对。")
        # All links are bound and checked against the guest registry at the source.
        # Also exercise the public guest projection across the full ranking.
        records = doc["records"]
        sample = {index for index in range(0, len(records), max(1, len(records) // 20))}
        sample.update((0, min(99, len(records) - 1), len(records) - 1))
        support.verify_guest_links({**doc, "records": [records[index] for index in sorted(sample)]})
        support._sop_links(stage["documents"]["sops"])
        support._write_state(pending, stage)
        return publish(stage)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--dashboard-only", action="store_true", help="Refresh the latest log without publishing the tables.")
    parser.add_argument("--tickets-only", action="store_true", help="Only synchronize the Qiyu ticket table.")
    parser.add_argument("--ticket-incremental", action="store_true", help="Use saved ticket sync checkpoints.")
    parser.add_argument("--links-only", action="store_true", help="Format existing topic and SOP guest links without changing their destinations.")
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.refresh and not args.apply:
        parser.error("--refresh requires --apply")
    if args.dashboard_only and (not args.apply or args.refresh):
        parser.error("--dashboard-only requires --apply and cannot be used with --refresh")
    if args.tickets_only and (not args.apply or args.refresh or args.dashboard_only):
        parser.error("--tickets-only requires --apply and is independent of other modes")
    if args.ticket_incremental and not args.tickets_only:
        parser.error("--ticket-incremental requires --tickets-only")
    if args.links_only and (not args.apply or args.refresh or args.dashboard_only or args.tickets_only):
        parser.error("--links-only requires --apply and is independent of other modes")
    state_dir = args.state_dir.absolute()
    try:
        if args.tickets_only:
            from deploy.feishu_sync import tickets

            result = tickets.run(state_dir, incremental=args.ticket_incremental)
        elif args.links_only:
            result = _links_only(state_dir)
        else:
            result = run(state_dir, args.apply, refresh=args.refresh, dashboard_only=args.dashboard_only)
    except _SYNC_ERRORS as exc:
        message = str(exc) if type(exc) is ValueError or isinstance(exc, sync.SyncError) else "全量同步的数据暂不可核验。"
        raise SystemExit(message) from None
    print(json.dumps(result, ensure_ascii=True))
    if result["status"] == "failed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
