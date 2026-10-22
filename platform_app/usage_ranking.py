"""Admin-only, metadata-only usage aggregation; unknown consumption stays explicit."""
from calendar import monthrange
from datetime import datetime, time, timedelta

from .store import CST, Rejected

PAGE_SIZE = 25
ROLLING_MONTHS = {'last_3_months': 3, 'last_6_months': 6, 'last_year': 12}


def period_bounds(period, reference_date, now):
    anchor = reference_date or now.date()
    if anchor > now.date():
        raise Rejected('不能选择未来日期')
    if period == 'day':
        start, end = anchor, anchor + timedelta(days=1)
    elif period == 'week':
        start = anchor - timedelta(days=anchor.weekday())
        end = start + timedelta(days=7)
    elif period == 'month':
        start = anchor.replace(day=1)
        end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    elif period in ROLLING_MONTHS:
        # A date-inclusive window ending at the following midnight, shifted by
        # calendar months, rather than an arbitrary 90/180/365-day duration.
        end = anchor + timedelta(days=1)
        year, month = divmod(end.year * 12 + end.month - 1 - ROLLING_MONTHS[period], 12)
        if year < 1:
            raise Rejected('所选日期过早，无法计算完整统计区间')
        start = end.replace(year=year, month=month + 1, day=min(end.day, monthrange(year, month + 1)[1]))
    else:
        raise Rejected('请选择有效的统计周期')
    return start, end


def _attempt_token(field):
    # Providers can omit usage; malformed historical metadata must not break the board.
    return (f"CASE WHEN json_valid(a.usage) THEN CASE WHEN json_type(a.usage,'$.{field}')='integer' "
            f"AND json_extract(a.usage,'$.{field}')>=0 THEN json_extract(a.usage,'$.{field}') END END")


def _request_token(field):
    return f"CASE WHEN typeof(r.{field})='integer' AND r.{field}>=0 THEN r.{field} END"


def ranking(store, *, period='day', reference_date=None, search='', only_unknown=False, page=1, now=None):
    now = now or datetime.now(CST)
    start, end = period_bounds(period, reference_date, now)
    start_ts = datetime.combine(start, time.min, CST).timestamp()
    end_ts = min(datetime.combine(end, time.min, CST).timestamp(), now.timestamp())
    with store.db() as db:
        db.execute('BEGIN')  # Schema detection and the aggregate share one read snapshot.
        has_attempts = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_attempts'").fetchone()
        if has_attempts:
            total = _attempt_token('total_tokens')
            attempts = f'''
                SELECT a.request_id, COUNT(*) attempts,
                       SUM({_attempt_token('prompt_tokens')}) prompt_tokens,
                       SUM({_attempt_token('completion_tokens')}) completion_tokens,
                       SUM({total}) total_tokens,
                       MAX(CASE WHEN ({total}) IS NULL AND a.status='started' AND r.status='reserved'
                           THEN 1 ELSE 0 END) pending_usage,
                       MAX(CASE WHEN ({total}) IS NULL AND NOT (a.status='started' AND r.status='reserved')
                           THEN 1 ELSE 0 END) unknown_usage
                FROM period_requests r JOIN model_attempts a ON a.request_id=r.id GROUP BY a.request_id
            '''
        else:
            attempts = '''SELECT NULL request_id, 0 attempts, NULL prompt_tokens, NULL completion_tokens,
                          NULL total_tokens, 0 pending_usage, 0 unknown_usage WHERE 0'''
        total = _request_token('total_tokens')
        sql = f'''
            WITH period_requests AS (
                SELECT id,user_id,status,prompt_tokens,completion_tokens,total_tokens FROM requests
                WHERE created>=? AND created<? AND started=1
            ), attempts AS ({attempts}), request_usage AS (
                SELECT r.user_id,
                       CASE WHEN a.attempts IS NOT NULL THEN a.prompt_tokens ELSE {_request_token('prompt_tokens')} END prompt_tokens,
                       CASE WHEN a.attempts IS NOT NULL THEN a.completion_tokens ELSE {_request_token('completion_tokens')} END completion_tokens,
                       CASE WHEN a.attempts IS NOT NULL THEN a.total_tokens ELSE {total} END total_tokens,
                       CASE WHEN a.attempts IS NOT NULL THEN a.pending_usage
                            WHEN ({total}) IS NULL AND r.status='reserved' THEN 1 ELSE 0 END pending_usage,
                       CASE WHEN a.attempts IS NOT NULL THEN a.unknown_usage
                            WHEN ({total}) IS NULL AND r.status!='reserved' THEN 1 ELSE 0 END unknown_usage
                FROM period_requests r LEFT JOIN attempts a ON a.request_id=r.id
            ), user_usage AS (
                SELECT u.id user_id,u.username,u.role,u.active,COUNT(r.user_id) requests,
                       COALESCE(SUM(r.prompt_tokens),0) prompt_tokens,
                       COALESCE(SUM(r.completion_tokens),0) completion_tokens,
                       COALESCE(SUM(r.total_tokens),0) total_tokens,
                       COALESCE(SUM(r.unknown_usage),0) unknown_usage,
                       COALESCE(SUM(r.pending_usage),0) pending_usage
                FROM users u LEFT JOIN request_usage r ON r.user_id=u.id GROUP BY u.id
            ), ranked AS (
                SELECT *,ROW_NUMBER() OVER (ORDER BY total_tokens DESC,username COLLATE BINARY,user_id) rank
                FROM user_usage
            ), filtered AS (
                SELECT * FROM ranked WHERE instr(lower(username),lower(?))>0 AND (?=0 OR unknown_usage>0)
            ), bounds AS (
                SELECT COUNT(*) users_total,MIN(?,MAX(1,(COUNT(*)+{PAGE_SIZE - 1})/{PAGE_SIZE})) page FROM filtered
            ), page_rows AS (
                SELECT * FROM filtered ORDER BY rank LIMIT {PAGE_SIZE} OFFSET (SELECT (page-1)*{PAGE_SIZE} FROM bounds)
            ), summary AS (
                SELECT COALESCE(SUM(requests),0) all_requests,COALESCE(SUM(total_tokens),0) all_tokens,
                       COALESCE(SUM(unknown_usage),0) all_unknown,COALESCE(SUM(pending_usage),0) all_pending FROM user_usage
            ) SELECT s.*,b.*,p.* FROM summary s CROSS JOIN bounds b LEFT JOIN page_rows p ON 1=1 ORDER BY p.rank
        '''
        records = db.execute(sql, (start_ts, end_ts, search.strip(), int(only_unknown), page)).fetchall()
    head = records[0]
    fields = ('rank', 'user_id', 'username', 'role', 'active', 'requests', 'prompt_tokens',
              'completion_tokens', 'total_tokens', 'unknown_usage', 'pending_usage')
    return {
        'period': period, 'reference_date': (reference_date or now.date()).isoformat(),
        'start_date': start.isoformat(), 'end_date': (end - timedelta(days=1)).isoformat(),
        'timezone': 'Asia/Shanghai', 'generated_at': now.timestamp(),
        'summary': {'requests': head['all_requests'], 'total_tokens': head['all_tokens'],
                    'unknown_usage': head['all_unknown'], 'pending_usage': head['all_pending']},
        'rows': [{**{key: row[key] for key in fields}, 'active': bool(row['active'])}
                 for row in records if row['user_id'] is not None],
        'page': head['page'], 'page_size': PAGE_SIZE, 'users_total': head['users_total'],
        'pages': max(1, (head['users_total'] + PAGE_SIZE - 1) // PAGE_SIZE),
    }
