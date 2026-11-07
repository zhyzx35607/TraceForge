class Payments:
    def __init__(self, ledger):
        self.ledger = ledger
        self.completed = {}
    def charge(self, tenant_id, request_key, amount):
        identity = request_key
        if identity not in self.completed:
            self.completed[identity] = self.ledger.charge(tenant_id, amount)
        return self.completed[identity]
