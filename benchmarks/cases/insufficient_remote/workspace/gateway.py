from config import DEFAULT_TIMEOUT_SECONDS


def fetch_report(transport, report_id):
    return transport.fetch(report_id, timeout=DEFAULT_TIMEOUT_SECONDS)
