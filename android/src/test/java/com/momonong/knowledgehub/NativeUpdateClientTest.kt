package com.momonong.knowledgehub

import java.io.ByteArrayInputStream
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import kotlinx.coroutines.CancellationException
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
class NativeUpdateClientTest {
    @get:Rule val temporary = TemporaryFolder()

    private val release = NativeUpdateRelease(5, "1.0.4", 3,
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
    private val metadata = """
        {"applicationId":"com.momonong.knowledgehub","versionCode":5,"versionName":"1.0.4",
         "sizeBytes":3,"sha256":"${release.sha256}"}
    """.trimIndent()

    @Test fun comparesInstalledReleaseByVersionCode() {
        val client = NativeUpdateClient { FixtureConnection(it, 200, metadata) }
        val published = client.check()
        assertEquals(release, published)
        assertEquals(NativeUpdateAvailability.AVAILABLE, published.availability(4))
        assertEquals(NativeUpdateAvailability.CURRENT, published.availability(5))
        assertEquals(NativeUpdateAvailability.INSTALLED_NEWER, published.availability(6))
    }

    @Test fun requiresBoundedMetadataWithTheExpectedApplicationAndTypes() {
        listOf(
            metadata.replace("com.momonong.knowledgehub", "com.example.other"),
            metadata.replace("\"versionCode\":5", "\"versionCode\":\"5\""),
            metadata.replace("\"versionCode\":5", "\"versionCode\":5.0"),
            metadata.replace("\"sizeBytes\":3", "\"sizeBytes\":0"),
            metadata.replace("\"sizeBytes\":3", "\"sizeBytes\":1099511627776"),
            metadata.replace(release.sha256, release.sha256.uppercase()),
            metadata.replace("\"versionCode\":5", "\"versionCode\":5,\"versionCode\":6"),
            "$metadata{}",
            " ".repeat(17 * 1024) + metadata,
        ).forEach { invalid ->
            val client = NativeUpdateClient { FixtureConnection(it, 200, invalid) }
            assertThrows(NativeUpdateFailure::class.java) { client.check() }
        }
    }

    @Test fun onlyApkReceivesTheCookieAndReportsBytes() {
        val requests = mutableListOf<FixtureConnection>()
        val client = NativeUpdateClient { url ->
            FixtureConnection(url, 200, if (url == SitePolicy.APK_DOWNLOAD) "abc" else metadata).also(requests::add)
        }
        val candidate = temporary.newFile()
        val progress = mutableListOf<Long>()
        client.download(client.check(), "test-session=value", candidate, progress::add) {}

        assertEquals(listOf("${SitePolicy.HOME}android-release.json", SitePolicy.APK_DOWNLOAD), requests.map { it.url.toString() })
        assertNull(requests[0].getRequestProperty("Cookie"))
        assertEquals("test-session=value", requests[1].getRequestProperty("Cookie"))
        assertTrue(requests.all { !it.instanceFollowRedirects && !it.useCaches && it.disconnected })
        assertEquals(listOf(3L), progress)
        assertEquals("abc", candidate.readText())
    }

    @Test fun requestsLoginForProtectedApkAndNeverFollowsRedirects() {
        for (code in listOf(401, 403, 302, 307)) {
            val request = FixtureConnection(SitePolicy.APK_DOWNLOAD, code, "")
            var connections = 0
            val client = NativeUpdateClient {
                connections++
                request
            }
            val candidate = temporary.newFile()
            val failure = assertThrows(NativeUpdateFailure::class.java) {
                client.download(release, "test-session=value", candidate, {}) {}
            }
            assertEquals(code in listOf(401, 403), failure.requiresLogin)
            assertEquals(1, connections)
            assertFalse(request.instanceFollowRedirects)
            assertTrue(request.disconnected)
            assertFalse(candidate.exists())
        }
    }

    @Test fun deletesFailedPartialDownloadsAndAllowsRetry() {
        for (fixture in listOf(
            FixtureConnection(SitePolicy.APK_DOWNLOAD, 200, "ab"),
            FixtureConnection(SitePolicy.APK_DOWNLOAD, 200, "abcd"),
            FixtureConnection(SitePolicy.APK_DOWNLOAD, 200, "def"),
            FixtureConnection(SitePolicy.APK_DOWNLOAD, 200, "abc", 4),
        )) {
            val candidate = temporary.newFile()
            assertThrows(NativeUpdateFailure::class.java) {
                NativeUpdateClient { fixture }.download(release, null, candidate, {}) {}
            }
            assertFalse(candidate.exists())
        }
        val candidate = temporary.newFile()
        assertThrows(CancellationException::class.java) {
            NativeUpdateClient { FixtureConnection(it, 200, "abc") }
                .download(release, null, candidate, {}, { throw CancellationException() })
        }
        assertFalse(candidate.exists())
        NativeUpdateClient { FixtureConnection(it, 200, "abc") }.download(release, null, candidate, {}) {}
        assertEquals("abc", candidate.readText())
    }

    private class FixtureConnection(url: String, val code: Int, val body: String, val length: Long = -1) :
        HttpURLConnection(URL(url)) {
        var disconnected = false
        override fun getResponseCode(): Int = code
        override fun getContentLengthLong(): Long = length
        override fun getInputStream() = ByteArrayInputStream(body.toByteArray())
        override fun getHeaderField(name: String): String? =
            if (name == "Location") "https://example.invalid/update.apk" else null
        override fun connect() = Unit
        override fun disconnect() { disconnected = true }
        override fun usingProxy() = false
    }
}
