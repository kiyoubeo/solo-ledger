"""Telegram Bot (@SoloLedgerBot) notification helper."""

from typing import Any, Dict

import httpx

from sololedger.config import settings
from sololedger.models import ParsedSmsResult


async def send_telegram_memo_prompt(
    tx_id: int, parsed: ParsedSmsResult, rule_result: Dict[str, Any]
) -> bool:
    """Send an interactive message to the CEO's Telegram chat when an expense
    requires item clarification (e.g., Coupang) or attendee info (e.g., dining).
    """
    token = settings.telegram_bot_token
    chat_id = settings.telegram_chat_id
    if not token or token == "YOUR_TELEGRAM_BOT_TOKEN" or not chat_id:
        return False

    text = (
        f"[법인카드 실시간 전표 생성 알림]\n"
        f"- 전표번호: #{tx_id}\n"
        f"- 가맹점: {parsed.raw_merchant_name}\n"
        f"- 결제금액: {parsed.total_amount:,}원\n"
        f"- 1차 분류: {rule_result['account_name']} ({rule_result['vat_deduction_type']})\n\n"
        f"상세 품목(온라인 결제) 또는 참석자 명단(외부 미팅 시)을 답장으로 입력하거나 영수증 사진을 전송해 주십시오."
    )
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(url, json={"chat_id": chat_id, "text": text})
        return resp.status_code == 200
