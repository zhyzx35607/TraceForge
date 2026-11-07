from audit import record_discount


def apply_coupon(total, percent):
    if percent > 100:
        raise ValueError('discount must be within 0..100')
    audit = record_discount(percent)
    return {'total': total * (1 - percent / 100), 'audit': audit}
