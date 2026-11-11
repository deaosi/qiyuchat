"""Read-only projections for the high-frequency issue workspace."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import re

from . import voc_quality as quality, voc_variants as variants
from .voc_domain import _message_refs


ZONE = timezone(timedelta(hours=8), "Asia/Shanghai")
_DAY = timedelta(days=1)
_CLASSIFIED = frozenset({"done", "excluded"})


@dataclass(frozen=True)
class Period:
    start: date | None
    end: date | None

    def __post_init__(self) -> None:
        # This also validates that the exclusive upper bound is representable.
        if self.end is not None:
            _ = self.end + _DAY

    def bounds(self) -> tuple[int, int]:
        return tuple(int(datetime(day.year, day.month, day.day, tzinfo=ZONE).timestamp() * 1000) if day else 0
                     for day in (self.start, self.end + _DAY if self.end else None))

    def rows(self, values: dict[int, dict]) -> dict[int, dict]:
        low, high = self.bounds()
        if not (low or high):
            return values
        return {key: row for key, row in values.items() if row["asked_at"] > 0
                and (not low or row["asked_at"] >= low) and (not high or row["asked_at"] < high)}

    def public(self, sources: dict[int, dict], states: dict[int, str]) -> dict:
        return {"start": self.start.isoformat() if self.start else "", "end": self.end.isoformat() if self.end else "",
                "source_pairs": len(sources), "processed_pairs": sum(states.get(qa_id) in _CLASSIFIED for qa_id in sources)}


def periods(filters: dict) -> tuple[Period, Period | None]:
    try:
        days = int(filters.get("days") or 0)
        if not 0 <= days <= 3660:
            raise ValueError("日期范围须在 0–3660 天之间。")
        first = date.fromisoformat(filters["start"]) if filters.get("start") else None
        last = date.fromisoformat(filters["end"]) if filters.get("end") else None
        if first and last and first > last:
            raise ValueError("开始日期不能晚于结束日期。")
        if not first and not last and days:
            last = datetime.now(ZONE).date()
            first = last - _DAY * (days - 1)
        current = Period(first, last)
        previous = Period(first - (last - first + _DAY), first - _DAY) if first and last else None
    except (TypeError, OverflowError) as exc:
        raise ValueError("日期范围无效。") from exc
    return current, previous


def comparison(current: dict, previous: dict | None, last_synced_at: str) -> dict:
    comparable = False
    if previous is None:
        note = "选择有起止日期的范围后，可对比相邻等长周期。"
    elif not previous["source_pairs"]:
        note = "上一周期暂无入库来源，趋势待积累。"
    elif not current["source_pairs"]:
        note = "当前周期暂无入库来源。"
    else:
        current_rate = current["processed_pairs"] / current["source_pairs"]
        previous_rate = previous["processed_pairs"] / previous["source_pairs"]
        comparable = (min(current["processed_pairs"], previous["processed_pairs"]) >= 5
                      and min(current_rate, previous_rate) >= .5 and abs(current_rate - previous_rate) <= .2 + 1e-9)
        if not comparable:
            note = "已分类样本量不足或两期覆盖差异较大，数量仅反映已整理来源。"
        else:
            coverage = "已整理样本，非全量。" if min(current_rate, previous_rate) < 1 else "按当前入库并已分类的来源比较。"
            note = coverage + "上升需多至少 2 次，且每百条已分类来源中的咨询次数增加。"
    return {"current": current, "previous": previous, "comparable": comparable,
            "note": note, "last_synced_at": last_synced_at}


def trend(current: int, previous: int, observed_earlier: bool, scope: dict) -> dict:
    prior = scope["previous"]
    comparable = scope["comparable"]
    observed_previous = previous if prior and prior["processed_pairs"] else None
    delta = current - observed_previous if observed_previous is not None else None
    status = "unknown"
    if comparable:
        status = "stable"
        if previous == 0 and not observed_earlier:
            status = "new"
        elif delta >= 2 and current * prior["processed_pairs"] > previous * scope["current"]["processed_pairs"]:
            status = "rising"
        elif min(current, previous) >= 2:
            status = "persistent"
    return {"status": status, "previous_consultation_count": observed_previous, "delta": delta}


_PLATFORM_NAMES = (
    ("淘宝", ("淘宝", "手淘")), ("天猫", ("天猫",)), ("京东", ("京东",)),
    ("拼多多", ("拼多多",)), ("抖音", ("抖音", "飞鸽")), ("快手", ("快手",)),
    ("小红书", ("小红书",)), ("微信小店", ("微信小店", "视频号小店")),
    ("闲鱼", ("闲鱼",)), ("得物", ("得物",)),
)
_COMMERCE_SETTING = re.compile(r"(?:商品|电商|商家|店铺|淘宝|天猫|京东|拼多多|抖音|快手|小红书|微信小店|视频号|平台).{0,8}后台|商品.{0,8}(?:属性|规格|详情|资料|信息).{0,10}(?:设置|配置|录入|填写|维护|同步)")
_SCENARIO_SETTING = re.compile(r"(?:知汇鸽|机器人).{0,16}(?:场景|触发条件|开关|规则|限定|关键词|配置|设置)|场景.{0,10}(?:条件|开关|规则|限定|配置|设置|启用|开启|关闭|绑定)")
_CHECKED = re.compile(r"已(?:经)?(?:核对|核实|检查|确认)|(?:核对|核实|检查)过|复核")
_CORRECT = re.compile(r"(?:设置|配置|条件|规则|开关)(?:均|都|已|是|为|检查|核对|\s){0,6}(?:无误|正确|齐全|正常|一致)")
_REPRODUCED = re.compile(r"(?:复测|再次测试)(?:后|结果|显示|，|仍然|仍|依旧|依然|确实|当前|了|已|\s){0,16}(?:未触发|不触发|没触发|未命中|不命中|无回复|不回复|失败)")
_UNCERTAIN = re.compile(r"请|建议|如果|假如|可能|应该|看起来|待(?:核对|核实|检查|确认|复测)|(?:未|没|无法|尚未|还未|不能)(?:核对|核实|检查|确认|复测)|(?:尚未|未能|没有|不能|无法)复现|(?:商家|客户|您|你)(?:称|说|反馈)|转述|[？?]")


def _exact_ref(row: dict, ref: dict | None) -> dict | None:
    if not isinstance(ref, dict):
        return None
    side, start, end, quote = (ref.get(key) for key in ("source", "start", "end", "quote"))
    source = row["source"]
    text = source.get(side, "") if side in ("question", "answer") else ""
    if (not isinstance(quote, str) or type(start) is not int or type(end) is not int
            or ref.get("alignment") != "exact" or not 0 <= start < end <= len(text) or text[start:end] != quote
            or not variants._ref_bound(row, side, start, end)):
        return None
    return {"source": side, "quote": quote, "start": start, "end": end, "alignment": "exact",
            "role": "staff" if side == "answer" else "merchant", "source_refs": _message_refs(source, side, start, end)}


def platforms(rows: list[dict]) -> list[str]:
    quotes = []
    for row in rows:
        for ref in row["data"].get("domain", {}).get("platforms", []):
            checked = _exact_ref(row, ref)
            if checked:
                quotes.append(checked["quote"])
    return [name for name, aliases in _PLATFORM_NAMES if any(alias in quote for alias in aliases for quote in quotes)]


def case_details(row: dict) -> dict:
    data, source = row["data"], row["source"]
    domain = data.get("domain", {})
    diagnosis = domain.get("diagnosis", {})
    raw_refs = [*domain.get("reproduction_context", []), *domain.get("prerequisites", []),
                diagnosis.get("cause"), *diagnosis.get("evidence", [])]
    settings = {}
    for item in raw_refs:
        ref = _exact_ref(row, item)
        if ref:
            settings[ref["source"], ref["start"], ref["end"]] = ref
    commerce = [ref for ref in settings.values() if _COMMERCE_SETTING.search(ref["quote"])]
    scenario = [ref for ref in settings.values() if _SCENARIO_SETTING.search(ref["quote"])]
    actual = _exact_ref(row, domain.get("actual"))
    reproduced = [ref for ref in [actual, *settings.values()]
                  if ref and ref["role"] == "staff" and ref["source_refs"]
                  and _REPRODUCED.search(ref["quote"]) and not _UNCERTAIN.search(ref["quote"])]

    def checked(refs: list[dict]) -> bool:
        return any(ref["role"] == "staff" and ref["source_refs"] and _CHECKED.search(ref["quote"])
                   and _CORRECT.search(ref["quote"]) and not _UNCERTAIN.search(ref["quote"]) for ref in refs)

    ready = (domain.get("primary_intent") == "functional_anomaly" and diagnosis.get("status") in ("pending", "reported")
             and checked(commerce) and checked(scenario) and bool(reproduced))
    actual_refs = list({(ref["source"], ref["start"], ref["end"]): ref
                        for ref in [actual, *reproduced] if ref}.values())
    question = data["question_span"]
    anchors = _message_refs(source, "question", question["start"], question["end"])
    result = {"commerce_settings": commerce, "scenario_conditions": scenario, "actual": actual_refs,
              "suspected_report_ready": ready}
    if anchors:
        result["question_message_id"] = anchors[0]["message_id"]
    return result


def reuse_level(item: dict) -> str:
    """Presentation labels; only the canonical reuse decision grants direct use."""
    reuse = item.get("knowledge_reuse") or {}
    if reuse.get("level") in ("verified", "guidance"):
        return "direct"
    certificate = item.get("conversation_verification") or {}
    acceptance, effect = certificate.get("acceptance"), certificate.get("effect")
    status = certificate.get("status")
    if (status in ("negative", "rejected", "continuing") or acceptance in ("rejected", "continuing")
            or effect == "customer_reported_failure" or "conflict" in str(item.get("kind", ""))
            or any("conflict" in str(reason) for reason in certificate.get("reason_codes", []))):
        return "blocked"
    if reuse.get("level") == "reference" and status == "accepted":
        return "reference"
    return "history"


def gaps(rows: list[dict], knowledge: list[dict], assets: list[dict]) -> list[dict]:
    results = defaultdict(set)
    by_id = {row["id"]: variants.consultation_key(row) for row in rows}
    for row in rows:
        key, source = by_id[row["id"]], row["source"]
        if not source["answer"]:
            results["no_staff_reply"].add(key)
        elif not source["_staff_verified"]:
            results["unverified_staff_reply"].add(key)
        domain = row["data"].get("domain", {})
        if domain.get("primary_intent") == "functional_anomaly" and domain.get("diagnosis", {}).get("status") in ("reported", "pending"):
            results["insufficient_diagnosis"].add(key)
    for item in knowledge:
        level = item["reuse_level"]
        if level != "direct":
            code = "conflicting_evidence" if level == "blocked" else "closure_unconfirmed"
            results[code].update(by_id[ref["event_id"]] for ref in item["evidence"] if ref["event_id"] in by_id)
    for item in assets:
        if not item["stale"] and reuse_level(item) == "blocked":
            results["conflicting_evidence"].update(by_id[ref["event_id"]] for ref in item["evidence"] if ref["event_id"] in by_id)
    return [{"code": code, "count": len(keys)} for code, keys in sorted(results.items()) if keys]


def participants(rows: list[dict]) -> tuple[int, int]:
    known = {row["source"]["_known_merchant"] for row in rows if row["source"].get("_known_merchant")}
    unidentified = {quality.session_key(row["source"]) for row in rows if not row["source"].get("_known_merchant")}
    return len(known), len(unidentified)
