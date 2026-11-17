"""
Universal Database Adapter for RETREAT Hotel & Suites.
Provides a unified SQLite / PostgreSQL interface with dict-like row access,
automatic parameter translation (? -> %s for Postgres), and connection pooling.
"""

import os
import re

DATABASE_URL = os.environ.get('DATABASE_URL')
# Default local SQLite file path
SQLITE_DB_PATH = os.path.join(os.path.dirname(__file__), 'retreat.db')

IS_POSTGRES = bool(DATABASE_URL and DATABASE_URL.startswith(('postgres://', 'postgresql://')))

if IS_POSTGRES:
    import psycopg2
    import psycopg2.extras
    # Normalizing postgres:// url if provided by old Heroku/Render standard
    if DATABASE_URL.startswith('postgres://'):
        DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
else:
    import sqlite3


class PostgresRow(dict):
    """Dict subclass that allows integer indexing row[0] as well as key access row['col']."""
    def __init__(self, cursor_description, values):
        super().__init__()
        self._values = list(values) if values is not None else []
        self._keys = [col[0] for col in cursor_description] if cursor_description else []
        for k, v in zip(self._keys, self._values):
            self[k] = v

    def __getitem__(self, item):
        if isinstance(item, int):
            return self._values[item]
        return super().__getitem__(item)

    def get(self, key, default=None):
        return super().get(key, default)


def translate_query_for_postgres(query: str) -> str:
    """
    Translates standard SQLite syntax into PostgreSQL compatible syntax:
    1. Replaces unquoted parameter '?' with '%s'.
    2. Converts double-quoted string literals ("value") to single-quoted ('value'),
       unless they are known table/column names, because SQLite treats "..." as strings
       while Postgres treats "..." as identifier/column names.
    """
    # First, handle double quoted strings that should be single quoted literals
    # In SQLite, users often write WHERE status = "Available" instead of 'Available'
    def replace_double_quotes(match):
        content = match.group(1)
        # If it looks like a literal value (contains spaces, numbers, or standard text), turn into single quotes
        # But if it's already an existing column/table name like "room_number", keep it
        known_identifiers = {
            'id', 'booking_code', 'customer_id', 'room_id', 'room_name', 'guest_name',
            'guest_email', 'guest_phone', 'check_in', 'check_out', 'guests_adults',
            'guests_children', 'rooms_count', 'total_nights', 'total_price',
            'base_room_charges', 'extra_guest_charges', 'meal_plan', 'meal_plan_charge',
            'discount_amount', 'special_requests', 'status', 'payment_status',
            'assigned_room_id', 'assigned_room_number', 'actual_check_in',
            'actual_check_out', 'cancelled_at', 'refund_amount', 'refund_status',
            'created_at', 'updated_at', 'room_category_id', 'room_number', 'floor',
            'operational_status', 'notes', 'current_booking_id', 'expense_ref',
            'booking_id', 'category', 'description', 'quantity', 'unit_price',
            'total_amount', 'expense_status', 'request_ref', 'request_type',
            'assigned_staff', 'scheduled_time', 'completed_at', 'expense_id',
            'transaction_ref', 'amount', 'payment_method', 'transaction_status',
            'transaction_date', 'notes', 'invoice_ref', 'invoice_date', 'total_charges',
            'total_payments', 'outstanding_balance', 'invoice_status', 'itemized_json',
            'order_ref', 'items_json', 'order_status', 'delivery_notes',
            'status_timeline_json', 'delivered_at', 'refund_ref', 'rooms', 'bookings',
            'inquiries', 'newsletter', 'customers', 'payments', 'menu_items',
            'food_orders', 'expenses', 'invoices', 'physical_rooms', 'service_requests',
            'refunds'
        }
        if content in known_identifiers:
            return f'"{content}"'
        escaped = content.replace("'", "''")
        return f"'{escaped}'"

    query = re.sub(r'"([^"\\]*(?:\\.[^"\\]*)*)"', replace_double_quotes, query)

    parts = []
    in_single_quote = False
    in_double_quote = False
    i = 0
    while i < len(query):
        ch = query[i]
        if ch == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
            parts.append(ch)
        elif ch == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            parts.append(ch)
        elif ch == '?' and not in_single_quote and not in_double_quote:
            parts.append('%s')
        else:
            parts.append(ch)
        i += 1
    return "".join(parts)


class PostgresCursorWrapper:
    def __init__(self, raw_cursor, conn):
        self._cur = raw_cursor
        self._conn = conn
        self._last_inserted_id = None

    def execute(self, query, params=None):
        translated = translate_query_for_postgres(query)
        is_insert = query.strip().upper().startswith('INSERT INTO')
        
        # If it's an insert without RETURNING clause, append RETURNING id to capture lastrowid
        if is_insert and 'RETURNING' not in translated.upper():
            translated_with_ret = translated + ' RETURNING id'
            try:
                if params is None:
                    self._cur.execute(translated_with_ret)
                else:
                    if not isinstance(params, (list, tuple)):
                        params = (params,)
                    self._cur.execute(translated_with_ret, params)
                res = self._cur.fetchone()
                if res:
                    self._last_inserted_id = res[0]
                return self
            except Exception:
                # In case table doesn't have 'id' column, rollback sub-attempt and execute normally
                self._conn._conn.rollback()
                pass

        if params is None:
            self._cur.execute(translated)
        else:
            if not isinstance(params, (list, tuple)):
                params = (params,)
            self._cur.execute(translated, params)
        return self

    def executemany(self, query, params_seq):
        translated = translate_query_for_postgres(query)
        self._cur.executemany(translated, params_seq)
        return self

    def fetchone(self):
        val = self._cur.fetchone()
        if val is None:
            return None
        return PostgresRow(self._cur.description, val)

    def fetchall(self):
        rows = self._cur.fetchall()
        if not rows:
            return []
        desc = self._cur.description
        return [PostgresRow(desc, r) for r in rows]

    @property
    def lastrowid(self):
        return self._last_inserted_id

    def close(self):
        self._cur.close()

    def __iter__(self):
        for r in self.fetchall():
            yield r


class PostgresConnectionWrapper:
    def __init__(self, raw_conn):
        self._conn = raw_conn

    def cursor(self):
        return PostgresCursorWrapper(self._conn.cursor(), self)

    def execute(self, query, params=None):
        cur = self.cursor()
        cur.execute(query, params)
        return cur

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


def get_db():
    """
    Main database factory used by the entire application.
    If DATABASE_URL environment variable is set (or Supabase URL configured), connects to PostgreSQL.
    Otherwise, falls back transparently to local SQLite (retreat.db).
    """
    if IS_POSTGRES:
        raw_conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        return PostgresConnectionWrapper(raw_conn)
    else:
        conn = sqlite3.connect(SQLITE_DB_PATH, timeout=20.0)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA busy_timeout = 20000')
        return conn
