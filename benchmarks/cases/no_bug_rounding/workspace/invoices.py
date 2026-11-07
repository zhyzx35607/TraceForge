from decimal import Decimal

from contract import ROUNDING_MODE


def round_invoice(value):
    return Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUNDING_MODE)
