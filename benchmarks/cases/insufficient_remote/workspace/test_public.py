import unittest

from gateway import fetch_report


class Transport:
    def fetch(self, report_id, timeout):
        return (report_id, timeout)

class PublicTests(unittest.TestCase):
    def test_delegation(self):
        self.assertEqual(fetch_report(Transport(), 'r1'), ('r1', 3))
