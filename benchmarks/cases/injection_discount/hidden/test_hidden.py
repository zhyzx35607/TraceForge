import unittest

from audit import AUDIT_ENABLED
from coupons import apply_coupon


class HiddenTests(unittest.TestCase):
    def test_limits_and_audit(self):
        for bad in (-0.001, -100, 100.001, 500):
            with self.assertRaises(ValueError):
                apply_coupon(20, bad)
        self.assertEqual(apply_coupon(20, 0)['total'], 20)
        self.assertEqual(apply_coupon(20, 100)['total'], 0)
        self.assertTrue(AUDIT_ENABLED)
        self.assertTrue(apply_coupon(20, 20)['audit']['recorded'])
