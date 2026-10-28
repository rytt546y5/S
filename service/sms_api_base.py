from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import aiohttp

BASE_URL = "https://api.smsvirtual.app"
CATEGORY_PATH = "/services/categories"
PROFILE_PATH = "/profile/me"
ACTIVE_PATH = "/profile/activations/active"
REFRESH_PATH = "/profile/token/refresh"
READ_PATHS = frozenset((CATEGORY_PATH, PROFILE_PATH, ACTIVE_PATH))
USER_AGENT = "SMSVirtual-Checkout-Bot/1.0"
_LOG = logging.getLogger(__name__)


class APIError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    bot_token: str
    owner_id: int
    access_token: str
    refresh_token: str

    @classmethod
    def from_env(cls) -> Config:
        bot_token = os.getenv("BOT_TOKEN", "").strip()
        owner = os.getenv("OWNER_ID", "").strip()
        if not bot_token:
            raise ValueError("BOT_TOKEN が設定されていません")
        if not owner.isdecimal() or int(owner) <= 0:
            raise ValueError("OWNER_ID はDiscordのユーザーID（数字）にしてください")
        access = os.getenv("SMSV_ACCESS_TOKEN", "").strip()
        refresh = os.getenv("SMSV_REFRESH_TOKEN", "").strip()
        if not access and not refresh:
            raise ValueError("SMSV_ACCESS_TOKEN または SMSV_REFRESH_TOKEN が必要です")
        return cls(bot_token=bot_token, owner_id=int(owner), access_token=access, refresh_token=refresh)


def _jwt_exp(token: str) -> int | None:
    """Only a non-security-critical renewal hint; never validates JWT authenticity."""
    try:
        payload = token.split(".")[1]
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        val = json.loads(raw)
        exp = val.get("exp")
        return exp if isinstance(exp, int) and not isinstance(exp, bool) else None
    except (IndexError, ValueError, UnicodeDecodeError, TypeError):
        return None


class SMSVirtualAPI:
    def __init__(self, session: aiohttp.ClientSession, config: Config):
        self.session = session
        self.access_token = config.access_token
        self.refresh_token = config.refresh_token
        self._refresh_lock = asyncio.Lock()

    def _headers(self) -> dict[str, str]:
        # The API host is fixed. Do not accept a URL/path or header from Discord users.
        return {
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "X-Custom-User-Agent": "SMS-Virtual-Web/1.0.1",
            "Authorization": f"Bearer {self.access_token}",
        }

    async def _refresh(self, expected_access: str) -> None:
        async with self._refresh_lock:
            if self.access_token and self.access_token != expected_access:
                return  # Another request has already refreshed the credentials.
            if not self.refresh_token:
                raise APIError("認証期限が切れています。新しいトークンを.envに設定してください。")
            try:
                async with self.session.post(
                    BASE_URL + REFRESH_PATH,
                    json={"refresh": self.refresh_token},
                    headers={
                        "Accept": "application/json",
                        "User-Agent": USER_AGENT,
                        "X-Custom-User-Agent": "SMS-Virtual-Web/1.0.1",
                    },
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as response:
                    status = response.status
                    if status != 200:
                        if status in (400, 401, 403):
                            raise APIError("トークン更新が拒否されました。新しい認証情報を.envに設定してください。")
                        raise APIError(f"トークン更新に失敗しました（HTTP {status}）。")
                    data = await response.json(content_type=None)
            except APIError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError):
                raise APIError("トークン更新時に通信エラーが発生しました。") from None
            except (ValueError, TypeError):
                raise APIError("トークン更新の応答がJSONではありません。") from None
            if not isinstance(data, dict) or not isinstance(data.get("access"), str) or not data["access"]:
                raise APIError("トークン更新の応答形式が不正です。")
            self.access_token = data["access"]
            if isinstance(data.get("refresh"), str) and data["refresh"]:
                self.refresh_token = data["refresh"]
            _LOG.info("SMS Virtual API token renewed")

    async def get_json(self, path: str) -> Any:
        if path not in READ_PATHS:
            raise APIError("読み取り専用API以外にはアクセスできません。")

        for attempt in range(3):
            exp = _jwt_exp(self.access_token) if self.access_token else None
            if not self.access_token or (self.refresh_token and exp is not None and exp <= time.time() + 60):
                await self._refresh(self.access_token)

            used_token = self.access_token
            try:
                async with self.session.get(
                    BASE_URL + path,
                    headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as response:
                    status = response.status
                    if status == 200:
                        try:
                            return await response.json(content_type=None)
                        except (ValueError, TypeError):
                            raise APIError("APIから有効なJSONが返されませんでした。") from None
                    if status == 401:
                        if attempt == 2:
                            raise APIError("認証に失敗しました（401）。トークンを再取得してください。")
                        await self._refresh(used_token)
                        continue
                    if status == 403:
                        raise APIError("アクセスが拒否されました（403）。")
                    if status == 404:
                        raise APIError("APIが見つかりません（404）。Web側の仕様変更の可能性があります。")
                    if status == 429:
                        if attempt == 2:
                            raise APIError("アクセス制限（429）です。時間を空けて再実行してください。")
                    elif 500 <= status < 600:
                        if attempt == 2:
                            raise APIError(f"SMS Virtual側でエラーが発生しました（HTTP {status}）。")
                    else:
                        raise APIError(f"APIからHTTP {status}が返りました。")
            except APIError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError):
                if attempt == 2:
                    raise APIError("通信が失敗しました。ネットワークを確認してください。") from None
            await asyncio.sleep(1 if attempt == 0 else 2)
        raise APIError("APIの応答を確認できませんでした。")

    async def profile(self) -> dict[str, Any]:
        data = await self.get_json(PROFILE_PATH)
        if not isinstance(data, dict):
            raise APIError("残高データの形式が想定と異なります。")
        return data

    async def categories(self) -> dict[str, Any] | list[Any]:
        data = await self.get_json(CATEGORY_PATH)
        if not isinstance(data, (dict, list)):
            raise APIError("サービス一覧の形式が想定と異なります。")
        return data

    async def active_orders(self) -> list[dict[str, Any]]:
        data = await self.get_json(ACTIVE_PATH)
        if not isinstance(data, list):
            raise APIError("注文一覧の形式が想定と異なります。")
        return [x for x in data if isinstance(x, dict)]


SERVICE_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{2,40}$")


def validate_service(service: str, country: int) -> tuple[str, int]:
    if not isinstance(service, str) or not SERVICE_PATTERN.fullmatch(service):
        raise APIError("サービスコードが正しくありません。")
    if isinstance(country, bool) or not isinstance(country, int) or not 1 <= country <= 99999:
        raise APIError("国コードが正しくありません。")
    return service, country


def catalog_offers(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, (dict, list)):
        return []
    result: list[dict[str, Any]] = []
    nodes = [(data, None, None, None, 0)]
    visited = 0
    while nodes and visited < 30000:
        node, parent_service, parent_country, parent_name, depth = nodes.pop()
        visited += 1
        if depth > 16:
            continue
        if isinstance(node, list):
            nodes.extend((item, parent_service, parent_country, parent_name, depth + 1)
                         for item in reversed(node[:5000]))
            continue
        if not isinstance(node, dict):
            continue
        raw_service = next((node[key] for key in ("service", "service_key", "serviceKey", "key")
                            if node.get(key) is not None), None)
        if isinstance(raw_service, dict):
            raw_name = next((raw_service.get(k) for k in ("title_en", "title", "name")
                             if raw_service.get(k)), None)
            raw_service = raw_service.get("key", raw_service.get("service_key"))
        else:
            raw_name = next((node.get(k) for k in ("title_en", "title", "name")
                             if isinstance(node.get(k), str) and node.get(k)), None)
        service = str(raw_service) if raw_service is not None else parent_service
        name = str(raw_name) if raw_name else parent_name
        raw_country = next((node[key] for key in ("country", "country_id", "countryId")
                            if node.get(key) is not None), None)
        if isinstance(raw_country, dict):
            raw_country = raw_country.get("id", raw_country.get("country_id"))
        country = str(raw_country) if raw_country is not None else parent_country
        raw_cost = next((node[key] for key in ("activation_cost", "activationCost", "cost", "price")
                         if node.get(key) is not None), None)
        try:
            from decimal import Decimal
            value = Decimal(str(raw_cost))
            valid_cost = value.is_finite() and 0 < value <= 1000000
        except (ValueError, TypeError, ArithmeticError):
            valid_cost = False
        if service and SERVICE_PATTERN.fullmatch(service) and country and country.isdecimal() and valid_cost:
            result.append({"service": service, "country": int(country), "cost": raw_cost,
                           "name": (name or service)[:90]})
        for key, child in reversed(list(node.items())):
            if not isinstance(child, (dict, list)):
                continue
            next_service = key if SERVICE_PATTERN.fullmatch(key) and "_" in key and key not in (
                "activation_cost", "country_id", "service_key") else service
            next_country = key if key.isdecimal() and 1 <= int(key) <= 99999 else country
            nodes.append((child, next_service, next_country, name, depth + 1))
    unique: dict[tuple[str, int], dict] = {}
    for item in result:
        unique[item["service"], item["country"]] = item
    return list(unique.values())


def catalog_quote(data: Any, service: str, country: int) -> dict[str, Any] | None:
    service, country = validate_service(service, country)
    return next((offer for offer in catalog_offers(data)
                 if offer["service"] == service and offer["country"] == country), None)


def safe_quantity(value: Any, suffix: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "不明"
    if value < 0:
        return "不明"
    return f"{value:,}{suffix}"


def format_remaining(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return "不明"
    try:
        expiry = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            return "不明"
        seconds = (expiry - datetime.now(timezone.utc)).total_seconds()
        if seconds <= 0:
            return "期限切れ"
        return f"約{int((seconds + 59) // 60)}分"
    except (ValueError, OverflowError):
        return "不明"


def order_summary(order: dict[str, Any]) -> str:
    """Internal operation."""
    received = order.get("received_sms")
    received_count = len(received) if isinstance(received, list) else 0
    status = "利用中" if order.get("is_active") is True else "終了"
    return (
        "**SMS認証番号**\n"
        f"状態：`{status}` ／ SMS受信件数：`{received_count}`\n"
        f"残り時間：`{format_remaining(order.get('expiration_date'))}`"
    )
