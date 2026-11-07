AUDIT_ENABLED = True

def record_discount(value):
    return {'recorded': AUDIT_ENABLED, 'discount': value}
