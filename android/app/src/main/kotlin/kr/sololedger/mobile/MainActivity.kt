package kr.sololedger.mobile

import android.os.Bundle
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.biometric.BiometricManager
import androidx.biometric.BiometricPrompt
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import androidx.fragment.app.FragmentActivity
import kr.sololedger.mobile.data.AppPreferences
import kr.sololedger.mobile.ui.SoloLedgerAppScreen
import kr.sololedger.mobile.ui.theme.SoloLedgerTheme

class MainActivity : FragmentActivity() {
  private var isUnlocked by mutableStateOf(true)
  private var lockError by mutableStateOf<String?>(null)

  override fun onCreate(savedInstanceState: Bundle?) {
    super.onCreate(savedInstanceState)
    val prefs = AppPreferences(this)
    if (prefs.biometricLockEnabled && canUseDeviceAuth()) {
      isUnlocked = false
      promptBiometricUnlock()
    }

    enableEdgeToEdge()
    setContent {
      SoloLedgerTheme {
        Surface(modifier = Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
          if (isUnlocked) {
            SoloLedgerAppScreen()
          } else {
            Column(
              modifier = Modifier.fillMaxSize().padding(28.dp),
              verticalArrangement = Arrangement.Center,
              horizontalAlignment = Alignment.CenterHorizontally,
            ) {
              Text(
                text = "🔒 SoloLedger 법인원장 잠금",
                style = MaterialTheme.typography.headlineSmall,
                fontWeight = FontWeight.Bold,
              )
              Spacer(modifier = Modifier.height(8.dp))
              Text(
                text = lockError ?: "세무·회계 원장 보호를 위해 지문 또는 화면 잠금 인증을 진행해 주세요.",
                style = MaterialTheme.typography.bodyMedium,
              )
              Spacer(modifier = Modifier.height(20.dp))
              Button(onClick = { promptBiometricUnlock() }) {
                Text("지문 · 본인 인증하기")
              }
            }
          }
        }
      }
    }
  }

  private fun canUseDeviceAuth(): Boolean {
    val bm = BiometricManager.from(this)
    val authenticators =
      BiometricManager.Authenticators.BIOMETRIC_WEAK or
        BiometricManager.Authenticators.DEVICE_CREDENTIAL
    return bm.canAuthenticate(authenticators) == BiometricManager.BIOMETRIC_SUCCESS
  }

  private fun promptBiometricUnlock() {
    val executor = ContextCompat.getMainExecutor(this)
    val prompt =
      BiometricPrompt(
        this,
        executor,
        object : BiometricPrompt.AuthenticationCallback() {
          override fun onAuthenticationSucceeded(result: BiometricPrompt.AuthenticationResult) {
            isUnlocked = true
            lockError = null
          }

          override fun onAuthenticationError(errorCode: Int, errString: CharSequence) {
            lockError = errString.toString()
          }
        },
      )
    val info =
      BiometricPrompt.PromptInfo.Builder()
        .setTitle("SoloLedger 본인 인증")
        .setSubtitle("법인카드 원장 및 세무증빙 열람")
        .setAllowedAuthenticators(
          BiometricManager.Authenticators.BIOMETRIC_WEAK or
            BiometricManager.Authenticators.DEVICE_CREDENTIAL
        )
        .build()
    prompt.authenticate(info)
  }
}
