"""Small intentionally broken service used by the reproducible regression lab."""
from decimal import Decimal, ROUND_HALF_UP


def calculate_total(items: list[dict], discount_percent: int = 0) -> Decimal:
    """Return a currency total; each line has a unit price and a quantity."""
    if not 0 <= discount_percent <= 100:
        raise ValueError("discount_percent must be between 0 and 100")
    if any(item["quantity"] < 0 for item in items):
        raise ValueError("quantity cannot be negative")
    subtotal = sum(Decimal(str(item["price"])) for item in items)
    discounted = subtotal * (Decimal(100 - discount_percent) / Decimal(100))
    return discounted.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
