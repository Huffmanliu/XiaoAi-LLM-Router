"""Per-device turn ownership. No hardware stop commands or semantic model calls."""
import asyncio
from dataclasses import dataclass
import math

from .routing import normalized


STOP_WORDS = frozenset(normalized(s) for s in (
    '停止回答', '停止播报', '不用说了', '别说了', '不要说了', '闭嘴',
    '停止', '停下', '暂停', '停止播放', '暂停播放',
))


def stop_requested(text, wake_words=()):
    """Whole utterances only: explaining/quoting a stop command isn't a stop."""
    text = normalized(text)
    if text in STOP_WORDS:
        return True
    return any(text.startswith(word) and text[len(word):] in STOP_WORDS
               for word in (normalized(w) for w in wake_words) if word)


def valid_records(items, cutoff, now):
    cutoff = max(cutoff, now - 60)
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        rid, stamp, text = item.get('request_id'), item.get('ts'), item.get('text')
        if (isinstance(rid, str) and rid and isinstance(text, str) and text.strip()
                and not isinstance(stamp, bool) and isinstance(stamp, (int, float))
                and math.isfinite(stamp) and cutoff < stamp <= now + 5):
            result.append(item)
    return result


class DeviceTurn:
    def __init__(self):
        self.generation = 0
        self.request_id = None
        self.stamp = float('-inf')
        self.observed = {}
        self.task = None
        self.waiting_for_model = False
        self.blocked = False

    def observe(self, items, cutoff, now):
        """A fresh query supersedes speech before waiting for its native answer.

        Request identity, not an answer update, denotes a new interaction. A
        late older record never takes ownership back. Equal timestamps retain
        distinct identities; the last newly observed record owns the turn.
        """
        fresh = [item for item in valid_records(items, cutoff, now) if item['request_id'] not in self.observed]
        changed = False
        for item in sorted(fresh, key=lambda r: r['ts']):
            rid, stamp = item['request_id'], item['ts']
            if rid in self.observed:
                continue
            self.observed[rid] = stamp
            if stamp >= self.stamp and rid != self.request_id:
                self.request_id, self.stamp = rid, stamp
                self.blocked = item.get('takeover_allowed') is False
                self.generation += 1
                changed = True
        # Keyword mode can start before native metadata arrives. A later audio
        # action for this same query must also make way for the native function.
        if not self.blocked and any(item['request_id'] == self.request_id and item.get('takeover_allowed') is False
                                    for item in valid_records(items, cutoff, now)):
            self.blocked = True
            self.generation += 1
            changed = True
        self.observed = {rid: stamp for rid, stamp in self.observed.items() if stamp >= now - 120}
        if changed and self.task and not self.task.done() and not self.task.cancelling():
            self.task.cancel()
        return changed

    def token(self):
        return TurnToken(self, self.generation)


@dataclass(frozen=True)
class TurnToken:
    owner: DeviceTurn
    generation: int

    def current(self):
        return self.owner.generation == self.generation

    def checkpoint(self):
        if not self.current() or asyncio.current_task().cancelling():
            raise asyncio.CancelledError
