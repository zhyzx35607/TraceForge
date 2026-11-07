import unittest

from gateway import fetch_report


class BrokenTransport:
    def fetch(self, report_id, timeout):
        raise TimeoutError('upstream unavailable')

class HiddenTests(unittest.TestCase):
    def test_propagates_timeout(self):
        with self.assertRaises(TimeoutError):
            fetch_report(BrokenTransport(), 'r2')
