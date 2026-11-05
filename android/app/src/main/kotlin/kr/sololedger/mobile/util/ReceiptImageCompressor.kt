package kr.sololedger.mobile.util

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Matrix
import android.net.Uri
import android.provider.OpenableColumns
import android.util.Base64
import androidx.exifinterface.media.ExifInterface
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import kotlin.math.max

data class EncodedUploadFile(
  val fileName: String,
  val base64Content: String,
  val byteSize: Int,
)

object ReceiptImageCompressor {
  fun resolveDisplayName(context: Context, uri: Uri, fallback: String): String {
    return try {
      context.contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use { cursor ->
        if (cursor.moveToFirst()) {
          val idx = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
          if (idx >= 0) cursor.getString(idx)?.takeIf { it.isNotBlank() } else null
        } else {
          null
        }
      } ?: fallback
    } catch (_: Exception) {
      fallback
    }
  }

  fun encodeReceiptUri(context: Context, uri: Uri, fallbackName: String = "receipt.jpg"): EncodedUploadFile {
    val rawName = resolveDisplayName(context, uri, fallbackName)
    val mime = context.contentResolver.getType(uri).orEmpty().lowercase()
    val rawBytes =
      context.contentResolver.openInputStream(uri)?.use { it.readBytes() }
        ?: throw IllegalArgumentException("선택한 파일을 읽을 수 없습니다.")

    val isImage =
      mime.startsWith("image/") ||
        rawName.endsWith(".jpg", true) ||
        rawName.endsWith(".jpeg", true) ||
        rawName.endsWith(".png", true) ||
        rawName.endsWith(".webp", true)

    if (!isImage) {
      val b64 = Base64.encodeToString(rawBytes, Base64.NO_WRAP)
      return EncodedUploadFile(fileName = rawName, base64Content = b64, byteSize = rawBytes.size)
    }

    val compressed = compressImageBytesToJpeg(rawBytes, maxDimension = 1600, quality = 82)
    val baseName = rawName.substringBeforeLast('.', rawName).ifBlank { "receipt" }
    val normalizedName = "$baseName.jpg"
    val b64 = Base64.encodeToString(compressed, Base64.NO_WRAP)
    return EncodedUploadFile(fileName = normalizedName, base64Content = b64, byteSize = compressed.size)
  }

  private fun compressImageBytesToJpeg(rawBytes: ByteArray, maxDimension: Int, quality: Int): ByteArray {
    val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
    BitmapFactory.decodeByteArray(rawBytes, 0, rawBytes.size, bounds)
    if (bounds.outWidth <= 0 || bounds.outHeight <= 0) return rawBytes

    var sampleSize = 1
    while (max(bounds.outWidth / sampleSize, bounds.outHeight / sampleSize) > maxDimension * 2) {
      sampleSize *= 2
    }

    val decodeOpts = BitmapFactory.Options().apply { inSampleSize = sampleSize }
    val decoded = BitmapFactory.decodeByteArray(rawBytes, 0, rawBytes.size, decodeOpts) ?: return rawBytes
    val rotated = applyExifRotation(rawBytes, decoded)

    val maxSide = max(rotated.width, rotated.height)
    val scaled =
      if (maxSide > maxDimension) {
        val ratio = maxDimension.toFloat() / maxSide.toFloat()
        val targetW = (rotated.width * ratio).toInt().coerceAtLeast(1)
        val targetH = (rotated.height * ratio).toInt().coerceAtLeast(1)
        Bitmap.createScaledBitmap(rotated, targetW, targetH, true)
      } else {
        rotated
      }

    val out = ByteArrayOutputStream()
    scaled.compress(Bitmap.CompressFormat.JPEG, quality, out)
    if (scaled !== rotated) scaled.recycle()
    if (rotated !== decoded) rotated.recycle()
    decoded.recycle()
    return out.toByteArray()
  }

  private fun applyExifRotation(rawBytes: ByteArray, bitmap: Bitmap): Bitmap {
    return try {
      val exif = ExifInterface(ByteArrayInputStream(rawBytes))
      val orientation =
        exif.getAttributeInt(ExifInterface.TAG_ORIENTATION, ExifInterface.ORIENTATION_NORMAL)
      val degrees =
        when (orientation) {
          ExifInterface.ORIENTATION_ROTATE_90 -> 90f
          ExifInterface.ORIENTATION_ROTATE_180 -> 180f
          ExifInterface.ORIENTATION_ROTATE_270 -> 270f
          else -> 0f
        }
      if (degrees == 0f) {
        bitmap
      } else {
        val matrix = Matrix().apply { postRotate(degrees) }
        Bitmap.createBitmap(bitmap, 0, 0, bitmap.width, bitmap.height, matrix, true)
      }
    } catch (_: Exception) {
      bitmap
    }
  }
}
