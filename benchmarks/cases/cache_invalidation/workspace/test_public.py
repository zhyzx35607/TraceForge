import unittest

from service import CatalogService
from storage import CatalogStore


class PublicTests(unittest.TestCase):
    def test_write_then_read(self):
        service = CatalogService(CatalogStore({'book': 10}))
        self.assertEqual(service.get_price('book'), 10)
        service.set_price('book', 12)
        self.assertEqual(service.get_price('book'), 12)
