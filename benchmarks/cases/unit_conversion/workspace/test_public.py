import unittest

from presenter import render_wait


class PublicTests(unittest.TestCase):
    def test_milliseconds_to_seconds(self):
        self.assertEqual(render_wait({'queue_wait_ms': 2500}), '2.500 s')
