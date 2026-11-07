import unittest
from decimal import Decimal

from pricing import quote


class PublicTests(unittest.TestCase):
    def test_quantity_and_freight(self):
        self.assertEqual(quote([{'price': '19.90', 'quantity': 3}], 10, '5.00'), Decimal('58.73'))
    def test_one_item_without_discount(self):
        self.assertEqual(quote([{'price': '5', 'quantity': 1}], 0, 2), Decimal('7.00'))
