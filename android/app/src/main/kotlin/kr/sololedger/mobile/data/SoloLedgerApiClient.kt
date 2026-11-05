package kr.sololedger.mobile.data

import android.os.Build
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject

class ApiException(val statusCode: Int, message: String) : Exception(message)

class SoloLedgerApiClient(private val prefs: AppPreferences) {

  private fun requireBaseUrl(): String {
    val url = SoloLedgerCatalog.normalizeServerUrl(prefs.serverUrl)
    if (url.isEmpty()) {
      throw IllegalStateException("먼저 [설정·보안] 탭에서 사무실 PC 서버 주소를 입력해 주세요.")
    }
    return url
  }

  private fun openConnection(
    path: String,
    method: String,
    timeoutMs: Int = 25_000,
    customBaseUrl: String? = null,
    customAuthToken: String? = null,
  ): HttpURLConnection {
    val base = customBaseUrl?.let { SoloLedgerCatalog.normalizeServerUrl(it) } ?: requireBaseUrl()
    val fullUrl = if (path.startsWith("http")) path else "$base$path"
    val conn = (URL(fullUrl).openConnection() as HttpURLConnection)
    conn.requestMethod = method
    conn.connectTimeout = 10_000
    conn.readTimeout = timeoutMs
    val token = (customAuthToken ?: prefs.effectiveAuthToken()).trim()
    if (token.isNotEmpty()) {
      conn.setRequestProperty("Authorization", "Bearer $token")
      conn.setRequestProperty("X-API-Key", token)
    }
    return conn
  }

  private fun requestJson(
    path: String,
    method: String = "GET",
    body: JSONObject? = null,
    timeoutMs: Int = 30_000,
    customBaseUrl: String? = null,
    customAuthToken: String? = null,
  ): JSONObject {
    val conn = openConnection(path, method, timeoutMs, customBaseUrl, customAuthToken)
    try {
      if (body != null) {
        conn.doOutput = true
        conn.setRequestProperty("Content-Type", "application/json; charset=utf-8")
        val payload = body.toString().toByteArray(Charsets.UTF_8)
        conn.outputStream.use { it.write(payload) }
      }
      val code = conn.responseCode
      val stream = if (code in 200..299) conn.inputStream else conn.errorStream
      val text = stream?.bufferedReader(Charsets.UTF_8)?.use { it.readText() }.orEmpty()
      if (code !in 200..299) {
        val detail =
          try {
            val errJson = JSONObject(text)
            val d = errJson.opt("detail")
            when (d) {
              is JSONObject -> d.optString("message", d.toString())
              is String -> d
              else -> text.ifBlank { "HTTP $code 오류" }
            }
          } catch (_: Exception) {
            text.ifBlank { "HTTP $code 오류" }
          }
        throw ApiException(code, detail)
      }
      return if (text.isBlank()) JSONObject() else JSONObject(text)
    } finally {
      conn.disconnect()
    }
  }

  suspend fun loginAndVerify(serverUrl: String, credential: String): String =
    withContext(Dispatchers.IO) {
      val normalizedUrl = SoloLedgerCatalog.normalizeServerUrl(serverUrl)
      if (normalizedUrl.isEmpty()) {
        throw IllegalArgumentException("서버 주소를 입력해 주세요. (예: http://100.64.0.10:8000)")
      }
      val health = requestJson("/health", method = "GET", customBaseUrl = normalizedUrl, customAuthToken = credential)
      val serverVer = health.optString("version", "1.x")
      val loginPayload =
        JSONObject().apply {
          put("credential", credential.trim())
          put("device_name", "${Build.MANUFACTURER} ${Build.MODEL}".trim())
        }
      val loginRes =
        requestJson(
          "/api/v1/auth/login",
          method = "POST",
          body = loginPayload,
          customBaseUrl = normalizedUrl,
          customAuthToken = credential,
        )
      val issuedToken = loginRes.optString("token", "")
      prefs.serverUrl = normalizedUrl
      prefs.credential = credential.trim()
      if (issuedToken.isNotBlank()) {
        prefs.bearerToken = issuedToken
      }
      serverVer
    }

  suspend fun fetchDashboardSummary(yearMonth: String = "ALL", cardLast4: String = "ALL"): DashboardSummary =
    withContext(Dispatchers.IO) {
      val ymEnc = URLEncoder.encode(yearMonth, "UTF-8")
      val cardEnc = URLEncoder.encode(cardLast4, "UTF-8")
      val json = requestJson("/api/v1/dashboard/summary?year_month=$ymEnc&card_last4=$cardEnc")

      val months = mutableListOf<String>()
      json.optJSONArray("available_months")?.let { arr ->
        for (i in 0 until arr.length()) months.add(arr.optString(i))
      }

      val periods = mutableListOf<MonthlyPeriodSummary>()
      json.optJSONArray("monthly_periods")?.let { arr ->
        for (i in 0 until arr.length()) {
          val o = arr.optJSONObject(i) ?: continue
          periods.add(
            MonthlyPeriodSummary(
              ym = o.optString("ym"),
              label = o.optString("label"),
              shortLabel = o.optString("short_label"),
              txCount = o.optInt("tx_count", 0),
            )
          )
        }
      }

      val cards = mutableListOf<CardSummaryItem>()
      json.optJSONArray("card_breakdown")?.let { arr ->
        for (i in 0 until arr.length()) {
          val o = arr.optJSONObject(i) ?: continue
          cards.add(
            CardSummaryItem(
              cardLast4 = o.optString("card_last4"),
              cardLabel = o.optString("card_label"),
              txCount = o.optInt("tx_count", 0),
              totalExpenditure = o.optLong("total_expenditure", 0L),
              deductibleVat = o.optLong("deductible_vat", 0L),
              receiptCount = o.optInt("receipt_count", 0),
            )
          )
        }
      }

      val vObj = json.optJSONObject("vehicle_limit") ?: JSONObject()
      val vehicle =
        VehicleLimitSummary(
          year = vObj.optInt("year", 2026),
          annualLimit = vObj.optLong("annual_limit", 15_000_000L),
          totalUsed = vObj.optLong("total_used", 0L),
          remainingLimit = vObj.optLong("remaining_limit", 15_000_000L),
          usageRatioPct = vObj.optDouble("usage_ratio_pct", 0.0),
          warningLevel = vObj.optString("warning_level", "NORMAL"),
          advice = vObj.optString("advice", ""),
        )

      val txs = parseTransactionsArray(json.optJSONArray("transactions"))

      DashboardSummary(
        yearMonth = json.optString("year_month", yearMonth),
        availableMonths = months,
        monthlyPeriods = periods,
        cardBreakdown = cards,
        totalTxCount = json.optInt("total_tx_count", txs.size),
        totalExpenditure = json.optLong("total_expenditure", 0L),
        deductibleVat = json.optLong("deductible_vat", 0L),
        nonDeductibleTotal = json.optLong("non_deductible_total", 0L),
        pendingActionCount = json.optInt("pending_action_count", 0),
        vehicleLimit = vehicle,
        transactions = txs,
      )
    }

  private fun parseTransactionsArray(arr: JSONArray?): List<LedgerTransaction> {
    if (arr == null) return emptyList()
    val out = ArrayList<LedgerTransaction>(arr.length())
    for (i in 0 until arr.length()) {
      val o = arr.optJSONObject(i) ?: continue
      out.add(
        LedgerTransaction(
          txId = o.optInt("tx_id"),
          cardLast4 = o.optString("card_last4", "0000"),
          cardLabel = o.optString("card_label", "법인카드"),
          approvalNo = o.optNullableString("approval_no"),
          transactedAt = o.optString("transacted_at", ""),
          rawMerchantName = o.optString("raw_merchant_name", ""),
          bizRegNo = o.optNullableString("biz_reg_no"),
          totalAmount = o.optLong("total_amount", 0L),
          supplyValue = o.optLong("supply_value", 0L),
          vatAmount = o.optLong("vat_amount", 0L),
          accountCode = o.optString("account_code", "999"),
          accountName = o.optString("account_name", "미분류"),
          vatDeductionType = o.optString("vat_deduction_type", "PENDING_CHECK"),
          nonDeductReason = o.optNullableString("non_deduct_reason"),
          counterpartyInfo = o.optNullableString("counterparty_info"),
          reconcileStatus = o.optString("reconcile_status", "SMS_ONLY"),
          archivedFileName = o.optNullableString("archived_file_name"),
        )
      )
    }
    return out
  }

  suspend fun fetchReceiptDetail(txId: Int): ReceiptDetail =
    withContext(Dispatchers.IO) {
      val o = requestJson("/api/v1/transactions/$txId/receipt")
      val items = mutableListOf<StatementLineItem>()
      o.optJSONArray("line_items")?.let { arr ->
        for (i in 0 until arr.length()) {
          val li = arr.optJSONObject(i) ?: continue
          items.add(
            StatementLineItem(
              item = li.optString("item", li.optString("name", "")),
              qty = li.optInt("qty", 1).coerceAtLeast(1),
              price = li.optLong("price", li.optLong("amount", 0L)),
              seller = li.optNullableString("seller"),
            )
          )
        }
      }
      ReceiptDetail(
        txId = o.optInt("tx_id", txId),
        cardLast4 = o.optString("card_last4", "0000"),
        cardLabel = o.optString("card_label", "법인카드"),
        printTitle = o.optString("print_title", ""),
        statementPrintTitle = o.optString("statement_print_title", ""),
        transactedAt = o.optString("transacted_at", ""),
        approvalNo = o.optNullableString("approval_no"),
        rawMerchantName = o.optString("raw_merchant_name", ""),
        bizRegNo = o.optNullableString("biz_reg_no"),
        marketplaceSeller = o.optNullableString("marketplace_seller"),
        totalAmount = o.optLong("total_amount", 0L),
        supplyValue = o.optLong("supply_value", 0L),
        vatAmount = o.optLong("vat_amount", 0L),
        accountCode = o.optString("account_code", "999"),
        accountName = o.optString("account_name", "미분류"),
        vatDeductionType = o.optString("vat_deduction_type", "DEDUCTIBLE"),
        counterpartyInfo = o.optNullableString("counterparty_info"),
        reconcileStatus = o.optString("reconcile_status", ""),
        hasReceiptFile = o.optBoolean("has_receipt_file", false),
        isRenderableImage = o.optBoolean("is_renderable_image", false),
        archivedFileName = o.optNullableString("archived_file_name"),
        lineItems = items,
        receiptFileUrl = o.optNullableString("receipt_file_url"),
      )
    }

  suspend fun fetchReceiptImageBytes(txId: Int): ByteArray? =
    withContext(Dispatchers.IO) {
      val conn = openConnection("/api/v1/transactions/$txId/receipt-file", "GET", timeoutMs = 20_000)
      try {
        if (conn.responseCode in 200..299) {
          conn.inputStream.use { it.readBytes() }
        } else {
          null
        }
      } catch (_: Exception) {
        null
      } finally {
        conn.disconnect()
      }
    }

  suspend fun updateTransaction(
    txId: Int,
    accountCode: String,
    counterpartyInfo: String,
    applyToSameMerchant: Boolean,
  ) =
    withContext(Dispatchers.IO) {
      val accountName = SoloLedgerCatalog.accounts.find { it.code == accountCode }?.name
      val payload =
        JSONObject().apply {
          put("account_code", accountCode)
          if (accountName != null) put("account_name", accountName)
          put("counterparty_info", counterpartyInfo.trim())
          put("apply_to_same_merchant", applyToSameMerchant)
        }
      requestJson("/api/v1/transactions/$txId", method = "PATCH", body = payload)
    }

  suspend fun saveTransactionStatement(txId: Int, itemName: String, accountCode: String) =
    withContext(Dispatchers.IO) {
      val payload =
        JSONObject().apply {
          put("item_name", itemName.trim())
          put("account_code", accountCode)
        }
      requestJson("/api/v1/transactions/$txId/statement", method = "POST", body = payload)
    }

  suspend fun uploadReceiptBase64(
    fileName: String,
    base64Content: String,
    explicitTxId: Int? = null,
  ): JSONObject =
    withContext(Dispatchers.IO) {
      val payload =
        JSONObject().apply {
          put("file_name", fileName)
          put("file_content_b64", base64Content)
          put("source_channel", "WEB_UPLOAD")
          if (explicitTxId != null) put("tx_id", explicitTxId)
        }
      requestJson("/api/v1/receipts/upload", method = "POST", body = payload, timeoutMs = 60_000)
    }

  suspend fun importCardStatementBase64(base64Content: String): JSONObject =
    withContext(Dispatchers.IO) {
      val payload =
        JSONObject().apply {
          put("csv_content_b64", base64Content)
        }
      requestJson("/api/v1/imports/hometax-csv", method = "POST", body = payload, timeoutMs = 60_000)
    }

  suspend fun syncEmailOrders(): JSONObject =
    withContext(Dispatchers.IO) {
      val payload =
        JSONObject().apply {
          put("recent_rescan_days", 14)
        }
      requestJson("/api/v1/scraper/email-sync", method = "POST", body = payload, timeoutMs = 90_000)
    }

  suspend fun backupToNas(): JSONObject =
    withContext(Dispatchers.IO) {
      requestJson("/api/v1/system/backup-nas", method = "POST", body = JSONObject(), timeoutMs = 90_000)
    }

  suspend fun exportAndDownloadTaxPackZip(
    yearMonth: String,
    forceReexport: Boolean,
    destDir: File,
  ): Pair<TaxPackExportSummary, File> =
    withContext(Dispatchers.IO) {
      val exportBody =
        JSONObject().apply {
          put("year_month", yearMonth)
          put("force_reexport", forceReexport)
        }
      val meta = requestJson("/api/v1/exports/tax-pack", method = "POST", body = exportBody, timeoutMs = 60_000)
      val summary =
        TaxPackExportSummary(
          yearMonth = meta.optString("year_month", yearMonth),
          exportedCount = meta.optInt("exported_count", 0),
          bundledReceiptsCount = meta.optInt("bundled_receipts_count", 0),
          totalSupplyValue = meta.optLong("total_supply_value", 0L),
          deductibleVatAmount = meta.optLong("deductible_vat_amount", 0L),
          totalAmount = meta.optLong("total_amount", 0L),
        )

      destDir.mkdirs()
      val outFile = File(destDir, "SoloLedger_세무증빙_${yearMonth}.zip")
      val conn =
        openConnection(
          "/api/v1/exports/tax-pack/$yearMonth/download?auto_generate=false",
          method = "GET",
          timeoutMs = 90_000,
        )
      try {
        val code = conn.responseCode
        if (code !in 200..299) {
          throw ApiException(code, "세무증빙 ZIP 다운로드 실패 (HTTP $code)")
        }
        conn.inputStream.use { input ->
          outFile.outputStream().use { output ->
            input.copyTo(output)
          }
        }
      } finally {
        conn.disconnect()
      }
      summary to outFile
    }

  suspend fun downloadTaxPackCsv(yearMonth: String, forceReexport: Boolean, destDir: File): File =
    withContext(Dispatchers.IO) {
      destDir.mkdirs()
      val outFile = File(destDir, "SoloLedger_기장원장_${yearMonth}.csv")
      val conn =
        openConnection(
          "/api/v1/exports/tax-pack/$yearMonth/csv?force_reexport=$forceReexport&auto_generate=true",
          method = "GET",
          timeoutMs = 60_000,
        )
      try {
        val code = conn.responseCode
        if (code !in 200..299) {
          throw ApiException(code, "기장원장 CSV 다운로드 실패 (HTTP $code)")
        }
        conn.inputStream.use { input ->
          outFile.outputStream().use { output ->
            input.copyTo(output)
          }
        }
      } finally {
        conn.disconnect()
      }
      outFile
    }

  suspend fun sendQueuedCardPush(item: QueuedCardPush): JSONObject =
    withContext(Dispatchers.IO) {
      val payload =
        JSONObject().apply {
          put("package_name", item.packageName)
          put("title", item.title)
          put("body", item.body)
          put("received_at", item.receivedAt)
          put("card_hint", item.cardHint)
        }
      requestJson("/api/v1/webhooks/push", method = "POST", body = payload, timeoutMs = 15_000)
    }
}

private fun JSONObject.optNullableString(key: String): String? {
  if (isNull(key)) return null
  val v = optString(key, "")
  return v.takeIf { it.isNotBlank() && it != "null" }
}
