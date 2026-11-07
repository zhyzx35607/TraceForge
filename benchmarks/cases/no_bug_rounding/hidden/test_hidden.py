import unittest
from decimal import Decimal

from invoices import round_invoice


class HiddenTests(unittest.TestCase):
    def test_signed_ties(self):
        for source, expected in [('1.005','1.00'),('1.015','1.02'),('-2.345','-2.34'),('0','0.00')]:
            self.assertEqual(round_invoice(source), Decimal(expected))
