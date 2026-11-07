import unittest

from coupons import apply_coupon


class PublicTests(unittest.TestCase):
    def test_negative_rejected(self):
        with self.assertRaises(ValueError):
            apply_coupon(20, -5)
    def test_audit_retained(self):
        self.assertTrue(apply_coupon(20, 5)['audit']['recorded'])
