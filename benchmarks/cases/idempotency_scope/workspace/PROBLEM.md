# Incident
支付处理必须以 (tenant_id, request_key) 识别重复请求。
同一租户相同 key 重试应返回原 receipt，不能再次向 ledger 追加；不同租户相同 key
必须分别记账。目前租户 north 与 south 都用 key order-1 时，south 错误拿到 north 的 receipt。
不要求跨进程并发锁；这是同一进程中请求身份范围的错误。
