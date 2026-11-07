class CatalogStore:
    def __init__(self, prices):
        self.prices = dict(prices)
        self.read_count = {}
    def get(self, product):
        self.read_count[product] = self.read_count.get(product, 0) + 1
        return self.prices[product]
    def set(self, product, value):
        self.prices[product] = value
