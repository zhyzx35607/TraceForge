import unittest
from decimal import Decimal
from pricing import calculate_total


class PricingTests(unittest.TestCase):
    def test_multiple_quantity(self):
        self.assertEqual(calculate_total([{"price": "19.90", "quantity": 3}]), Decimal("59.70"))

    def test_discount_applies_to_full_subtotal(self):
        self.assertEqual(calculate_total([{"price": "10", "quantity": 2}], 10), Decimal("18.00"))

    def test_two_lines(self):
        self.assertEqual(calculate_total([{"price": "12.50", "quantity": 2}, {"price": "5", "quantity": 1}]), Decimal("30.00"))

    def test_zero_quantity(self):
        self.assertEqual(calculate_total([{"price": "7", "quantity": 0}]), Decimal("0.00"))

    def test_single_item(self):
        self.assertEqual(calculate_total([{"price": "12.50", "quantity": 1}]), Decimal("12.50"))

    def test_empty_cart(self):
        self.assertEqual(calculate_total([]), Decimal("0.00"))

    def test_invalid_discount(self):
        with self.assertRaises(ValueError):
            calculate_total([], 101)

    def test_negative_quantity(self):
        with self.assertRaises(ValueError):
            calculate_total([{"price": "1", "quantity": -1}])


if __name__ == "__main__":
    unittest.main()
