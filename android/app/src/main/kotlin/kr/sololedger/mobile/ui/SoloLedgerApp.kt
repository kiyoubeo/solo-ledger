package kr.sololedger.mobile.ui

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.provider.Settings
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.Checkbox
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import androidx.core.content.FileProvider
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import java.io.File
import kr.sololedger.mobile.data.LedgerTransaction
import kr.sololedger.mobile.data.ReceiptDetail
import kr.sololedger.mobile.data.SoloLedgerCatalog
import kr.sololedger.mobile.push.WooriPushListenerService

private enum class AppTab(val title: String, val iconText: String) {
  HOME("홈·요약", "📊"),
  CAPTURE("영수증·등록", "📷"),
  LEDGER("원장·명세서", "🧾"),
  TAX_EXPORT("세무사 제출", "📦"),
  SETTINGS("설정·보안", "⚙️"),
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SoloLedgerAppScreen(vm: SoloLedgerViewModel = viewModel()) {
  val state by vm.uiState.collectAsStateWithLifecycle()
  val context = LocalContext.current

  var selectedTabIndex by rememberSaveable {
    mutableIntStateOf(if (state.serverUrl.isBlank()) AppTab.SETTINGS.ordinal else AppTab.HOME.ordinal)
  }
  val currentTab = AppTab.entries[selectedTabIndex]

  var pendingCameraUri by remember { mutableStateOf<Uri?>(null) }
  var pendingAttachTxId by remember { mutableStateOf<Int?>(null) }

  val takePictureLauncher =
    rememberLauncherForActivityResult(ActivityResultContracts.TakePicture()) { success ->
      val uri = pendingCameraUri
      val txId = pendingAttachTxId
      pendingAttachTxId = null
      if (success && uri != null) {
        vm.uploadReceiptFromUri(uri, explicitTxId = txId, fallbackFileName = "camera_receipt.jpg")
      }
    }

  val pickReceiptLauncher =
    rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri: Uri? ->
      val txId = pendingAttachTxId
      pendingAttachTxId = null
      if (uri != null) {
        vm.uploadReceiptFromUri(uri, explicitTxId = txId, fallbackFileName = "mobile_receipt.jpg")
      }
    }

  val pickExcelCsvLauncher =
    rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri: Uri? ->
      if (uri != null) {
        vm.importCardExcelOrCsvFromUri(uri)
      }
    }

  fun startCameraCapture(forTxId: Int? = null) {
    pendingAttachTxId = forTxId
    val dir = File(context.cacheDir, "camera").apply { mkdirs() }
    val photoFile = File(dir, "receipt_${System.currentTimeMillis()}.jpg")
    val uri = FileProvider.getUriForFile(context, "${context.packageName}.fileprovider", photoFile)
    pendingCameraUri = uri
    takePictureLauncher.launch(uri)
  }

  fun startFileReceiptPicker(forTxId: Int? = null) {
    pendingAttachTxId = forTxId
    pickReceiptLauncher.launch(arrayOf("image/*", "application/pdf"))
  }

  LaunchedEffect(state.pendingShareArtifact) {
    val artifact = state.pendingShareArtifact ?: return@LaunchedEffect
    shareExportedFile(context, artifact)
    vm.consumePendingShareArtifact()
  }

  Scaffold(
    topBar = {
      TopAppBar(
        title = {
          Column {
            Text("SoloLedger 법인원장", fontWeight = FontWeight.Bold)
            Text(
              text =
                if (state.isConnected) "🟢 서버 연결됨 (${state.serverUrl})"
                else if (state.serverUrl.isBlank()) "⚪ 서버 미설정 — [설정·보안]에서 연결해 주세요"
                else "🟠 연결 확인 필요 (${state.serverUrl})",
              style = MaterialTheme.typography.labelSmall,
              maxLines = 1,
              overflow = TextOverflow.Ellipsis,
            )
          }
        },
        actions = {
          if (state.serverUrl.isNotBlank()) {
            TextButton(onClick = { vm.refreshDashboard() }) {
              Text("새로고침")
            }
          }
        },
      )
    },
    bottomBar = {
      NavigationBar {
        AppTab.entries.forEachIndexed { idx, tab ->
          NavigationBarItem(
            selected = selectedTabIndex == idx,
            onClick = {
              selectedTabIndex = idx
              vm.refreshPushQueueCount()
            },
            icon = { Text(tab.iconText) },
            label = { Text(tab.title, maxLines = 1) },
          )
        }
      }
    },
  ) { innerPadding ->
    Column(
      modifier =
        Modifier
          .fillMaxSize()
          .padding(innerPadding)
    ) {
      if (state.isLoading) {
        LinearProgressIndicator(modifier = Modifier.fillMaxWidth())
      }

      state.errorBanner?.let { err ->
        BannerBox(
          message = err,
          bgColor = Color(0xFFFEE2E2),
          textColor = Color(0xFF991B1B),
          onDismiss = { vm.clearBanners() },
        )
      }

      state.statusBanner?.let { msg ->
        BannerBox(
          message = msg,
          bgColor = Color(0xFFDCFCE7),
          textColor = Color(0xFF166534),
          onDismiss = { vm.clearBanners() },
        )
      }

      when (currentTab) {
        AppTab.HOME ->
          HomeSummaryTab(
            state = state,
            onSelectYearMonth = { ym -> vm.refreshDashboard(yearMonth = ym) },
            onSelectCard = { card ->
              vm.refreshDashboard(cardLast4 = card)
              selectedTabIndex = AppTab.LEDGER.ordinal
            },
            onJumpToPending = {
              vm.setFilterMode(LedgerFilterMode.NEEDS_ATTENTION)
              selectedTabIndex = AppTab.LEDGER.ordinal
            },
            onQuickCamera = { startCameraCapture(null) },
            onQuickTaxExport = { selectedTabIndex = AppTab.TAX_EXPORT.ordinal },
            onSyncEmails = { vm.triggerRemoteEmailSync() },
            onBackupNas = { vm.triggerRemoteNasBackup() },
            onGoSettings = { selectedTabIndex = AppTab.SETTINGS.ordinal },
          )
        AppTab.CAPTURE ->
          CaptureUploadTab(
            state = state,
            onTakePhoto = { startCameraCapture(null) },
            onPickReceipt = { startFileReceiptPicker(null) },
            onPickExcelCsv = {
              pickExcelCsvLauncher.launch(
                arrayOf(
                  "text/*",
                  "application/vnd.ms-excel",
                  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                  "application/octet-stream",
                )
              )
            },
            onFlushQueue = { vm.flushQueuedPushesNow() },
          )
        AppTab.LEDGER ->
          LedgerListTab(
            state = state,
            onSelectFilter = { vm.setFilterMode(it) },
            onSearchChange = { vm.setSearchQuery(it) },
            onClearCardFilter = { vm.refreshDashboard(cardLast4 = "ALL") },
            onClickTransaction = { tx -> vm.openTransactionDetail(tx.txId) },
          )
        AppTab.TAX_EXPORT ->
          TaxExportShareTab(
            state = state,
            onExportYmChange = { vm.setExportYearMonth(it) },
            onToggleForceReexport = { vm.setForceReexport(it) },
            onExportAndShareZip = { vm.exportAndShareTaxPackZip() },
            onShareCsvOnly = { vm.downloadAndShareCsvOnly() },
          )
        AppTab.SETTINGS ->
          SettingsSecurityTab(
            state = state,
            onSaveAndConnect = { url, cred -> vm.saveServerAndConnect(url, cred) },
            onToggleBiometric = { vm.updateBiometricLock(it) },
            onUpdateWooriPush = { enabled, hint -> vm.updateWooriPushSettings(enabled, hint) },
            onFlushQueue = { vm.flushQueuedPushesNow() },
          )
      }
    }
  }

  state.activeReceiptDetail?.let { detail ->
    TransactionDetailDialog(
      detail = detail,
      receiptBitmap = state.activeReceiptBitmap,
      onDismiss = { vm.closeTransactionDetail() },
      onSave = { accountCode, memo, applyAll, issueStatement ->
        vm.saveTransactionEdits(
          txId = detail.txId,
          accountCode = accountCode,
          memoAndItems = memo,
          applyToSameMerchant = applyAll,
          alsoIssueStatement = issueStatement,
        )
      },
      onAttachCamera = { startCameraCapture(forTxId = detail.txId) },
      onAttachFile = { startFileReceiptPicker(forTxId = detail.txId) },
    )
  }
}

@Composable
private fun BannerBox(
  message: String,
  bgColor: Color,
  textColor: Color,
  onDismiss: () -> Unit,
) {
  Row(
    modifier =
      Modifier
        .fillMaxWidth()
        .padding(horizontal = 12.dp, vertical = 4.dp)
        .clip(RoundedCornerShape(8.dp))
        .background(bgColor)
        .clickable { onDismiss() }
        .padding(horizontal = 12.dp, vertical = 8.dp),
    verticalAlignment = Alignment.CenterVertically,
  ) {
    Text(
      text = message,
      color = textColor,
      style = MaterialTheme.typography.bodySmall,
      fontWeight = FontWeight.Medium,
      modifier = Modifier.weight(1f),
    )
    Text("✕", color = textColor, modifier = Modifier.padding(start = 8.dp))
  }
}

@Composable
private fun HomeSummaryTab(
  state: SoloLedgerUiState,
  onSelectYearMonth: (String) -> Unit,
  onSelectCard: (String) -> Unit,
  onJumpToPending: () -> Unit,
  onQuickCamera: () -> Unit,
  onQuickTaxExport: () -> Unit,
  onSyncEmails: () -> Unit,
  onBackupNas: () -> Unit,
  onGoSettings: () -> Unit,
) {
  val summary = state.summary
  LazyColumn(
    modifier = Modifier.fillMaxSize().padding(horizontal = 14.dp),
    verticalArrangement = Arrangement.spacedBy(12.dp),
  ) {
    item { Spacer(modifier = Modifier.height(4.dp)) }

    if (state.serverUrl.isBlank()) {
      item {
        Card(
          colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.primaryContainer),
          modifier = Modifier.fillMaxWidth(),
        ) {
          Column(modifier = Modifier.padding(16.dp)) {
            Text("사무실 PC SoloLedger 서버를 연결해 주세요", fontWeight = FontWeight.Bold)
            Spacer(modifier = Modifier.height(6.dp))
            Text(
              "사내 Wi-Fi 주소(예: 192.168.0.x:8000) 또는 외부 접속용 Tailscale VPN 주소(예: 100.x.x.x:8000)를 입력하면 영수증 처리와 세무사 제출을 바로 실행할 수 있습니다.",
              style = MaterialTheme.typography.bodySmall,
            )
            Spacer(modifier = Modifier.height(10.dp))
            Button(onClick = onGoSettings) { Text("서버 주소 설정하러 가기") }
          }
        }
      }
    }

    if (summary != null) {
      item {
        Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
          Row(
            modifier = Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
          ) {
            FilterChip(
              selected = state.selectedYearMonth == "ALL",
              onClick = { onSelectYearMonth("ALL") },
              label = { Text("전체 기간") },
            )
            summary.monthlyPeriods.take(12).forEach { period ->
              FilterChip(
                selected = state.selectedYearMonth == period.ym,
                onClick = { onSelectYearMonth(period.ym) },
                label = { Text("${period.shortLabel} (${period.txCount})") },
              )
            }
          }
          if (summary.quarterlyPeriods.isNotEmpty()) {
            Row(
              modifier = Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()),
              horizontalArrangement = Arrangement.spacedBy(8.dp),
              verticalAlignment = Alignment.CenterVertically,
            ) {
              Text(
                text = "분기별:",
                style = MaterialTheme.typography.labelMedium,
                fontWeight = FontWeight.SemiBold,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
              )
              summary.quarterlyPeriods.take(8).forEach { qPeriod ->
                FilterChip(
                  selected = state.selectedYearMonth == qPeriod.yq,
                  onClick = { onSelectYearMonth(qPeriod.yq) },
                  label = { Text("${qPeriod.shortLabel} (${qPeriod.txCount})") },
                )
              }
            }
          }
        }
      }

      item {
        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(10.dp)) {
          MetricCard(
            title = "총 지출 (${summary.totalTxCount}건)",
            value = SoloLedgerCatalog.formatKrw(summary.totalExpenditure),
            accent = Color(0xFF1E3A8A),
            modifier = Modifier.weight(1f),
          )
          MetricCard(
            title = "공제 가능 부가세",
            value = SoloLedgerCatalog.formatKrw(summary.deductibleVat),
            accent = Color(0xFF15803D),
            modifier = Modifier.weight(1f),
          )
        }
      }

      item {
        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(10.dp)) {
          MetricCard(
            title = "불공제 합계",
            value = SoloLedgerCatalog.formatKrw(summary.nonDeductibleTotal),
            accent = Color(0xFFB45309),
            modifier = Modifier.weight(1f),
          )
          MetricCard(
            title = "미분류·소명 필요",
            value = "${summary.pendingActionCount}건",
            accent = if (summary.pendingActionCount > 0) Color(0xFFB91C1C) else Color(0xFF475569),
            modifier = Modifier.weight(1f).clickable { onJumpToPending() },
          )
        }
      }

      item {
        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
          Button(onClick = onQuickCamera, modifier = Modifier.weight(1f)) {
            Text("📷 영수증 촬영")
          }
          Button(
            onClick = onQuickTaxExport,
            colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF0F766E)),
            modifier = Modifier.weight(1f),
          ) {
            Text("📦 세무사 제출")
          }
        }
      }

      item {
        val veh = summary.vehicleLimit
        val ratio = (veh.usageRatioPct.toFloat() / 100f).coerceIn(0f, 1f)
        Card(modifier = Modifier.fillMaxWidth()) {
          Column(modifier = Modifier.padding(14.dp)) {
            Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
              Text("🚗 업무용승용차 연간 한도 (${veh.year}년)", fontWeight = FontWeight.Bold)
              Text("${veh.usageRatioPct}%", fontWeight = FontWeight.Bold)
            }
            Spacer(modifier = Modifier.height(6.dp))
            LinearProgressIndicator(progress = { ratio }, modifier = Modifier.fillMaxWidth())
            Spacer(modifier = Modifier.height(6.dp))
            Text(
              "누적 ${SoloLedgerCatalog.formatKrw(veh.totalUsed)} / 15,000,000원 (잔여 ${SoloLedgerCatalog.formatKrw(veh.remainingLimit)})",
              style = MaterialTheme.typography.bodySmall,
            )
          }
        }
      }

      item {
        Card(modifier = Modifier.fillMaxWidth()) {
          Column(modifier = Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text("⚡ 원격 자동화 실행", fontWeight = FontWeight.Bold)
            Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
              OutlinedButton(onClick = onSyncEmails, modifier = Modifier.weight(1f)) {
                Text("📧 이메일 명세서 스캔")
              }
              OutlinedButton(onClick = onBackupNas, modifier = Modifier.weight(1f)) {
                Text("💾 NAS 백업")
              }
            }
          }
        }
      }

      if (summary.cardBreakdown.isNotEmpty()) {
        item {
          Text("💳 카드별 지출 요약 (누르면 원장 필터)", fontWeight = FontWeight.Bold)
        }
        items(summary.cardBreakdown, key = { it.cardLast4 }) { card ->
          Card(
            modifier = Modifier.fillMaxWidth().clickable { onSelectCard(card.cardLast4) },
          ) {
            Row(
              modifier = Modifier.fillMaxWidth().padding(12.dp),
              horizontalArrangement = Arrangement.SpaceBetween,
              verticalAlignment = Alignment.CenterVertically,
            ) {
              Column {
                Text(card.cardLabel, fontWeight = FontWeight.SemiBold)
                Text(
                  "${card.txCount}건 · 증빙 ${card.receiptCount}건 · 공제VAT ${SoloLedgerCatalog.formatKrw(card.deductibleVat)}",
                  style = MaterialTheme.typography.bodySmall,
                  color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
              }
              Text(
                SoloLedgerCatalog.formatKrw(card.totalExpenditure),
                fontWeight = FontWeight.Bold,
                color = Color(0xFF1E3A8A),
              )
            }
          }
        }
      }
    }

    item { Spacer(modifier = Modifier.height(16.dp)) }
  }
}

@Composable
private fun MetricCard(
  title: String,
  value: String,
  accent: Color,
  modifier: Modifier = Modifier,
) {
  Card(modifier = modifier) {
    Column(modifier = Modifier.padding(12.dp)) {
      Text(title, style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
      Spacer(modifier = Modifier.height(4.dp))
      Text(value, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.ExtraBold, color = accent)
    }
  }
}

@Composable
private fun CaptureUploadTab(
  state: SoloLedgerUiState,
  onTakePhoto: () -> Unit,
  onPickReceipt: () -> Unit,
  onPickExcelCsv: () -> Unit,
  onFlushQueue: () -> Unit,
) {
  Column(
    modifier =
      Modifier
        .fillMaxSize()
        .verticalScroll(rememberScrollState())
        .padding(16.dp),
    verticalArrangement = Arrangement.spacedBy(14.dp),
  ) {
    Card(modifier = Modifier.fillMaxWidth()) {
      Column(modifier = Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
        Text("📷 종이 영수증 · 모바일 명세서 등록", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
        Text(
          "촬영한 사진은 자동으로 고해상도 압축(JPEG) 후 사무실 서버의 Vision OCR로 전송되어 가맹점·금액·사업자번호 추출 및 기존 전표 자동 매칭까지 처리됩니다.",
          style = MaterialTheme.typography.bodySmall,
          color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Button(onClick = onTakePhoto, modifier = Modifier.fillMaxWidth().height(50.dp)) {
          Text("📷 카메라로 영수증 바로 촬영", fontWeight = FontWeight.Bold)
        }
        OutlinedButton(onClick = onPickReceipt, modifier = Modifier.fillMaxWidth().height(48.dp)) {
          Text("🖼️ 갤러리 캡처본 · PDF 영수증 선택")
        }
      }
    }

    Card(modifier = Modifier.fillMaxWidth()) {
      Column(modifier = Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
        Text("📊 홈택스 · 우리카드 매입내역 일괄 반영", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
        Text(
          "스마트폰에 내려받은 홈택스 또는 우리카드 승인·매입내역(.xls, .xlsx, .csv) 파일을 올려 3단계 자동 대사를 실행합니다.",
          style = MaterialTheme.typography.bodySmall,
          color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        OutlinedButton(onClick = onPickExcelCsv, modifier = Modifier.fillMaxWidth()) {
          Text("📂 엑셀(.xls/.xlsx) · CSV 파일 업로드")
        }
      }
    }

    if (state.queuedPushCount > 0) {
      Card(
        colors = CardDefaults.cardColors(containerColor = Color(0xFFFEF3C7)),
        modifier = Modifier.fillMaxWidth(),
      ) {
        Row(
          modifier = Modifier.fillMaxWidth().padding(14.dp),
          horizontalArrangement = Arrangement.SpaceBetween,
          verticalAlignment = Alignment.CenterVertically,
        ) {
          Text(
            "오프라인 대기 중인 우리카드 알림 ${state.queuedPushCount}건",
            color = Color(0xFF92400E),
            fontWeight = FontWeight.SemiBold,
            modifier = Modifier.weight(1f),
          )
          Button(onClick = onFlushQueue) { Text("지금 전송") }
        }
      }
    }

    state.lastUploadMessage?.let { msg ->
      Card(
        colors = CardDefaults.cardColors(containerColor = Color(0xFFECFDF5)),
        modifier = Modifier.fillMaxWidth(),
      ) {
        Column(modifier = Modifier.padding(14.dp)) {
          Text("최근 처리 결과", fontWeight = FontWeight.Bold, color = Color(0xFF065F46))
          Spacer(modifier = Modifier.height(4.dp))
          Text(msg, style = MaterialTheme.typography.bodySmall, color = Color(0xFF065F46))
        }
      }
    }
  }
}

@Composable
private fun LedgerListTab(
  state: SoloLedgerUiState,
  onSelectFilter: (LedgerFilterMode) -> Unit,
  onSearchChange: (String) -> Unit,
  onClearCardFilter: () -> Unit,
  onClickTransaction: (LedgerTransaction) -> Unit,
) {
  val allTxs = state.summary?.transactions.orEmpty()
  val query = state.searchQuery.trim().lowercase()
  val filtered =
    remember(allTxs, state.filterMode, query) {
      allTxs.filter { tx ->
        val modeMatch =
          when (state.filterMode) {
            LedgerFilterMode.ALL -> true
            LedgerFilterMode.NEEDS_ATTENTION -> tx.needsAttention
            LedgerFilterMode.DINING_ENTERTAINMENT -> tx.accountCode in setOf("811", "813")
            LedgerFilterMode.VEHICLE -> tx.accountCode == "822"
          }
        val queryMatch =
          if (query.isEmpty()) {
            true
          } else {
            tx.rawMerchantName.lowercase().contains(query) ||
              tx.counterpartyInfo.orEmpty().lowercase().contains(query) ||
              tx.accountName.lowercase().contains(query) ||
              tx.totalAmount.toString().contains(query)
          }
        modeMatch && queryMatch
      }
    }

  Column(modifier = Modifier.fillMaxSize().padding(horizontal = 12.dp)) {
    OutlinedTextField(
      value = state.searchQuery,
      onValueChange = onSearchChange,
      placeholder = { Text("가맹점명 · 품목 · 참석자 · 금액 검색") },
      singleLine = true,
      modifier = Modifier.fillMaxWidth().padding(top = 6.dp),
    )

    Row(
      modifier = Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()).padding(vertical = 6.dp),
      horizontalArrangement = Arrangement.spacedBy(6.dp),
    ) {
      if (state.selectedCardLast4 != "ALL") {
        FilterChip(
          selected = true,
          onClick = onClearCardFilter,
          label = { Text("카드:${state.selectedCardLast4} ✕") },
        )
      }
      FilterChip(
        selected = state.filterMode == LedgerFilterMode.ALL,
        onClick = { onSelectFilter(LedgerFilterMode.ALL) },
        label = { Text("전체 (${allTxs.size})") },
      )
      FilterChip(
        selected = state.filterMode == LedgerFilterMode.NEEDS_ATTENTION,
        onClick = { onSelectFilter(LedgerFilterMode.NEEDS_ATTENTION) },
        label = { Text("⚠️ 미분류·소명필요") },
      )
      FilterChip(
        selected = state.filterMode == LedgerFilterMode.DINING_ENTERTAINMENT,
        onClick = { onSelectFilter(LedgerFilterMode.DINING_ENTERTAINMENT) },
        label = { Text("🍽️ 식대·접대비") },
      )
      FilterChip(
        selected = state.filterMode == LedgerFilterMode.VEHICLE,
        onClick = { onSelectFilter(LedgerFilterMode.VEHICLE) },
        label = { Text("🚗 차량유지비") },
      )
    }

    if (filtered.isEmpty()) {
      Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        Text("조건에 해당하는 전표 내역이 없습니다.", color = MaterialTheme.colorScheme.onSurfaceVariant)
      }
    } else {
      LazyColumn(
        modifier = Modifier.fillMaxSize(),
        verticalArrangement = Arrangement.spacedBy(8.dp),
      ) {
        items(filtered, key = { it.txId }) { tx ->
          TransactionRowCard(tx = tx, onClick = { onClickTransaction(tx) })
        }
        item { Spacer(modifier = Modifier.height(14.dp)) }
      }
    }
  }
}

@Composable
private fun TransactionRowCard(tx: LedgerTransaction, onClick: () -> Unit) {
  val vatColor =
    when (tx.vatDeductionType) {
      "DEDUCTIBLE" -> Color(0xFF15803D)
      "NON_DEDUCTIBLE" -> Color(0xFFB45309)
      else -> Color(0xFF64748B)
    }
  val vatLabel =
    when (tx.vatDeductionType) {
      "DEDUCTIBLE" -> "부가세 공제"
      "NON_DEDUCTIBLE" -> "불공제"
      "EXEMPT" -> "면세"
      else -> "확인필요"
    }

  Card(modifier = Modifier.fillMaxWidth().clickable { onClick() }) {
    Column(modifier = Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
      Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
        Text(
          text = tx.rawMerchantName,
          style = MaterialTheme.typography.titleSmall,
          fontWeight = FontWeight.Bold,
          maxLines = 1,
          overflow = TextOverflow.Ellipsis,
          modifier = Modifier.weight(1f),
        )
        Text(
          text = SoloLedgerCatalog.formatKrw(tx.totalAmount),
          style = MaterialTheme.typography.titleSmall,
          fontWeight = FontWeight.ExtraBold,
          color = Color(0xFF1E3A8A),
        )
      }

      Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
        Text(
          text = "${tx.transactedAt.take(16)} · ${tx.cardLabel}",
          style = MaterialTheme.typography.bodySmall,
          color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Text(
          text = vatLabel,
          style = MaterialTheme.typography.labelSmall,
          color = vatColor,
          fontWeight = FontWeight.Bold,
        )
      }

      Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically,
      ) {
        Text(
          text = "[${tx.accountCode} ${tx.accountName}] ${tx.counterpartyInfo?.ifBlank { "(적요 미입력)" } ?: "(적요 미입력)"}",
          style = MaterialTheme.typography.bodySmall,
          color = if (tx.needsAttention) Color(0xFFB91C1C) else MaterialTheme.colorScheme.onSurface,
          maxLines = 1,
          overflow = TextOverflow.Ellipsis,
          modifier = Modifier.weight(1f),
        )
        if (!tx.archivedFileName.isNullOrBlank()) {
          Text("📎증빙", style = MaterialTheme.typography.labelSmall, color = Color(0xFF2563EB))
        }
      }
    }
  }
}

@Composable
private fun TaxExportShareTab(
  state: SoloLedgerUiState,
  onExportYmChange: (String) -> Unit,
  onToggleForceReexport: (Boolean) -> Unit,
  onExportAndShareZip: () -> Unit,
  onShareCsvOnly: () -> Unit,
) {
  val months = state.summary?.availableMonths.orEmpty()
  Column(
    modifier =
      Modifier
        .fillMaxSize()
        .verticalScroll(rememberScrollState())
        .padding(16.dp),
    verticalArrangement = Arrangement.spacedBy(14.dp),
  ) {
    Card(modifier = Modifier.fillMaxWidth()) {
      Column(modifier = Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Text(
          "📦 세무사 제출용 증빙 패키지 즉시 생성 · 전송",
          style = MaterialTheme.typography.titleMedium,
          fontWeight = FontWeight.Bold,
        )
        Text(
          "선택한 귀속 연월의 표준 기장원장(CSV)과 정규화된 모든 매출전표·거래명세서·영수증 파일을 ZIP으로 묶어 내려받은 뒤, 카카오톡·이메일 공유 창을 즉시 엽니다.",
          style = MaterialTheme.typography.bodySmall,
          color = MaterialTheme.colorScheme.onSurfaceVariant,
        )

        if (months.isNotEmpty()) {
          Row(
            modifier = Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()),
            horizontalArrangement = Arrangement.spacedBy(6.dp),
          ) {
            months.take(8).forEach { ym ->
              FilterChip(
                selected = state.exportYearMonth == ym,
                onClick = { onExportYmChange(ym) },
                label = { Text("${ym.take(4)}.${ym.drop(4)}월") },
              )
            }
          }
        }

        OutlinedTextField(
          value = state.exportYearMonth,
          onValueChange = onExportYmChange,
          label = { Text("제출 귀속 연월 (YYYYMM)") },
          placeholder = { Text("예: 202611") },
          keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
          singleLine = true,
          modifier = Modifier.fillMaxWidth(),
        )

        Row(
          modifier = Modifier.fillMaxWidth(),
          horizontalArrangement = Arrangement.SpaceBetween,
          verticalAlignment = Alignment.CenterVertically,
        ) {
          Column(modifier = Modifier.weight(1f)) {
            Text("해당 월 전체 내역 포함 (권장)", fontWeight = FontWeight.SemiBold)
            Text(
              "이전에 이미 내보냈던 전표도 빠짐없이 다시 포함해 최신 상태로 묶습니다.",
              style = MaterialTheme.typography.labelSmall,
              color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
          }
          Switch(checked = state.forceReexport, onCheckedChange = onToggleForceReexport)
        }

        Button(
          onClick = onExportAndShareZip,
          colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF0F766E)),
          modifier = Modifier.fillMaxWidth().height(54.dp),
        ) {
          Text("📦 세무증빙 ZIP 생성 후 카톡·메일로 바로 보내기", fontWeight = FontWeight.Bold)
        }

        OutlinedButton(onClick = onShareCsvOnly, modifier = Modifier.fillMaxWidth()) {
          Text("📄 기장원장 엑셀(CSV) 파일만 보내기")
        }
      }
    }

    state.lastExportSummary?.let { exp ->
      Card(
        colors = CardDefaults.cardColors(containerColor = Color(0xFFF0FDF4)),
        modifier = Modifier.fillMaxWidth(),
      ) {
        Column(modifier = Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
          Text("✅ 최근 생성된 세무 제출 패키지 (${exp.yearMonth})", fontWeight = FontWeight.Bold, color = Color(0xFF166534))
          Text("• 포함 전표 건수: ${exp.exportedCount}건 (첨부 증빙 ${exp.bundledReceiptsCount}개)")
          Text("• 공급가액 합계: ${SoloLedgerCatalog.formatKrw(exp.totalSupplyValue)}")
          Text("• 공제 가능 부가세: ${SoloLedgerCatalog.formatKrw(exp.deductibleVatAmount)}")
          Text("• 총 결제 합계: ${SoloLedgerCatalog.formatKrw(exp.totalAmount)}", fontWeight = FontWeight.Bold)
        }
      }
    }
  }
}

@Composable
private fun SettingsSecurityTab(
  state: SoloLedgerUiState,
  onSaveAndConnect: (String, String) -> Unit,
  onToggleBiometric: (Boolean) -> Unit,
  onUpdateWooriPush: (Boolean, String) -> Unit,
  onFlushQueue: () -> Unit,
) {
  val context = LocalContext.current
  var urlInput by remember(state.serverUrl) { mutableStateOf(state.serverUrl) }
  var credInput by remember(state.credential) { mutableStateOf(state.credential) }
  var cardHintInput by remember(state.wooriCardHint) { mutableStateOf(state.wooriCardHint) }
  val hasNotifPermission = remember(state.wooriPushEnabled) { WooriPushListenerService.isNotificationAccessGranted(context) }

  Column(
    modifier =
      Modifier
        .fillMaxSize()
        .verticalScroll(rememberScrollState())
        .padding(16.dp),
    verticalArrangement = Arrangement.spacedBy(14.dp),
  ) {
    Card(modifier = Modifier.fillMaxWidth()) {
      Column(modifier = Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
        Text("🖥️ 사무실 PC SoloLedger 서버 연결", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
        OutlinedTextField(
          value = urlInput,
          onValueChange = { urlInput = it },
          label = { Text("서버 주소 (Tailscale VPN 또는 사내 IP)") },
          placeholder = { Text("예: http://100.64.0.10:8000") },
          singleLine = true,
          modifier = Modifier.fillMaxWidth(),
        )
        OutlinedTextField(
          value = credInput,
          onValueChange = { credInput = it },
          label = { Text("관리자 비밀번호 또는 SOLOLEDGER_API_KEY") },
          visualTransformation = PasswordVisualTransformation(),
          singleLine = true,
          modifier = Modifier.fillMaxWidth(),
        )
        Button(
          onClick = { onSaveAndConnect(urlInput, credInput) },
          modifier = Modifier.fillMaxWidth().height(48.dp),
        ) {
          Text("🔌 연결 테스트 및 인증 토큰 발급·저장", fontWeight = FontWeight.Bold)
        }
      }
    }

    Card(modifier = Modifier.fillMaxWidth()) {
      Column(modifier = Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
        Text("🔒 앱 보안 및 우리WON카드 푸시 자동 수집", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)

        Row(
          modifier = Modifier.fillMaxWidth(),
          horizontalArrangement = Arrangement.SpaceBetween,
          verticalAlignment = Alignment.CenterVertically,
        ) {
          Column(modifier = Modifier.weight(1f)) {
            Text("지문 · 화면잠금 생체인증 사용", fontWeight = FontWeight.SemiBold)
            Text("외부에서 앱 실행 시 본인 인증을 거치도록 보호합니다.", style = MaterialTheme.typography.labelSmall)
          }
          Switch(checked = state.biometricLockEnabled, onCheckedChange = onToggleBiometric)
        }

        HorizontalDivider()

        Row(
          modifier = Modifier.fillMaxWidth(),
          horizontalArrangement = Arrangement.SpaceBetween,
          verticalAlignment = Alignment.CenterVertically,
        ) {
          Column(modifier = Modifier.weight(1f)) {
            Text("우리WON카드 결제 푸시 자동 수집 (선택)", fontWeight = FontWeight.SemiBold)
            Text(
              "켜 두면 우리카드 결제 알림 발생 시 광고를 걸러내고 서버 원장에 자동 등록합니다 (오프라인 시 임시저장).",
              style = MaterialTheme.typography.labelSmall,
            )
          }
          Switch(
            checked = state.wooriPushEnabled,
            onCheckedChange = { enabled -> onUpdateWooriPush(enabled, cardHintInput) },
          )
        }

        if (state.wooriPushEnabled) {
          OutlinedTextField(
            value = cardHintInput,
            onValueChange = {
              cardHintInput = it
              onUpdateWooriPush(true, it)
            },
            label = { Text("기본 우리법인카드 뒷 4자리 (번호 생략 알림 대비)") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
          )
          Text(
            text =
              if (hasNotifPermission) "✅ 안드로이드 알림 접근 권한이 허용되어 있습니다."
              else "⚠️ 아래 버튼을 눌러 [SoloLedger 법인원장]의 알림 접근 권한을 허용해 주세요.",
            style = MaterialTheme.typography.bodySmall,
            color = if (hasNotifPermission) Color(0xFF15803D) else Color(0xFFB45309),
          )
          OutlinedButton(
            onClick = {
              context.startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS))
            },
            modifier = Modifier.fillMaxWidth(),
          ) {
            Text("안드로이드 알림 접근 권한 설정 열기")
          }
          if (state.queuedPushCount > 0) {
            OutlinedButton(onClick = onFlushQueue, modifier = Modifier.fillMaxWidth()) {
              Text("대기 중인 카드 푸시 ${state.queuedPushCount}건 즉시 전송")
            }
          }
        }
      }
    }

    Card(
      colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant),
      modifier = Modifier.fillMaxWidth(),
    ) {
      Column(modifier = Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
        Text("💡 외부에서 가장 안전하게 접속하는 방법 (포트포워딩 불필요)", fontWeight = FontWeight.Bold)
        Text("1. 사무실 상시 PC와 대표님 스마트폰에 무료 'Tailscale' 앱을 설치 후 같은 계정으로 로그인합니다.", style = MaterialTheme.typography.bodySmall)
        Text("2. 사무실 PC에 부여된 전용 주소(예: http://100.x.x.x:8000)를 위 서버 주소 칸에 입력합니다.", style = MaterialTheme.typography.bodySmall)
        Text("3. 공유기 포트를 외부 인터넷에 개방하지 않아 해킹 위험 없이 어디서든 영수증 처리·세무사 전송이 가능합니다.", style = MaterialTheme.typography.bodySmall)
      }
    }
  }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun TransactionDetailDialog(
  detail: ReceiptDetail,
  receiptBitmap: android.graphics.Bitmap?,
  onDismiss: () -> Unit,
  onSave: (accountCode: String, memo: String, applyAll: Boolean, issueStatement: Boolean) -> Unit,
  onAttachCamera: () -> Unit,
  onAttachFile: () -> Unit,
) {
  var docMode by remember {
    mutableStateOf(if (detail.archivedFileName?.startsWith("거래명세서_") == true) "STATEMENT" else "SLIP")
  }
  var selectedAccount by remember(detail.txId, detail.accountCode) { mutableStateOf(detail.accountCode) }
  var memoText by remember(detail.txId, detail.counterpartyInfo) {
    mutableStateOf(detail.counterpartyInfo.orEmpty())
  }
  var applySameMerchant by remember { mutableStateOf(true) }

  Dialog(
    onDismissRequest = onDismiss,
    properties = DialogProperties(usePlatformDefaultWidth = false),
  ) {
    Surface(
      shape = RoundedCornerShape(16.dp),
      modifier = Modifier.fillMaxWidth(0.95f).heightIn(max = 720.dp),
    ) {
      Column(
        modifier =
          Modifier
            .fillMaxWidth()
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
      ) {
        Row(
          modifier = Modifier.fillMaxWidth(),
          horizontalArrangement = Arrangement.SpaceBetween,
          verticalAlignment = Alignment.CenterVertically,
        ) {
          Text(
            text = if (docMode == "STATEMENT") "📄 거래명세서 (#${detail.txId})" else "🧾 신용카드 매출전표 (#${detail.txId})",
            style = MaterialTheme.typography.titleMedium,
            fontWeight = FontWeight.Bold,
          )
          TextButton(onClick = onDismiss) { Text("닫기") }
        }

        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
          FilterChip(
            selected = docMode == "SLIP",
            onClick = { docMode = "SLIP" },
            label = { Text("🧾 신용카드 매출전표") },
          )
          FilterChip(
            selected = docMode == "STATEMENT",
            onClick = { docMode = "STATEMENT" },
            label = { Text("📄 거래명세서") },
          )
        }

        Card(
          colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant),
          modifier = Modifier.fillMaxWidth(),
        ) {
          Column(modifier = Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(5.dp)) {
            Text(
              text = if (docMode == "STATEMENT") "[ 거 래 명 세 서 ]" else "[신용카드 매출전표]",
              fontWeight = FontWeight.ExtraBold,
              color = Color(0xFF1E3A8A),
              modifier = Modifier.align(Alignment.CenterHorizontally),
            )
            HorizontalDivider(modifier = Modifier.padding(vertical = 4.dp))
            SlipInfoRow("거래일시", detail.transactedAt)
            SlipInfoRow("카드종류", detail.cardLabel)
            SlipInfoRow("승인번호", detail.approvalNo ?: "TX-${detail.txId}")
            SlipInfoRow("가맹점명", detail.rawMerchantName)
            SlipInfoRow(
              "사업자번호",
              SoloLedgerCatalog.formatBizRegNo(detail.bizRegNo, detail.marketplaceSeller),
            )

            if (docMode == "STATEMENT") {
              HorizontalDivider(modifier = Modifier.padding(vertical = 4.dp))
              Text("품목 및 규격 내역", style = MaterialTheme.typography.labelMedium, fontWeight = FontWeight.Bold)
              if (detail.lineItems.isNotEmpty()) {
                detail.lineItems.forEach { li ->
                  val sellerSuffix = if (!li.seller.isNullOrBlank()) " (판매자: ${li.seller})" else ""
                  Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                    Text(
                      text = "• ${li.item}$sellerSuffix × ${li.qty}",
                      style = MaterialTheme.typography.bodySmall,
                      modifier = Modifier.weight(1f),
                    )
                    Text(
                      text = SoloLedgerCatalog.formatKrw(li.price),
                      style = MaterialTheme.typography.bodySmall,
                      fontWeight = FontWeight.SemiBold,
                    )
                  }
                }
              } else {
                Text(
                  text = "• ${memoText.ifBlank { "${detail.rawMerchantName} 구매 품목" }} × 1 (${SoloLedgerCatalog.formatKrw(detail.totalAmount)})",
                  style = MaterialTheme.typography.bodySmall,
                )
              }
            }

            HorizontalDivider(modifier = Modifier.padding(vertical = 4.dp))
            SlipInfoRow("공급가액", SoloLedgerCatalog.formatKrw(detail.supplyValue))
            SlipInfoRow("부가세액(VAT)", "${SoloLedgerCatalog.formatKrw(detail.vatAmount)} (${detail.vatDeductionType})")
            SlipInfoRow("합계금액", SoloLedgerCatalog.formatKrw(detail.totalAmount), boldValue = true)
          }
        }

        if (receiptBitmap != null) {
          Text("첨부 영수증 이미지", style = MaterialTheme.typography.labelMedium, fontWeight = FontWeight.Bold)
          Image(
            bitmap = receiptBitmap.asImageBitmap(),
            contentDescription = "영수증 이미지",
            contentScale = ContentScale.FillWidth,
            modifier = Modifier.fillMaxWidth().clip(RoundedCornerShape(8.dp)),
          )
        }

        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
          OutlinedButton(onClick = onAttachCamera, modifier = Modifier.weight(1f)) {
            Text("📷 영수증 촬영 첨부")
          }
          OutlinedButton(onClick = onAttachFile, modifier = Modifier.weight(1f)) {
            Text("📎 이미지/PDF 첨부")
          }
        }

        Text("회계 계정과목 선택", style = MaterialTheme.typography.labelMedium, fontWeight = FontWeight.Bold)
        FlowRow(
          horizontalArrangement = Arrangement.spacedBy(6.dp),
          verticalArrangement = Arrangement.spacedBy(4.dp),
        ) {
          SoloLedgerCatalog.accounts.forEach { opt ->
            FilterChip(
              selected = selectedAccount == opt.code,
              onClick = { selectedAccount = opt.code },
              label = { Text(opt.label, style = MaterialTheme.typography.labelSmall) },
            )
          }
        }

        OutlinedTextField(
          value = memoText,
          onValueChange = { memoText = it },
          label = { Text("적요 · 참석자 · 거래명세서 품목명") },
          placeholder = { Text("예: A사 김철수 대표 미팅 / 외장하드 구매") },
          modifier = Modifier.fillMaxWidth(),
        )

        Row(verticalAlignment = Alignment.CenterVertically) {
          Checkbox(checked = applySameMerchant, onCheckedChange = { applySameMerchant = it })
          Text("동일 가맹점 미분류 전표에도 계정과목 자동 적용", style = MaterialTheme.typography.bodySmall)
        }

        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
          Button(
            onClick = { onSave(selectedAccount, memoText, applySameMerchant, false) },
            modifier = Modifier.weight(1f),
          ) {
            Text("💾 분류·적요 저장")
          }
          Button(
            onClick = { onSave(selectedAccount, memoText, applySameMerchant, true) },
            colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF1E3A8A)),
            modifier = Modifier.weight(1f),
          ) {
            Text("📄 명세서까지 발급")
          }
        }
      }
    }
  }
}

@Composable
private fun SlipInfoRow(label: String, value: String, boldValue: Boolean = false) {
  Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
    Text(label, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
    Spacer(modifier = Modifier.width(12.dp))
    Text(
      text = value,
      style = MaterialTheme.typography.bodySmall,
      fontWeight = if (boldValue) FontWeight.ExtraBold else FontWeight.Medium,
    )
  }
}

private fun shareExportedFile(context: Context, artifact: ShareableExportArtifact) {
  val uri = FileProvider.getUriForFile(context, "${context.packageName}.fileprovider", artifact.file)
  val shareIntent =
    Intent(Intent.ACTION_SEND).apply {
      type = artifact.mimeType
      putExtra(Intent.EXTRA_STREAM, uri)
      putExtra(Intent.EXTRA_SUBJECT, artifact.file.nameWithoutExtension)
      putExtra(Intent.EXTRA_TEXT, "SoloLedger 세무 기장원장 및 증빙 자료(${artifact.file.name})를 전달드립니다.")
      addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
    }
  val chooser =
    Intent.createChooser(shareIntent, artifact.chooserTitle).apply {
      addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
    }
  context.startActivity(chooser)
}
