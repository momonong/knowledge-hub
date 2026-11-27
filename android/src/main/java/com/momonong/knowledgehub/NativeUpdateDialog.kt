package com.momonong.knowledgehub

import android.content.ActivityNotFoundException
import android.content.Context
import android.content.Intent
import android.webkit.CookieManager
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.DialogProperties
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import java.io.File
import java.io.IOException
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

private sealed interface NativeUpdateStatus {
    data object Closed : NativeUpdateStatus
    data object Checking : NativeUpdateStatus
    data object Installed : NativeUpdateStatus
    data class Current(val installed: NativeUpdateInstalled, val newerThanRelease: Boolean) : NativeUpdateStatus
    data class Available(val installed: NativeUpdateInstalled, val release: NativeUpdateRelease) : NativeUpdateStatus
    data class Downloading(
        val available: Available,
        val attempt: Any,
        val percent: Int = 0,
    ) : NativeUpdateStatus
    data class Ready(
        val apk: NativeUpdateApk,
        val allowed: Boolean,
        val awaiting: NativeUpdateAction? = null,
        val notice: String? = null,
    ) : NativeUpdateStatus
    data class Failed(val message: String, val requiresLogin: Boolean = false) : NativeUpdateStatus
}

private enum class NativeUpdateAction { SETTINGS, VERIFYING, INSTALLER }

private class NativeUpdateController(
    context: Context,
    private val scope: CoroutineScope,
    private val client: NativeUpdateClient,
) {
    private val installer = NativeUpdateInstaller(context)
    private val cache = NativeUpdateCache(context.cacheDir)
    val status = MutableStateFlow<NativeUpdateStatus>(NativeUpdateStatus.Closed)
    private var task: Job? = null

    fun open() {
        if (status.value == NativeUpdateStatus.Closed) check()
    }

    fun check() = start(NativeUpdateStatus.Checking) {
        val next = withContext(Dispatchers.IO) {
            val operation = currentCoroutineContext()
            val installed = installer.installed()
            cache.clean(installed.versionCode)
            val release = client.check { operation.ensureActive() }
            when (release.availability(installed.versionCode)) {
                NativeUpdateAvailability.AVAILABLE -> NativeUpdateStatus.Available(installed, release)
                NativeUpdateAvailability.CURRENT -> NativeUpdateStatus.Current(installed, newerThanRelease = false)
                NativeUpdateAvailability.INSTALLED_NEWER -> NativeUpdateStatus.Current(installed, newerThanRelease = true)
            }
        }
        status.value = next
    }

    fun download(available: NativeUpdateStatus.Available) {
        val downloading = NativeUpdateStatus.Downloading(available, attempt = Any())
        start(downloading) {
            val cookie = CookieManager.getInstance().getCookie(SitePolicy.APK_DOWNLOAD)
            var candidate: File? = null
            var verified: File? = null
            var keepVerified = false
            try {
                val ready = withContext(Dispatchers.IO) {
                    val operation = currentCoroutineContext()
                    val release = available.release
                    val file = cache.stage(release).also { candidate = it }
                    client.download(release, cookie, file, { bytes ->
                        val percent = (bytes * 100 / release.sizeBytes).toInt()
                        status.update { current ->
                            if (current is NativeUpdateStatus.Downloading && current.attempt === downloading.attempt) {
                                current.copy(percent = percent)
                            } else current
                        }
                    }, { operation.ensureActive() })
                    installer.verify(file, release)
                    operation.ensureActive()
                    val published = cache.publish(file).also { verified = it }
                    NativeUpdateStatus.Ready(NativeUpdateApk(release, published), allowed = installer.canInstall())
                }
                status.value = ready
                keepVerified = true
            } finally {
                candidate?.delete()
                if (!keepVerified) verified?.delete()
            }
        }
    }

    fun cancelDownload() {
        val downloading = status.value as? NativeUpdateStatus.Downloading ?: return
        task?.cancel()
        status.value = downloading.available
    }

    fun close() {
        task?.cancel()
        val ready = status.value as? NativeUpdateStatus.Ready
        if (ready?.awaiting != NativeUpdateAction.INSTALLER) ready?.apk?.file?.delete()
        status.value = NativeUpdateStatus.Closed
    }

    fun requestInstall(launch: (Intent) -> Unit) {
        val ready = status.value as? NativeUpdateStatus.Ready ?: return
        if (ready.awaiting != null) return
        start(ready.copy(awaiting = NativeUpdateAction.VERIFYING, notice = null)) {
            try {
                if (installer.canInstall()) {
                    val intent = withContext(Dispatchers.IO) {
                        val operation = currentCoroutineContext()
                        installer.installIntent(ready.apk) { operation.ensureActive() }
                    }
                    status.value = ready.copy(allowed = true, awaiting = NativeUpdateAction.INSTALLER, notice = null)
                    launch(intent)
                } else {
                    status.value = ready.copy(allowed = false, awaiting = NativeUpdateAction.SETTINGS, notice = null)
                    launch(installer.settingsIntent())
                }
            } catch (_: ActivityNotFoundException) {
                status.value = ready.copy(notice = "此裝置無法開啟 Android 安裝或設定畫面。")
            } catch (_: SecurityException) {
                status.value = ready.copy(notice = "Android 已限制此裝置安裝更新。")
            } catch (error: IOException) {
                ready.apk.file.delete()
                throw error
            }
        }
    }

    fun externalResult(resultCode: Int, data: Intent?) {
        val ready = status.value as? NativeUpdateStatus.Ready ?: return
        when (ready.awaiting) {
            NativeUpdateAction.SETTINGS -> {
                val allowed = installer.canInstall()
                status.value = ready.copy(allowed = allowed, awaiting = null, notice = if (allowed) {
                    "已允許從 Knowledge Hub 安裝應用程式。"
                } else {
                    "請允許從 Knowledge Hub 安裝應用程式後再繼續。"
                })
            }
            NativeUpdateAction.INSTALLER -> {
                ready.apk.file.delete()
                val notice = nativeInstallNotice(resultCode, data)
                status.value = if (notice == null) NativeUpdateStatus.Installed else NativeUpdateStatus.Failed(notice)
            }
            else -> Unit
        }
    }

    private fun start(initial: NativeUpdateStatus, perform: suspend () -> Unit) {
        if (task?.isActive == true) return
        status.value = initial
        task = scope.launch {
            try {
                perform()
            } catch (error: CancellationException) {
                throw error
            } catch (error: NativeUpdateFailure) {
                currentCoroutineContext().ensureActive()
                status.value = NativeUpdateStatus.Failed(error.message.orEmpty(), error.requiresLogin)
            } catch (_: Exception) {
                currentCoroutineContext().ensureActive()
                status.value = NativeUpdateStatus.Failed("這次更新未完成，請重新檢查更新。")
            }
        }
    }
}

@Composable
internal fun NativeUpdateDialog(visible: Boolean, onDismiss: () -> Unit, onLogin: () -> Unit) {
    NativeUpdateDialog(visible, onDismiss, onLogin, client = remember { NativeUpdateClient() })
}

@Composable
internal fun NativeUpdateDialog(
    visible: Boolean,
    onDismiss: () -> Unit,
    onLogin: () -> Unit,
    client: NativeUpdateClient,
) {
    val context = LocalContext.current.applicationContext
    val scope = rememberCoroutineScope()
    val controller = remember(context, scope, client) { NativeUpdateController(context, scope, client) }
    val status by controller.status.collectAsStateWithLifecycle()
    val systemActivity = rememberLauncherForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
        controller.externalResult(result.resultCode, result.data)
    }
    LaunchedEffect(visible, controller) {
        if (visible) controller.open() else controller.close()
    }
    DisposableEffect(controller) {
        onDispose { controller.close() }
    }
    if (!visible) return

    val current = status
    val waiting = current is NativeUpdateStatus.Ready && current.awaiting != null
    val close = {
        controller.close()
        onDismiss()
    }
    AlertDialog(
        onDismissRequest = close,
        properties = DialogProperties(dismissOnBackPress = !waiting, dismissOnClickOutside = !waiting),
        modifier = Modifier.testTag("native-update-dialog"),
        title = { Text("更新應用程式") },
        text = {
            Column(
                verticalArrangement = Arrangement.spacedBy(12.dp),
                modifier = Modifier.verticalScroll(rememberScrollState()),
            ) {
                NativeUpdateMessage(current)
                LegacyBookmarkExport()
            }
        },
        confirmButton = {
            when (current) {
                is NativeUpdateStatus.Available ->
                    TextButton(onClick = { controller.download(current) }) { Text("下載更新") }
                is NativeUpdateStatus.Ready -> TextButton(
                    enabled = !waiting,
                    onClick = { controller.requestInstall { systemActivity.launch(it) } },
                ) { Text(if (current.allowed) "安裝更新" else "開啟 Android 設定") }
                is NativeUpdateStatus.Failed ->
                    if (current.requiresLogin) {
                        TextButton(onClick = { controller.close(); onLogin() }) { Text("在 App 內登入") }
                    } else {
                        TextButton(onClick = controller::check) { Text("重新檢查") }
                    }
                else -> Unit
            }
        },
        dismissButton = {
            if (current is NativeUpdateStatus.Downloading) {
                TextButton(onClick = controller::cancelDownload) { Text("取消下載") }
            } else {
                TextButton(onClick = close, enabled = !waiting) { Text("關閉") }
            }
        },
    )
}

@Composable
private fun NativeUpdateMessage(status: NativeUpdateStatus) {
    val notice = when (status) {
        NativeUpdateStatus.Closed, NativeUpdateStatus.Checking -> "正在檢查更新…"
        NativeUpdateStatus.Installed -> "Android 已完成更新。"
        is NativeUpdateStatus.Current -> if (status.newerThanRelease) {
            "目前安裝的版本 ${status.installed.versionName} 比已發行版本新。"
        } else {
            "目前安裝的版本 ${status.installed.versionName} 已是最新版本。"
        }
        is NativeUpdateStatus.Available ->
            "目前版本 ${status.installed.versionName}\n可更新至 ${status.release.versionName}"
        is NativeUpdateStatus.Downloading -> if (status.percent == 100) "正在驗證更新檔…" else "下載中 · ${status.percent}%"
        is NativeUpdateStatus.Ready -> status.notice ?: when (status.awaiting) {
            NativeUpdateAction.VERIFYING -> "正在重新驗證更新檔…"
            NativeUpdateAction.SETTINGS -> "請在 Android 設定中允許從 Knowledge Hub 安裝應用程式。"
            NativeUpdateAction.INSTALLER -> "請在 Android 安裝畫面確認更新。"
            null -> if (status.allowed) {
                "版本 ${status.apk.release.versionName} 已驗證。下一步由你在 Android 安裝畫面確認。"
            } else {
                "版本 ${status.apk.release.versionName} 已驗證。請先在 Android 設定允許從 Knowledge Hub 安裝應用程式。"
            }
        }
        is NativeUpdateStatus.Failed -> status.message
    }
    val progress = status as? NativeUpdateStatus.Downloading
    Text(notice, modifier = if (progress == null) {
        Modifier.semantics { liveRegion = LiveRegionMode.Polite }
    } else Modifier)
    when {
        progress != null && progress.percent < 100 ->
            LinearProgressIndicator(progress = { progress.percent / 100f }, modifier = Modifier.fillMaxWidth())
        progress != null || status == NativeUpdateStatus.Checking || status == NativeUpdateStatus.Closed ||
            (status is NativeUpdateStatus.Ready && status.awaiting == NativeUpdateAction.VERIFYING) ->
            LinearProgressIndicator(modifier = Modifier.fillMaxWidth())
    }
}
