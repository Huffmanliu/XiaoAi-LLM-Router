"""Conservative local failure matching; no model calls and no arbitrary regular expressions.

Seed families adapted from UNSUPPORTED_PATTERNS in:
https://github.com/open-speaker-llm/xiaomi_ai_llm/blob/main/device/native_first_client.sh
Broad standalone words were deliberately replaced with conjunctions.
"""
import unicodedata

DEFAULT_DIALOGUE = {'routing_mode': 'fallback', 'answer_prefix': 'deepseek回答',
                    'wake_words': ['问问AI', '问问 AI', '深思'], 'routing_updated': 0}
SEED_RULES = [
    ('开源实测：难住后继续学习', 'exact', '这可把我难住了，看来要更努力学习了'),
    ('难住且需要学习', 'phrases', '难住\n学习'),
    ('问住且需要学习', 'phrases', '问住\n学习'),
    ('明确回答不上', 'phrases', '我\n回答不上'),
    ('不会回答且需要学习', 'phrases', '我\n不会\n学习'),
    ('第一人称仍在学习', 'phrases', '我\n还在学习'),
    ('需要再学习', 'phrases', '需要再学习'),
]
# Operational failures cannot be repaired by generating an answer.
EXCLUSIONS = ('没听清', '没听懂', '再说一遍', '网络', '联网', '连接失败', '设备离线',
              '设备不在线', '找不到设备', '没有找到设备', '无法控制', '控制失败', '操作失败',
              '不支持该操作', '不支持此操作')


def normalized(text):
    return ''.join(c for c in unicodedata.normalize('NFKC', text).casefold()
                   if not c.isspace() and not unicodedata.category(c).startswith('P'))


def failure_match(answer, rules):
    text = normalized(answer)
    if not text:
        return {'takeover': False, 'reason': '等待小爱回答', 'matched': []}
    if any(normalized(word) in text for word in EXCLUSIONS):
        return {'takeover': False, 'reason': '属于听写、网络或设备操作异常，不自动补答', 'matched': []}
    matched = []
    for rule in rules:
        if not rule['enabled']:
            continue
        parts = [normalized(p) for p in rule['content'].splitlines() if normalized(p)]
        if rule['match_type'] == 'exact':
            hit = text == normalized(rule['content'])
        else:
            # Short, explicit failure utterances only; avoid matching a long normal explanation.
            hit = bool(parts) and len(text) <= 100 and all(p in text for p in parts)
        if hit:
            matched.append({'id': rule['id'], 'name': rule['name']})
    return {'takeover': bool(matched), 'reason': '命中已启用的失败话术' if matched else '未命中失败话术',
            'matched': matched}
