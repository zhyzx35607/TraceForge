from datetime import datetime


def parse_event(value):
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError('timezone offset is required')
    return parsed
