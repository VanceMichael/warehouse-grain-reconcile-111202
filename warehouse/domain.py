from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal


def overview(connection):
    rows = [dict(record) for record in connection.execute('SELECT order_id, SUM(units) AS units FROM packages GROUP BY order_id ORDER BY order_id')]
    return rows
