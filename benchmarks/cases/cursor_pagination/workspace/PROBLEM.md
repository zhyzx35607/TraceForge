# Incident
事件按 (created_at, id) 升序排序，用最后一条返回记录的这两个字段作为 exclusive cursor。
客户翻页时，同一秒创建的事件有漏项。page_size=2，id=a,b,c 都在时刻 10，d 在时刻 11。
第一页 a,b 后，第二页应为 c,d。改变 page_size 不应改变最终遍历得到的事件集合。
cursor 的含义是严格晚于完整二元组，而不是单独晚于时间戳。
