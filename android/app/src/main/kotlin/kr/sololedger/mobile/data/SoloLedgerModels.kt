package kr.sololedger.mobile.data

import java.text.NumberFormat
import java.util.Locale

data class TaxAccountOption(
  val code: String,
  val name: String,
  val shortDesc: String,
) {
  val label: String
    get() = "$code $name"
}

object SoloLedgerCatalog {
  val accounts: List<TaxAccountOption> =
    listOf(
      TaxAccountOption("811", "복리후생비", "임직원 식대·음료 (공제)"),
      TaxAccountOption("813", "기업업무추진비", "거래처 접대·선물 (불공제)"),
      TaxAccountOption("830", "소모품비", "사무용품·전산소모품 (공제)"),
      TaxAccountOption("822", "차량유지비", "주유·주차·정비 (불공제·1500만 한도)"),
      TaxAccountOption("812", "여비교통비", "출장·KTX·대중교통"),
      TaxAccountOption("814", "통신비", "인터넷·우편·클라우드"),
      TaxAccountOption("826", "도서인쇄비", "서적·인쇄·전자책"),
      TaxAccountOption("831", "지급수수료", "세무기장·SaaS·인증수수료"),
      TaxAccountOption("212", "비품", "노트북·모니터 등 자산"),
      TaxAccountOption("999", "가계정(미분류)", "확인 및 소명 필요"),
    )

  val wooriPackages: Set<String> =
    setOf(
      "com.wooricard.smartapp",
      "com.wooricard.wpay",
      "com.wooricard.smartapp.corp",
      "com.wooribank.smart.npib",
    )

  fun normalizeServerUrl(raw: String): String {
    val trimmed = raw.trim().trimEnd('/')
    if (trimmed.isEmpty()) return ""
    return if (trimmed.startsWith("http://") || trimmed.startsWith("https://")) {
      trimmed
    } else {
      "http://$trimmed"
    }
  }

  fun formatKrw(amount: Long): String {
    return NumberFormat.getNumberInstance(Locale.KOREA).format(amount) + "원"
  }

  fun formatBizRegNo(rawBiz: String?, marketplaceSeller: String?): String {
    val clean = rawBiz?.trim().orEmpty()
    val digits = clean.filter { it.isDigit() }
    if (digits.length == 10) {
      return "${digits.substring(0, 3)}-${digits.substring(3, 5)}-${digits.substring(5)}"
    }
    if (clean.isNotEmpty()) return clean
    if (!marketplaceSeller.isNullOrBlank()) {
      return "미등록 (입점판매자: ${marketplaceSeller.trim()} · 홈택스 대사 대기)"
    }
    return "미등록 (홈택스·카드내역 대사 시 확정)"
  }

  fun isWooriOrTargetCardPackage(packageName: String): Boolean {
    val pkg = packageName.trim().lowercase(Locale.ROOT)
    return pkg in wooriPackages || pkg.contains("wooricard")
  }

  fun isPromotionalPush(title: String, body: String): Boolean {
    val combined = "$title\n$body".trim()
    if (combined.isEmpty()) return true
    val adTokens = listOf("(광고)", "[광고]", "광고)", "수신거부", "이벤트 응모", "캐시백 혜택", "이용대금명세서")
    if (adTokens.any { combined.contains(it) }) return true
    val txTokens = listOf("승인", "취소", "일시불", "개월", "할부", "체크결제", "법인결제", "결제완료")
    return txTokens.none { combined.contains(it) }
  }
}

data class MonthlyPeriodSummary(
  val ym: String,
  val label: String,
  val shortLabel: String,
  val txCount: Int,
)

data class CardSummaryItem(
  val cardLast4: String,
  val cardLabel: String,
  val txCount: Int,
  val totalExpenditure: Long,
  val deductibleVat: Long,
  val receiptCount: Int,
)

data class VehicleLimitSummary(
  val year: Int,
  val annualLimit: Long,
  val totalUsed: Long,
  val remainingLimit: Long,
  val usageRatioPct: Double,
  val warningLevel: String,
  val advice: String,
)

data class LedgerTransaction(
  val txId: Int,
  val cardLast4: String,
  val cardLabel: String,
  val approvalNo: String?,
  val transactedAt: String,
  val rawMerchantName: String,
  val bizRegNo: String?,
  val totalAmount: Long,
  val supplyValue: Long,
  val vatAmount: Long,
  val accountCode: String,
  val accountName: String,
  val vatDeductionType: String,
  val nonDeductReason: String?,
  val counterpartyInfo: String?,
  val reconcileStatus: String,
  val archivedFileName: String?,
) {
  val needsAttention: Boolean
    get() = accountCode == "999" || counterpartyInfo.isNullOrBlank()
}

data class DashboardSummary(
  val yearMonth: String,
  val availableMonths: List<String>,
  val monthlyPeriods: List<MonthlyPeriodSummary>,
  val cardBreakdown: List<CardSummaryItem>,
  val totalTxCount: Int,
  val totalExpenditure: Long,
  val deductibleVat: Long,
  val nonDeductibleTotal: Long,
  val pendingActionCount: Int,
  val vehicleLimit: VehicleLimitSummary,
  val transactions: List<LedgerTransaction>,
)

data class StatementLineItem(
  val item: String,
  val qty: Int,
  val price: Long,
  val seller: String?,
)

data class ReceiptDetail(
  val txId: Int,
  val cardLast4: String,
  val cardLabel: String,
  val printTitle: String,
  val statementPrintTitle: String,
  val transactedAt: String,
  val approvalNo: String?,
  val rawMerchantName: String,
  val bizRegNo: String?,
  val marketplaceSeller: String?,
  val totalAmount: Long,
  val supplyValue: Long,
  val vatAmount: Long,
  val accountCode: String,
  val accountName: String,
  val vatDeductionType: String,
  val counterpartyInfo: String?,
  val reconcileStatus: String,
  val hasReceiptFile: Boolean,
  val isRenderableImage: Boolean,
  val archivedFileName: String?,
  val lineItems: List<StatementLineItem>,
  val receiptFileUrl: String?,
)

data class TaxPackExportSummary(
  val yearMonth: String,
  val exportedCount: Int,
  val bundledReceiptsCount: Int,
  val totalSupplyValue: Long,
  val deductibleVatAmount: Long,
  val totalAmount: Long,
)

data class QueuedCardPush(
  val id: String,
  val packageName: String,
  val title: String,
  val body: String,
  val receivedAt: String,
  val cardHint: String,
)
