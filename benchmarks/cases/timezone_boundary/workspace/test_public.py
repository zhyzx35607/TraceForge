import unittest

from billing import billing_day


class PublicTests(unittest.TestCase):
    def test_previous_day(self):
        self.assertEqual(billing_day('2026-01-01T00:30:00+00:00', -300), '2025-12-31')
    def test_same_zone(self):
        self.assertEqual(billing_day('2026-07-05T12:00:00+08:00', 480), '2026-07-05')
