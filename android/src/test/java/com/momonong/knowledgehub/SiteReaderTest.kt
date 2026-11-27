package com.momonong.knowledgehub

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.net.Uri
import android.view.View
import android.view.ViewGroup
import android.webkit.CookieManager
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.width
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Rect
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertHeightIsAtLeast
import androidx.compose.ui.test.assertIsSelected
import androidx.compose.ui.test.assertIsNotSelected
import androidx.compose.ui.test.assertWidthIsAtLeast
import androidx.compose.ui.test.captureToImage
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.junit4.v2.createEmptyComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onRoot
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.dp
import androidx.test.core.app.ActivityScenario
import java.io.ByteArrayOutputStream
import java.io.File
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode
import kotlin.math.roundToInt

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
class SiteReaderTest {
    @get:Rule val compose = createAndroidComposeRule<MainActivity>()

    @Test
    fun theWebsiteIsTheOnlyReaderOnLaunchAndAfterANetworkFailure() {
        fun assertOnlyWebReader() {
            compose.onNodeWithTag("brief-list").assertDoesNotExist()
            compose.onNodeWithTag("open-offline").assertDoesNotExist()
            compose.onNodeWithTag("open-site").assertDoesNotExist()
            compose.onNodeWithText("離線閱讀").assertDoesNotExist()
            compose.onNodeWithText("更新應用程式").assertDoesNotExist()
            compose.onNodeWithContentDescription("更新應用程式").assertIsDisplayed()
                .assertWidthIsAtLeast(48.dp).assertHeightIsAtLeast(48.dp)
        }
        assertOnlyWebReader()
        val web = compose.runOnIdle {
            checkNotNull(compose.activity.window.decorView.findWebView()).also {
                assertEquals("https://momonong.me/knowledge/", shadowOf(it).lastLoadedUrl)
                it.webViewClient.onPageStarted(it, SitePolicy.HOME, null)
                it.webViewClient.onReceivedHttpError(it, homeRequest,
                    WebResourceResponse("text/plain", "utf-8", 503, "Unavailable", emptyMap(), null))
            }
        }
        assertOnlyWebReader()
        compose.onNodeWithText("重試連線").performClick()
        compose.runOnIdle {
            assertTrue(web === compose.activity.window.decorView.findWebView())
            assertEquals(SitePolicy.HOME, shadowOf(web).lastLoadedUrl)
        }
    }

    @Test
    @Config(qualifiers = "zh-rTW-w390dp-h780dp-mdpi")
    @GraphicsMode(GraphicsMode.Mode.NATIVE)
    fun coveredScreenUsesThreeNamedNativeDestinationsAndAnIconUpdateAction() {
        compose.onNodeWithTag("reader-navigation-bar").assertIsDisplayed()
        compose.onNodeWithTag("reader-navigation-rail").assertDoesNotExist()
        for (title in listOf("今日", "接續", "知識庫")) {
            compose.onNodeWithText(title).assertIsDisplayed().assertWidthIsAtLeast(48.dp).assertHeightIsAtLeast(48.dp)
        }
        compose.onNodeWithContentDescription("更新應用程式").assertIsDisplayed()
            .assertWidthIsAtLeast(48.dp).assertHeightIsAtLeast(48.dp)
        compose.onNodeWithText("更新應用程式").assertDoesNotExist()
        captureReader("reader-cover", "reader-navigation-bar")
    }

    @Test
    @Config(qualifiers = "zh-rTW-w840dp-h700dp-mdpi")
    @GraphicsMode(GraphicsMode.Mode.NATIVE)
    fun unfoldedScreenUsesANativeRail() {
        compose.onNodeWithTag("reader-navigation-rail").assertIsDisplayed()
        compose.onNodeWithTag("reader-navigation-bar").assertDoesNotExist()
        compose.onNodeWithText("接續").assertIsDisplayed().assertHeightIsAtLeast(48.dp)
        compose.onNodeWithContentDescription("更新應用程式").assertIsDisplayed().assertHeightIsAtLeast(48.dp)
        captureReader("reader-expanded", "reader-navigation-rail")
    }

    @Test
    @Config(qualifiers = "zh-rTW-w680dp-h330dp-mdpi")
    @GraphicsMode(GraphicsMode.Mode.NATIVE)
    fun shortLandscapeKeepsNavigationOffTheBottom() {
        compose.onNodeWithTag("reader-navigation-rail").assertIsDisplayed()
        compose.onNodeWithText("知識庫").assertIsDisplayed()
        compose.onNodeWithContentDescription("更新應用程式").assertIsDisplayed()
        captureReader("reader-landscape", "reader-navigation-rail")
    }

    @Test
    @Config(qualifiers = "zh-rTW-w320dp-h700dp-night-mdpi")
    @GraphicsMode(GraphicsMode.Mode.NATIVE)
    fun narrowDarkModeKeepsAllNavigationActionsAtLargeFontScale() {
        compose.runOnIdle {
            compose.activity.setContent {
                val density = LocalDensity.current.density
                CompositionLocalProvider(LocalDensity provides Density(density, fontScale = 2f)) {
                    ReaderTheme {
                        SiteReader(onBackAtRoot = {}, loginForUpdate = false, onReturnToUpdate = {}, onUpdate = {})
                    }
                }
            }
        }
        for (title in listOf("今日", "接續", "知識庫")) {
            compose.onNodeWithText(title).assertIsDisplayed().assertWidthIsAtLeast(48.dp).assertHeightIsAtLeast(48.dp)
        }
        compose.onNodeWithContentDescription("更新應用程式").assertIsDisplayed().assertHeightIsAtLeast(48.dp)
        captureReader("reader-large-font-dark", "reader-navigation-bar")
    }

    @Test
    @Config(qualifiers = "zh-rTW-w840dp-h700dp-mdpi")
    fun resizingTheSameReaderMovesNavigationWithoutReloadingTheArticle() {
        var width by mutableStateOf(390.dp)
        compose.runOnIdle {
            compose.activity.setContent {
                ReaderTheme {
                    Box(Modifier.width(width)) {
                        SiteReader(onBackAtRoot = {}, loginForUpdate = false, onReturnToUpdate = {}, onUpdate = {})
                    }
                }
            }
        }
        val article = SitePolicy.HOME + "items/12?version=34#outline"
        val web = compose.runOnIdle {
            checkNotNull(compose.activity.window.decorView.findWebView()).also {
                it.loadUrl(article)
                it.webViewClient.onPageStarted(it, article, null)
                it.webViewClient.onPageFinished(it, article)
            }
        }
        compose.onNodeWithTag("reader-navigation-bar").assertIsDisplayed()
        compose.runOnIdle { width = 840.dp }
        compose.onNodeWithTag("reader-navigation-rail").assertIsDisplayed()
        compose.runOnIdle {
            assertTrue(web === compose.activity.window.decorView.findWebView())
            assertEquals(article, shadowOf(web).lastLoadedUrl)
            assertEquals(0, shadowOf(web).reloadInvocations)
        }
    }

    @Test
    fun nativeTabsUseSafePageNavigationAndFollowSuccessfulSiteHistory() {
        val home = SitePolicy.HOME
        val library = home + "library"
        val reading = home + "reading"
        val web = compose.runOnIdle {
            checkNotNull(compose.activity.window.decorView.findWebView()).also {
                assertTrue(it.settings.userAgentString.split(Regex("\\s+")).contains("KnowledgeHubAndroid/2"))
                it.webViewClient.onPageStarted(it, home, null)
                it.webViewClient.onPageFinished(it, home)
            }
        }
        compose.onNodeWithText("今日").assertIsSelected().performClick()
        compose.runOnIdle { assertNull(shadowOf(web).lastEvaluatedJavascript) }
        compose.onNodeWithText("接續").performClick().assertIsNotSelected()
        compose.onNodeWithText("知識庫").performClick().assertIsNotSelected()
        compose.runOnIdle {
            assertEquals(home, shadowOf(web).lastLoadedUrl)
            assertTrue(shadowOf(web).lastEvaluatedJavascript.endsWith(
                "(${JSONObject.quote(reading)}, ${JSONObject.quote(SitePolicy.ORIGIN)})",
            ))
            checkNotNull(shadowOf(web).lastEvaluatedJavascriptCallback).onReceiveValue("true")
            web.loadUrl(library)
            web.webViewClient.onPageStarted(web, library, null)
            web.webViewClient.onPageFinished(web, library)
        }
        compose.onNodeWithText("知識庫").assertIsSelected()
        compose.runOnIdle {
            val article = home + "items/12?version=34"
            web.loadUrl(article)
            web.webViewClient.onPageStarted(web, article, null)
            web.webViewClient.onPageFinished(web, article)
            web.loadUrl(reading)
            web.webViewClient.onPageStarted(web, reading, null)
            web.webViewClient.onReceivedHttpError(
                web, mainFrameRequest(reading),
                WebResourceResponse("text/html", "utf-8", 401, "Unauthorized", emptyMap(), null),
            )
            web.webViewClient.onPageFinished(web, reading)
        }
        compose.onNodeWithText("知識庫").assertIsSelected()
        compose.runOnIdle {
            web.webViewClient.onPageStarted(web, reading, null)
            web.webViewClient.onPageFinished(web, reading)
            assertTrue(web === compose.activity.window.decorView.findWebView())
        }
        compose.onNodeWithText("接續").assertIsSelected()
    }

    @Test
    fun olderWebViewStillOpensThePasswordPageWithPrivateAutofillSettings() {
        compose.runOnIdle {
            val web = checkNotNull(compose.activity.window.decorView.findWebView())
            val cookies = CookieManager.getInstance()
            assertTrue(cookies.acceptCookie())
            assertFalse(cookies.acceptThirdPartyCookies(web))
            assertEquals(View.IMPORTANT_FOR_AUTOFILL_NO_EXCLUDE_DESCENDANTS, web.importantForAutofill)
            web.webViewClient.onPageStarted(web, SitePolicy.HOME, null)
            web.webViewClient.onReceivedHttpError(
                web, homeRequest, WebResourceResponse("text/html", "utf-8", 401, "Unauthorized", emptyMap(), null),
            )
            web.webViewClient.onPageFinished(web, SitePolicy.HOME)
            assertEquals(SitePolicy.HOME, shadowOf(web).lastLoadedUrl)
            assertEquals(View.VISIBLE, web.visibility)
        }
        compose.onNodeWithText("重試連線").assertDoesNotExist()
    }

    @Test
    fun ownerSettingsOpensInTheBrowserAfterAnExplicitClickAndConfirmation() {
        val settings = "https://momonong.me/knowledge/settings/"
        val gesture = object : WebResourceRequest by homeRequest {
            override fun getUrl(): Uri = Uri.parse(settings)
            override fun hasGesture() = true
        }
        compose.runOnIdle {
            val web = checkNotNull(compose.activity.window.decorView.findWebView())
            for (request in listOf(
                object : WebResourceRequest by gesture { override fun hasGesture() = false },
                object : WebResourceRequest by gesture { override fun isRedirect() = true },
                object : WebResourceRequest by gesture { override fun isForMainFrame() = false },
                object : WebResourceRequest by gesture { override fun getMethod() = "POST" },
            )) {
                assertTrue(web.webViewClient.shouldOverrideUrlLoading(web, request))
            }
        }
        compose.onNodeWithText("在系統瀏覽器管理信任裝置").assertDoesNotExist()
        compose.runOnIdle {
            val web = checkNotNull(compose.activity.window.decorView.findWebView())
            assertTrue(web.webViewClient.shouldOverrideUrlLoading(web, gesture))
            assertNull(shadowOf(compose.activity).nextStartedActivity)
        }
        compose.onNodeWithText("在系統瀏覽器管理信任裝置").assertIsDisplayed()
        compose.onNodeWithText("開啟瀏覽器").performClick()
        compose.runOnIdle {
            val intent = checkNotNull(shadowOf(compose.activity).nextStartedActivity)
            assertEquals(Intent.ACTION_VIEW, intent.action)
            assertEquals(settings, intent.dataString)
            val web = checkNotNull(compose.activity.window.decorView.findWebView())
            assertEquals(SitePolicy.HOME, shadowOf(web).lastLoadedUrl)
        }
    }

    @Test
    fun closingTheNativeUpdateDialogKeepsTheSameWebView() {
        val original = compose.runOnIdle { compose.activity.window.decorView.findWebView() }
        compose.onNodeWithContentDescription("更新應用程式").performClick()
        compose.onNodeWithTag("native-update-dialog").assertIsDisplayed()
        compose.onNodeWithText("關閉").performClick()
        compose.runOnIdle {
            assertTrue(original === compose.activity.window.decorView.findWebView())
            assertNull(shadowOf(compose.activity).nextStartedActivity)
        }
    }

    @Test
    fun recreatingTheActivityResumesOnlyTheLastSafePage() {
        val library = SitePolicy.HOME + "library"
        compose.runOnIdle {
            val web = checkNotNull(compose.activity.window.decorView.findWebView())
            for (url in listOf(library, SitePolicy.HOME + "access/login")) {
                web.loadUrl(url)
                web.webViewClient.onPageStarted(web, url, null)
                web.webViewClient.onPageFinished(web, url)
            }
            assertNull(shadowOf(web).lastEvaluatedJavascript)
        }
        compose.activityRule.scenario.recreate()
        compose.runOnIdle {
            val restored = checkNotNull(compose.activity.window.decorView.findWebView())
            assertEquals(library, shadowOf(restored).lastLoadedUrl)
            assertEquals(0, shadowOf(restored).reloadInvocations)
        }
        compose.onNodeWithTag("open-offline").assertDoesNotExist()
    }

    @Test
    fun systemBackFromAFailedArticleUsesTheWebHistoryAndExitsAtTheRoot() {
        val activity = compose.activity
        val web = compose.runOnIdle {
            val view = checkNotNull(activity.window.decorView.findWebView())
            val article = SitePolicy.HOME + "items/12?version=34"
            view.loadUrl(article)
            view.webViewClient.onPageStarted(view, article, null)
            view.webViewClient.onReceivedHttpError(view, object : WebResourceRequest by homeRequest {
                override fun getUrl(): Uri = Uri.parse(article)
            }, WebResourceResponse("text/plain", "utf-8", 503, "Unavailable", emptyMap(), null))
            view
        }
        compose.onNodeWithText("重試連線").assertIsDisplayed()
        compose.runOnIdle {
            shadowOf(web).pushEntryToHistory(SitePolicy.HOME)
            shadowOf(web).pushEntryToHistory(SitePolicy.HOME + "items/12?version=34")
            activity.onBackPressedDispatcher.onBackPressed()
            assertEquals(1, shadowOf(web).goBackInvocations)
        }
        compose.onNodeWithText("重試連線").assertDoesNotExist()
        compose.runOnIdle {
            activity.onBackPressedDispatcher.onBackPressed()
            assertTrue(activity.isFinishing)
        }
    }

    @Test
    fun onlyTheSignedInWebsiteCanReturnToTheUpdateDialog() {
        val login = mutableStateOf(false)
        var ready = 0
        val article = SitePolicy.HOME + "items/12?version=34"
        compose.runOnIdle {
            compose.activity.setContent {
                ReaderTheme {
                    SiteReader(onBackAtRoot = {}, loginForUpdate = login.value, onReturnToUpdate = { ready++ }, onUpdate = {})
                }
            }
        }
        val web = compose.runOnIdle {
            checkNotNull(compose.activity.window.decorView.findWebView()).also {
                it.loadUrl(article)
                it.webViewClient.onPageStarted(it, article, null)
                it.webViewClient.onPageFinished(it, article)
                login.value = true
            }
        }
        compose.runOnIdle {
            assertTrue(web === compose.activity.window.decorView.findWebView())
            assertEquals(SitePolicy.HOME, shadowOf(web).lastLoadedUrl)
            web.webViewClient.onPageStarted(web, SitePolicy.HOME, null)
            web.webViewClient.onReceivedHttpError(web, homeRequest,
                WebResourceResponse("text/html", "utf-8", 401, "Unauthorized", emptyMap(), null))
            web.webViewClient.onPageFinished(web, SitePolicy.HOME)
            assertNull(shadowOf(web).lastEvaluatedJavascriptCallback)
            web.webViewClient.onPageStarted(web, SitePolicy.HOME, null)
            web.webViewClient.onPageFinished(web, SitePolicy.HOME)
            val stale = checkNotNull(shadowOf(web).lastEvaluatedJavascriptCallback)
            web.webViewClient.onPageStarted(web, SitePolicy.HOME, null)
            stale.onReceiveValue("true")
            assertEquals(0, ready)
            web.webViewClient.onPageFinished(web, SitePolicy.HOME)
            checkNotNull(shadowOf(web).lastEvaluatedJavascriptCallback).onReceiveValue("false")
            assertEquals(0, ready)
            web.webViewClient.onPageFinished(web, SitePolicy.HOME)
            checkNotNull(shadowOf(web).lastEvaluatedJavascriptCallback).onReceiveValue("true")
            assertEquals(1, ready)
        }
    }

    @Test
    @Config(qualifiers = "zh-rTW-w840dp-h700dp-mdpi")
    fun anUpgradeWithDeviceBookmarksStillOpensTheSiteAndOffersOnlyAFileExport() {
        compose.onNodeWithText("匯出舊收藏").assertDoesNotExist()
        val saved = """{"id":"model-1","title":"原來收藏的文章","sourceUrl":"https://arxiv.org/abs/2611.00001"}"""
        val prefs = compose.activity.getSharedPreferences("reader", Context.MODE_PRIVATE)
        prefs.edit().putStringSet("bookmarks", setOf("model-1")).putString("bookmark:model-1", saved)
            .putString("lastPaper", "model-1").commit()
        compose.activityRule.scenario.recreate()
        compose.onNodeWithText("匯出舊收藏").assertDoesNotExist()
        compose.onNodeWithContentDescription("更新應用程式").performClick()
        compose.waitUntil(5_000) { compose.onAllNodesWithText("匯出舊收藏").fetchSemanticsNodes().isNotEmpty() }
        compose.onNodeWithTag("open-offline").assertDoesNotExist()
        compose.onNodeWithText("匯出舊收藏").performClick()
        val destination = ByteArrayOutputStream()
        compose.runOnIdle {
            val activity = compose.activity
            val intent = checkNotNull(shadowOf(activity).peekNextStartedActivityForResult()).intent
            assertEquals(Intent.ACTION_CREATE_DOCUMENT, intent.action)
            assertEquals("text/html", intent.type)
            val uri = Uri.parse("content://local-test/selected-bookmarks.html")
            shadowOf(activity.contentResolver).registerOutputStreamSupplier(uri) { destination }
            shadowOf(activity).receiveResult(intent, Activity.RESULT_OK, Intent().setData(uri))
            assertEquals(SitePolicy.HOME, shadowOf(checkNotNull(activity.window.decorView.findWebView())).lastLoadedUrl)
        }
        compose.waitUntil(5_000) { destination.toString("UTF-8").contains("原來收藏的文章") }
        assertEquals(saved, prefs.getString("bookmark:model-1", null))
    }

    private val homeRequest = mainFrameRequest(SitePolicy.HOME)

    private fun captureReader(name: String, navigationTag: String) {
        compose.runOnIdle {
            checkNotNull(compose.activity.window.decorView.findWebView()).also {
                it.webViewClient.onPageStarted(it, SitePolicy.HOME, null)
                it.webViewClient.onPageFinished(it, SitePolicy.HOME)
            }
        }
        val root = compose.onRoot()
        val rootBounds = root.fetchSemanticsNode().boundsInRoot
        val webBounds = compose.onNodeWithTag("reader-web-view").fetchSemanticsNode().boundsInRoot
        val navBounds = compose.onNodeWithTag(navigationTag).fetchSemanticsNode().boundsInRoot
        assertTrue(webBounds.width > 0 && webBounds.height > 0)
        assertFalse(webBounds.overlaps(navBounds))
        if (navigationTag == "reader-navigation-bar") {
            assertEquals(rootBounds.width.roundToInt(), webBounds.width.roundToInt())
            assertEquals(rootBounds.height.roundToInt(), (webBounds.height + navBounds.height).roundToInt())
        } else {
            assertEquals(rootBounds.height.roundToInt(), webBounds.height.roundToInt())
            assertEquals(rootBounds.width.roundToInt(), (webBounds.width + navBounds.width).roundToInt())
        }
        val directory = System.getProperty("knowledgehub.screenshots") ?: return
        val image = root.captureToImage().asAndroidBitmap()
        File(directory, "$name.png").apply { parentFile?.mkdirs() }.outputStream().use {
            assertTrue(image.compress(Bitmap.CompressFormat.PNG, 100, it))
        }
        fun pixelBounds(rect: Rect) = JSONObject()
            .put("x", rect.left.roundToInt()).put("y", rect.top.roundToInt())
            .put("width", rect.width.roundToInt()).put("height", rect.height.roundToInt())
        File(directory, "$name.json").writeText(
            JSONObject().put("width", image.width).put("height", image.height)
                .put("webView", pixelBounds(webBounds)).put("navigation", pixelBounds(navBounds)).toString(2) + "\n",
        )
    }
}

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
class SiteReaderResumeTest {
    @get:Rule val compose = createEmptyComposeRule()

    @Test
    fun freshLaunchReturnsToTheLastReadableArticleAndItsSection() {
        val article = SitePolicy.HOME + "items/12?version=34"
        val section = article + "#methods"
        val unauthorized = SitePolicy.HOME + "items/34?version=12"
        ActivityScenario.launch(MainActivity::class.java).use { session ->
            compose.waitForIdle()
            session.onActivity { activity ->
                val web = checkNotNull(activity.window.decorView.findWebView())
                assertEquals(SitePolicy.HOME, shadowOf(web).lastLoadedUrl)
                web.loadUrl(article)
                web.webViewClient.onPageStarted(web, article, null)
                web.webViewClient.onPageFinished(web, article)
                web.webViewClient.doUpdateVisitedHistory(web, section, false)
                web.loadUrl(unauthorized)
                web.webViewClient.onPageStarted(web, unauthorized, null)
                web.webViewClient.doUpdateVisitedHistory(web, unauthorized + "#outline", false)
                web.webViewClient.onReceivedHttpError(
                    web, mainFrameRequest(unauthorized),
                    WebResourceResponse("text/html", "utf-8", 401, "Unauthorized", emptyMap(), null),
                )
                web.webViewClient.onPageFinished(web, unauthorized)
                for (path in listOf("access/login", "items/56?version=78&draft=synthetic", "library")) {
                    val url = SitePolicy.HOME + path
                    web.loadUrl(url)
                    web.webViewClient.onPageStarted(web, url, null)
                    web.webViewClient.onPageFinished(web, url)
                }
            }
        }
        ActivityScenario.launch(MainActivity::class.java).use { session ->
            compose.waitForIdle()
            session.onActivity { activity ->
                val web = checkNotNull(activity.window.decorView.findWebView())
                assertEquals(section, shadowOf(web).lastLoadedUrl)
                assertEquals(0, shadowOf(web).reloadInvocations)
            }
        }
    }
}

private fun mainFrameRequest(url: String): WebResourceRequest = object : WebResourceRequest {
    override fun getUrl(): Uri = Uri.parse(url)
    override fun isForMainFrame() = true
    override fun isRedirect() = false
    override fun hasGesture() = false
    override fun getMethod() = "GET"
    override fun getRequestHeaders() = emptyMap<String, String>()
}

private fun View.findWebView(): WebView? {
    if (this is WebView) return this
    if (this !is ViewGroup) return null
    return (0 until childCount).firstNotNullOfOrNull { getChildAt(it).findWebView() }
}
