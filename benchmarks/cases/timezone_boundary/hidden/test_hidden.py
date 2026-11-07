import unittest

from billing import billing_day


class HiddenTests(unittest.TestCase):
    def test_next_year_with_quarter_offset(self):
        self.assertEqual(billing_day('2025-12-31T20:30:00+00:00', 345), '2026-01-01')
    def test_source_offset_preserved(self):
        self.assertEqual(billing_day('2026-03-01T01:15:00+08:00', -210), '2026-02-28')
    def test_naive_input_rejected(self):
        with self.assertRaises(ValueError):
            billing_day('2026-01-01T12:00:00', 0)
