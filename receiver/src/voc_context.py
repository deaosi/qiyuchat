"""Validate conversation scope metadata; previous text is never factual evidence."""
from datetime import date

from .voc_service import ServiceError

PROJECTION = {'days': 0, 'start': '', 'end': '', 'group': '', 'staff': '', 'customer': '', 'q': '',
              'support_intent': 'all', 'diagnosis_status': 'all', 'replied': '', 'cluster': 0}


def valid_date(value):
    return isinstance(value, str) and len(value) == 10 and date.fromisoformat(value).isoformat() == value


def query_projection(filters):
    result = {key: filters.get(key, default) for key, default in PROJECTION.items()}
    if filters.get('focus_topic'):
        result['focus_topic'] = filters['focus_topic']
    return result


def context_scope(filters, body):
    context = body.get('context')
    if context is None:
        return dict(filters), []
    if not isinstance(context, dict) or set(context) != {'query_filters', 'citations'}:
        raise ServiceError('上轮查询上下文格式不正确。')
    scope, refs = context['query_filters'], context['citations']
    if not isinstance(scope, dict) or set(scope) - {*PROJECTION, 'focus_topic'}:
        raise ServiceError('上轮查询范围包含无效字段。')
    for key, value in scope.items():
        if key in ('days', 'cluster'):
            if type(value) is not int or value < 0 or (key == 'days' and value > 365) or value > 2**53 - 1:
                raise ServiceError('上轮查询范围数值不正确。')
        elif not isinstance(value, str) or len(value) > 500:
            raise ServiceError('上轮查询范围格式不正确。')
    try:
        for key in ('start', 'end'):
            if scope.get(key) and not valid_date(scope[key]):
                raise ValueError()
        if scope.get('start') and scope.get('end') and scope['start'] > scope['end']:
            raise ValueError()
    except ValueError:
        raise ServiceError('上轮查询日期不正确。') from None
    if scope.get('support_intent', 'all') not in ('all', 'configuration_guidance', 'functional_anomaly', 'unclear'):
        raise ServiceError('上轮支持类型不正确。')
    if scope.get('diagnosis_status', 'all') not in ('all', 'reported', 'pending', 'config_cause_confirmed', 'platform_limit_confirmed', 'defect_confirmed', 'not_applicable'):
        raise ServiceError('上轮诊断状态不正确。')
    if scope.get('replied', '') not in ('', 'answered', 'unanswered'):
        raise ServiceError('上轮回复筛选不正确。')
    if not isinstance(refs, list) or len(refs) > 10:
        raise ServiceError('上轮引用过多。')
    for ref in refs:
        if (not isinstance(ref, dict) or set(ref) != {'kind', 'id'} or ref['kind'] not in ('topic', 'knowledge', 'event')
                or not isinstance(ref['id'], str) or not 1 <= len(ref['id']) <= 200):
            raise ServiceError('上轮引用格式不正确。')
    # A UI source/customer switch always wins over old conversation metadata.
    fixed = ('group', 'staff', 'customer', 'q', 'replied', 'cluster')
    if ((scope.get('focus_topic') or '') != (filters.get('focus_topic') or '')
            or any((scope.get(k) or PROJECTION[k]) != (filters.get(k) or PROJECTION[k]) for k in fixed)):
        return dict(filters), []
    return {**filters, **scope}, refs


def apply_dates(scoped, plan):
    start, end = plan.get('start'), plan.get('end')
    if start is not None or end is not None:
        try:
            if not start or not end or not valid_date(start) or not valid_date(end) or start > end:
                raise ValueError()
        except ValueError:
            raise ServiceError('模型返回的日期范围无效。', 502) from None
        scoped.update(days=0, start=start, end=end)
    elif plan['days'] is not None:
        scoped.update(days=plan['days'], start='', end='')
