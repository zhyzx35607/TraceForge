from policy import can_read_admin


def read_admin_report(role):
    if not can_read_admin(role):
        raise PermissionError('admin role required')
    return {'total': 42}
