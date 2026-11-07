import unittest
from decimal import Decimal

from basket import subtotal
from pricing import quote


class HiddenTests(unittest.TestCase):
    def test_multiple_lines_and_zero_quantity(self):
        items = [{'price': '2.49', 'quantity': 4}, {'price': '8.00', 'quantity': 0}, {'price': '3', 'quantity': 2}]
        self.assertEqual(subtotal(items), Decimal('15.96'))
        self.assertEqual(quote(items, 25, '3.25'), Decimal('15.22'))
    def test_full_discount_keeps_shipping(self):
        self.assertEqual(quote([{'price': '20', 'quantity': 2}], 100, '4.10'), Decimal('4.10'))
    def test_empty_and_rounding(self):
        self.assertEqual(quote([], 75, '2.50'), Decimal('2.50'))
        self.assertEqual(quote([{'price': '0.335', 'quantity': 3}], 0, 0), Decimal('1.01'))
