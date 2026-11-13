"""Read the existing scoped VOC procedure, handling and evidence-gap views."""

from __future__ import annotations

from collections import defaultdict
import re

from . import voc_episode_projection
from .clean.rules import mask_pii
from .extract.text import clean_visible_text
from .voc_service import ServiceError


_GAPS = {
    'applicability': '适用条件', 'case_evidence': '可核对的处理记录', 'complete_procedure': '完整操作步骤',
    'post_change_retest': '操作后复测', 'verified_outcome': '客户复测结果',
    'conflicting_case': '不同案例的处理结果冲突', 'platform': '平台', 'module': '功能模块',
    'condition': '触发条件', 'staff_reply': '人工回复', 'root_cause': '原因确认',
    'later_counterevidence': '后续失败或疑问尚未核对', 'unbound_condition': '适用条件缺少原话依据',
    'unsafe_step': '操作安全依据', 'customer_result': '客户复测结果', 'retest': '复测步骤',
}
_EXCEPTIONS = {'context_needed': '待补上下文', 'extraction_failed': '来源整理失败', 'possible_duplicate': '可能重复'}
_CASE_SECTIONS = {'diagnosis': '单案排查判断', 'action': '客服处理记录', 'decision': '适用限制',
                  'next_step': '会话支持的后续建议'}
_VERIFICATION = {'observed': '会话中记录的复测', 'expected': '预期通过标准'}
_PRIVATE = re.compile(
    r'https?://[^\s，。；！？]+|\bqiyu-[\w-]+:[^\s，。；！？]+|'
    r'\b(?:episode_|playbook_|case_path_|issue_|branch_|sop_|ctx_|t_|i_|k_)[a-f0-9]{12,}\b', re.I,
)


def _text(value, limit: int | None = 480) -> str:
    if not isinstance(value, str):
        return ''
    text = ' '.join(_PRIVATE.sub('[已隐藏]', mask_pii(clean_visible_text(value))).split())
    return text if limit is None or len(text) <= limit else text[:limit] + '…'


def _anchor(item) -> tuple[int, str] | None:
    if (isinstance(item, dict) and type(item.get('qa_id')) is int and item['qa_id'] > 0
            and isinstance(item.get('message_id'), str) and item['message_id']):
        return item['qa_id'], item['message_id']
    return None


def _anchors(evidence: list[dict]) -> tuple[tuple[int, str], ...]:
    return tuple(dict.fromkeys(anchor for item in evidence if (anchor := _anchor(item))))[:8]


def _branch(item: dict, branch: dict, *, complete: bool) -> dict | None:
    condition, retest = _text(branch.get('condition'), 1200), _text(branch.get('retest'), 1200)
    steps = [(step, text) for step in branch['steps'] if (text := _text(step.get('text'), 1200))]
    results = branch['result_evidence']
    feedback = list(dict.fromkeys(quote for ref in results if (quote := _text(ref.get('quote')))))
    verified = branch.get('status') == 'verified' and bool(feedback)
    if not condition or not steps or not retest or (complete and not verified):
        return None
    lines = ['已有客户复测依据。' if verified else '已核验指引，效果待复测。',
             f'适用条件\n{condition}', '处理步骤']
    lines += [f'{index}. {text}' for index, (_, text) in enumerate(steps, 1)]
    lines.append(f'复测方法\n{retest}')
    procedure = '\n'.join(lines)
    if verified:
        lines.append('已有案例的客户反馈\n' + '\n'.join(feedback[:3]))
    if escalation := _text(branch.get('escalation')):
        lines.append('升级处理\n' + escalation)

    # Prefer applicability and step anchors. A result can be a later turn
    # outside the extracted Q&A pair even though the SOP proves its origin.
    refs = [*branch['condition_evidence'], *(ref for step, _ in steps for ref in step['evidence']),
            *branch['retest_evidence'], *results]
    entry = {'id': branch['id'], 'group': item['id'], 'kind': 'branch', 'title': _text(item['title']),
             'lines': lines, 'anchors': _anchors(refs)}
    procedure_parts = [condition, retest, *(text for _, text in steps)]
    if complete and len(steps) == len(branch['steps']) and all(len(part) <= 1200 for part in procedure_parts):
        entry['verified_branch'] = {'condition': condition, 'procedure': procedure,
                                    'evidence_groups': [branch['condition_evidence'],
                                                        *(step['evidence'] for step, _ in steps),
                                                        branch['retest_evidence'], results]}
    return entry


def _case(item: dict) -> dict | None:
    problem = item['problem']
    question = _text(problem.get('quote'))
    solution = item.get('solution') or {}
    sections = solution.get('sections') or []
    title = _text(solution.get('title')) or question
    if not title:
        return None
    lines = ['单案处理记录。']
    if question and question != title:
        lines.append('客户问题\n' + question)
    if platform := _text(item.get('platform')):
        lines.append('涉及平台\n' + platform)
    observed = False
    for section in sections:
        kind, content = section.get('kind'), _text(section.get('text'))
        if not content or kind == 'problem':
            continue
        if kind == 'verification':
            mode = section.get('mode')
            observed |= mode == 'observed'
            label = _VERIFICATION.get(mode, '会话中的验证说明')
        else:
            label = _CASE_SECTIONS.get(kind)
        if label:
            lines.append(f'{label}\n{content}')
    if not sections:
        for step in item['steps']:
            if content := _text(step.get('action')):
                label = '客服处理记录' if step.get('actor') == 'staff' else '客户反馈'
                lines.append(f'{label}\n{content}')
    if not observed:
        lines.append('本记录未提供实际复测结果。')
    return {'id': item['id'], 'group': item['id'], 'kind': 'case', 'title': title,
            'lines': lines, 'anchors': _anchors([problem])}


def _investigation(item: dict) -> dict:
    labels = list(dict.fromkeys(label for key in item['missing'] if (label := _GAPS.get(key) or _text(key, 100))))
    lines = ['当前处理指引还在排查。']
    if labels:
        lines.append('缺少的依据\n' + '、'.join(labels))
    return {'id': item['id'], 'group': item['id'], 'kind': 'investigation', 'title': _text(item['title']),
            'lines': lines, 'anchors': _anchors(item['evidence'])}


def _candidates(engine, cfg, filters: dict, name: str) -> list[dict]:
    if name == 'gaps':
        rows = engine.exceptions(cfg, filters)['items']
        focus = filters.get('focus_topic')
        intent, diagnosis = filters.get('support_intent'), filters.get('diagnosis_status')
        if intent not in ('', None, 'all') or diagnosis not in ('', None, 'all'):
            # Unclassified extraction failures are not evidence about one
            # explicitly selected support or diagnosis category.
            rows = [item for item in rows if item.get('domain')]
        return [{'id': item['id'], 'kind': 'gap', 'title': _text(item['question']) or '来源缺口',
                 'lines': [_EXCEPTIONS.get(item['kind'], '待核对的依据'), _text(item['reason'])],
                 'topic': item.get('topic_id')} for item in rows if not focus or item.get('topic_id') == focus]
    complete = name == 'sops'
    data = engine.complete_sops(cfg, filters) if complete else engine.playbooks(cfg, filters)
    branches, investigations = [], []
    for item in data['items']:
        current = [result for branch in item['branches'] if (result := _branch(item, branch, complete=complete))]
        branches.extend(current)
        if not complete and not current:
            investigations.append(_investigation(item))
    cases = [result for item in data.get('case_paths', []) if (result := _case(item))]
    return [*branches, *cases, *investigations]


def _event_anchors(row: dict) -> set[tuple[int, str]]:
    source, data = row['source'], row['data']
    qa_id = row['qa_id']
    anchors = set()
    for parts, spans in ((source.get('_question_parts', []), [data['question_span']]),
                         (source.get('_answer_parts', []), data['claims'])):
        for part in parts:
            if any(part['start'] < span['end'] and span['start'] < part['end'] for span in spans):
                anchors.add((qa_id, part['message_id']))
    return anchors


def _resolve_event_anchors(engine, cfg, filters: dict, anchors: set[tuple[int, str]]) -> dict:
    events = defaultdict(set)
    if anchors:
        # Restrict source loading to the chosen cases and use the same projected
        # event view as the citation drawer, including episode and legacy IDs.
        current = engine._sources(cfg, voc_episode_projection.source_filters(filters),
                                  qa_ids={qa_id for qa_id, _ in anchors})
        for row in engine._topic_view(cfg, filters, current=current):
            for anchor in _event_anchors(row) & anchors:
                events[anchor].add(row['id'])
    return {anchor: next(iter(identities)) for anchor, identities in events.items() if len(identities) == 1}

def _citations(entries: list[dict], events: dict) -> list[dict]:
    references = {}
    for item in entries:
        if identity := item.get('topic'):
            kind = 'topic'
        else:
            kind = 'event'
            identity = next((events[anchor] for anchor in item.get('anchors', ()) if anchor in events), '')
        if identity:
            references.setdefault((kind, identity), {'kind': kind, 'id': identity, 'title': item['title'][:120]})
    return list(references.values())


def _witnesses(entry: dict) -> dict:
    groups = []
    for evidence in entry['verified_branch']['evidence_groups']:
        by_qa = defaultdict(list)
        for ref in evidence:
            if (anchor := _anchor(ref)) and (quote := _text(ref.get('quote'), None)) and len(quote) <= 1600:
                by_qa[anchor[0]].append((anchor, quote))
        groups.append(by_qa)
    complete = set(groups[0]).intersection(*groups[1:])
    return {qa: [group[qa] for group in groups] for qa in sorted(complete)}


def _branch_evidence(entry: dict, witnesses: dict, events: dict) -> dict | None:
    branch = entry['verified_branch']
    for groups in witnesses.values():
        # The result can be a later message outside the extracted event. Link
        # through an unambiguous scoped condition, step or retest in this QA.
        identity = next((events[anchor] for group in groups[:-1] for anchor, _ in group if anchor in events), '')
        if not identity:
            continue
        feedback = list(dict.fromkeys(quote for _, quote in groups[-1]))[:3]
        proof = list(dict.fromkeys(quote for group in groups[:-1] for _, quote in group)) + feedback
        body = '\n'.join((entry['title'], branch['procedure'], '已有案例的客户反馈', *feedback))
        return {'body': body, 'proof': '\n'.join(proof), 'condition': branch['condition'], 'feedback': feedback,
                'citations': [{'kind': 'event', 'id': identity, 'title': entry['title'][:120]}]}
    return None


def _heading(name: str, entries: list[dict], shown: int) -> str:
    if not entries:
        return {'sops': '当前范围未查到符合条件的完整 SOP。',
                'playbooks': '当前范围未查到符合条件的处理指引或单案记录。',
                'gaps': '当前范围未查到符合条件的整理或证据缺口。'}[name]
    if name == 'gaps':
        return f'当前范围匹配 {len(entries)} 项整理或证据缺口，展示 {shown} 项。'
    branch_count = sum(item['kind'] == 'branch' for item in entries)
    procedure_count = len({item['group'] for item in entries if item['kind'] == 'branch'})
    if name == 'sops':
        return f'当前范围匹配 {procedure_count} 项完整 SOP，共 {branch_count} 个已验证分支，展示 {shown} 个分支。'
    cases = sum(item['kind'] == 'case' for item in entries)
    investigations = sum(item['kind'] == 'investigation' for item in entries)
    return (f'当前范围匹配 {procedure_count} 项处理指引（{branch_count} 个分支）、{cases} 条单案记录、'
            f'{investigations} 项待补依据的排查，展示 {shown} 条结果。')


def read_solution(cfg, filters: dict, name: str, query: str, limit: int, *, engine) -> tuple[str, list[dict]]:
    """Read a bounded set of scoped facts and supported source citations."""
    text, citations, _ = read_solution_evidence(cfg, filters, name, query, limit, engine=engine)
    return text, citations


def read_solution_evidence(cfg, filters: dict, name: str, query: str, limit: int, *, engine) -> tuple[str, list[dict], list[dict]]:
    """Also expose original evidence for visible SOPs with a scoped complete witness."""
    if name not in ('sops', 'playbooks', 'gaps') or not isinstance(query, str) or len(query.strip()) > 1000:
        raise ServiceError('处理依据查询无效。')
    if type(limit) is not int or not 1 <= limit <= 10:
        raise ServiceError('处理依据查询结果数须为 1–10。')
    entries = _candidates(engine, cfg, filters, name)
    if query.strip() and entries:
        candidates = [{**item, 'question': item['title'], 'steps': item['lines']} for item in entries]
        entries = engine.relevant_items(cfg, candidates, query.strip(), kind='knowledge')
    visible = entries[:limit]
    lines = [_heading(name, entries, len(visible))]
    anchors = {anchor for item in visible for anchor in item.get('anchors', ())}
    supported = []
    for item in visible:
        lines.append('\n'.join([item['title'], *(line for line in item['lines'] if line)]))
        if name == 'sops' and 'verified_branch' in item:
            witnesses = _witnesses(item)
            supported.append((item, witnesses))
            anchors.update(anchor for groups in witnesses.values() for group in groups[:-1] for anchor, _ in group)
    events = _resolve_event_anchors(engine, cfg, filters, anchors)
    evidence = [entry for item, witnesses in supported if (entry := _branch_evidence(item, witnesses, events))]
    return '\n\n'.join(lines), _citations(visible, events), evidence
