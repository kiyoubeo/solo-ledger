package kr.sololedger.mobile.data

import android.content.Context
import android.content.SharedPreferences
import java.io.File
import org.json.JSONArray
import org.json.JSONObject

class AppPreferences(context: Context) {
  private val appContext = context.applicationContext
  private val prefs: SharedPreferences =
    appContext.getSharedPreferences("sololedger_mobile_prefs", Context.MODE_PRIVATE)
  private val queueFile = File(appContext.filesDir, "offline_push_queue.json")
  private val lock = Any()

  var serverUrl: String
    get() = prefs.getString("server_url", "") ?: ""
    set(value) = prefs.edit().putString("server_url", SoloLedgerCatalog.normalizeServerUrl(value)).apply()

  var credential: String
    get() = prefs.getString("credential", "dev-sololedger-secret-key") ?: "dev-sololedger-secret-key"
    set(value) = prefs.edit().putString("credential", value.trim()).apply()

  var bearerToken: String
    get() = prefs.getString("bearer_token", "") ?: ""
    set(value) = prefs.edit().putString("bearer_token", value.trim()).apply()

  var wooriPushEnabled: Boolean
    get() = prefs.getBoolean("woori_push_enabled", false)
    set(value) = prefs.edit().putBoolean("woori_push_enabled", value).apply()

  var wooriCardHint: String
    get() = prefs.getString("woori_card_hint", "6534") ?: "6534"
    set(value) = prefs.edit().putString("woori_card_hint", value.trim().ifEmpty { "6534" }).apply()

  var biometricLockEnabled: Boolean
    get() = prefs.getBoolean("biometric_lock_enabled", false)
    set(value) = prefs.edit().putBoolean("biometric_lock_enabled", value).apply()

  fun effectiveAuthToken(): String {
    return bearerToken.ifBlank { credential }
  }

  fun enqueuePush(item: QueuedCardPush) {
    synchronized(lock) {
      val current = getQueuedPushesMutable()
      if (current.any { it.body == item.body && it.receivedAt.take(16) == item.receivedAt.take(16) }) {
        return
      }
      current.add(item)
      saveQueuedPushesInternal(current.takeLast(200))
    }
  }

  fun getQueuedPushes(): List<QueuedCardPush> {
    synchronized(lock) {
      return getQueuedPushesMutable()
    }
  }

  fun removeQueuedPushes(ids: Set<String>) {
    if (ids.isEmpty()) return
    synchronized(lock) {
      val remaining = getQueuedPushesMutable().filterNot { it.id in ids }
      saveQueuedPushesInternal(remaining)
    }
  }

  private fun getQueuedPushesMutable(): MutableList<QueuedCardPush> {
    if (!queueFile.exists()) return mutableListOf()
    return try {
      val arr = JSONArray(queueFile.readText(Charsets.UTF_8))
      val list = mutableListOf<QueuedCardPush>()
      for (i in 0 until arr.length()) {
        val obj = arr.optJSONObject(i) ?: continue
        list.add(
          QueuedCardPush(
            id = obj.optString("id"),
            packageName = obj.optString("packageName", "com.wooricard.smartapp"),
            title = obj.optString("title", ""),
            body = obj.optString("body", ""),
            receivedAt = obj.optString("receivedAt", ""),
            cardHint = obj.optString("cardHint", "6534"),
          )
        )
      }
      list
    } catch (_: Exception) {
      mutableListOf()
    }
  }

  private fun saveQueuedPushesInternal(items: List<QueuedCardPush>) {
    val arr = JSONArray()
    for (item in items) {
      arr.put(
        JSONObject().apply {
          put("id", item.id)
          put("packageName", item.packageName)
          put("title", item.title)
          put("body", item.body)
          put("receivedAt", item.receivedAt)
          put("cardHint", item.cardHint)
        }
      )
    }
    queueFile.writeText(arr.toString(), Charsets.UTF_8)
  }
}
