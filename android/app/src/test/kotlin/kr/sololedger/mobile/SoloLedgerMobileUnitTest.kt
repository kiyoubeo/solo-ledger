package kr.sololedger.mobile

import kr.sololedger.mobile.data.SoloLedgerCatalog
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class SoloLedgerMobileUnitTest {
  @Test
  fun normalizeServerUrlAddsHttpAndStripsTrailingSlash() {
    assertEquals("http://100.64.0.10:8000", SoloLedgerCatalog.normalizeServerUrl("100.64.0.10:8000/"))
    assertEquals("https://ledger.example.com", SoloLedgerCatalog.normalizeServerUrl("https://ledger.example.com/"))
    assertEquals("", SoloLedgerCatalog.normalizeServerUrl("   "))
  }

  @Test
  fun formatBizRegNoFormats10DigitsAndMarketplaceSeller() {
    assertEquals("120-88-00767", SoloLedgerCatalog.formatBizRegNo("1208800767", null))
    assertTrue(SoloLedgerCatalog.formatBizRegNo(null, "오픈마켓상사").contains("입점판매자: 오픈마켓상사"))
  }

  @Test
  fun wooriPushFilterSeparatesRealApprovalsFromAds() {
    assertTrue(SoloLedgerCatalog.isWooriOrTargetCardPackage("com.wooricard.smartapp"))
    assertFalse(SoloLedgerCatalog.isWooriOrTargetCardPackage("com.kakao.talk"))

    assertTrue(
      SoloLedgerCatalog.isPromotionalPush(
        "[광고] 우리WON카드 이벤트",
        "지금 이벤트 응모하면 30,000원 캐시백 혜택!"
      )
    )
    assertFalse(
      SoloLedgerCatalog.isPromotionalPush(
        "[우리카드] 승인알림",
        "우리(6534)승인 18,500원 일시불 11/05 12:30 스타벅스 여의도점"
      )
    )
  }
}
