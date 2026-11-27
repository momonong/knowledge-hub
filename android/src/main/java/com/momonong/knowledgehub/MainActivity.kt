package com.momonong.knowledgehub

import android.os.Bundle
import android.webkit.CookieManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        setContent {
            var updateVisible by rememberSaveable { mutableStateOf(false) }
            var loginForUpdate by rememberSaveable { mutableStateOf(false) }
            fun openUpdate() {
                loginForUpdate = false
                updateVisible = true
            }
            ReaderTheme {
                Surface(Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.surfaceContainerLow) {
                    SiteReader(
                        onBackAtRoot = ::finish,
                        loginForUpdate = loginForUpdate,
                        onReturnToUpdate = ::openUpdate,
                        onUpdate = ::openUpdate,
                    )
                }
                NativeUpdateDialog(
                    visible = updateVisible,
                    onDismiss = { updateVisible = false },
                    onLogin = {
                        updateVisible = false
                        loginForUpdate = true
                    },
                )
            }
        }
    }

    override fun onStop() {
        super.onStop()
        // Keep the site's persistent cookies available after Android stops the app process.
        CookieManager.getInstance().flush()
    }
}
