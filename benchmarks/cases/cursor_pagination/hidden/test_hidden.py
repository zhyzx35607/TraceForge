import unittest

from pagination import page


class HiddenTests(unittest.TestCase):
    def test_all_page_sizes(self):
        events = [{'created_at': time, 'id': key} for time, key in [(2,'d'),(1,'b'),(1,'a'),(2,'c'),(3,'e')]]
        for size in (1, 2, 3, 8):
            collected, cursor = [], None
            for _ in range(10):
                batch, cursor = page(events, cursor, size)
                if not batch:
                    break
                collected.extend(e['id'] for e in batch)
            self.assertEqual(collected, list('abcde'))
    def test_exclusive_and_missing_cursor(self):
        values, _ = page([{'created_at': 4, 'id': 'c'}], (4, 'b'), 1)
        self.assertEqual([x['id'] for x in values], ['c'])
        self.assertEqual(page([{'created_at': 4, 'id': 'c'}], (4,'c'), 1), ([], None))
