from datetime import timedelta, timezone

from parsing import parse_event


def billing_day(value, merchant_offset_minutes):
    instant = parse_event(value)
    merchant_zone = timezone(timedelta(minutes=merchant_offset_minutes))
    return instant.replace(tzinfo=merchant_zone).date().isoformat()
