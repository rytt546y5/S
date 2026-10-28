import aiohttp
import asyncio
import datetime
import os
import re
import logging
import uuid as uuid_lib
from useragent_changer import UserAgent

ua =UserAgent('iphone')
_log = logging.getLogger(__name__)
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=25, connect=10, sock_read=20)
_RECEIVE_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10, sock_read=22)


_LINK_URL_RE = re.compile(r'^(?:https?://)?pay\.paypay\.ne\.jp/([A-Za-z0-9]+)/?$')
_LINK_CODE_RE = re.compile(r'^[A-Za-z0-9]+$')

def parse_link(cd):
    """Normalize a PayPay P2P link to its verification code. Returns None if invalid."""
    if not isinstance(cd, str):
        return None
    cd = cd.strip()
    if not cd:
        return None
    m = _LINK_URL_RE.match(cd)
    if m:
        return m.group(1)
    if _LINK_CODE_RE.match(cd):
        return cd
    return None

# --- send login request ---
async def login(phoneNumber: str, password: str, uuid: str):
    headers = {
        'User-Agent': ua.set(),
        'Accept' : 'application/json, text/plain, */*',
        'Content-Type' : 'application/json',
        'Origin': 'https://www.paypay.ne.jp',
        'Referer':'https://www.paypay.ne.jp/app/account/sign-in',
    }
    payload = {
        "scope":"SIGN_IN",
        "client_uuid":f"{uuid}",
        "grant_type":"password",
        "username":phoneNumber,
        "password":password,
        "add_otp_prefix": True,
        "language":"ja"
    }
    async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
        async with session.post("https://www.paypay.ne.jp/app/v1/oauth/token", headers=headers, json=payload) as login_request_response:
            return await login_request_response.json()

# --- one-time-password authentication ---
async def login_otp(set_uuid, otp, otpid, otp_pre):
    """Reference vending workflow: OTP validates the device, then saves phone/password/UUID."""
    headers = {
        'User-Agent': ua.set(),
        'Accept': 'application/json, text/plain, */*',
        'Content-Type': 'application/json',
        'Origin': 'https://www.paypay.ne.jp',
        'Referer': 'https://www.paypay.ne.jp/app/account/sign-in',
    }
    payload = {
        'scope': 'SIGN_IN', 'client_uuid': str(set_uuid),
        'grant_type': 'otp', 'otp_prefix': str(otp_pre),
        'otp': otp, 'otp_reference_id': otpid,
        'username_type': 'MOBILE', 'language': 'ja',
    }
    async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
        async with session.post('https://www.paypay.ne.jp/app/v1/oauth/token', headers=headers, json=payload) as response:
            answer = await response.json(content_type=None)
    if not isinstance(answer, dict) or response.status != 200:
        return 'ERR'
    if answer.get('response_type') == 'ErrorResponse' or answer.get('error'):
        return 'ERR'
    # Same result interpretation as supplied vending source, with HTTP and JSON checks.
    return 'OK'

async def check_link(cd):
    cd = parse_link(cd)
    if cd is None:
        return False

    headers={
        "Accept":"application/json, text/plain, */*",
        'User-Agent': ua.set(),
        "Content-Type":"application/json"
    }
    
    async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
        try:
            async with session.get(f"https://www.paypay.ne.jp/app/v2/p2p-api/getP2PLinkInfo?verificationCode={cd}", headers=headers) as response:
                response.raise_for_status()
                link_info = await response.json()
            
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            _log.warning("PayPay link lookup failed: %s", type(e).__name__)
            return False
    
    result_code = link_info.get("header", {}).get("resultCode")
    if result_code != "S0000":
        return False

    order_status = link_info.get("payload", {}).get("orderStatus")
    if order_status == "PENDING":
        return link_info
    else:
        return False
    
async def link_rev(cd: str, phoneNumber: str, password: str, uuid: str, link_password: str = None):
    cd = parse_link(cd)
    if cd is None:
        return False

    async with aiohttp.ClientSession(timeout=_RECEIVE_TIMEOUT) as session:
        base_headers = {
            "Accept": "application/json, text/plain, */*",
            'User-Agent': ua.set(),
            "Content-Type": "application/json"
        }
        
        try:
            async with session.get(f"https://www.paypay.ne.jp/app/v2/p2p-api/getP2PLinkInfo?verificationCode={cd}", headers=base_headers) as response:
                response.raise_for_status()
                link_info = await response.json()

            if link_info.get("header", {}).get("resultCode") != "S0000" or link_info.get("payload", {}).get("orderStatus") != "PENDING":
                return False
            
            if link_info.get("payload", {}).get("pendingP2PInfo", {}).get("isSetPasscode") and link_password is None:
                return False

        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            _log.warning("PayPay receipt link lookup failed: %s", type(e).__name__)
            return False
        
        login_payload = {
            "scope":"SIGN_IN",
            "client_uuid":f"{uuid}",
            "grant_type":"password",
            "username":phoneNumber,
            "password":password,
            "add_otp_prefix": True,
            "language":"ja"
            }

        login_headers = {
            'User-Agent': ua.set(),
            'Accept' : 'application/json, text/plain, */*',
            'Content-Type' : 'application/json',
            'Origin': 'https://www.paypay.ne.jp',
            'Referer':'https://pay.paypay.ne.jp/'+cd,
        }

        async with session.post("https://www.paypay.ne.jp/app/v1/oauth/token", headers=login_headers, json=login_payload) as response:
            login_response = await response.json(content_type=None)
            if response.status != 200 or not isinstance(login_response, dict):
                return "LOGINERR"
            try:
                login_response = (login_response["access_token"])
            except:
                try:
                    login_response["otp_reference_id"]
                    return "LOGINERR"
                except:
                    return "LOGINERR"
        
        receive_payload = {
            "verificationCode":cd,
            "client_uuid":uuid,
            "requestAt":str(datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).strftime('%Y-%m-%dT%H:%M:%S+0900')),
            "requestId": str(uuid_lib.uuid4()).upper(),
            "orderId":link_info["payload"]["message"]["data"]["orderId"],
            "senderMessageId":link_info["payload"]["message"]["messageId"],
            "senderChannelUrl":link_info["payload"]["message"]["chatRoomId"],
            "iosMinimumVersion":"3.45.0",
            "androidMinimumVersion":"3.45.0"
            }
        
        if link_password:
            receive_payload["passcode"]=link_password

        try:
            async with session.post("https://www.paypay.ne.jp/app/v2/p2p-api/acceptP2PSendMoneyLink", json=receive_payload, headers=base_headers) as response:
                http_ok = response.ok
                receive_data = await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            _log.warning("PayPay receive POST outcome uncertain: %s", type(e).__name__)
            return "UNCERTAIN"

        if not isinstance(receive_data, dict):
            return "UNCERTAIN"

        if http_ok and receive_data.get("header", {}).get("resultCode") == "S0000":
            return True

        is_pending_hold = (
            receive_data.get("header", {}).get("resultCode") == "S9999" and
            receive_data.get("error", {}).get("backendResultCode") == "42007013"
        )
        if is_pending_hold:
            return "PENDING_HOLD"

        _log.warning("PayPay receive not confirmed, result code: %s", str(receive_data.get("header", {}).get("resultCode"))[:24])
        return "UNCERTAIN"

async def get_link_status(cd):
    cd = parse_link(cd)
    if cd is None:
        return None

    headers={
        "Accept":"application/json, text/plain, */*",
        'User-Agent': ua.set(),
        "Content-Type":"application/json"
    }

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        try:
            async with session.get(f"https://www.paypay.ne.jp/app/v2/p2p-api/getP2PLinkInfo?verificationCode={cd}", headers=headers) as response:
                response.raise_for_status()
                link_info = await response.json()

        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return None

    if not isinstance(link_info, dict) or link_info.get("header", {}).get("resultCode") != "S0000":
        return None

    return link_info.get("payload", {}).get("orderStatus")

