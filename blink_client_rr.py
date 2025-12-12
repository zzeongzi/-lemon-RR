import os
from typing import Any, Dict, Optional, List, TypedDict

import aiohttp
from dotenv import load_dotenv

load_dotenv()

BLINK_API_URL = os.getenv("BLINK_API_URL", "https://api.blink.sv/graphql")
BLINK_API_KEY = os.getenv("BLINK_API_KEY")
BLINK_WALLET_ID = os.getenv("BLINK_WALLET_ID")


class BlinkError(Exception):
    pass


class GraphQLError(TypedDict, total=False):
    message: str
    path: List[str]
    locations: List[Dict[str, int]]


async def _blink_request(
    query: str,
    variables: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Blink GraphQL API 호출 공통 함수.
    """
    if not BLINK_API_KEY or not BLINK_WALLET_ID:
        print(
            "[Blink] 환경 변수 누락:",
            "BLINK_API_URL=", BLINK_API_URL,
            "BLINK_API_KEY 설정됨=", bool(BLINK_API_KEY),
            "BLINK_WALLET_ID 설정됨=", bool(BLINK_WALLET_ID),
        )
        raise BlinkError("BLINK_API_KEY 또는 BLINK_WALLET_ID 가 설정되지 않았습니다.")

    headers: Dict[str, str] = {
        "Content-Type": "application/json",
        "X-API-KEY": BLINK_API_KEY,
    }

    payload: Dict[str, Any] = {"query": query}
    if variables is not None:
        payload["variables"] = variables

    async with aiohttp.ClientSession() as session:
        async with session.post(BLINK_API_URL, json=payload, headers=headers) as resp:
            text = await resp.text()
            print(f"[Blink] 응답 <- status={resp.status}, body={text}")

            if resp.status >= 400:
                raise BlinkError(f"Blink HTTP error {resp.status}: {text}")

            try:
                data: Dict[str, Any] = await resp.json()
            except Exception as e:
                raise BlinkError(f"Blink JSON decode error: {e}, body={text}")

    errors: Optional[List[GraphQLError]] = data.get("errors")  # type: ignore[assignment]
    if errors:
        raise BlinkError(f"Blink GraphQL errors: {errors}")

    result: Dict[str, Any] = data.get("data", {})
    return result


# ─────────────────────────────────────────────
# 인보이스 생성
# ─────────────────────────────────────────────

async def create_invoice(amount_sats: int, memo: str) -> Dict[str, Any]:
    """
    Blink lnInvoiceCreate: 새 인보이스 생성.
    """
    if amount_sats <= 0:
        raise BlinkError("amount_sats must be > 0")

    print(f"[Blink] 인보이스 생성 요청: amount={amount_sats}, memo={memo}")

    query = """
    mutation lnInvoiceCreate($input: LnInvoiceCreateInput!) {
      lnInvoiceCreate(input: $input) {
        invoice {
          paymentHash
          paymentRequest
          satoshis
        }
        errors {
          message
        }
      }
    }
    """

    variables: Dict[str, Any] = {
        "input": {
            "walletId": BLINK_WALLET_ID,
            "amount": amount_sats,
            "memo": memo,
        }
    }

    data = await _blink_request(query, variables)
    ln_data: Dict[str, Any] = data.get("lnInvoiceCreate", {})
    errors = ln_data.get("errors")
    invoice = ln_data.get("invoice")

    if errors:
        raise BlinkError(f"Blink lnInvoiceCreate errors: {errors}")
    if not invoice:
        raise BlinkError("Blink lnInvoiceCreate returned no invoice")

    payment_hash = invoice.get("paymentHash")
    payment_request = invoice.get("paymentRequest")
    satoshis = invoice.get("satoshis")

    if not payment_hash or not payment_request or satoshis is None:
        raise BlinkError(f"Blink lnInvoiceCreate invalid invoice: {invoice}")

    return {
        "payment_hash": payment_hash,
        "payment_request": payment_request,
        "amount": int(satoshis),
    }


# ─────────────────────────────────────────────
# 인보이스 결제 여부 확인
# ─────────────────────────────────────────────

async def check_payment(payment_request: str) -> bool:
    """
    인보이스 결제 여부 확인.
    Blink 는 paymentRequest 기반으로만 확인 (walletId 포함 X).
    """
    if not payment_request:
        raise BlinkError("payment_request is empty")

    query = """
    query lnInvoicePaymentStatus($input: LnInvoicePaymentStatusInput!) {
      lnInvoicePaymentStatus(input: $input) {
        status
        errors {
          message
        }
      }
    }
    """

    # ⚠️ walletId 를 넣으면 안 된다 (스키마 에러 발생)
    variables: Dict[str, Any] = {
        "input": {
            "paymentRequest": payment_request,
        }
    }

    print(f"[Blink] 결제 상태 조회: paymentRequest={payment_request[:60]}...")
    try:
        data = await _blink_request(query, variables)
    except BlinkError as e:
        # 상태 조회 실패 시에는 예외를 밖으로 터뜨리지 말고 False 반환
        print("[Blink] lnInvoicePaymentStatus 호출 중 BlinkError:", e)
        return False

    print("[Blink] lnInvoicePaymentStatus raw:", data)

    status_data: Dict[str, Any] = data.get("lnInvoicePaymentStatus", {})
    errors: Optional[List[Dict[str, Any]]] = status_data.get("errors")
    if errors:
        print(f"[Blink] lnInvoicePaymentStatus errors: {errors}")
        return False

    status = status_data.get("status")
    print(f"[Blink] 결제 상태: {status}")
    return status in ("PAID", "SETTLED", "SUCCESS")


# ─────────────────────────────────────────────
# 인보이스 지불(출금)
# ─────────────────────────────────────────────

async def pay_invoice(bolt11: str, memo: str = "") -> Dict[str, Any]:
    """
    LnInvoicePaymentSendPayment: 외부 BOLT11 인보이스 지불(출금).
    """
    if not bolt11:
        raise BlinkError("bolt11 is empty")

    print(f"[Blink] 인보이스 지불 요청: memo={memo}")

    query = """
    mutation lnInvoicePaymentSend($input: LnInvoicePaymentSendInput!) {
      lnInvoicePaymentSend(input: $input) {
        status
        errors {
          message
        }
      }
    }
    """

    variables: Dict[str, Any] = {
        "input": {
            "walletId": BLINK_WALLET_ID,
            "paymentRequest": bolt11,
            "memo": memo,
        }
    }

    data = await _blink_request(query, variables)
    pay_data: Dict[str, Any] = data.get("lnInvoicePaymentSend", {})
    errors = pay_data.get("errors")
    status = pay_data.get("status")

    if errors:
        raise BlinkError(f"Blink lnInvoicePaymentSend errors: {errors}")

    print(f"[Blink] 인보이스 지불 상태: {status}")
    return {"success": status in ("SUCCESS", "PAID", "SETTLED"), "status": status}
