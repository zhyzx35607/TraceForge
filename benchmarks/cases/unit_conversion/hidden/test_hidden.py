import unittest

from presenter import render_wait


class HiddenTests(unittest.TestCase):
    def test_boundary_values(self):
        for value, expected in [(0, '0.000 s'), (1, '0.001 s'), (999, '0.999 s'), (1000, '1.000 s'), (123456, '123.456 s')]:
            self.assertEqual(render_wait({'queue_wait_ms': value}), expected)
    def test_original_record_untouched(self):
        record = {'queue_wait_ms': 1250}
        render_wait(record)
        self.assertEqual(record, {'queue_wait_ms': 1250})
