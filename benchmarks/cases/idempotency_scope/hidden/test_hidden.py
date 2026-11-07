import unittest

from ledger import Ledger
from payments import Payments


class HiddenTests(unittest.TestCase):
    def test_same_scope_retries_return_original(self):
        ledger = Ledger(); service = Payments(ledger)
        original = service.charge('team', 'same', 10)
        retried = service.charge('team', 'same', 999)
        self.assertEqual(retried, original)
        self.assertEqual(len(ledger.entries), 1)
    def test_tuple_boundaries_do_not_collide(self):
        ledger = Ledger(); service = Payments(ledger)
        service.charge('a:b', 'c', 1)
        service.charge('a', 'b:c', 2)
        service.charge('a', 'other', 3)
        self.assertEqual(len(ledger.entries), 3)
