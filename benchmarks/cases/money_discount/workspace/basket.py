from decimal import Decimal


def subtotal(items):
    return sum((Decimal(str(item['price'])) for item in items), Decimal(0))
