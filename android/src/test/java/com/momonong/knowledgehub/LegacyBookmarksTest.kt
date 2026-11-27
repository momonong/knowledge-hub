package com.momonong.knowledgehub

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import java.io.StringWriter
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
class LegacyBookmarksTest {
    @Test fun exportKeepsOnlySavedSnapshotsIncludingOlderOnesWithoutSiteLinks() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val prefs = context.getSharedPreferences("reader", Context.MODE_PRIVATE)
        val article = JSONObject().put("title", "模型 A < B").put("sitePath", "/knowledge/items/9?version=11#S3")
            .put("sourceUrl", "https://arxiv.org/abs/2611.10001v1")
            .put("lesson", JSONObject().put("example", "原來的導讀"))
        val old = JSONObject().put("title", "較早的收藏").put("sourceUrl", "https://arxiv.org/abs/2611.10002v2")
        prefs.edit().putStringSet("bookmarks", setOf("one", "two", "damaged", "id-only"))
            .putString("bookmark:one", article.toString()).putString("bookmark:two", old.toString())
            .putString("bookmark:damaged", "<script>bad()</script>")
            .putString("issue", "unrelated-cached-paper").putString("unrelated", "unrelated-preference").commit()
        val before = prefs.all
        val html = StringWriter().also { LegacyBookmarks(prefs).writeTo(it) }.toString()
        assertTrue(html.contains("模型 A &lt; B"))
        assertTrue(html.contains("href=\"https://momonong.me/knowledge/items/9?version=11#S3\""))
        assertTrue(html.contains("href=\"https://arxiv.org/abs/2611.10002v2\""))
        assertTrue(html.contains("原來的導讀"))
        assertTrue(html.contains("id-only"))
        assertTrue(html.contains("&lt;script&gt;bad()&lt;/script&gt;"))
        assertFalse(html.contains("<script>"))
        assertFalse(html.contains("unrelated"))
        assertEquals(before, prefs.all)
    }
}
