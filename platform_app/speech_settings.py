"""Platform speech timing settings, independent of model and user quotas."""
from math import isfinite

DEFAULT_SPEECH = {'max_chunk_chars': 60, 'chars_per_second': 3.5, 'safety_margin_seconds': 1.0}
BOUNDS = {'max_chunk_chars': (10, 100), 'chars_per_second': (1, 10), 'safety_margin_seconds': (0, 10)}


def speech_settings(base=None, saved=None):
    result = dict(DEFAULT_SPEECH)
    for source in (base, saved):
        if not isinstance(source, dict):
            continue
        for key, (low, high) in BOUNDS.items():
            value = source.get(key)
            if type(value) not in (int, float) or not isfinite(value) or not low <= value <= high:
                continue
            if key == 'max_chunk_chars' and type(value) is not int:
                continue
            result[key] = value
    return result
