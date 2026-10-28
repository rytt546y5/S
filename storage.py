from __future__ import annotations

import json
from contextlib import contextmanager
import os
import sqlite3
import time
import secrets
from pathlib import Path

from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
DATA.mkdir(exist_ok=True)
DB = DATA / 'orders.sqlite3'
KEY = DATA / 'storage.key'


def crypto() -> Fernet:
    if not KEY.exists():
        fd = os.open(KEY, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(Fernet.generate_key())
        except BaseException:
            KEY.unlink(missing_ok=True)
            raise
    return Fernet(KEY.read_bytes())


@contextmanager
def db():
    cx = sqlite3.connect(DB, timeout=20)
    try:
        cx.row_factory = sqlite3.Row
        cx.execute('PRAGMA journal_mode=WAL')
        cx.execute('PRAGMA busy_timeout=20000')
        with cx:
            yield cx
    finally:
        cx.close()


def initialize():
    with db() as cx:
        cx.executescript('''
        CREATE TABLE IF NOT EXISTS auth (
          owner_id INTEGER PRIMARY KEY, credentials BLOB NOT NULL);
        CREATE TABLE IF NOT EXISTS orders (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          buyer_id INTEGER NOT NULL,
          guild_id INTEGER NOT NULL,
          pay_code TEXT NOT NULL UNIQUE,
          pay_order_id TEXT,
          amount INTEGER NOT NULL,
          status TEXT NOT NULL,
          source TEXT NOT NULL DEFAULT 'paypay',
          sms_service_key TEXT,
          sms_country_id INTEGER,
          sms_order_id TEXT,
          phone TEXT,
          expires_at INTEGER,
          hold_deadline INTEGER,
          approval_tries INTEGER NOT NULL DEFAULT 0,
          swaps INTEGER NOT NULL DEFAULT 0,
          error TEXT,
          created_at INTEGER NOT NULL,
          updated_at INTEGER NOT NULL,
          issued_at INTEGER,
          dm_channel_id INTEGER,
          dm_message_id INTEGER,
          sms_received_at INTEGER);
        CREATE INDEX IF NOT EXISTS orders_sms_id ON orders(sms_order_id);
        CREATE TABLE IF NOT EXISTS seen_sms (
          order_id INTEGER NOT NULL, fingerprint TEXT NOT NULL,
          PRIMARY KEY(order_id, fingerprint));
        CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS achievement_posts (
          order_id INTEGER PRIMARY KEY,
          channel_id INTEGER NOT NULL,
          message_id INTEGER,
          status TEXT NOT NULL,
          updated_at INTEGER NOT NULL,
          FOREIGN KEY(order_id) REFERENCES orders(id));
        ''')
        columns = {row[1] for row in cx.execute('PRAGMA table_info(orders)')}
        if 'expires_at' not in columns:
            cx.execute('ALTER TABLE orders ADD COLUMN expires_at INTEGER')
        if 'hold_deadline' not in columns:
            cx.execute('ALTER TABLE orders ADD COLUMN hold_deadline INTEGER')
        if 'approval_tries' not in columns:
            cx.execute('ALTER TABLE orders ADD COLUMN approval_tries INTEGER NOT NULL DEFAULT 0')
        if 'source' not in columns:
            cx.execute("ALTER TABLE orders ADD COLUMN source TEXT NOT NULL DEFAULT 'paypay'")
        for field in ('issued_at', 'dm_channel_id', 'dm_message_id'):
            if field not in columns:
                cx.execute(f'ALTER TABLE orders ADD COLUMN {field} INTEGER')
        if 'sms_service_key' not in columns:
            cx.execute('ALTER TABLE orders ADD COLUMN sms_service_key TEXT')
        if 'sms_country_id' not in columns:
            cx.execute('ALTER TABLE orders ADD COLUMN sms_country_id INTEGER')
        if 'sms_received_at' not in columns:
            cx.execute('ALTER TABLE orders ADD COLUMN sms_received_at INTEGER')
        # Older bot versions recorded only SMS messages delivered to Discord.
        # An already delivered code must also permanently block exchanges.
        cx.execute('''UPDATE orders SET sms_received_at=COALESCE(
            sms_received_at, updated_at) WHERE sms_received_at IS NULL
            AND EXISTS(SELECT 1 FROM seen_sms WHERE seen_sms.order_id=orders.id)''')


def save_login(owner_id: int, phone: str, password: str, device_id: str,
               token_response: dict | None = None):
    payload = {'phone': phone, 'password': password, 'uuid': device_id}
    if token_response and isinstance(token_response.get('access_token'), str) and token_response['access_token']:
        from services.paypay_session import expiry_from_response
        payload['access_token'] = token_response['access_token']
        if isinstance(token_response.get('refresh_token'), str):
            payload['refresh_token'] = token_response['refresh_token']
        payload['expires_at'] = expiry_from_response(token_response)
    encrypted = crypto().encrypt(json.dumps(payload).encode())
    with db() as cx:
        cx.execute('INSERT OR REPLACE INTO auth VALUES (?, ?)', (owner_id, encrypted))


def load_login(owner_id: int) -> dict | None:
    with db() as cx:
        row = cx.execute('SELECT credentials FROM auth WHERE owner_id=?', (owner_id,)).fetchone()
    return json.loads(crypto().decrypt(row['credentials'])) if row else None


def update_auth_token(owner_id: int, device: str, response: dict, expires_at: int | None) -> bool:
    token = response.get('access_token')
    if not isinstance(token, str) or not token:
        raise ValueError('missing access token')
    with db() as cx:
        cx.execute('BEGIN IMMEDIATE')
        row = cx.execute('SELECT credentials FROM auth WHERE owner_id=?', (owner_id,)).fetchone()
        if row is None:
            return False
        payload = json.loads(crypto().decrypt(row['credentials']))
        if payload.get('uuid') != device:
            return False
        payload['access_token'] = token
        if isinstance(response.get('refresh_token'), str):
            payload['refresh_token'] = response['refresh_token']
        payload['expires_at'] = expires_at
        cx.execute('UPDATE auth SET credentials=? WHERE owner_id=?',
                   (crypto().encrypt(json.dumps(payload).encode()), owner_id))
    return True


def invalidate_auth_token(owner_id: int, failed_token: str) -> None:
    with db() as cx:
        cx.execute('BEGIN IMMEDIATE')
        row = cx.execute('SELECT credentials FROM auth WHERE owner_id=?', (owner_id,)).fetchone()
        if row is None:
            return
        payload = json.loads(crypto().decrypt(row['credentials']))
        if payload.get('access_token') != failed_token:
            return
        payload.pop('access_token', None)
        payload.pop('expires_at', None)
        cx.execute('UPDATE auth SET credentials=? WHERE owner_id=?',
                   (crypto().encrypt(json.dumps(payload).encode()), owner_id))


def set_setting(name: str, value: str):
    with db() as cx: cx.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(name,value))


def get_setting(name: str, default=''):
    with db() as cx: row = cx.execute('SELECT value FROM settings WHERE name=?',(name,)).fetchone()
    return row['value'] if row else default


def make_order(buyer_id: int, guild_id: int, code: str, amount: int, pay_order_id: str = None, service_key: str = None, country_id: int = None) -> int:
    now = int(time.time())
    with db() as cx:
        cur = cx.execute('INSERT INTO orders(buyer_id,guild_id,pay_code,pay_order_id,amount,status,created_at,updated_at,sms_service_key,sms_country_id) VALUES (?,?,?,?,?,?,?,?,?,?)',
                         (buyer_id,guild_id,code,pay_order_id,amount,'checking',now,now,service_key,country_id))
        return cur.lastrowid


class UnfinishedOrderError(Exception):
    pass


def make_test_order(owner_id: int, guild_id: int, service_key: str = None, country_id: int = None) -> int:
    """Create a durable single-number admin order without touching PayPay.

    Claim the order before sending the non-idempotent activation request.
    """
    now = int(time.time())
    unique_code = f'test-{secrets.token_hex(16)}'
    with db() as cx:
        cx.execute('BEGIN IMMEDIATE')
        unfinished = cx.execute("""SELECT 1 FROM orders WHERE buyer_id=? AND status IN (
          'checking','active','hold','issuing','replacing','paid_unfulfilled',
          'payment_uncertain','needs_review'
        ) LIMIT 1""", (owner_id,)).fetchone()
        if unfinished:
            raise UnfinishedOrderError('処理中の注文があります。')
        cursor = cx.execute("""INSERT INTO orders(
          buyer_id,guild_id,pay_code,pay_order_id,amount,status,source,created_at,updated_at,sms_service_key,sms_country_id
        ) VALUES (?,?,?,NULL,0,'paid_unfulfilled','test',?,?,?,?)""",
        (owner_id, guild_id, unique_code, now, now, service_key, country_id))
        return cursor.lastrowid


def get_order(order_id: int) -> dict | None:
    with db() as cx: row = cx.execute('SELECT * FROM orders WHERE id=?',(order_id,)).fetchone()
    return dict(row) if row else None


def order_by_code(code: str) -> dict | None:
    with db() as cx: row = cx.execute('SELECT * FROM orders WHERE pay_code=?',(code,)).fetchone()
    return dict(row) if row else None


def active_orders() -> list[dict]:
    with db() as cx:
        rows = cx.execute("SELECT * FROM orders WHERE status IN ('active','paid_unfulfilled','hold','issuing','replacing') ORDER BY id").fetchall()
    return [dict(r) for r in rows]



def buyer_has_unfinished(buyer_id: int) -> bool:
    with db() as cx:
        row = cx.execute("""SELECT 1 FROM orders WHERE buyer_id=? AND status IN (
          'checking','active','hold','issuing','replacing','paid_unfulfilled',
          'payment_uncertain','needs_review'
        ) LIMIT 1""", (buyer_id,)).fetchone()
    return row is not None


def claim_status(order_id: int, expected: str, target: str) -> bool:
    with db() as cx:
        cur = cx.execute('UPDATE orders SET status=?, error=NULL, updated_at=? WHERE id=? AND status=?',
                         (target, int(time.time()), order_id, expected))
        return cur.rowcount == 1


def update_order(order_id: int, **fields):
    allowed = {'status','sms_order_id','phone','expires_at','swaps','error','hold_deadline','approval_tries','issued_at','dm_channel_id','dm_message_id','sms_received_at'}
    if not fields or not set(fields).issubset(allowed): raise ValueError('invalid fields')
    columns = ','.join(f'{k}=?' for k in fields)
    with db() as cx:
        cx.execute(f'UPDATE orders SET {columns}, updated_at=? WHERE id=?', (*fields.values(),int(time.time()),order_id))


def mark_sms_received(order_id: int, sms_order_id: str) -> bool:
    """Lock replacement as soon as the API shows an incoming SMS.

    Independent of Discord delivery; scoped to the current remote activation
    so a stale poll response cannot lock a newly issued phone number.
    """
    now = int(time.time())
    with db() as cx:
        cursor = cx.execute('''UPDATE orders SET sms_received_at=?, updated_at=?
            WHERE id=? AND sms_order_id=? AND sms_received_at IS NULL
            AND status IN ('active', 'replacing')''',
            (now, now, order_id, str(sms_order_id)))
        return cursor.rowcount == 1


def unseen_sms(order_id: int, fingerprint: str) -> bool:
    with db() as cx: return cx.execute('SELECT 1 FROM seen_sms WHERE order_id=? AND fingerprint=?',(order_id,fingerprint)).fetchone() is None


def mark_sms(order_id: int, fingerprint: str):
    with db() as cx: cx.execute('INSERT OR IGNORE INTO seen_sms VALUES (?,?)',(order_id,fingerprint))


def consume_approval_try(order_id: int, now: int, max_attempts: int) -> int | None:
    """Atomic counter which does not reset when the bot is restarted."""
    with db() as cx:
        cur = cx.execute('''UPDATE orders SET approval_tries=approval_tries+1,updated_at=?
                              WHERE id=? AND status='hold' AND hold_deadline>=?
                              AND approval_tries<?''', (now, order_id, now, max_attempts))
        if cur.rowcount != 1:
            return None
        return cx.execute('SELECT approval_tries FROM orders WHERE id=?', (order_id,)).fetchone()[0]


def claim_achievement_post(order_id: int, channel_id: int) -> bool:
    """Claim once before Discord send to avoid duplicate posts after uncertainty/restart."""
    with db() as cx:
        cursor = cx.execute(
            "INSERT OR IGNORE INTO achievement_posts(order_id,channel_id,message_id,status,updated_at) "
            "VALUES (?,?,NULL,'sending',?)", (order_id, channel_id, int(time.time())))
        return cursor.rowcount == 1


def get_achievement_post(order_id: int) -> dict | None:
    with db() as cx:
        row = cx.execute('SELECT * FROM achievement_posts WHERE order_id=?', (order_id,)).fetchone()
    return dict(row) if row else None


def achievement_posted(order_id: int, message_id: int):
    with db() as cx:
        cx.execute("UPDATE achievement_posts SET status='posted',message_id=?,updated_at=? "
                   "WHERE order_id=? AND status='sending'",
                   (message_id, int(time.time()), order_id))


def achievement_received(order_id: int):
    with db() as cx:
        cx.execute("UPDATE achievement_posts SET status='received',updated_at=? "
                   "WHERE order_id=? AND status='posted'",
                   (int(time.time()), order_id))


def achievement_needs_review(order_id: int):
    """Discord send/edit may have reached its destination; never blindly send twice."""
    with db() as cx:
        cx.execute("UPDATE achievement_posts SET status='needs_review',updated_at=? "
                   "WHERE order_id=? AND status IN ('sending','posted')",
                   (int(time.time()), order_id))


def clear_paypay_login(owner_id: int) -> int:
    """Discard only encrypted PayPay credentials for this owner.

    The orders, payment link dedupe, SMS records, achievements, settings,
    and encryption key are intentionally left untouched.
    """
    with db() as cx:
        cur = cx.execute('DELETE FROM auth WHERE owner_id=?', (owner_id,))
        return cur.rowcount
