import unittest

from reports import read_admin_report


class HiddenTests(unittest.TestCase):
    def test_roles_remain_strict(self):
        for role in ('', 'Admin', 'user', 'root', None):
            with self.assertRaises(PermissionError):
                read_admin_report(role)
