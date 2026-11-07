# Incident
CatalogService 首次 get_price 会缓存商品价格。set_price 成功后，同一 service 的后续 get_price
仍返回旧价格。更新应立即对后续读取可见，且不得清空其他商品的缓存。
公开 Store.read_count 可用于确认无关商品没有被重新加载。未知商品读取应保持 KeyError。
