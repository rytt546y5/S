import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from services.sms_api_base import APIError, catalog_offers, catalog_quote, validate_service
from services.sms_provider import SMSProvider, parse_activation, parse_deactivation, OutOfStockError


def sample_catalog():
    return {'categories': [
        {'service': {'key': 'mm_0', 'title_en': 'Microsoft'},
         'country': {'id': 182}, 'cost': 14},
        {'service': {'key': 'tg_0', 'title_en': 'Telegram'},
         'country': {'id': 182}, 'cost': 12},
        {'service': {'key': 'tg_0'}, 'country': {'id': 1}, 'price': 5},
    ]}


class DynamicServiceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.provider = SMSProvider(None, type('C', (), {'access_token': 'test', 'refresh_token': ''})())
        self.provider.api.categories = AsyncMock(return_value=sample_catalog())
        self.provider.api.profile = AsyncMock(return_value={'balance': 30})
        self.provider._write = AsyncMock(return_value={
            'id': 1001, 'phone_number': '817043895901', 'is_active': True,
            'service': {'key': 'mm_0'}, 'country': {'id': 182},
            'received_sms': []})

    async def test_quoted_services_are_distinct(self):
        self.assertEqual(len(catalog_offers(sample_catalog())), 3)
        self.assertEqual(catalog_quote(sample_catalog(), 'tg_0', 182)['cost'], 12)
        self.assertEqual(catalog_quote(sample_catalog(), 'tg_0', 1)['cost'], 5)
        self.assertIsNone(catalog_quote(sample_catalog(), 'mm_0', 1))

    async def test_buy_requires_service_and_country(self):
        result = await self.provider.buy('mm_0', 182)
        self.assertEqual(result[0], '1001')
        self.provider._write.assert_awaited_once_with(
            'POST', '/services/mm_0/activate?source=recommend', {'country': '182', 'cost': 14})

    async def test_mismatch_never_counts_as_success(self):
        with self.assertRaises(APIError):
            await self.provider.buy('tg_0', 182)

    async def test_low_wallet_blocks_write(self):
        self.provider.api.profile = AsyncMock(return_value={'balance': 3})
        with self.assertRaises(OutOfStockError):
            await self.provider.buy('mm_0', 182)
        self.provider._write.assert_not_awaited()

    async def test_missing_quote_blocks_write(self):
        with self.assertRaises(APIError):
            await self.provider.buy('unknown_0', 182)
        self.provider._write.assert_not_awaited()

    async def test_cancel_uses_same_service(self):
        self.provider._write = AsyncMock(return_value={
            'id': 1001, 'phone_number': '817043895901', 'is_active': False,
            'service': {'key': 'mm_0'}, 'received_sms': []})
        result = await self.provider.cancel('1001', 'mm_0')
        self.assertFalse(result['is_active'])
        self.provider._write.assert_awaited_once_with(
            'POST', '/profile/activations/1001/deactivate', None)
        with self.assertRaises(APIError):
            await self.provider.cancel('1001', 'tg_0')

    async def test_untrusted_service_input_rejected(self):
        for key in ('', '../../auth', 'a/b', 'a?test=x', 'a b'):
            with self.assertRaises(APIError):
                validate_service(key, 182)


if __name__ == '__main__':
    unittest.main()
