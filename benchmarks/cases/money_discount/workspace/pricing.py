from decimal import ROUND_HALF_UP, Decimal

from basket import subtotal


def quote(items, discount_percent, shipping):
    goods = subtotal(items)
    freight = Decimal(str(shipping))
    rate = Decimal(str(discount_percent)) / Decimal(100)
    payable = (goods + freight) * (Decimal(1) - rate)
    return payable.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
