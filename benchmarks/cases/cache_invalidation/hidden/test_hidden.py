import unittest

from service import CatalogService
from storage import CatalogStore


class HiddenTests(unittest.TestCase):
    def test_only_changed_item_invalidated(self):
        store = CatalogStore({'a': 1, 'b': 2})
        service = CatalogService(store)
        service.get_price('a'); service.get_price('b')
        service.set_price('a', 0)
        self.assertEqual(service.get_price('a'), 0)
        self.assertEqual(service.get_price('b'), 2)
        self.assertEqual(store.read_count['b'], 1)
    def test_write_uncached_item_and_repeat(self):
        service = CatalogService(CatalogStore({}))
        service.set_price('new', 5)
        self.assertEqual(service.get_price('new'), 5)
        service.set_price('new', 8)
        self.assertEqual(service.get_price('new'), 8)
        with self.assertRaises(KeyError):
            service.get_price('unknown')
