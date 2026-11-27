package com.momonong.knowledgehub

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

private val day = lightColorScheme(
    primary = Color(0xFF284F65), onPrimary = Color.White,
    primaryContainer = Color(0xFFDFE8EB), onPrimaryContainer = Color(0xFF203F50),
    secondary = Color(0xFF465B50), onSecondary = Color.White,
    tertiary = Color(0xFF69551D), onTertiary = Color.White,
    tertiaryContainer = Color(0xFFF3DD8B), onTertiaryContainer = Color(0xFF3E3216),
    surface = Color(0xFFF6F3E9), background = Color(0xFFF6F3E9),
    onSurface = Color(0xFF252D2E), onBackground = Color(0xFF252D2E),
    surfaceContainerLowest = Color(0xFFFCF9F1), surfaceContainerLow = Color(0xFFF0ECE0),
    surfaceContainer = Color(0xFFEDE8DC), surfaceContainerHigh = Color(0xFFE7E2D6),
    surfaceContainerHighest = Color(0xFFE1DCCD), onSurfaceVariant = Color(0xFF515A59),
    outline = Color(0xFF727975), outlineVariant = Color(0xFFCCC9BD),
)
private val night = darkColorScheme(
    primary = Color(0xFFA5CDE0), onPrimary = Color(0xFF142A36),
    primaryContainer = Color(0xFF283C45), onPrimaryContainer = Color(0xFFC2DDEB),
    secondary = Color(0xFFB0C7B6), onSecondary = Color(0xFF20392A),
    tertiary = Color(0xFFDDCB87), onTertiary = Color(0xFF392D0A),
    tertiaryContainer = Color(0xFF58491F), onTertiaryContainer = Color(0xFFFFEDB0),
    surface = Color(0xFF202524), background = Color(0xFF202524),
    onSurface = Color(0xFFF0ECE0), onBackground = Color(0xFFF0ECE0),
    surfaceContainerLowest = Color(0xFF1B2121), surfaceContainerLow = Color(0xFF252C2B),
    surfaceContainer = Color(0xFF2A3130), surfaceContainerHigh = Color(0xFF343A37),
    surfaceContainerHighest = Color(0xFF3B423E), onSurfaceVariant = Color(0xFFC2C7BD),
    outline = Color(0xFF939E94), outlineVariant = Color(0xFF49524D),
)

@Composable
internal fun ReaderTheme(content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = if (isSystemInDarkTheme()) night else day, content = content)
}
