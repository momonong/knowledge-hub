package com.momonong.knowledgehub

import android.content.ActivityNotFoundException
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.net.http.SslError
import android.view.View
import android.webkit.CookieManager
import android.webkit.RenderProcessGoneDetail
import android.webkit.SslErrorHandler
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalResources
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.content.edit
import androidx.webkit.WebSettingsCompat
import androidx.webkit.WebViewCompat
import androidx.webkit.WebViewFeature
import java.io.ByteArrayInputStream
import org.json.JSONObject

private const val LAST_ARTICLE_URL = "last_article_url"

@Composable
internal fun SiteReader(
    onBackAtRoot: () -> Unit,
    loginForUpdate: Boolean,
    onReturnToUpdate: () -> Unit,
    onUpdate: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val context = LocalContext.current
    val resources = LocalResources.current
    val waitingForLogin by rememberUpdatedState(loginForUpdate)
    val returnToUpdate by rememberUpdatedState(onReturnToUpdate)
    val readerResume = remember(context) { context.getSharedPreferences("site_reader", Context.MODE_PRIVATE) }
    val navigationScript = remember(resources) {
        resources.openRawResource(R.raw.reader_navigation).bufferedReader().use { it.readText().trim() }
    }
    val headerScript = remember(resources) {
        val script = resources.openRawResource(R.raw.reader_header).bufferedReader().use { it.readText().trim() }
        "$script(${JSONObject.quote(SitePolicy.ORIGIN)})"
    }
    // Save only a vetted GET destination. A WebView history bundle can retain POST state.
    var resumeUrl by rememberSaveable {
        val lastArticle = readerResume.getString(LAST_ARTICLE_URL, null)?.takeIf(SitePolicy::isFixedArticle)
        mutableStateOf(lastArticle ?: SitePolicy.HOME)
    }
    var retryUrl by remember { mutableStateOf(SitePolicy.resumableUrl(resumeUrl) ?: SitePolicy.HOME) }
    var webView by remember { mutableStateOf<WebView?>(null) }
    var rendererAvailable by remember { mutableStateOf(true) }
    var loading by remember { mutableStateOf(true) }
    var failure by remember { mutableStateOf<String?>(null) }
    var notice by remember { mutableStateOf<String?>(null) }
    var external by remember { mutableStateOf<String?>(null) }
    var selected by rememberSaveable { mutableStateOf<ReaderDestination?>(null) }
    var pendingDispatch by remember { mutableStateOf(false) }
    var navigationEpoch by remember { mutableStateOf(0) }

    fun load(url: String) {
        failure = null
        notice = null
        loading = true
        val view = webView
        if (view == null) {
            retryUrl = url
            rendererAvailable = true
        } else {
            view.stopLoading()
            view.loadUrl(url)
        }
    }

    fun goBack() {
        val view = webView
        val hasHistory = view?.canGoBack() == true
        if (hasHistory) {
            failure = null
            view.goBack()
        }
        if (loginForUpdate) onReturnToUpdate() else if (!hasHistory) onBackAtRoot()
    }

    fun navigateTo(url: String) {
        if (loading || pendingDispatch || loginForUpdate) return
        val view = webView
        if (view == null) {
            load(url)
            return
        }
        val currentUrl = view.url ?: return
        if (!SitePolicy.allowsSite(currentUrl) || currentUrl.substringBefore('#') == url) return
        val epoch = navigationEpoch
        pendingDispatch = true
        val invocation = "$navigationScript(${JSONObject.quote(url)}, ${JSONObject.quote(SitePolicy.ORIGIN)})"
        view.evaluateJavascript(invocation) { dispatched ->
            if (view === webView && epoch == navigationEpoch) {
                pendingDispatch = false
                if (dispatched != "true" && failure == null) {
                    notice = "請先完成目前的操作，再切換頁面。"
                }
            }
        }
    }

    LaunchedEffect(loginForUpdate) {
        if (loginForUpdate && (!loading || retryUrl != SitePolicy.HOME)) load(SitePolicy.HOME)
    }
    BackHandler { goBack() }
    ReaderNavigationLayout(
        selected = selected,
        onSelect = { navigateTo(it.url) },
        onManage = { navigateTo(SitePolicy.MANAGE) },
        onUpdate = onUpdate,
        canNavigate = !loading && failure == null,
        showNavigation = !loginForUpdate,
        modifier = modifier,
    ) {
        Column(Modifier.fillMaxSize()) {
            if (loading && failure == null) LinearProgressIndicator(Modifier.fillMaxWidth())
            notice?.let {
                Surface(color = MaterialTheme.colorScheme.surfaceContainerHigh) {
                    Text(it, modifier = Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 10.dp),
                        style = MaterialTheme.typography.bodySmall)
                }
            }
            Box(Modifier.weight(1f).fillMaxWidth()) {
                if (rendererAvailable) {
                    AndroidView(
                        modifier = Modifier.fillMaxSize().testTag("reader-web-view"),
                        factory = { viewContext ->
                            WebView(viewContext).also { view ->
                                webView = view
                                configureSiteWebView(view)
                                val headerOnFinish = if (WebViewFeature.isFeatureSupported(WebViewFeature.DOCUMENT_START_SCRIPT)) {
                                    WebViewCompat.addDocumentStartJavaScript(view, headerScript, setOf(SitePolicy.ORIGIN))
                                    null
                                } else {
                                    headerScript
                                }
                                view.webViewClient = KnowledgeSiteClient(
                                    onStart = { url ->
                                        navigationEpoch++
                                        pendingDispatch = false
                                        loading = true
                                        failure = null
                                        notice = null
                                        retryUrl = SitePolicy.resumableUrl(url) ?: resumeUrl
                                    },
                                    onFinish = {
                                        if (headerOnFinish != null) view.evaluateJavascript(headerOnFinish, null)
                                        loading = false
                                    },
                                    onSafePage = { url ->
                                        SitePolicy.readerDestination(url)?.let { selected = it }
                                        SitePolicy.resumableUrl(url)?.let { safeUrl ->
                                            resumeUrl = safeUrl
                                            if (SitePolicy.isFixedArticle(safeUrl) &&
                                                readerResume.getString(LAST_ARTICLE_URL, null) != safeUrl
                                            ) {
                                                readerResume.edit { putString(LAST_ARTICLE_URL, safeUrl) }
                                            }
                                        }
                                    },
                                    awaitingUpdateLogin = { waitingForLogin },
                                    onHomeReady = { returnToUpdate() },
                                    onFailure = { message -> failure = message; loading = false },
                                    onNotice = { message -> notice = message },
                                    onExternal = { url -> external = url },
                                    onRendererGone = {
                                        if (webView === view) {
                                            webView = null
                                            rendererAvailable = false
                                            pendingDispatch = false
                                        }
                                    },
                                )
                                view.setDownloadListener { url, _, _, mime, _ ->
                                    loading = false
                                    if (SitePolicy.isFixedPdf(url.orEmpty()) && mime == "application/pdf") {
                                        notice = "這個 WebView 無法直接顯示 PDF。請在原文區選「出版者 PDF」，由瀏覽器開啟同一版次。"
                                    } else {
                                        notice = "這個連結需要下載檔案。請回到原文區開啟出版者版本。"
                                    }
                                }
                                view.loadUrl(retryUrl)
                            }
                        },
                        onRelease = { view ->
                            if (webView === view) webView = null
                            view.destroy()
                        },
                        update = { view ->
                            // INVISIBLE also keeps the website out of the accessibility tree while the error is shown.
                            view.visibility = if (failure == null) View.VISIBLE else View.INVISIBLE
                        },
                    )
                }
                failure?.let { message ->
                    Surface(modifier = Modifier.fillMaxSize()) {
                        Column(
                            modifier = Modifier.padding(24.dp),
                            verticalArrangement = Arrangement.spacedBy(12.dp, Alignment.CenterVertically),
                            horizontalAlignment = Alignment.CenterHorizontally,
                        ) {
                            Text(message, style = MaterialTheme.typography.bodyLarge)
                            Button(onClick = { load(retryUrl) }) { Text("重試連線") }
                        }
                    }
                }
            }
        }
    }
    external?.let { url ->
        AlertDialog(
            onDismissRequest = { external = null },
            title = {
                Text(if (url == SitePolicy.OWNER_SETTINGS) "在系統瀏覽器管理信任裝置" else "在系統瀏覽器開啟原文")
            },
            text = { Text(Uri.parse(url).host.orEmpty()) },
            confirmButton = {
                TextButton(onClick = {
                    external = null
                    if (!launchBrowser(context, url)) notice = "系統沒有可開啟此連結的瀏覽器。"
                }) { Text("開啟瀏覽器") }
            },
            dismissButton = { TextButton(onClick = { external = null }) { Text("留在站內") } },
        )
    }
}

private fun configureSiteWebView(view: WebView) {
    view.importantForAutofill = View.IMPORTANT_FOR_AUTOFILL_NO_EXCLUDE_DESCENDANTS
    with(view.settings) {
        javaScriptEnabled = true
        domStorageEnabled = true
        allowFileAccess = false
        allowContentAccess = false
        mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
        javaScriptCanOpenWindowsAutomatically = false
        setSupportMultipleWindows(false)
        useWideViewPort = true
        userAgentString = "${userAgentString.trimEnd()} KnowledgeHubAndroid/2"
        if (WebViewFeature.isFeatureSupported(WebViewFeature.WEB_AUTHENTICATION)) {
            WebSettingsCompat.setWebAuthenticationSupport(this, WebSettingsCompat.WEB_AUTHENTICATION_SUPPORT_FOR_APP)
        }
    }
    CookieManager.getInstance().apply {
        setAcceptCookie(true)
        setAcceptThirdPartyCookies(view, false)
    }
}

private fun launchBrowser(context: Context, url: String): Boolean {
    val address = SitePolicy.externalNavigation(url, userGesture = true) ?: return false
    val intent = Intent(Intent.ACTION_VIEW, Uri.parse(address)).apply {
        addCategory(Intent.CATEGORY_BROWSABLE)
        selector = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_APP_BROWSER)
    }
    return try {
        context.startActivity(intent)
        true
    } catch (_: ActivityNotFoundException) {
        false
    } catch (_: SecurityException) {
        false
    }
}

private class KnowledgeSiteClient(
    private val onStart: (String) -> Unit,
    private val onFinish: () -> Unit,
    private val onSafePage: (String) -> Unit,
    private val awaitingUpdateLogin: () -> Boolean,
    private val onHomeReady: () -> Unit,
    private val onFailure: (String) -> Unit,
    private val onNotice: (String) -> Unit,
    private val onExternal: (String) -> Unit,
    private val onRendererGone: () -> Unit,
) : WebViewClient() {
    private var navigation = 0
    private var mainFrameHttpError = false
    private var pendingUrl: String? = null
    private var finishedUrl: String? = null
    private var failed = false
    private var gone = false

    override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
        val url = request.url.toString()
        if (SitePolicy.allowsSite(url)) return false
        if (request.isForMainFrame && request.method == "GET" && request.hasGesture() && !request.isRedirect) {
            val external = SitePolicy.externalNavigation(url, userGesture = true)
            if (external == null) onNotice("這個連結無法在閱讀器開啟。") else onExternal(external)
        }
        return true
    }

    override fun shouldInterceptRequest(view: WebView, request: WebResourceRequest): WebResourceResponse? {
        if (SitePolicy.allowsResource(request.url.toString())) return null
        return WebResourceResponse("text/plain", "utf-8", 403, "Blocked", emptyMap(), ByteArrayInputStream(byteArrayOf()))
    }

    override fun onPageStarted(view: WebView, url: String, favicon: android.graphics.Bitmap?) {
        navigation++
        pendingUrl = url
        finishedUrl = null
        failed = false
        mainFrameHttpError = false
        if (SitePolicy.allowsSite(url)) {
            onStart(url)
        } else {
            view.stopLoading()
            fail("這個網址超出 Knowledge Hub 閱讀範圍。")
        }
    }

    override fun onPageFinished(view: WebView, url: String) {
        if (gone || failed || !SitePolicy.allowsSite(url) || view.url != url) return
        onFinish()
        if (mainFrameHttpError) return
        finishedUrl = url
        onSafePage(url)
        if (!awaitingUpdateLogin() || !SitePolicy.isHome(url)) return
        val token = navigation
        view.evaluateJavascript("Boolean(document.querySelector('header.app-header') && document.querySelector('main.workspace'))") { ready ->
            if (ready == "true" && !gone && !failed && !mainFrameHttpError && token == navigation &&
                view.url == url && awaitingUpdateLogin()) {
                onHomeReady()
            }
        }
    }

    override fun doUpdateVisitedHistory(view: WebView, url: String, isReload: Boolean) {
        // Track same-document citation/section jumps without reloading or replaying form data.
        if (!failed && !gone && !mainFrameHttpError && url.substringBefore('#') == finishedUrl?.substringBefore('#') && '#' in url) {
            onSafePage(url)
        }
    }

    override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
        if (!request.isForMainFrame || gone) return
        val message = when (error.errorCode) {
            ERROR_FAILED_SSL_HANDSHAKE -> "網站憑證驗證失敗，請稍後重試。"
            ERROR_HOST_LOOKUP, ERROR_CONNECT, ERROR_TIMEOUT -> "無法連線，請確認網路後重試。"
            else -> "網站頁面載入失敗，請稍後重試。"
        }
        fail(message)
    }

    override fun onReceivedHttpError(view: WebView, request: WebResourceRequest, errorResponse: WebResourceResponse) {
        // The site's unauthenticated response is a real HTTP 401 login page.
        if (!request.isForMainFrame || gone) return
        val url = request.url.toString().substringBefore('#')
        if (pendingUrl != null && url != pendingUrl?.substringBefore('#') && url != view.url?.substringBefore('#')) return
        mainFrameHttpError = true
        if (errorResponse.statusCode >= 500) fail("Knowledge Hub 暫時無法連線，請稍後重試。")
    }

    override fun onReceivedSslError(view: WebView, handler: SslErrorHandler, error: SslError) {
        handler.cancel()
        if (error.url == pendingUrl) fail("網站憑證驗證失敗，請稍後重試。")
    }

    override fun onFormResubmission(view: WebView, dontResend: android.os.Message, resend: android.os.Message) {
        dontResend.sendToTarget()
        fail("這頁需要重新開啟，先到網站確認上一個操作的結果。")
    }

    override fun onRenderProcessGone(view: WebView, detail: RenderProcessGoneDetail): Boolean {
        gone = true
        onRendererGone()
        fail("網站閱讀器中斷，可以重新連線後繼續。")
        return true
    }

    private fun fail(message: String) {
        failed = true
        onFailure(message)
    }
}
