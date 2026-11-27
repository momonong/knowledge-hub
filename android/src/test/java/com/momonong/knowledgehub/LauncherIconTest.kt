package com.momonong.knowledgehub

import android.app.Application
import android.graphics.Bitmap
import androidx.test.core.app.ApplicationProvider
import java.io.File
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
@GraphicsMode(GraphicsMode.Mode.NATIVE)
class LauncherIconTest {
    private val context get() = ApplicationProvider.getApplicationContext<Application>()
    @Test fun launcherIconRendersThroughTheAndroidAdaptiveMask() {
        val icon = requireNotNull(context.getDrawable(R.mipmap.ic_launcher))
        assertTrue(icon is android.graphics.drawable.AdaptiveIconDrawable)
        val bitmap = Bitmap.createBitmap(180, 180, Bitmap.Config.ARGB_8888)
        icon.setBounds(0, 0, bitmap.width, bitmap.height)
        icon.draw(android.graphics.Canvas(bitmap))
        val pixels = IntArray(bitmap.width * bitmap.height)
        bitmap.getPixels(pixels, 0, bitmap.width, 0, 0, bitmap.width, bitmap.height)
        for (swatch in listOf("#285B77", "#F6F3E9")) {
            val color = android.graphics.Color.parseColor(swatch)
            assertTrue("Book color $swatch should survive the adaptive mask", pixels.count { it == color } > 100)
        }
        val dir = System.getProperty("knowledgehub.screenshots") ?: return
        File(dir, "launcher-icon.png").apply { parentFile?.mkdirs() }.outputStream().use {
            assertTrue(bitmap.compress(Bitmap.CompressFormat.PNG, 100, it))
        }
    }

}
