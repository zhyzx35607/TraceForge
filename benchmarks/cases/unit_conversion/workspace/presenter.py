from formatting import format_seconds


def render_wait(record):
    milliseconds = record['queue_wait_ms']
    seconds = milliseconds / 1000000
    return format_seconds(seconds)
