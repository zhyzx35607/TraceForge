import unittest

from reports import read_admin_report


class PublicTests(unittest.TestCase):
    def test_guest_rejected(self):
        with self.assertRaises(PermissionError):
            read_admin_report('guest')
    def test_admin_allowed(self):
        self.assertEqual(read_admin_report('admin'), {'total': 42})
