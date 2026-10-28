from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from typing import Any

import aiohttp

from .sms_api_base import SMSVirtualAPI, APIError, catalog_quote, catalog_offers, validate_service, Config, BASE_URL

BUY_PATH_PATTERN = re.compile(r'^/services/[a-zA-Z0-9_-]{2,40}/activate\?source=recommend$')
CANCEL_PATH = '/profile/activations/{order_id}/deactivate'
_ID_PATTERN = re.compile(r'^[0-9]{1,40}$')
_LOG = logging.getLogger(__name__)


def parse_cost(value: Any) -> int | float:
    if value is None or isinstance(value, bool):
        raise APIError('番号の最新料金を確認できません。')
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise APIError('番号の料金データが不正です。') from None
    if not amount.is_finite() or amount <= 0 or amount > 1_000_000:
        raise APIError('番号の料金データが不正です。')
    return int(amount) if amount == amount.to_integral_value() else float(amount)


def as_payload(value: Any) -> dict:
    if not isinstance(value, dict):
        raise APIError('SMS Virtualの応答形式が不明です。管理者が確認してください。')
    for key in ('data', 'activation', 'order', 'result'):
        sub = value.get(key)
        if isinstance(sub, dict):
            value = sub
    return value


def _check_identity(obj: dict, expected_id: str | None = None) -> tuple[str, str]:
    order_id = obj.get('id')
    phone = obj.get('phone_number')
    if isinstance(order_id, bool) or not isinstance(order_id, (str, int)) or not _ID_PATTERN.fullmatch(str(order_id)):
        raise APIError('応答の注文IDが不正です。管理者の確認が必要です。')
    if not isinstance(phone, str) or not phone.strip():
        raise APIError('応答の電話番号がありません。管理者の確認が必要です。')
    if expected_id is not None and str(order_id) != str(expected_id):
        raise APIError('キャンセル応答の注文IDが一致しません。再発行を停止しました。')
    return str(order_id), phone.strip()



def public_phone(value: str) -> str:
    """Render +81/81 numbers in a domestic dialing format."""
    raw = str(value).strip()
    digits = re.sub(r'[\s()\-]', '', raw)
    digits = digits.lstrip('+')
    if re.fullmatch(r'81(?:0)?\d{9,10}', digits):
        suffix = digits[2:]
        return suffix if suffix.startswith('0') else '0' + suffix
    if re.fullmatch(r'0\d{9,10}', digits):
        return digits
    return raw


def parse_expiration(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        date = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if date.tzinfo is None:
            return None
        return int(date.timestamp())
    except (ValueError, OverflowError):
        return None

def parse_activation(response: Any, service_key: str, country_id: int) -> tuple[str, str, int | None]:
    obj = as_payload(response)
    order_id, phone = _check_identity(obj)
    if obj.get('is_active') is not True:
        raise APIError('番号発行APIで有効状態を確認できません。管理者の確認が必要です。')
    service = obj.get('service')
    country = obj.get('country')
    if not isinstance(service, dict) or service.get('key') != service_key:
        raise APIError('発行された番号の種類が一致しません。管理者の確認が必要です。')
    if not isinstance(country, dict) or str(country.get('id')) != str(country_id):
        raise APIError('発行された番号の国コードが一致しません。管理者の確認が必要です。')
    return order_id, phone, parse_expiration(obj.get('expiration_date'))


def parse_deactivation(response: Any, expected_id: str, service_key: str) -> dict:
    obj = as_payload(response)
    _check_identity(obj, expected_id)
    service = obj.get('service')
    if isinstance(service, dict) and service.get('key') != service_key:
        raise APIError('キャンセルされたサービスが一致しません。再発行を停止しました。')
    if obj.get('is_active') is not False:
        raise APIError('旧番号の無効化を確認できません。再発行を停止しました。')
    if not isinstance(obj.get('received_sms'), list):
        raise APIError('キャンセル応答のSMS履歴を確認できません。再発行を停止しました。')
    return obj


def sms_items(row: dict) -> list[str]:
    raw = row.get('received_sms')
    if not isinstance(raw, list):
        return []
    result = []
    for item in raw:
        if isinstance(item, str):
            if item:
                result.append(item)
        elif isinstance(item, dict):
            text = next((item[k] for k in ('text', 'sms', 'message', 'body', 'code')
                         if isinstance(item.get(k), str) and item[k]), None)
            if text:
                result.append(text)
    return result


class OutOfStockError(APIError):
    """No supplier stock or not enough supplier funds for one activation."""


class SMSProvider:
    def __init__(self, session: aiohttp.ClientSession, config: Config):
        self.api = SMSVirtualAPI(session, config)
        self.session = session

    @property
    def write_ready(self) -> bool:
        return True

    @property
    def replacement_ready(self) -> bool:
        return True

    async def available_services(self, country: int = 182) -> list[dict]:
        catalog = await self.api.categories()
        return [row for row in catalog_offers(catalog) if row['country'] == country]

    @staticmethod
    def wallet_balance(profile: dict) -> Decimal:
        """Parse only named balance fields; never infer from unrelated amounts."""
        row = profile
        for _ in range(3):
            if 'balance' in row:
                break
            child = next((row[k] for k in ('data', 'wallet', 'profile')
                          if isinstance(row.get(k), dict)), None)
            if child is None:
                break
            row = child
        try:
            raw = row['balance']
            if isinstance(raw, bool):
                raise ValueError('boolean balance')
            balance = Decimal(str(raw))
        except (KeyError, TypeError, ValueError, InvalidOperation):
            raise APIError('残高情報を確認できません。') from None
        if not balance.is_finite() or balance < 0:
            raise APIError('残高情報を確認できません。')
        return balance

    async def offering(self, service_key: str, country_id: int) -> dict:
        service_key, country_id = validate_service(service_key, country_id)
        offer = catalog_quote(await self.api.categories(), service_key, country_id)
        if not offer:
            raise APIError('選択したサービスの購入料金を確認できません。')
        return {'activation_cost': parse_cost(offer['cost']), 'source': 'catalog'}

    async def stock_snapshot(self, service_key: str, country_id: int = 182) -> dict:
        service_key, country_id = validate_service(service_key, country_id)
        balance = self.wallet_balance(await self.api.profile())
        offering = await self.offering(service_key, country_id)
        cost = offering['activation_cost']
        return {'balance': balance, 'cost': cost,
                'cost_source': offering['source'],
                'available': balance >= Decimal(str(cost))}

    async def ensure_available(self, service_key: str, country_id: int = 182) -> dict:
        result = await self.stock_snapshot(service_key, country_id)
        if not result['available']:
            raise OutOfStockError('在庫がありません。')
        return result

    async def active(self) -> list[dict]:
        return await self.api.active_orders()

    async def _write(self, method: str, path: str, payload: dict | None) -> Any:
        if not BUY_PATH_PATTERN.fullmatch(path) and not re.fullmatch(r'/profile/activations/[0-9]{1,40}/deactivate', path):
            raise APIError('許可されていない書き込みAPIです。')
        for attempt in range(2):
            expected = self.api.access_token
            if not expected and self.api.refresh_token:
                await self.api._refresh(expected)
            headers = self.api._headers()
            args = ({'json': payload} if payload is not None else {'data': b''})
            try:
                async with self.session.request(
                    method, BASE_URL + path, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=20), **args,
                ) as response:
                    if response.status == 401 and attempt == 0:
                        await self.api._refresh(self.api.access_token)
                        continue
                    if response.status not in (200, 201, 202):
                        raise APIError(f'SMS VirtualのAPIが処理を拒否しました（HTTP {response.status}）。'
                                       '操作結果を確認するまで再実行しないでください。')
                    try:
                        data = await response.json(content_type=None)
                    except (ValueError, TypeError):
                        raise APIError('APIの応答を解析できません。操作結果を確認するまで再実行しないでください。') from None
                    if isinstance(data, dict) and (data.get('success') is False or data.get('status') is False):
                        raise APIError('APIが処理失敗を返しました。操作結果を確認してください。')
                    return data
            except (aiohttp.ClientError, asyncio.TimeoutError):
                raise APIError('SMS Virtualとの通信が切れました。操作結果を確認するまで再実行しないでください。') from None
        raise APIError('API認証を更新できません。')

    async def buy(self, service_key: str, country_id: int = 182) -> tuple[str, str, int | None]:
        service_key, country_id = validate_service(service_key, country_id)
        status = await self.ensure_available(service_key, country_id)
        path = f'/services/{service_key}/activate?source=recommend'
        data = await self._write('POST', path,
                                 {'country': str(country_id), 'cost': status['cost']})
        return parse_activation(data, service_key, country_id)

    async def cancel(self, order_id: str, service_key: str) -> dict:
        validate_service(service_key, 182)
        order_id = str(order_id)
        if not _ID_PATTERN.fullmatch(order_id):
            raise APIError('注文IDが不正です。')
        path = CANCEL_PATH.format(order_id=order_id)
        data = await self._write('POST', path, None)
        return parse_deactivation(data, order_id, service_key)
