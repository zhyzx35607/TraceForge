import unittest

from pagination import page


class PublicTests(unittest.TestCase):
    def test_tie_after_cursor(self):
        events = [{'created_at': 10, 'id': key} for key in 'abc'] + [{'created_at': 11, 'id': 'd'}]
        first, cursor = page(events, limit=2)
        second, _ = page(events, cursor, 2)
        self.assertEqual([e['id'] for e in first + second], list('abcd'))
