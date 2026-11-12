"""Standalone, read-only presentation for the redacted VOC guest projection."""

from __future__ import annotations

from html import escape
import re


_KINDS = {"topics": "高频问题", "sops": "处理 SOP"}
_ROLES = {
    "unidentified": ("身份未确认", "neutral"),
    "user": ("客户", "customer"),
    "customer": ("客户", "customer"),
    "staff": ("客服", "staff"),
    "bot": ("机器人", "neutral"),
    "system": ("系统", "neutral"),
}
_LEGACY_SOP_SECTIONS = {"问题现象", "适用条件", "处理步骤", "处理流程", "人工回复记录", "复测结果", "待补证据"}
_STEP_SECTION_NAMES = {"处理步骤", "处理流程", "客服给出的处理步骤"}
_STEP_PREFIX = re.compile(r"^\s*\d+\s*(?:[.．)]\s+|、\s*)")

_STYLE = """
:root{color-scheme:light;--paper:#fff;--surface:#fafafa;--muted:#f5f5f5;--ink:#171717;--subtle:#595959;--line:#e6e6e6;--strong:#d4d4d4;--radius:12px;font-family:"Geist","PingFang SC","Microsoft YaHei",system-ui,sans-serif;color:var(--ink);background:var(--surface);line-height:1.6}
*,*::before,*::after{box-sizing:border-box}
body{margin:0}
h1,h2,h3,h4,p,dl,dd,ol,ul{margin:0}
ol,ul{padding:0}
time,.num{font-variant-numeric:tabular-nums}
.masthead{border-bottom:1px solid var(--line);background:var(--paper)}
.masthead-inner,.page{width:min(1120px,100%);margin-inline:auto;padding-inline:clamp(16px,4vw,36px)}
.masthead-inner{display:flex;min-height:58px;align-items:center;justify-content:space-between;gap:12px}
.brand{display:flex;align-items:center;gap:9px;font-size:.875rem;font-weight:700}
.brand-mark{width:24px;height:24px;display:grid;place-items:center;background:var(--ink);border-radius:6px;color:#fff;font-size:.65rem;letter-spacing:-.05em}
.view-label,.meta,.note{color:var(--subtle);font-size:.8125rem}
.page{padding-block:clamp(16px,4vw,32px) 56px;display:grid;gap:20px}
.hero,.panel,.case{background:var(--paper);border:1px solid var(--line);border-radius:var(--radius)}
.hero{padding:clamp(18px,4vw,30px);background:linear-gradient(150deg,#fff 50%,#f5f5f5)}
.hero h1{font-size:clamp(1.35rem,3vw,1.875rem);line-height:1.4;letter-spacing:-.025em;overflow-wrap:anywhere;margin:.6rem 0 .35rem}
.hero-meta,.section-title,.case-meta,.chat-meta{display:flex;flex-wrap:wrap;align-items:center;gap:8px}
.hero .summary{font-size:.9rem;color:var(--subtle)}
.chip{display:inline-flex;align-items:center;min-height:23px;padding:1px 8px;border:1px solid var(--line);border-radius:6px;background:var(--paper);color:var(--subtle);font-size:.75rem;line-height:1.45}
.chip.kind{color:#fff;background:var(--ink);border-color:var(--ink);font-weight:600}
.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(112px,1fr));gap:12px;margin-top:17px;padding-top:15px;border-top:1px solid var(--line)}
.metric dt{color:var(--subtle);font-size:.8125rem}
.metric dd{font-weight:650;font-size:1.075rem;overflow-wrap:anywhere}
.section-title{justify-content:space-between;align-items:baseline;gap:6px 10px;margin-bottom:10px}
h2{font-size:1.025rem;font-weight:650;line-height:1.45}
h3{font-size:.9375rem;font-weight:650;line-height:1.5}
.panel{padding:clamp(15px,3vw,22px)}
.taxonomy{width:100%;border-collapse:collapse;text-align:left;font-size:.875rem;table-layout:fixed}
.taxonomy th,.taxonomy td{overflow-wrap:anywhere;padding:8px 10px;vertical-align:top}
.taxonomy th{background:var(--muted);font-size:.8125rem;color:var(--subtle);font-weight:600}
.taxonomy th:first-child{width:34%;border-top-left-radius:7px}
.taxonomy th:last-child{border-top-right-radius:7px}
.taxonomy .count{width:5rem;text-align:right;white-space:nowrap}
.taxonomy tbody tr+tr{border-top:1px solid var(--line)}
.sections{display:grid;gap:14px;min-width:0}
.section-body{font-size:.9rem;white-space:pre-wrap;overflow-wrap:anywhere;display:grid;gap:.45rem}
.steps{list-style:none;counter-reset:step;display:grid;gap:12px;margin:2px 0 0 8px;padding-left:15px;border-left:1px solid var(--line)}
.steps li{position:relative;counter-increment:step;min-width:0}
.steps li::before{content:counter(step);position:absolute;left:-25px;top:1px;display:grid;place-items:center;width:19px;height:19px;border:1px solid var(--strong);border-radius:50%;background:var(--paper);color:var(--subtle);font-size:.675rem;line-height:1}
.step-label{font-size:.8125rem;font-weight:650;margin-bottom:3px}
.step-feedback{margin-top:6px;border-radius:6px;background:var(--muted);padding:6px 8px;color:var(--subtle);font-size:.8125rem}
.case-list{display:grid;gap:10px}
.case{overflow:hidden}
.case>summary{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:12px;padding:14px clamp(15px,3vw,22px);cursor:pointer;list-style:none;background:linear-gradient(180deg,#fafafa,#fff)}
.case>summary::-webkit-details-marker{display:none}
.case>summary:hover{background:var(--muted)}
.case>summary:focus-visible{outline:2px solid var(--ink);outline-offset:-2px}
.case-heading{min-width:0;display:grid;gap:5px}
.case-name{font-size:.9375rem;font-weight:650;overflow-wrap:anywhere}
.case-chev{width:8px;height:8px;border-right:1.5px solid #666;border-bottom:1.5px solid #666;transform:rotate(45deg);margin:0 3px 4px}
.case[open] .case-chev{transform:rotate(225deg);margin-bottom:0}
.case-content{border-top:1px solid var(--line);padding:clamp(14px,3vw,22px)}
.case-content.with-facts{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.25fr);gap:16px;align-items:start}
.fact{border:1px solid var(--line);background:var(--surface);border-radius:9px;padding:12px}
.fact h3{margin-bottom:6px}
.chat{min-width:0}
.chat-head{display:flex;flex-wrap:wrap;align-items:baseline;justify-content:space-between;gap:4px 12px;margin-bottom:7px}
.chat-list{list-style:none;background:var(--surface);border:1px solid var(--line);border-radius:10px;display:grid;gap:14px;margin-top:12px;padding:clamp(12px,3vw,20px)}
.chat-row{display:flex;min-width:0}
.chat-row.staff{justify-content:flex-end}
.chat-row.neutral{justify-content:center}
.chat-turn{display:grid;gap:4px;min-width:0;max-width:90%;justify-items:start}
.staff .chat-turn{justify-items:end}
.neutral .chat-turn{justify-items:center}
.chat-meta{gap:4px 9px;padding:0 3px;color:var(--subtle);font-size:.75rem}
.staff .chat-meta{justify-content:flex-end}
.chat-bubble{white-space:pre-wrap;overflow-wrap:anywhere;border:1px solid var(--line);border-radius:11px;background:var(--paper);padding:8px 11px;font-size:.875rem;max-width:100%}
.customer .chat-bubble{border-top-left-radius:4px}
.staff .chat-bubble{border-top-right-radius:4px;background:linear-gradient(180deg,#eeeeee,#f4f4f4);border-color:#dedede}
.neutral .chat-bubble{border:none;background:transparent;text-align:center;color:var(--subtle)}
@media(max-width:740px){.case-content.with-facts{grid-template-columns:minmax(0,1fr)}}
@media(max-width:430px){.taxonomy th,.taxonomy td{padding-inline:6px}.taxonomy .count{width:3.8rem}.chat-turn{max-width:95%}.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}}
"""


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _list(value: object) -> list:
    return value if isinstance(value, list) else []


def _count(value: object) -> str:
    return f"{value:,}" if type(value) is int and value >= 0 else ""


def _metric_value(value: object) -> str:
    return _count(value) or _text(value)


def _metrics(items: object) -> str:
    result = []
    for item in _list(items):
        if not isinstance(item, dict):
            continue
        label, value = _text(item.get("label")), _metric_value(item.get("value"))
        if label and value:
            result.append(f'<div class="metric"><dt>{escape(label)}</dt><dd class="num">{escape(value)}</dd></div>')
    return f'<dl class="metrics">{"".join(result)}</dl>' if result else ""


def _taxonomy(items: object) -> str:
    rows = []
    for item in _list(items):
        if not isinstance(item, dict):
            continue
        primary, secondary = _text(item.get("primary")), _text(item.get("secondary"))
        if primary or secondary:
            rows.append((primary, secondary, _count(item.get("count"))))
    if not rows:
        return ""
    has_count = any(count for _, _, count in rows)
    cells = []
    for primary, secondary, count in rows:
        count_cell = f'<td class="count num">{count or "—"}</td>' if has_count else ""
        cells.append(f'<tr><td>{escape(primary) or "—"}</td><td>{escape(secondary) or "—"}</td>{count_cell}</tr>')
    count_header = '<th scope="col" class="count">咨询次数</th>' if has_count else ""
    return (
        '<section class="panel" aria-labelledby="classification"><div class="section-title">'
        '<h2 id="classification">问题分类</h2></div><table class="taxonomy"><thead>'
        f'<tr><th scope="col">一级分类</th><th scope="col">二级分类</th>{count_header}</tr>'
        f'</thead><tbody>{"".join(cells)}</tbody></table></section>'
    )


def _section_lines(title: str, lines: list[str]) -> str:
    if title in _STEP_SECTION_NAMES:
        groups: list[list[str]] = []
        for line in lines:
            if title == "处理流程" and line.startswith("客户反馈：") and groups:
                groups[-1].append(f'<p class="step-feedback">{escape(line)}</p>')
                continue
            step = _STEP_PREFIX.sub("", line, count=1)
            heading, separator, text = step.partition("\n")
            if title == "处理流程" and separator and heading.startswith(("排查 ·", "处理 ·", "复测 ·", "升级处理 ·")):
                groups.append([f'<p class="step-label">{escape(heading)}</p><p>{escape(text)}</p>'])
            else:
                groups.append([f"<p>{escape(step)}</p>"])
        steps = "".join(f"<li>{''.join(parts)}</li>" for parts in groups)
        return f'<ol class="steps section-body">{steps}</ol>'
    return f'<div class="section-body">{"".join(f"<p>{escape(line)}</p>" for line in lines)}</div>'


def _sections(items: object, *, case: bool = False) -> str:
    result = []
    for item in _list(items):
        if not isinstance(item, dict):
            continue
        title = _text(item.get("title"))
        lines = [line for line in _list(item.get("lines")) if isinstance(line, str) and line.strip()]
        if title and lines:
            tag, panel = ("h3", "fact") if case else ("h2", "panel")
            result.append(
                f'<section class="{panel}"><div class="section-title"><{tag}>{escape(title)}</{tag}></div>'
                f'{_section_lines(title, lines)}</section>'
            )
    return f'<div class="sections">{"".join(result)}</div>' if result else ""


def _legacy_sections(kind: str, detail: str) -> list[dict]:
    """Give already issued, unstructured SOP snapshots the same presentation."""
    if not detail.strip():
        return []
    result: list[dict] = [{"title": "成果详情", "lines": []}]
    for line in detail.splitlines():
        line = line.strip()
        if kind == "sops" and line in _LEGACY_SOP_SECTIONS:
            result.append({"title": line, "lines": []})
        elif line:
            result[-1]["lines"].append(line)
    return result


def _chat(case: dict) -> str:
    rows = []
    for message in _list(case.get("messages")):
        if not isinstance(message, dict):
            continue
        body = _text(message.get("text"))
        if not body:
            continue
        label, side = _ROLES.get(_text(message.get("role")), ("消息", "neutral"))
        label = _text(message.get("sender_name")) or label
        stamp = _text(message.get("time"))
        time_markup = f"<time>{escape(stamp)}</time>" if stamp else ""
        rows.append(
            f'<li class="chat-row {side}"><div class="chat-turn">'
            f'<div class="chat-meta"><strong>{escape(label)}</strong>{time_markup}</div>'
            f'<p class="chat-bubble">{escape(body)}</p></div></li>'
        )
    if rows:
        scope = case.get("scope")
        if scope == "pair":
            note = "以下为该条目关联的脱敏问答。"
        elif scope == "limited":
            note = "会话较长，展示与本次咨询最相关的部分。"
        else:
            note = "以下为该条目关联的脱敏会话片段，时间为北京时间。"
        messages = f'<ol class="chat-list" aria-label="脱敏会话上下文">{"".join(rows)}</ol>'
        count = f'<span class="meta num">{len(rows):,} 条</span>'
    else:
        note, messages, count = "当前条目暂无可公开查看的会话上下文。", "", ""
    return (
        f'<section class="chat" aria-label="会话上下文"><div class="chat-head"><h3>聊天记录</h3>{count}</div>'
        f'<p class="note">{note}</p>{messages}</section>'
    )


def _cases(items: list[dict], total: int) -> str:
    result = []
    for case in items:
        if not isinstance(case, dict):
            continue
        idx = len(result) + 1
        title = _text(case.get("title")) or f"会话 {idx}"
        stamp, category = _text(case.get("time")), _text(case.get("category"))
        meta = (f'<span class="chip">{escape(category)}</span>' if category else "")
        meta += f'<time class="meta">{escape(stamp)}</time>' if stamp else ""
        facts = _sections(case.get("sections"), case=True)
        group = f'<span class="case-meta">{meta}</span>' if meta else ""
        content_class = "case-content with-facts" if facts else "case-content"
        opened = " open" if idx == 1 else ""
        result.append(
            f'<details class="case"{opened}><summary><span class="case-heading">'
            f'<span class="case-name">{escape(title)}</span>{group}</span>'
            f'<span class="case-chev" aria-hidden="true"></span></summary>'
            f'<div class="{content_class}">{facts}{_chat(case)}</div></details>'
        )
    if not result:
        return '<section class="panel"><h2 class="section-title">会话上下文</h2><p class="note">当前条目暂无可公开查看的会话上下文。</p></section>'
    count = (f"展示 {len(result):,} / 共 {total:,} 条" if type(total) is int and total > len(result)
             else f"{len(result):,} 条会话")
    return (
        '<section aria-labelledby="evidence"><div class="section-title"><h2 id="evidence">会话上下文</h2>'
        f'<span class="meta num">{count}</span></div><div class="case-list">{"".join(result)}</div></section>'
    )


def render_page(entry: dict, cases: list[dict], *, total_cases: int = 0) -> bytes:
    """Render only the given redacted result and its authorized evidence."""
    kind = _text(entry.get("kind"))
    kind_label = _KINDS.get(kind, "已发布内容")
    title = escape(_text(entry.get("title")))
    start, end = _text(entry.get("start")), _text(entry.get("end"))
    period = start if start == end else f"{start} 至 {end}"
    period_line = f'<span class="meta num">统计区间 {escape(period)}</span>' if start and end else ""
    summary = _text(entry.get("summary"))
    summary_line = f'<p class="summary">{escape(summary)}</p>' if summary else ""
    sections = _sections(entry.get("sections")) or _sections(_legacy_sections(kind, _text(entry.get("detail"))))
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="robots" content="noindex,nofollow,noarchive">'
        f'<title>{title} · 语海 VOC</title><style>{_STYLE}</style></head><body>'
        '<header class="masthead"><div class="masthead-inner"><div class="brand">'
        '<span class="brand-mark" aria-hidden="true">V</span>语海 VOC</div>'
        '<span class="view-label">访客阅览</span></div></header>'
        '<main class="page"><article class="hero"><div class="hero-meta">'
        f'<span class="chip kind">{kind_label}</span>{period_line}</div>'
        f'<h1>{title}</h1>{summary_line}{_metrics(entry.get("metrics"))}</article>'
        f'{_taxonomy(entry.get("taxonomy"))}{sections}{_cases(cases, total_cases)}</main></body></html>'
    ).encode("utf-8")
