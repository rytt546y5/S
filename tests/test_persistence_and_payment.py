import ast
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import storage


class StorageSchemaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'orders.sqlite3'
        self.patch = patch.object(storage, 'DB', self.db)
        self.patch.start()
        storage.initialize()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def test_different_services_are_durable(self):
        one = storage.make_order(101, 200, 'test_link_a', 600, 'p001', 'mm_0', 182)
        two = storage.make_order(102, 200, 'test_link_b', 600, 'p002', 'tg_0', 1)
        self.assertEqual((storage.get_order(one)['sms_service_key'],
                          storage.get_order(one)['sms_country_id']), ('mm_0', 182))
        self.assertEqual((storage.get_order(two)['sms_service_key'],
                          storage.get_order(two)['sms_country_id']), ('tg_0', 1))

    def test_admin_order_keeps_service(self):
        key = storage.make_test_order(999, 200, 'mm_0', 182)
        row = storage.get_order(key)
        self.assertEqual(row['sms_service_key'], 'mm_0')
        self.assertEqual(row['status'], 'paid_unfulfilled')
        self.assertEqual(row['source'], 'test')

    def test_migration_has_no_default_service(self):
        with storage.db() as cx:
            cx.execute('''INSERT INTO orders(buyer_id,guild_id,pay_code,amount,status,created_at,updated_at)
                          VALUES (1, 2, 'legacy', 600, 'active', 0, 0)''')
            row = cx.execute('SELECT sms_service_key,sms_country_id FROM orders WHERE pay_code="legacy"').fetchone()
        self.assertIsNone(row['sms_service_key'])
        self.assertIsNone(row['sms_country_id'])


class PaymentSourceTests(unittest.TestCase):
    def test_fresh_request_uuid(self):
        path = Path(__file__).resolve().parents[1] / 'services' / 'paypayu.py'
        source = path.read_text()
        self.assertIn('"requestId": str(uuid_lib.uuid4()).upper()', source)
        self.assertNotIn('"requestId":link_info', source)
        self.assertNotIn('LICENSE_KEY', source)
        self.assertNotIn('order/check', source)
        ast.parse(source)

    def test_no_implicit_default_service_in_commands(self):
        path = Path(__file__).resolve().parents[1] / 'cogs' / 'sales.py'
        tree = ast.parse(path.read_text())
        sales = next(c for c in tree.body if isinstance(c, ast.ClassDef) and c.name == 'Sales')
        for name in ('order', 'setup_panel'):
            fn = next(c for c in sales.body if isinstance(c, ast.AsyncFunctionDef) and c.name == name)
            self.assertIn('service', [a.arg for a in fn.args.args])
            args = fn.args.args
            required = args[:len(args)-len(fn.args.defaults)]
            self.assertIn('service', [arg.arg for arg in required])


if __name__ == '__main__':
    unittest.main()
