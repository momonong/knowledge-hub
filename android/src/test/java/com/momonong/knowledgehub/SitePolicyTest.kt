package com.momonong.knowledgehub

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class SitePolicyTest {
    @Test fun onlyBareNavigationPagesCanRestoreTheWebSections() {
        for (section in listOf("reading", "library", "manage")) {
            val page = SitePolicy.HOME + section
            assertEquals(page, SitePolicy.resumableUrl(page))
            assertNull(SitePolicy.resumableUrl(page + "?q=private"))
            assertNull(SitePolicy.resumableUrl(page + "#form-draft"))
        }
        assertNull(SitePolicy.resumableUrl(SitePolicy.HOME + "access/login"))
        assertNull(SitePolicy.resumableUrl(SitePolicy.HOME + "sync"))
    }

    @Test fun articleRestorationKeepsTheExactVersionAndAnchor() {
        val fixed = "https://momonong.me/knowledge/items/12?version=34#kh-visual-S3"
        assertEquals(fixed, SitePolicy.resumableUrl(fixed))
        for (path in listOf("/items/12?version=34", "/knowledge/items/12", "/knowledge/items/12?version=0",
            "/knowledge/items/12?version=34&private=secret", "/knowledge/export.json")) {
            assertNull(path, SitePolicy.resumableUrl(SitePolicy.ORIGIN + path))
        }
    }

    @Test fun webViewKeepsLoginAndReadingResourcesOnTheAllowedOrigin() {
        for (path in listOf("/knowledge", "/knowledge/", "/knowledge/access/session", "/knowledge/access/login",
            "/knowledge/static/reader.js", "/knowledge/items/1?version=2#S3",
            "/knowledge/items/1/visuals/pdf/2?version=2", "/knowledge/items/1/followup?version=2")) {
            assertTrue(path, SitePolicy.allowsSite("https://momonong.me$path"))
        }
        assertTrue(SitePolicy.allowsResource("https://momonong.me/sites/_icons/v1/knowledge.svg"))
        for (url in listOf("https://momonong.me/", "https://momonong.me/home-api/session", "https://momonong.me/knowledge-other/",
            "https://momonong.me/knowledge/export.json?anything=1", "https://momonong.me/knowledge/%65xport.json",
            "https://momonong.me/knowledge/../quota/", "https://momonong.me/knowledge/%2e%2e/quota/",
            "https://momonong.me/knowledge/%2faccess/session", "https://momonong.me:444/knowledge/",
            "http://momonong.me/knowledge/", "https://momonong.me.attacker.example/knowledge/",
            "https://momonong.me@attacker.example/knowledge/", "https://attacker.example@momonong.me/knowledge/",
            "https://momonong.me\\@attacker.example/knowledge/", "file:///data/local/tmp/paper", "content://org.example/article",
            "intent://arxiv.org/#Intent;scheme=https;end", "javascript:alert(1)", "https://arxiv.org/abs/2611.27734")) {
            assertFalse(url, SitePolicy.allowsResource(url))
        }
    }

    @Test fun onlyTheFixedOwnerSettingsLinkCanLeaveTheWebViewForOnSiteManagement() {
        val settings = "https://momonong.me/knowledge/settings/"
        assertFalse(SitePolicy.allowsResource(settings))
        assertEquals(settings, SitePolicy.externalNavigation(settings, userGesture = true))
        assertNull(SitePolicy.externalNavigation(settings, userGesture = false))
        for (url in listOf(settings.removeSuffix("/"), settings + "?x=1", settings + "#devices",
            settings + "child/", "https://momonong.me/knowledge//settings/", "https://momonong.me/home-api/session")) {
            assertFalse(url, SitePolicy.allowsResource(url))
            assertNull(url, SitePolicy.externalNavigation(url, userGesture = true))
        }
    }

    @Test fun externalNavigationRequiresAUserGestureAndAPlainHttpsAddress() {
        val arxiv = "https://arxiv.org/abs/2611.27734v1"
        assertEquals(arxiv, SitePolicy.externalNavigation(arxiv, userGesture = true))
        assertNull(SitePolicy.externalNavigation(arxiv, userGesture = false))
        for (url in listOf("https://momonong.me/knowledge/export.json", "https://momonong.me/other/",
            "http://arxiv.org/abs/2611.27734", "https://private@arxiv.org/abs/2611.27734", "https://arxiv.org:8080/",
            "https://arxiv.org\\path", "https://arxiv.org:bad/", "https://arxiv.org:/", "mailto:reader@example.org",
            "intent://arxiv.org/#Intent;scheme=https;end", "data:text/html,test", "file:///system/test")) {
            assertNull(url, SitePolicy.externalNavigation(url, userGesture = true))
        }
    }
}
