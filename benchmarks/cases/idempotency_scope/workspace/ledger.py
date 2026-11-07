class Ledger:
    def __init__(self):
        self.entries = []
    def charge(self, tenant_id, amount):
        receipt = {'number': len(self.entries) + 1, 'tenant': tenant_id, 'amount': amount}
        self.entries.append(receipt)
        return receipt
