package com.momonong.knowledgehub

import java.net.URI

internal object SitePolicy {
    const val ORIGIN = "https://momonong.me"
    const val HOME = "$ORIGIN/knowledge/"
    const val MANAGE = "${HOME}manage"
    const val APK_DOWNLOAD = "${HOME}knowledge-hub.apk"
    const val OWNER_SETTINGS = "${HOME}settings/"
    private const val SITE_HOST = "momonong.me"
    private val itemPath = Regex("/knowledge/items/[1-9][0-9]{0,18}")
    private val itemVersion = Regex("version=[1-9][0-9]{0,18}")
    private val anchor = Regex("(?:[A-Za-z0-9_.:~!-]|%[0-9A-Fa-f]{2})+")
    private val siteIcons = Regex("/sites/_icons/v1/knowledge(?:\\.(?:ico|svg)|-180\\.png)")
    private val navigationPages = setOf(HOME + "reading", HOME + "library", MANAGE)

    fun allowsSite(url: String): Boolean {
        val uri = httpsUri(url) ?: return false
        val path = uri.rawPath
        val section = path.removePrefix("/knowledge").trimStart('/').substringBefore('/')
        return uri.host.equals(SITE_HOST, ignoreCase = true) &&
            (path == "/knowledge" || path.startsWith("/knowledge/")) &&
            section != "settings" &&
            '%' !in path && path.split('/').none { it == "." || it == ".." || it == "export.json" }
    }

    fun allowsResource(url: String): Boolean {
        if (allowsSite(url)) return true
        val uri = httpsUri(url) ?: return false
        return uri.host.equals(SITE_HOST, ignoreCase = true) && siteIcons.matches(uri.rawPath)
    }

    fun isHome(url: String): Boolean = allowsSite(url) && URI(url).rawPath == "/knowledge/"

    fun readerDestination(url: String): ReaderDestination? {
        if (!allowsSite(url)) return null
        val path = URI(url).rawPath
        return when {
            path == "/knowledge" || path == "/knowledge/" -> ReaderDestination.TODAY
            path == "/knowledge/reading" -> ReaderDestination.CONTINUE
            path == "/knowledge/library" || path.startsWith("/knowledge/library/") -> ReaderDestination.LIBRARY
            else -> null
        }
    }

    fun resumableUrl(url: String): String? = when {
        isHome(url) -> HOME
        url in navigationPages -> url
        isFixedArticle(url) -> url
        else -> null
    }

    fun isFixedPdf(url: String): Boolean {
        if (!allowsSite(url)) return false
        val uri = URI(url)
        return uri.rawPath.endsWith("/pdf") && itemPath.matches(uri.rawPath.removeSuffix("/pdf")) &&
            itemVersion.matches(uri.rawQuery.orEmpty())
    }

    fun externalNavigation(url: String, userGesture: Boolean): String? {
        if (!userGesture) return null
        if (url == OWNER_SETTINGS) return url
        val uri = httpsUri(url) ?: return null
        return url.takeUnless { uri.host.equals(SITE_HOST, ignoreCase = true) }
    }

    internal fun isFixedArticle(url: String): Boolean {
        if (!allowsSite(url)) return false
        val uri = URI(url)
        val fragment = uri.rawFragment
        return itemPath.matches(uri.rawPath) && itemVersion.matches(uri.rawQuery.orEmpty()) &&
            (fragment == null || (fragment.length <= 240 && anchor.matches(fragment) && uri.fragment.none { it.isWhitespace() || it.isISOControl() }))
    }

    private fun httpsUri(url: String): URI? {
        if (url.length !in 1..4096 || url.any { it.isWhitespace() || it.isISOControl() || it == '\\' }) return null
        val uri = try { URI(url) } catch (_: java.net.URISyntaxException) { return null }
        val host = uri.host ?: return null
        val expectedAuthority = if (uri.port == 443) "$host:443" else host
        if (!uri.scheme.equals("https", ignoreCase = true) || host.endsWith('.') || uri.rawUserInfo != null ||
            uri.port !in listOf(-1, 443) || !uri.rawAuthority.equals(expectedAuthority, ignoreCase = true)) return null
        return uri
    }
}
