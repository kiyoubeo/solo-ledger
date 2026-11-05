package kr.sololedger.mobile.push

import android.app.Notification
import android.content.ComponentName
import android.content.Context
import android.provider.Settings
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.UUID
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import kr.sololedger.mobile.data.AppPreferences
import kr.sololedger.mobile.data.QueuedCardPush
import kr.sololedger.mobile.data.SoloLedgerApiClient
import kr.sololedger.mobile.data.SoloLedgerCatalog

class WooriPushListenerService : NotificationListenerService() {
  private val serviceScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

  override fun onNotificationPosted(sbn: StatusBarNotification?) {
    if (sbn == null || sbn.isOngoing) return
    val prefs = AppPreferences(applicationContext)
    if (!prefs.wooriPushEnabled) return

    val pkg = sbn.packageName ?: return
    if (!SoloLedgerCatalog.isWooriOrTargetCardPackage(pkg)) return

    val extras = sbn.notification?.extras ?: return
    val title = extras.getCharSequence(Notification.EXTRA_TITLE)?.toString()?.trim().orEmpty()
    val bigText = extras.getCharSequence(Notification.EXTRA_BIG_TEXT)?.toString()?.trim().orEmpty()
    val shortText = extras.getCharSequence(Notification.EXTRA_TEXT)?.toString()?.trim().orEmpty()
    val body = bigText.ifEmpty { shortText }

    if (SoloLedgerCatalog.isPromotionalPush(title, body)) return

    val isoTime =
      DateTimeFormatter.ISO_LOCAL_DATE_TIME.format(
        Instant.ofEpochMilli(sbn.postTime).atZone(ZoneId.systemDefault())
      )
    val queued =
      QueuedCardPush(
        id = UUID.randomUUID().toString(),
        packageName = pkg,
        title = title,
        body = body,
        receivedAt = isoTime,
        cardHint = prefs.wooriCardHint,
      )
    prefs.enqueuePush(queued)

    if (prefs.serverUrl.isNotBlank()) {
      serviceScope.launch {
        flushOfflineQueue(applicationContext)
      }
    }
  }

  override fun onDestroy() {
    serviceScope.cancel()
    super.onDestroy()
  }

  companion object {
    fun isNotificationAccessGranted(context: Context): Boolean {
      val flat =
        Settings.Secure.getString(context.contentResolver, "enabled_notification_listeners")
          .orEmpty()
      val cn = ComponentName(context, WooriPushListenerService::class.java).flattenToString()
      return flat.contains(cn)
    }

    suspend fun flushOfflineQueue(context: Context): Int {
      val prefs = AppPreferences(context)
      if (prefs.serverUrl.isBlank()) return 0
      val api = SoloLedgerApiClient(prefs)
      val items = prefs.getQueuedPushes()
      if (items.isEmpty()) return 0

      val deliveredIds = mutableSetOf<String>()
      for (item in items) {
        try {
          api.sendQueuedCardPush(item)
          deliveredIds.add(item.id)
        } catch (_: Exception) {
          // Keep remaining items in offline queue for next retry
          break
        }
      }
      prefs.removeQueuedPushes(deliveredIds)
      return deliveredIds.size
    }
  }
}
