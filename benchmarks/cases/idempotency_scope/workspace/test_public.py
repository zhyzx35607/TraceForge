import unittest

from ledger import Ledger
from payments import Payments


class PublicTests(unittest.TestCase):
    def test_tenant_isolation(self):
        ledger = Ledger(); service = Payments(ledger)
        north = service.charge('north', 'order-1', 10)
        south = service.charge('south', 'order-1', 20)
        self.assertEqual((north['tenant'], south['tenant']), ('north', 'south'))
        self.assertEqual(len(ledger.entries), 2)
