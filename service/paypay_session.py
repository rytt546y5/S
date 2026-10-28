from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Any

import aiohttp

from storage import load_login, update_auth_token, invalidate_auth_token
from services import paypayu

_log = logging.getLogger(__name__)
_auth_locks: dict[int, asyncio.Lock] = {}


class ReauthenticationRequired(Exception):
    pass


class AuthenticationUnavailable(Exception):
    pass


def expiry_from_response(data: dict, *, now: int | None = None) -> int | None:
    now = int(time.time()) if now is None else now
    raw = data.get('expires_in')
    if isinstance(raw, (int, float, str)):
        try:
            duration = float(raw)
            if 0 < duration <= 86400 * 365:
                return now + int(duration)
        except (ValueError, TypeError, OverflowError):
            pass
    token = data.get('access_token')
    if not isinstance(token, str) or token.count('.') != 2:
        return None
    try:
        payload = token.split('.')[1]
        decoded = base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4))
        exp = json.loads(decoded).get('exp')
        return int(exp) if isinstance(exp, (int, float)) else None
    except (ValueError, TypeError, UnicodeDecodeError):
        return None


def session_is_usable(creds: dict, *, now: int | None = None) -> bool:
    now = int(time.time()) if now is None else now
    token = creds.get('access_token')
    if not isinstance(token, str) or not token:
        return False
    expiry = creds.get('expires_at')
    # Unknown expiry is not grounds to assume failure; a definitive 401
    # during acceptance will clear the token and trigger a safe re-login.
    return expiry is None or (isinstance(expiry, int) and expiry > now + 60)


async def get_access_token(owner_id: int, *, force_refresh: bool = False) -> str:
    lock = _auth_locks.setdefault(owner_id, asyncio.Lock())
    async with lock:
        creds = load_login(owner_id)
        if not creds:
            raise ReauthenticationRequired('PayPayが未登録です。/paypay_login を実行してください。')
        if not force_refresh and session_is_usable(creds):
            return creds['access_token']
        phone, password, device = creds.get('phone'), creds.get('password'), creds.get('uuid')
        if not all(isinstance(item, str) and item for item in (phone, password, device)):
            raise ReauthenticationRequired('PayPayの再ログインが必要です。/paypay_login を実行してください。')
        try:
            response = await paypayu.login(phone, password, device)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            _log.warning('PayPay session restoration unavailable: %s', type(exc).__name__)
            raise AuthenticationUnavailable('PayPayとの通信に失敗しました。') from exc
        if not isinstance(response, dict):
            raise AuthenticationUnavailable('PayPayの認証応答が不正です。')
        token = response.get('access_token')
        if isinstance(token, str) and token:
            if not update_auth_token(owner_id, device, response, expiry_from_response(response)):
                raise ReauthenticationRequired('認証情報が変更されました。再度操作してください。')
            _log.info('PayPay saved session restored')
            return token
        if response.get('otp_reference_id'):
            raise ReauthenticationRequired('PayPay側でOTP認証が必要です。/paypay_login を実行してください。')
        raise ReauthenticationRequired('PayPayの認証が切れています。/paypay_login を実行してください。')


def invalidate_failed_token(owner_id: int, failed_token: str) -> None:
    invalidate_auth_token(owner_id, failed_token)
