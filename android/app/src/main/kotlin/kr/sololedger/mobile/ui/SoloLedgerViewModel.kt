package kr.sololedger.mobile.ui

import android.app.Application
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.net.Uri
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import java.io.File
import java.time.LocalDate
import java.time.format.DateTimeFormatter
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kr.sololedger.mobile.data.AppPreferences
import kr.sololedger.mobile.data.DashboardSummary
import kr.sololedger.mobile.data.ReceiptDetail
import kr.sololedger.mobile.data.SoloLedgerApiClient
import kr.sololedger.mobile.data.TaxPackExportSummary
import kr.sololedger.mobile.push.WooriPushListenerService
import kr.sololedger.mobile.util.ReceiptImageCompressor

enum class LedgerFilterMode {
  ALL,
  NEEDS_ATTENTION,
  DINING_ENTERTAINMENT,
  VEHICLE,
}

data class ShareableExportArtifact(
  val file: File,
  val mimeType: String,
  val chooserTitle: String,
)

data class SoloLedgerUiState(
  val isLoading: Boolean = false,
  val isConnected: Boolean = false,
  val serverVersion: String = "",
  val serverUrl: String = "",
  val credential: String = "",
  val wooriPushEnabled: Boolean = false,
  val wooriCardHint: String = "6534",
  val biometricLockEnabled: Boolean = false,
  val queuedPushCount: Int = 0,
  val selectedYearMonth: String = LocalDate.now().format(DateTimeFormatter.ofPattern("yyyyMM")),
  val exportYearMonth: String = LocalDate.now().format(DateTimeFormatter.ofPattern("yyyyMM")),
  val forceReexport: Boolean = true,
  val selectedCardLast4: String = "ALL",
  val filterMode: LedgerFilterMode = LedgerFilterMode.ALL,
  val searchQuery: String = "",
  val summary: DashboardSummary? = null,
  val activeReceiptDetail: ReceiptDetail? = null,
  val activeReceiptBitmap: Bitmap? = null,
  val lastExportSummary: TaxPackExportSummary? = null,
  val lastUploadMessage: String? = null,
  val statusBanner: String? = null,
  val errorBanner: String? = null,
  val pendingShareArtifact: ShareableExportArtifact? = null,
)

class SoloLedgerViewModel(application: Application) : AndroidViewModel(application) {
  private val prefs = AppPreferences(application)
  private val api = SoloLedgerApiClient(prefs)

  private val _uiState =
    MutableStateFlow(
      SoloLedgerUiState(
        serverUrl = prefs.serverUrl,
        credential = prefs.credential,
        wooriPushEnabled = prefs.wooriPushEnabled,
        wooriCardHint = prefs.wooriCardHint,
        biometricLockEnabled = prefs.biometricLockEnabled,
        queuedPushCount = prefs.getQueuedPushes().size,
      )
    )
  val uiState: StateFlow<SoloLedgerUiState> = _uiState.asStateFlow()

  init {
    if (prefs.serverUrl.isNotBlank()) {
      refreshDashboard(initialAutoPickLatestMonth = true)
    }
  }

  fun clearBanners() {
    _uiState.update { it.copy(statusBanner = null, errorBanner = null) }
  }

  fun consumePendingShareArtifact() {
    _uiState.update { it.copy(pendingShareArtifact = null) }
  }

  fun refreshPushQueueCount() {
    _uiState.update { it.copy(queuedPushCount = prefs.getQueuedPushes().size) }
  }

  fun saveServerAndConnect(serverUrl: String, credential: String) {
    viewModelScope.launch {
      _uiState.update { it.copy(isLoading = true, errorBanner = null, statusBanner = null) }
      try {
        val ver = api.loginAndVerify(serverUrl, credential)
        val flushed = WooriPushListenerService.flushOfflineQueue(getApplication())
        _uiState.update {
          it.copy(
            isConnected = true,
            serverVersion = ver,
            serverUrl = prefs.serverUrl,
            credential = prefs.credential,
            queuedPushCount = prefs.getQueuedPushes().size,
            statusBanner =
              if (flushed > 0) "서버 연결 완료 (v$ver) · 대기 중이던 카드 알림 ${flushed}건 자동 반영됨"
              else "서버 연결 및 인증 완료 (SoloLedger v$ver)",
          )
        }
        refreshDashboard(initialAutoPickLatestMonth = true)
      } catch (e: Exception) {
        _uiState.update {
          it.copy(
            isLoading = false,
            isConnected = false,
            errorBanner = "서버 연결 실패: ${e.message}",
          )
        }
      }
    }
  }

  fun refreshDashboard(
    yearMonth: String = _uiState.value.selectedYearMonth,
    cardLast4: String = _uiState.value.selectedCardLast4,
    initialAutoPickLatestMonth: Boolean = false,
  ) {
    if (prefs.serverUrl.isBlank()) return
    viewModelScope.launch {
      _uiState.update {
        it.copy(
          isLoading = true,
          selectedYearMonth = yearMonth,
          selectedCardLast4 = cardLast4,
          errorBanner = null,
        )
      }
      try {
        WooriPushListenerService.flushOfflineQueue(getApplication())
        var data = api.fetchDashboardSummary(yearMonth = yearMonth, cardLast4 = cardLast4)
        var resolvedYm = data.yearMonth
        var resolvedExportYm = _uiState.value.exportYearMonth
        if (
          initialAutoPickLatestMonth &&
            data.totalTxCount == 0 &&
            data.monthlyPeriods.any { it.txCount > 0 }
        ) {
          val latestActiveYm = data.monthlyPeriods.first { it.txCount > 0 }.ym
          data = api.fetchDashboardSummary(yearMonth = latestActiveYm, cardLast4 = cardLast4)
          resolvedYm = data.yearMonth
          resolvedExportYm = latestActiveYm
        } else if (resolvedYm != "ALL" && resolvedYm.length == 6) {
          resolvedExportYm = resolvedYm
        }
        _uiState.update {
          it.copy(
            isLoading = false,
            isConnected = true,
            selectedYearMonth = resolvedYm,
            exportYearMonth = resolvedExportYm,
            queuedPushCount = prefs.getQueuedPushes().size,
            summary = data,
          )
        }
      } catch (e: Exception) {
        _uiState.update {
          it.copy(
            isLoading = false,
            isConnected = false,
            errorBanner = "원장 조회 오류: ${e.message}",
          )
        }
      }
    }
  }

  fun setFilterMode(mode: LedgerFilterMode) {
    _uiState.update { it.copy(filterMode = mode) }
  }

  fun setSearchQuery(query: String) {
    _uiState.update { it.copy(searchQuery = query) }
  }

  fun setExportYearMonth(ym: String) {
    _uiState.update { it.copy(exportYearMonth = ym.filter { c -> c.isDigit() }.take(6)) }
  }

  fun setForceReexport(enabled: Boolean) {
    _uiState.update { it.copy(forceReexport = enabled) }
  }

  fun updateWooriPushSettings(enabled: Boolean, cardHint: String) {
    prefs.wooriPushEnabled = enabled
    prefs.wooriCardHint = cardHint
    _uiState.update {
      it.copy(
        wooriPushEnabled = prefs.wooriPushEnabled,
        wooriCardHint = prefs.wooriCardHint,
      )
    }
  }

  fun updateBiometricLock(enabled: Boolean) {
    prefs.biometricLockEnabled = enabled
    _uiState.update { it.copy(biometricLockEnabled = enabled) }
  }

  fun flushQueuedPushesNow() {
    viewModelScope.launch {
      _uiState.update { it.copy(isLoading = true, errorBanner = null) }
      try {
        val sent = WooriPushListenerService.flushOfflineQueue(getApplication())
        _uiState.update {
          it.copy(
            isLoading = false,
            queuedPushCount = prefs.getQueuedPushes().size,
            statusBanner = "대기 중인 카드 푸시 ${sent}건을 서버로 전송했습니다.",
          )
        }
        if (sent > 0) refreshDashboard()
      } catch (e: Exception) {
        _uiState.update { it.copy(isLoading = false, errorBanner = "푸시 전송 오류: ${e.message}") }
      }
    }
  }

  fun openTransactionDetail(txId: Int) {
    viewModelScope.launch {
      _uiState.update { it.copy(isLoading = true, errorBanner = null, activeReceiptBitmap = null) }
      try {
        val detail = api.fetchReceiptDetail(txId)
        val bmp =
          if (detail.hasReceiptFile && detail.isRenderableImage) {
            api.fetchReceiptImageBytes(txId)?.let { bytes ->
              BitmapFactory.decodeByteArray(bytes, 0, bytes.size)
            }
          } else {
            null
          }
        _uiState.update {
          it.copy(
            isLoading = false,
            activeReceiptDetail = detail,
            activeReceiptBitmap = bmp,
          )
        }
      } catch (e: Exception) {
        _uiState.update {
          it.copy(isLoading = false, errorBanner = "전표 상세 조회 실패: ${e.message}")
        }
      }
    }
  }

  fun closeTransactionDetail() {
    _uiState.update { it.copy(activeReceiptDetail = null, activeReceiptBitmap = null) }
  }

  fun saveTransactionEdits(
    txId: Int,
    accountCode: String,
    memoAndItems: String,
    applyToSameMerchant: Boolean,
    alsoIssueStatement: Boolean,
  ) {
    viewModelScope.launch {
      _uiState.update { it.copy(isLoading = true, errorBanner = null) }
      try {
        api.updateTransaction(
          txId = txId,
          accountCode = accountCode,
          counterpartyInfo = memoAndItems,
          applyToSameMerchant = applyToSameMerchant,
        )
        if (alsoIssueStatement && memoAndItems.isNotBlank()) {
          api.saveTransactionStatement(
            txId = txId,
            itemName = memoAndItems,
            accountCode = accountCode,
          )
        }
        val refreshedDetail = api.fetchReceiptDetail(txId)
        _uiState.update {
          it.copy(
            isLoading = false,
            activeReceiptDetail = refreshedDetail,
            statusBanner =
              if (alsoIssueStatement) "전표 분류 및 전자 거래명세서가 저장되었습니다."
              else "전표 계정과목 및 적요가 저장되었습니다.",
          )
        }
        refreshDashboard()
      } catch (e: Exception) {
        _uiState.update { it.copy(isLoading = false, errorBanner = "전표 저장 실패: ${e.message}") }
      }
    }
  }

  fun uploadReceiptFromUri(uri: Uri, explicitTxId: Int? = null, fallbackFileName: String = "camera_receipt.jpg") {
    viewModelScope.launch {
      _uiState.update { it.copy(isLoading = true, errorBanner = null, lastUploadMessage = null) }
      try {
        val encoded = ReceiptImageCompressor.encodeReceiptUri(getApplication(), uri, fallbackFileName)
        val res =
          api.uploadReceiptBase64(
            fileName = encoded.fileName,
            base64Content = encoded.base64Content,
            explicitTxId = explicitTxId,
          )
        val linkedTxId = res.optInt("tx_id", 0)
        val archivedName = res.optString("archived_file_name", encoded.fileName)
        val sizeKb = (encoded.byteSize / 1024).coerceAtLeast(1)
        val msg = "영수증 등록 완료 (#$linkedTxId) · $archivedName (${sizeKb}KB)"
        _uiState.update {
          it.copy(
            isLoading = false,
            lastUploadMessage = msg,
            statusBanner = msg,
          )
        }
        refreshDashboard()
      } catch (e: Exception) {
        _uiState.update {
          it.copy(
            isLoading = false,
            errorBanner = "영수증 업로드 실패: ${e.message}",
          )
        }
      }
    }
  }

  fun importCardExcelOrCsvFromUri(uri: Uri) {
    viewModelScope.launch {
      _uiState.update { it.copy(isLoading = true, errorBanner = null) }
      try {
        val encoded = ReceiptImageCompressor.encodeReceiptUri(getApplication(), uri, "card_statement.xls")
        val res = api.importCardStatementBase64(encoded.base64Content)
        val total = res.optInt("total_rows", res.optInt("imported_count", 0))
        val msg = "카드·홈택스 매입내역 파일 대사 완료 (${encoded.fileName}, 처리 ${total}건)"
        _uiState.update {
          it.copy(
            isLoading = false,
            lastUploadMessage = msg,
            statusBanner = msg,
          )
        }
        refreshDashboard(initialAutoPickLatestMonth = true)
      } catch (e: Exception) {
        _uiState.update {
          it.copy(isLoading = false, errorBanner = "매입내역 파일 반영 실패: ${e.message}")
        }
      }
    }
  }

  fun triggerRemoteEmailSync() {
    viewModelScope.launch {
      _uiState.update { it.copy(isLoading = true, errorBanner = null) }
      try {
        val res = api.syncEmailOrders()
        val matched = res.optInt("matched_count", 0)
        val scanned = res.optInt("scanned_messages", 0)
        _uiState.update {
          it.copy(
            isLoading = false,
            statusBanner = "이메일 스캔 완료 (확인 ${scanned}통 · 명세서 매칭 ${matched}건)",
          )
        }
        refreshDashboard()
      } catch (e: Exception) {
        _uiState.update { it.copy(isLoading = false, errorBanner = "이메일 스캔 오류: ${e.message}") }
      }
    }
  }

  fun triggerRemoteNasBackup() {
    viewModelScope.launch {
      _uiState.update { it.copy(isLoading = true, errorBanner = null) }
      try {
        val res = api.backupToNas()
        val syncedCount = res.optInt("synced_files_count", 0)
        val skippedCount = res.optInt("skipped_existing_count", 0)
        _uiState.update {
          it.copy(
            isLoading = false,
            statusBanner = "ipTIME NAS 백업 완료 (신규 복사 ${syncedCount}개 · 기존 유지 ${skippedCount}개)",
          )
        }
      } catch (e: Exception) {
        _uiState.update { it.copy(isLoading = false, errorBanner = "NAS 백업 오류: ${e.message}") }
      }
    }
  }

  fun exportAndShareTaxPackZip() {
    val ym = _uiState.value.exportYearMonth.trim()
    if (ym.length != 6 || ym.any { !it.isDigit() }) {
      _uiState.update { it.copy(errorBanner = "귀속 연월을 6자리 숫자(YYYYMM, 예: 202611)로 입력해 주세요.") }
      return
    }
    viewModelScope.launch {
      _uiState.update { it.copy(isLoading = true, errorBanner = null) }
      try {
        val exportDir = File(getApplication<Application>().cacheDir, "exports")
        val (summary, zipFile) =
          api.exportAndDownloadTaxPackZip(
            yearMonth = ym,
            forceReexport = _uiState.value.forceReexport,
            destDir = exportDir,
          )
        _uiState.update {
          it.copy(
            isLoading = false,
            lastExportSummary = summary,
            statusBanner = "세무증빙 패키지(${zipFile.name}) 다운로드 완료! 공유할 앱을 선택하세요.",
            pendingShareArtifact =
              ShareableExportArtifact(
                file = zipFile,
                mimeType = "application/zip",
                chooserTitle = "${ym}월 세무사 제출용 증빙 ZIP 보내기",
              ),
          )
        }
      } catch (e: Exception) {
        _uiState.update {
          it.copy(isLoading = false, errorBanner = "세무증빙 ZIP 생성·다운로드 실패: ${e.message}")
        }
      }
    }
  }

  fun downloadAndShareCsvOnly() {
    val ym = _uiState.value.exportYearMonth.trim()
    if (ym.length != 6 || ym.any { !it.isDigit() }) {
      _uiState.update { it.copy(errorBanner = "귀속 연월을 6자리 숫자(YYYYMM)로 입력해 주세요.") }
      return
    }
    viewModelScope.launch {
      _uiState.update { it.copy(isLoading = true, errorBanner = null) }
      try {
        val exportDir = File(getApplication<Application>().cacheDir, "exports")
        val csvFile =
          api.downloadTaxPackCsv(
            yearMonth = ym,
            forceReexport = _uiState.value.forceReexport,
            destDir = exportDir,
          )
        _uiState.update {
          it.copy(
            isLoading = false,
            statusBanner = "기장원장 CSV(${csvFile.name}) 준비 완료!",
            pendingShareArtifact =
              ShareableExportArtifact(
                file = csvFile,
                mimeType = "text/csv",
                chooserTitle = "${ym}월 기장원장 CSV 보내기",
              ),
          )
        }
      } catch (e: Exception) {
        _uiState.update {
          it.copy(isLoading = false, errorBanner = "기장원장 CSV 다운로드 실패: ${e.message}")
        }
      }
    }
  }
}
