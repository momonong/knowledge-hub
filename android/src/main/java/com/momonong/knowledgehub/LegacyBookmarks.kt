package com.momonong.knowledgehub

import android.content.ActivityNotFoundException
import android.content.Context
import android.content.SharedPreferences
import android.text.TextUtils.htmlEncode
import android.widget.Toast
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.produceState
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.ui.platform.LocalContext
import java.io.IOException
import java.io.Writer
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONException
import org.json.JSONObject

// Versions through 1.0.5 stored a complete snapshot under bookmark:<id>.
// Read only these records. The site session and other device preferences do not belong in an export.
internal class LegacyBookmarks(private val prefs: SharedPreferences) {
    fun ids(): List<String> = try {
        prefs.getStringSet("bookmarks", null).orEmpty().sorted()
    } catch (_: ClassCastException) {
        emptyList()
    }

    fun writeTo(out: Writer) {
        out.write("""<!doctype html><html lang="zh-Hant"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Knowledge Hub 舊收藏</title><h1>Knowledge Hub 舊收藏</h1><ol>""")
        val records = prefs.all
        for (id in ids()) {
            val saved = records["bookmark:$id"] as? String
            val paper = try { saved?.let(::JSONObject) } catch (_: JSONException) { null }
            val title = paper?.optString("title").orEmpty().ifBlank { id }
            out.write("<li><h2>${htmlEncode(title)}</h2>")
            val site = SitePolicy.ORIGIN + paper?.optString("sitePath").orEmpty()
            if (SitePolicy.isFixedArticle(site)) {
                out.write("""<p><a href="${htmlEncode(site)}">到 Knowledge Hub 讀這一版</a></p>""")
            }
            SitePolicy.externalNavigation(paper?.optString("sourceUrl").orEmpty(), userGesture = true)?.let { url ->
                out.write("""<p><a href="${htmlEncode(url)}">出版者原文</a></p>""")
            }
            out.write("<details><summary>已存的收藏資料</summary><pre>")
            out.write(htmlEncode(paper?.toString(2) ?: saved ?: id))
            out.write("</pre></details></li>")
        }
        out.write("</ol></html>")
    }
}

@Composable
internal fun LegacyBookmarkExport() {
    val context = LocalContext.current
    val archive = remember(context) {
        LegacyBookmarks(context.getSharedPreferences("reader", Context.MODE_PRIVATE))
    }
    val hasBookmarks by produceState(false, archive) {
        value = withContext(Dispatchers.IO) { archive.ids().isNotEmpty() }
    }
    val scope = rememberCoroutineScope()
    fun notice(message: String) = Toast.makeText(context, message, Toast.LENGTH_LONG).show()
    val picker = rememberLauncherForActivityResult(ActivityResultContracts.CreateDocument("text/html")) { uri ->
        if (uri != null) scope.launch {
            try {
                withContext(Dispatchers.IO) {
                    val stream = context.contentResolver.openOutputStream(uri, "wt")
                        ?: throw IOException("Could not open the selected document")
                    stream.bufferedWriter(Charsets.UTF_8).use(archive::writeTo)
                }
                notice("舊收藏已匯出")
            } catch (_: IOException) {
                notice("寫入失敗，請重新選擇儲存位置。")
            } catch (_: SecurityException) {
                notice("無法寫入這個位置，請選擇其他位置。")
            }
        }
    }
    if (hasBookmarks) TextButton(onClick = {
        try {
            picker.launch("Knowledge-Hub-舊收藏.html")
        } catch (_: ActivityNotFoundException) {
            notice("此裝置目前無法開啟檔案儲存視窗。")
        }
    }) { Text("匯出舊收藏") }
}
