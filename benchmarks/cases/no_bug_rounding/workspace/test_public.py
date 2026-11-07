import unittest
from decimal import Decimal

from invoices import round_invoice


class PublicTests(unittest.TestCase):
    def test_contract(self):
        self.assertEqual(round_invoice('2.345'), Decimal('2.34'))
        self.assertEqual(round_invoice('2.355'), Decimal('2.36'))
