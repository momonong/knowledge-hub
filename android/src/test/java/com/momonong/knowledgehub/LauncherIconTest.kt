package com.momonong.knowledgehub

import android.app.Application
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.drawable.AdaptiveIconDrawable
import android.graphics.drawable.Drawable
import androidx.test.core.app.ApplicationProvider
import java.io.File
import kotlin.math.hypot
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
    private val icon get() = context.getDrawable(R.mipmap.ic_launcher) as AdaptiveIconDrawable

    @Test fun launcherIconRendersThroughTheAndroidAdaptiveMask() {
        val bitmap = render(icon, 180)
        val pixels = IntArray(bitmap.width * bitmap.height)
        bitmap.getPixels(pixels, 0, bitmap.width, 0, 0, bitmap.width, bitmap.height)
        for (swatch in listOf("#285B77", "#F6F3E9")) {
            val color = Color.parseColor(swatch)
            assertTrue("Book color $swatch should survive the adaptive mask", pixels.count { it == color } > 100)
        }
        saveScreenshot(bitmap, "launcher-icon.png")
    }

    @Test fun entireBookFitsInsideTheLauncherSafeCircle() {
        val pixelsPerDp = 4
        val size = 108 * pixelsPerDp
        val center = size / 2.0
        val foreground = render(icon.foreground, size)
        saveScreenshot(foreground, "launcher-foreground.png")

        val row = IntArray(size)
        var outerRadiusDp = 0.0
        for (y in 0 until size) {
            foreground.getPixels(row, 0, size, 0, y, size, 1)
            for (x in row.indices) {
                if (Color.alpha(row[x]) >= 128) {
                    val distance = hypot(x + 0.5 - center, y + 0.5 - center) / pixelsPerDp
                    outerRadiusDp = maxOf(outerRadiusDp, distance)
                }
            }
        }
        assertTrue(
            "Book reaches ${outerRadiusDp}dp from center, safe radius is 33dp",
            outerRadiusDp > 0 && outerRadiusDp <= 33,
        )
    }

    private fun render(drawable: Drawable, size: Int): Bitmap {
        val bitmap = Bitmap.createBitmap(size, size, Bitmap.Config.ARGB_8888)
        drawable.setBounds(0, 0, size, size)
        drawable.draw(Canvas(bitmap))
        return bitmap
    }

    private fun saveScreenshot(bitmap: Bitmap, name: String) {
        val dir = System.getProperty("knowledgehub.screenshots") ?: return
        File(dir, name).apply { parentFile?.mkdirs() }.outputStream().use {
            assertTrue(bitmap.compress(Bitmap.CompressFormat.PNG, 100, it))
        }
    }
}
