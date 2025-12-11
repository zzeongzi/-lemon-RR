# cashu_client.py
import requests
from typing import Dict, Any

from config import (
    CASHU_MINT_URL,
    CASHU_MINT_KEYSETS_ENDPOINT,
    CASHU_MINT_REDEEM_ENDPOINT,
    CASHU_MINT_MINT_ENDPOINT,
)

class CashuError(Exception):
    pass

def _url(path: str) -> str:
    if not path.startswith("/"):
        path = "/" + path
    return f"{CASHU_MINT_URL}{path}"

def get_mint_info() -> Dict[str, Any]:
    """
    Minibits 민트 키/정보 조회.
    실제 엔드포인트가 /keys가 아닐 수도 있으니 .env로 조정.
    """
    try:
        resp = requests.get(_url(CASHU_MINT_KEYSETS_ENDPOINT), timeout=10)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        raise CashuError(f"Failed to fetch mint info: {e}") from e

def redeem_token(token: str) -> int:
    """
    Minibits 민트에 ecash 토큰을 상환하고, 해당 토큰의 총 sats 양(int)을 반환.

    예시 형태 (실제 Minibits 문서/코드를 보고 맞출 것):

      POST {CASHU_MINT_URL}/redeem
      Body: { "token": "<cashu-token-string>" }
      Response: { "status": "ok", "amount": 1234 }

    응답 필드는 실제 스펙에 따라 조정해야 한다.
    """
    if not token or len(token.strip()) == 0:
        raise CashuError("Empty token")

    try:
        resp = requests.post(
            _url(CASHU_MINT_REDEEM_ENDPOINT),
            json={"token": token},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        raise CashuError(f"Redeem request failed: {e}") from e

    status = data.get("status")
    if status not in ("ok", "OK", "success"):
        raise CashuError(f"Redeem failed: {data}")

    # 실제 필드 이름 확인 필요: "amount", "totalAmount", ...
    amount = data.get("amount") or data.get("totalAmount")
    if not isinstance(amount, int) or amount <= 0:
        raise CashuError(f"Invalid amount from mint: {amount}")

    return amount

def mint_token(amount: int) -> str:
    """
    Minibits 민트에서 amount(sats) 만큼 새 캐슈 토큰을 발행.

    예시 형태 (실제 Minibits 문서/코드를 보고 맞출 것):

      POST {CASHU_MINT_URL}/mint
      Body: { "amount": 1234 }
      Response: { "status": "ok", "token": "<cashu-token-string>" }

    응답 필드는 실제 스펙에 따라 조정해야 한다.
    """
    if amount <= 0:
        raise CashuError("Amount must be positive")

    try:
        resp = requests.post(
            _url(CASHU_MINT_MINT_ENDPOINT),
            json={"amount": amount},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        raise CashuError(f"Mint request failed: {e}") from e

    status = data.get("status")
    if status not in ("ok", "OK", "success"):
        raise CashuError(f"Mint failed: {data}")

    token = data.get("token")
    if not isinstance(token, str) or not token:
        raise CashuError("Mint did not return a valid token")

    return token
