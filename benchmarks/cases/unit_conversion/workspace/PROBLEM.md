# Incident
监控 ingress.queue_wait_ms 接收毫秒数，presenter.render_wait 应输出秒，保留三位小数。
2500 毫秒应显示 2.500 s；目前显示 0.003 s。队列中记录格式仍为毫秒，不能改变存储schema。
零和小于一秒的时长必须可表示。不要修改 formatter 的保留三位小数规则。
