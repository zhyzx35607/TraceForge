class CatalogService:
    def __init__(self, store):
        self.store = store
        self.cache = {}
    def get_price(self, product):
        if product not in self.cache:
            self.cache[product] = self.store.get(product)
        return self.cache[product]
    def set_price(self, product, value):
        self.store.set(product, value)
