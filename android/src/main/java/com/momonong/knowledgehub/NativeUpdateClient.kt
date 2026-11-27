package com.momonong.knowledgehub

import android.util.JsonReader
import android.util.JsonToken
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.net.HttpURLConnection
import java.net.URI
import java.security.MessageDigest

internal const val NATIVE_UPDATE_PACKAGE = "com.momonong.knowledgehub"
internal const val NATIVE_UPDATE_MIME = "application/vnd.android.package-archive"
private const val RELEASE_URL = "${SitePolicy.HOME}android-release.json"
private const val MAX_RELEASE_BYTES = 16 * 1024
private const val MAX_APK_BYTES = 256L * 1024 * 1024
private const val LIMIT_MESSAGE = "更新檔的大小與發布資訊不符。"
private val releaseInteger = Regex("[1-9][0-9]*")
private val releaseSha256 = Regex("[0-9a-f]{64}")

internal enum class NativeUpdateAvailability { AVAILABLE, CURRENT, INSTALLED_NEWER }

internal data class NativeUpdateRelease(
    val versionCode: Long,
    val versionName: String,
    val sizeBytes: Long,
    val sha256: String,
) {
    fun availability(installedCode: Long): NativeUpdateAvailability = when {
        versionCode > installedCode -> NativeUpdateAvailability.AVAILABLE
        versionCode < installedCode -> NativeUpdateAvailability.INSTALLED_NEWER
        else -> NativeUpdateAvailability.CURRENT
    }
}

internal class NativeUpdateFailure(message: String, val requiresLogin: Boolean = false) : IOException(message)

internal class NativeUpdateClient(
    private val open: (String) -> HttpURLConnection = { URI(it).toURL().openConnection() as HttpURLConnection },
) {
    fun check(checkActive: () -> Unit = {}): NativeUpdateRelease = get(RELEASE_URL, "application/json") { response ->
        val buffer = ByteArray(1024)
        val contents = ByteArrayOutputStream()
        response.inputStream.use { input ->
            while (true) {
                checkActive()
                val read = input.read(buffer)
                if (read == -1) break
                if (contents.size() + read > MAX_RELEASE_BYTES) {
                    throw NativeUpdateFailure("更新資訊超過大小限制。")
                }
                contents.write(buffer, 0, read)
            }
        }
        parseRelease(contents.toByteArray())
    }

    fun download(
        release: NativeUpdateRelease,
        cookie: String?,
        destination: File,
        onProgress: (Long) -> Unit,
        checkActive: () -> Unit,
    ) {
        var complete = false
        try {
            checkActive()
            get(SitePolicy.APK_DOWNLOAD, NATIVE_UPDATE_MIME, cookie) { response ->
                val length = response.contentLengthLong
                if (length >= 0 && length != release.sizeBytes) throw NativeUpdateFailure(LIMIT_MESSAGE)
                response.inputStream.use { input ->
                    destination.outputStream().buffered().use { output ->
                        verifyNativeUpdateBytes(input, release, output, onProgress, checkActive)
                    }
                }
            }
            checkActive()
            complete = true
        } finally {
            if (!complete) destination.delete()
        }
    }

    private fun <T> get(
        url: String,
        accept: String,
        cookie: String? = null,
        read: (HttpURLConnection) -> T,
    ): T {
        val request = open(url)
        try {
            request.requestMethod = "GET"
            request.instanceFollowRedirects = false
            request.useCaches = false
            request.connectTimeout = 8_000
            request.readTimeout = 12_000
            request.setRequestProperty("Accept", accept)
            request.setRequestProperty("Accept-Encoding", "identity")
            request.setRequestProperty("Cache-Control", "no-cache, no-store")
            if (url == SitePolicy.APK_DOWNLOAD && !cookie.isNullOrBlank()) request.setRequestProperty("Cookie", cookie)

            return when (val code = request.responseCode) {
                200 -> read(request)
                401, 403 -> if (url == SitePolicy.APK_DOWNLOAD) {
                    throw NativeUpdateFailure("請在 App 內登入 Knowledge Hub，再回來更新。", requiresLogin = true)
                } else {
                    throw NativeUpdateFailure("目前無法取得版本資訊，請稍後重試。")
                }
                404 -> throw NativeUpdateFailure("目前沒有可讀取的版本資訊或更新檔。")
                in 300..399 -> throw NativeUpdateFailure("更新網址發生轉址，請稍後重試。")
                else -> throw NativeUpdateFailure("更新服務目前無法使用（HTTP $code）。")
            }
        } finally {
            request.disconnect()
        }
    }
}

internal fun verifyNativeUpdateBytes(
    input: InputStream,
    release: NativeUpdateRelease,
    output: OutputStream? = null,
    onProgress: (Long) -> Unit = {},
    checkActive: () -> Unit = {},
) {
    if (release.sizeBytes !in 1..MAX_APK_BYTES) throw NativeUpdateFailure(LIMIT_MESSAGE)
    val digest = MessageDigest.getInstance("SHA-256")
    val buffer = ByteArray(64 * 1024)
    var received = 0L
    while (true) {
        checkActive()
        val count = input.read(buffer)
        if (count == -1) break
        received += count
        if (received > release.sizeBytes) throw NativeUpdateFailure(LIMIT_MESSAGE)
        digest.update(buffer, 0, count)
        output?.write(buffer, 0, count)
        onProgress(received)
    }
    if (received != release.sizeBytes) throw NativeUpdateFailure(LIMIT_MESSAGE)
    if (digest.digest().joinToString("") { "%02x".format(it) } != release.sha256) {
        throw NativeUpdateFailure("更新檔的 SHA-256 校驗失敗，請重新下載。")
    }
}

private fun parseRelease(bytes: ByteArray): NativeUpdateRelease {
    val invalid = NativeUpdateFailure("更新資訊格式有誤，請稍後重試。")
    try {
        val fields = mutableMapOf<String, String>()
        JsonReader(bytes.decodeToString(throwOnInvalidSequence = true).reader()).use { reader ->
            reader.beginObject()
            while (reader.hasNext()) {
                val key = reader.nextName()
                val expected = when (key) {
                    "applicationId", "versionName", "sha256" -> JsonToken.STRING
                    "versionCode", "sizeBytes" -> JsonToken.NUMBER
                    else -> {
                        reader.skipValue()
                        continue
                    }
                }
                if (key in fields || reader.peek() != expected) throw invalid
                fields[key] = reader.nextString()
            }
            reader.endObject()
            if (reader.peek() != JsonToken.END_DOCUMENT) throw invalid
        }
        fun positiveInteger(key: String) =
            fields[key]?.takeIf(releaseInteger::matches)?.toLongOrNull() ?: throw invalid

        if (fields["applicationId"] != NATIVE_UPDATE_PACKAGE) throw invalid
        val versionName = fields["versionName"]?.takeIf { name ->
            name.isNotBlank() && name.length <= 64 &&
                name.none { it.isISOControl() || Character.getType(it) == Character.FORMAT.toInt() }
        } ?: throw invalid
        val sizeBytes = positiveInteger("sizeBytes").takeIf { it <= MAX_APK_BYTES } ?: throw invalid
        val sha256 = fields["sha256"]?.takeIf(releaseSha256::matches) ?: throw invalid
        return NativeUpdateRelease(positiveInteger("versionCode"), versionName, sizeBytes, sha256)
    } catch (_: IOException) {
        throw invalid
    } catch (_: IllegalStateException) {
        throw invalid
    }
}
