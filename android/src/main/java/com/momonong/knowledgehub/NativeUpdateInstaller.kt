package com.momonong.knowledgehub

import android.app.Activity
import android.content.ActivityNotFoundException
import android.content.Context
import android.content.Intent
import android.content.pm.PackageInfo
import android.content.pm.PackageInstaller
import android.content.pm.PackageManager
import android.content.pm.Signature
import android.net.Uri
import android.os.Build
import android.provider.Settings
import androidx.core.content.FileProvider
import androidx.core.content.pm.PackageInfoCompat
import java.io.File
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.util.concurrent.TimeUnit

class NativeUpdateFileProvider : FileProvider()

internal data class NativeUpdateInstalled(val versionCode: Long, val versionName: String)
internal data class NativeUpdateApk(val release: NativeUpdateRelease, val file: File)
internal data class NativeUpdateIdentity(
    val packageName: String,
    val versionCode: Long,
    val versionName: String?,
    val signers: Set<Signature>,
)

internal fun verifyNativeUpdateIdentity(
    release: NativeUpdateRelease,
    installed: NativeUpdateIdentity,
    archive: NativeUpdateIdentity,
) {
    if (installed.packageName != NATIVE_UPDATE_PACKAGE || archive.packageName != installed.packageName) {
        throw NativeUpdateFailure("更新檔的套件名稱與 Knowledge Hub 不符。")
    }
    if (archive.versionCode <= installed.versionCode || archive.versionCode != release.versionCode ||
        archive.versionName != release.versionName
    ) {
        throw NativeUpdateFailure("更新檔的版本與發布資訊不符，或版本過舊。")
    }
    if (installed.signers.isEmpty() || archive.signers != installed.signers) {
        throw NativeUpdateFailure("更新檔的簽署憑證與目前安裝的 Knowledge Hub 不符。")
    }
}

internal fun nativeInstallNotice(resultCode: Int, result: Intent?): String? {
    val status = result?.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_SUCCESS)
    return when {
        status == PackageInstaller.STATUS_FAILURE_BLOCKED -> "Android 安全防護或裝置政策已封鎖此更新。"
        resultCode == Activity.RESULT_OK && (status == null || status == PackageInstaller.STATUS_SUCCESS) -> null
        resultCode == Activity.RESULT_FIRST_USER || (status != null && status > PackageInstaller.STATUS_SUCCESS) ->
            "Android 回報安裝失敗，可以重新下載後再試。"
        else -> "Android 安裝已取消，可以重新下載後再試。"
    }
}

internal class NativeUpdateInstaller(private val context: Context) {
    private val packages = context.packageManager

    @Suppress("DEPRECATION")
    fun installed(): NativeUpdateInstalled {
        val info = packages.getPackageInfo(context.packageName, 0)
        return NativeUpdateInstalled(
            PackageInfoCompat.getLongVersionCode(info),
            info.versionName.orEmpty().ifBlank { PackageInfoCompat.getLongVersionCode(info).toString() },
        )
    }

    @Suppress("DEPRECATION") // API 26–27 require GET_SIGNATURES; some archive readers need both flags on later APIs.
    fun verify(file: File, release: NativeUpdateRelease) {
        val flags = PackageManager.GET_SIGNATURES or
            if (Build.VERSION.SDK_INT >= 28) PackageManager.GET_SIGNING_CERTIFICATES else 0
        val installed = packages.getPackageInfo(context.packageName, flags)
        val archive = packages.getPackageArchiveInfo(file.absolutePath, flags)
            ?: throw NativeUpdateFailure("Android 無法讀取更新檔的 APK 資訊或簽署憑證。")
        verifyNativeUpdateIdentity(release, installed.identity(), archive.identity())
    }

    fun canInstall(): Boolean = packages.canRequestPackageInstalls()

    fun settingsIntent() =
        Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.fromParts("package", context.packageName, null))

    @Suppress("DEPRECATION") // ACTION_INSTALL_PACKAGE requests Android's user confirmation and reports the result.
    fun installIntent(apk: NativeUpdateApk, checkActive: () -> Unit = {}): Intent {
        if (!canInstall()) throw NativeUpdateFailure("請先在 Android 設定允許從 Knowledge Hub 安裝更新。")
        if (!apk.file.isFile || apk.file.length() != apk.release.sizeBytes) {
            throw NativeUpdateFailure("更新檔已遺失或不完整，請重新下載。")
        }
        apk.file.inputStream().buffered().use { verifyNativeUpdateBytes(it, apk.release, checkActive = checkActive) }
        verify(apk.file, apk.release)

        val uri = FileProvider.getUriForFile(context, "${context.packageName}.updates", apk.file)
        val intent = Intent(Intent.ACTION_INSTALL_PACKAGE).setDataAndType(uri, NATIVE_UPDATE_MIME)
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            .putExtra(Intent.EXTRA_RETURN_RESULT, true)
        val installer = packages.resolveActivity(intent, PackageManager.MATCH_DEFAULT_ONLY or PackageManager.MATCH_SYSTEM_ONLY)
            ?: throw ActivityNotFoundException()
        return intent.setPackage(installer.activityInfo.packageName)
    }

    @Suppress("DEPRECATION")
    private fun PackageInfo.identity(): NativeUpdateIdentity {
        val currentSigners = if (Build.VERSION.SDK_INT >= 28) signingInfo?.apkContentsSigners else signatures
        return NativeUpdateIdentity(packageName, PackageInfoCompat.getLongVersionCode(this), versionName,
            currentSigners.orEmpty().toSet())
    }
}

internal class NativeUpdateCache(cache: File) {
    private val staging = File(cache, "native-updates/staging")
    private val verified = File(cache, "native-updates/verified")

    fun stage(release: NativeUpdateRelease): File {
        Files.createDirectories(staging.toPath())
        return File.createTempFile("kh-update-${release.versionCode}-", ".apk", staging)
    }

    fun publish(candidate: File): File {
        Files.createDirectories(verified.toPath())
        val destination = File(verified, candidate.name)
        return Files.move(candidate.toPath(), destination.toPath(), StandardCopyOption.ATOMIC_MOVE).toFile()
    }

    fun clean(installedVersion: Long) {
        val expiry = System.currentTimeMillis() - TimeUnit.DAYS.toMillis(1)
        for (directory in listOf(staging, verified)) {
            directory.listFiles()?.forEach { file ->
                val version = file.name.removePrefix("kh-update-").substringBefore('-').toLongOrNull()
                if (file.lastModified() < expiry || (version != null && version <= installedVersion)) file.delete()
            }
        }
    }
}
