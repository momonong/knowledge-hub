package com.momonong.knowledgehub

import androidx.annotation.DrawableRes
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.ime
import androidx.compose.foundation.layout.safeDrawing
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.NavigationBarItemDefaults
import androidx.compose.material3.NavigationRail
import androidx.compose.material3.NavigationRailItem
import androidx.compose.material3.NavigationRailItemDefaults
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.layout.Layout
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.unit.Constraints
import androidx.compose.ui.unit.dp

internal enum class ReaderDestination(val url: String, val title: String, @DrawableRes val icon: Int) {
    TODAY(SitePolicy.HOME, "今日", R.drawable.ic_reader_today),
    CONTINUE(SitePolicy.HOME + "reading", "接續", R.drawable.ic_reader_continue),
    LIBRARY(SitePolicy.HOME + "library", "知識庫", R.drawable.ic_reader_library),
}

@Composable
internal fun ReaderNavigationLayout(
    selected: ReaderDestination?,
    onSelect: (ReaderDestination) -> Unit,
    onUpdate: () -> Unit,
    showNavigation: Boolean,
    modifier: Modifier = Modifier,
    content: @Composable () -> Unit,
) {
    BoxWithConstraints(modifier.fillMaxSize().windowInsetsPadding(WindowInsets.safeDrawing)) {
        val useRail = maxHeight >= 272.dp && (maxWidth >= 600.dp || (maxWidth >= 440.dp && maxHeight < 400.dp))
        val imeVisible = WindowInsets.ime.getBottom(LocalDensity.current) > 0
        // The reader stays in the same composition slot while the navigation moves.
        Layout(
            content = {
                Box { content() }
                if (showNavigation && !imeVisible) {
                    ReaderNavigation(useRail, selected, onSelect, onUpdate)
                }
            },
        ) { children, constraints ->
            val width = constraints.maxWidth
            val height = constraints.maxHeight
            val navigation = children.getOrNull(1)?.measure(constraints.copy(minWidth = 0, minHeight = 0))
            val left = if (useRail) navigation?.width ?: 0 else 0
            val bottom = if (useRail) 0 else navigation?.height ?: 0
            val reader = children.first().measure(Constraints.fixed(width - left, height - bottom))
            layout(width, height) {
                reader.placeRelative(left, 0)
                navigation?.placeRelative(0, if (useRail) 0 else height - bottom)
            }
        }
    }
}

@Composable
private fun ReaderNavigation(
    useRail: Boolean,
    selected: ReaderDestination?,
    onSelect: (ReaderDestination) -> Unit,
    onUpdate: () -> Unit,
) {
    val colors = MaterialTheme.colorScheme
    if (useRail) {
        NavigationRail(
            modifier = Modifier.fillMaxHeight().verticalScroll(rememberScrollState()).testTag("reader-navigation-rail"),
            containerColor = colors.surfaceContainerLow,
            windowInsets = WindowInsets(0, 0, 0, 0),
        ) {
            val itemColors = NavigationRailItemDefaults.colors(
                selectedIconColor = colors.onPrimaryContainer,
                selectedTextColor = colors.primary,
                indicatorColor = colors.primaryContainer,
                unselectedIconColor = colors.onSurfaceVariant,
                unselectedTextColor = colors.onSurfaceVariant,
            )
            for (destination in ReaderDestination.entries) {
                NavigationRailItem(
                    selected = destination == selected,
                    onClick = { onSelect(destination) },
                    icon = { Icon(painterResource(destination.icon), contentDescription = null) },
                    label = { Text(destination.title) },
                    colors = itemColors,
                )
            }
            UpdateIconButton(onUpdate)
        }
    } else {
        NavigationBar(
            modifier = Modifier.testTag("reader-navigation-bar"),
            containerColor = colors.surfaceContainerLow,
            windowInsets = WindowInsets(0, 0, 0, 0),
        ) {
            val itemColors = NavigationBarItemDefaults.colors(
                selectedIconColor = colors.onPrimaryContainer,
                selectedTextColor = colors.primary,
                indicatorColor = colors.primaryContainer,
                unselectedIconColor = colors.onSurfaceVariant,
                unselectedTextColor = colors.onSurfaceVariant,
            )
            for (destination in ReaderDestination.entries) {
                NavigationBarItem(
                    selected = destination == selected,
                    onClick = { onSelect(destination) },
                    icon = { Icon(painterResource(destination.icon), contentDescription = null) },
                    label = { Text(destination.title) },
                    colors = itemColors,
                )
            }
            UpdateIconButton(onUpdate)
        }
    }
}

@Composable
private fun UpdateIconButton(onUpdate: () -> Unit) {
    IconButton(onClick = onUpdate, modifier = Modifier.size(48.dp).testTag("open-update")) {
        Icon(
            painterResource(R.drawable.ic_app_update),
            contentDescription = "更新應用程式",
            modifier = Modifier.size(24.dp),
            tint = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}
