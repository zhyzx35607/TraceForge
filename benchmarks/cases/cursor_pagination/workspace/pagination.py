from ordering import event_key


def page(events, after=None, limit=2):
    if limit < 1:
        raise ValueError('positive page size required')
    ordered = sorted(events, key=event_key)
    if after is not None:
        ordered = [event for event in ordered if event['created_at'] > after[0]]
    selected = ordered[:limit]
    cursor = event_key(selected[-1]) if selected else None
    return selected, cursor
