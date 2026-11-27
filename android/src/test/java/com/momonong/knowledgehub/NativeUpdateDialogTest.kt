package com.momonong.knowledgehub

import android.app.Application
import android.graphics.Bitmap
import androidx.activity.compose.setContent
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.test.ExperimentalTestApi
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.captureToImage
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.junit4.v2.createEmptyComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.waitUntilAtLeastOneExists
import androidx.test.core.app.ActivityScenario
import androidx.test.core.app.ApplicationProvider
import java.io.File
import java.net.HttpURLConnection
import java.net.URI
import java.util.concurrent.atomic.AtomicInteger
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode

@OptIn(ExperimentalTestApi::class)
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28], qualifiers = "zh-rTW-w390dp-h844dp-mdpi")
@GraphicsMode(GraphicsMode.Mode.NATIVE)
class NativeUpdateDialogTest {
    @get:Rule val ui = createEmptyComposeRule()

    private val app = ApplicationProvider.getApplicationContext<Application>()
    private val installed get() = NativeUpdateInstaller(app).installed()

    @Test fun showsCurrentVersionAndClosesTheNativeDialogWithoutStartingAnotherActivity() {
        val installedVersion = installed
        val downloads = AtomicInteger()
        val client = updateClient(installedVersion.versionCode, installedVersion.versionName, downloads = downloads)
        val visible = mutableStateOf(true)

        ActivityScenario.launch(MainActivity::class.java).use { scenario ->
            scenario.onActivity { activity ->
                activity.setContent {
                    ReaderTheme {
                        NativeUpdateDialog(
                            visible = visible.value,
                            onDismiss = { visible.value = false },
                            onLogin = { error("Current version must not request login") },
                            client = client,
                        )
                    }
                }
            }
            val current = "目前安裝的版本 ${installedVersion.versionName} 已是最新版本。"
            ui.waitUntilAtLeastOneExists(hasText(current), timeoutMillis = 5_000)
            ui.onNodeWithText(current).assertIsDisplayed()
            ui.onNodeWithTag("native-update-dialog").assertIsDisplayed()
            ui.onNodeWithText("下載更新").assertDoesNotExist()
            capture("native-update-current")
            ui.onNodeWithText("關閉").performClick()
            ui.onNodeWithTag("native-update-dialog").assertDoesNotExist()
            assertEquals(0, downloads.get())
            scenario.assertNoExternalActivity()
        }
    }

    @Test fun availableReleaseOffersInAppLoginAfterAnUnauthorizedDownload() = checkProtectedDownload(401)

    @Test fun availableReleaseOffersInAppLoginAfterAForbiddenDownload() = checkProtectedDownload(403)

    private fun checkProtectedDownload(httpStatus: Int) {
        val downloads = AtomicInteger()
        val installedVersion = installed
        val versionName = if (installedVersion.versionName == "1.0.4") "1.0.5" else "1.0.4"
        val client = updateClient(installedVersion.versionCode + 1, versionName, httpStatus, downloads)
        val logins = AtomicInteger()
        val visible = mutableStateOf(true)
        ActivityScenario.launch(MainActivity::class.java).use { scenario ->
            scenario.onActivity { activity ->
                activity.setContent {
                    ReaderTheme {
                        NativeUpdateDialog(
                            visible = visible.value,
                            onDismiss = { visible.value = false },
                            onLogin = {
                                logins.incrementAndGet()
                                visible.value = false
                            },
                            client = client,
                        )
                    }
                }
            }
            ui.waitUntilAtLeastOneExists(hasText("下載更新"), timeoutMillis = 5_000)
            ui.onNodeWithText("可更新至 $versionName", substring = true).assertIsDisplayed()
            if (httpStatus == 401) capture("native-update-available")
            ui.onNodeWithText("下載更新").assertIsDisplayed().performClick()
            ui.waitUntilAtLeastOneExists(hasText("在 App 內登入"), timeoutMillis = 5_000)
            ui.onNodeWithText("請在 App 內登入 Knowledge Hub，再回來更新。").assertIsDisplayed()
            if (httpStatus == 401) capture("native-update-login")
            ui.onNodeWithText("在 App 內登入").assertIsDisplayed().performClick()
            assertEquals(1, logins.get())
            assertEquals(1, downloads.get())
            ui.onNodeWithTag("native-update-dialog").assertDoesNotExist()
            scenario.assertNoExternalActivity()
        }
    }

    private fun updateClient(
        versionCode: Long,
        versionName: String,
        downloadStatus: Int = 500,
        downloads: AtomicInteger,
    ): NativeUpdateClient = NativeUpdateClient { url ->
        when (url) {
            "${SitePolicy.HOME}android-release.json" -> FixtureConnection(url, 200, """
                {"applicationId":"com.momonong.knowledgehub","versionCode":$versionCode,"versionName":"$versionName",
                 "sizeBytes":3,"sha256":"ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"}
            """.trimIndent())
            SitePolicy.APK_DOWNLOAD -> {
                downloads.incrementAndGet()
                FixtureConnection(url, downloadStatus, "")
            }
            else -> error("Unexpected fixture URL")
        }
    }

    private fun ActivityScenario<MainActivity>.assertNoExternalActivity() {
        onActivity { activity ->
            assertNull(shadowOf(activity).nextStartedActivity)
            assertNull(shadowOf(app).nextStartedActivity)
        }
    }

    private fun capture(name: String) {
        val directory = System.getProperty("knowledgehub.screenshots") ?: return
        val image = ui.onNodeWithTag("native-update-dialog").captureToImage().asAndroidBitmap()
        File(directory, "$name.png").apply { parentFile?.mkdirs() }.outputStream().use {
            assertTrue(image.compress(Bitmap.CompressFormat.PNG, 100, it))
        }
    }

    private class FixtureConnection(url: String, private val status: Int, private val body: String) :
        HttpURLConnection(URI(url).toURL()) {
        override fun getResponseCode() = status
        override fun getInputStream() = body.byteInputStream()
        override fun connect() = Unit
        override fun disconnect() = Unit
        override fun usingProxy() = false
    }
}
