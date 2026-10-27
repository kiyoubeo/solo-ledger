"""Public Data Portal NTS (National Tax Service) Business Registration Status API Client."""

import re
from typing import Any, Dict, List, Optional

import httpx

from sololedger.config import settings
from sololedger.models import NtsStatusResult

_CHECKSUM_WEIGHTS = (1, 3, 7, 1, 3, 7, 1, 3, 5)


def normalize_biz_reg_no(raw_biz_no: str) -> str:
    """Strip hyphens/whitespace and validate 10-digit format."""
    cleaned = re.sub(r"[\s\-]", "", raw_biz_no or "")
    if not re.fullmatch(r"\d{10}", cleaned):
        raise ValueError(f"Invalid business registration number format (expected 10 digits): {raw_biz_no}")
    return cleaned


def validate_biz_reg_no_checksum(biz_no: str) -> bool:
    """Validate 10-digit Korean Business Registration Number checksum."""
    try:
        cleaned = normalize_biz_reg_no(biz_no)
    except ValueError:
        return False

    digits = [int(ch) for ch in cleaned]
    total = sum(d * w for d, w in zip(digits[:9], _CHECKSUM_WEIGHTS))
    total += (digits[8] * 5) // 10
    check_digit = (10 - (total % 10)) % 10
    return check_digit == digits[9]


def map_nts_status_item(item: Dict[str, Any]) -> NtsStatusResult:
    """Convert a single item from NTS `/status` API JSON into NtsStatusResult."""
    biz_no = normalize_biz_reg_no(str(item.get("b_no", "")))
    b_stt_cd = str(item.get("b_stt_cd", "")).strip()
    b_stt = str(item.get("b_stt", "")).strip()
    tax_type_cd = str(item.get("tax_type_cd", "")).strip()
    tax_type_msg = str(item.get("tax_type", "")).strip()
    end_dt = str(item.get("end_dt", "")).strip() or None

    # 1. Map business operating status (ACTIVE, SUSPENDED, CLOSED)
    if b_stt_cd == "01" or "계속" in b_stt:
        biz_status = "ACTIVE"
    elif b_stt_cd == "02" or "휴업" in b_stt:
        biz_status = "SUSPENDED"
    elif b_stt_cd == "03" or "폐업" in b_stt:
        biz_status = "CLOSED"
    else:
        biz_status = "CLOSED" if "등록되지 않은" in tax_type_msg else "ACTIVE"

    # 2. Map tax type code (GENERAL, SIMPLIFIED, EXEMPT, UNREGISTERED)
    if "등록되지 않은" in tax_type_msg:
        tax_type_code = "UNREGISTERED"
    elif tax_type_cd == "01" or "일반과세자" in tax_type_msg:
        tax_type_code = "GENERAL"
    elif tax_type_cd == "07" or "세금계산서 발급사업자" in tax_type_msg:
        # Simplified taxpayer eligible to issue tax invoices -> VAT deductible
        tax_type_code = "GENERAL"
    elif tax_type_cd in ("02", "03") or "간이과세자" in tax_type_msg or "과세특례자" in tax_type_msg:
        tax_type_code = "SIMPLIFIED"
    elif tax_type_cd in ("04", "05", "06") or "면세" in tax_type_msg or "비영리" in tax_type_msg:
        tax_type_code = "EXEMPT"
    else:
        tax_type_code = "UNKNOWN"

    return NtsStatusResult(
        biz_reg_no=biz_no,
        tax_type_code=tax_type_code,
        biz_status=biz_status,
        is_valid_checksum=validate_biz_reg_no_checksum(biz_no),
        raw_tax_type_msg=tax_type_msg or None,
        closed_dt=end_dt,
        from_cache=False,
    )


async def fetch_nts_biz_status(
    biz_numbers: List[str],
    service_key: Optional[str] = None,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> Dict[str, NtsStatusResult]:
    """Query Public Data Portal NTS `/status` API for up to 100 business registration numbers.

    If `service_key` is not configured and no custom transport is supplied, returns a safe
    offline fallback result with checksum validation so local development runs without errors.
    """
    normalized_list = [normalize_biz_reg_no(b) for b in biz_numbers]
    resolved_key = service_key if service_key is not None else settings.nts_api_service_key

    if not resolved_key and transport is None:
        return {
            b_no: NtsStatusResult(
                biz_reg_no=b_no,
                tax_type_code="GENERAL" if validate_biz_reg_no_checksum(b_no) else "UNREGISTERED",
                biz_status="ACTIVE" if validate_biz_reg_no_checksum(b_no) else "CLOSED",
                is_valid_checksum=validate_biz_reg_no_checksum(b_no),
                raw_tax_type_msg="LOCAL_FALLBACK_NO_API_KEY",
                from_cache=False,
            )
            for b_no in normalized_list
        }

    url = f"{settings.nts_api_base_url.rstrip('/')}/status"
    params = {"serviceKey": resolved_key or "mock-service-key"}
    payload = {"b_no": normalized_list}

    async with httpx.AsyncClient(transport=transport, timeout=10.0) as client:
        response = await client.post(url, params=params, json=payload)
        response.raise_for_status()
        body = response.json()

    results: Dict[str, NtsStatusResult] = {}
    for raw_item in body.get("data", []):
        mapped = map_nts_status_item(raw_item)
        results[mapped.biz_reg_no] = mapped

    for b_no in normalized_list:
        if b_no not in results:
            results[b_no] = NtsStatusResult(
                biz_reg_no=b_no,
                tax_type_code="UNKNOWN",
                biz_status="ACTIVE",
                is_valid_checksum=validate_biz_reg_no_checksum(b_no),
                from_cache=False,
            )

    return results
