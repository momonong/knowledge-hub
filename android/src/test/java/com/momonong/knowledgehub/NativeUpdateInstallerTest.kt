package com.momonong.knowledgehub

import android.app.Activity
import android.app.Application
import android.content.ActivityNotFoundException
import android.content.Intent
import android.content.pm.ActivityInfo
import android.content.pm.ApplicationInfo
import android.content.pm.PackageInfo
import android.content.pm.PackageInstaller
import android.content.pm.ResolveInfo
import android.content.pm.Signature
import android.content.pm.SigningInfo
import android.net.Uri
import android.provider.Settings
import androidx.core.content.FileProvider
import androidx.test.core.app.ApplicationProvider
import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
class NativeUpdateInstallerTest {
    private val app = ApplicationProvider.getApplicationContext<Application>()
    private val signer = Signature(byteArrayOf(1, 2, 3))
    private val installed = NativeUpdateIdentity(NATIVE_UPDATE_PACKAGE, 4, "1.0.3", setOf(signer))
    private val candidate = installed.copy(versionCode = 5, versionName = "1.0.4")
    private val release = NativeUpdateRelease(5, "1.0.4", 3,
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")

    @Test fun requiresExpectedPackageVersionAndIdenticalCurrentSigners() {
        verifyNativeUpdateIdentity(release, installed, candidate)
        listOf(
            candidate.copy(packageName = "com.example.other"),
            candidate.copy(versionCode = 4),
            candidate.copy(versionCode = 6),
            candidate.copy(versionName = "1.0.5"),
            candidate.copy(signers = setOf(Signature(byteArrayOf(1, 2, 4)))),
            candidate.copy(signers = emptySet()),
        ).forEach { identity ->
            assertThrows(NativeUpdateFailure::class.java) { verifyNativeUpdateIdentity(release, installed, identity) }
        }
        assertThrows(NativeUpdateFailure::class.java) {
            verifyNativeUpdateIdentity(release, candidate, candidate)
        }
    }

    @Suppress("DEPRECATION")
    @Test fun handoffRequiresPermissionAndOnlyGrantsReadToTheSystemInstaller() = withVerifiedFile { apk ->
        val installer = NativeUpdateInstaller(app)
        val manager = shadowOf(app.packageManager)

        manager.setCanRequestPackageInstalls(false)
        assertFalse(installer.canInstall())
        assertEquals(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, installer.settingsIntent().action)
        assertEquals("package:${app.packageName}", installer.settingsIntent().dataString)
        assertThrows(NativeUpdateFailure::class.java) { installer.installIntent(apk) }

        manager.setCanRequestPackageInstalls(true)
        assertThrows(ActivityNotFoundException::class.java) { installer.installIntent(apk) }
        registerSystemInstaller(apk.file)
        val intent = installer.installIntent(apk)
        assertEquals(Intent.ACTION_INSTALL_PACKAGE, intent.action)
        assertEquals("android.fixture.installer", intent.`package`)
        assertEquals("content", intent.data?.scheme)
        assertEquals("${app.packageName}.updates", intent.data?.authority)
        assertEquals(Intent.FLAG_GRANT_READ_URI_PERMISSION, intent.flags)
        assertTrue(intent.getBooleanExtra(Intent.EXTRA_RETURN_RESULT, false))
        assertEquals("abc", app.contentResolver.openInputStream(checkNotNull(intent.data))!!.use {
            it.readBytes().decodeToString()
        })
    }

    @Test fun rechecksSizeDigestAndInstalledVersionBeforeHandoff() = withVerifiedFile { apk ->
        val installer = NativeUpdateInstaller(app)
        val manager = shadowOf(app.packageManager)
        manager.setCanRequestPackageInstalls(true)
        assertThrows(NativeUpdateFailure::class.java) {
            installer.installIntent(apk.copy(release = release.copy(sizeBytes = 12)))
        }
        assertThrows(NativeUpdateFailure::class.java) {
            installer.installIntent(apk.copy(release = release.copy(sha256 = "0".repeat(64))))
        }
        manager.getInternalMutablePackageInfo(app.packageName).longVersionCode = candidate.versionCode
        assertThrows(NativeUpdateFailure::class.java) { installer.installIntent(apk) }
    }

    @Test fun fileProviderOnlyMapsTheVerifiedDirectory() {
        val store = NativeUpdateCache(app.cacheDir)
        val staged = store.stage(release)
        try {
            assertThrows(IllegalArgumentException::class.java) {
                FileProvider.getUriForFile(app, "${app.packageName}.updates", staged)
            }
            assertThrows(IllegalArgumentException::class.java) {
                FileProvider.getUriForFile(app, "${app.packageName}.updates", File(app.filesDir, "reading-state"))
            }
        } finally {
            staged.delete()
        }
    }

    @Test fun reportsAnInstallationOnlyWhenAndroidReturnsSuccess() {
        assertNull(nativeInstallNotice(Activity.RESULT_OK, null))
        assertNotNull(nativeInstallNotice(Activity.RESULT_CANCELED, null))
        assertNotNull(nativeInstallNotice(Activity.RESULT_FIRST_USER, null))
        val blocked = Intent().putExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE_BLOCKED)
        assertTrue(nativeInstallNotice(Activity.RESULT_FIRST_USER, blocked).orEmpty().contains("封鎖"))
    }

    @Suppress("DEPRECATION")
    private fun withVerifiedFile(test: (NativeUpdateApk) -> Unit) {
        val cache = NativeUpdateCache(app.cacheDir)
        val file = cache.publish(cache.stage(release).apply { writeText("abc") })
        try {
            val signing = SigningInfo().also { shadowOf(it).setSignatures(arrayOf(signer)) }
            val packages = shadowOf(app.packageManager)
            packages.getInternalMutablePackageInfo(app.packageName).apply {
                longVersionCode = installed.versionCode
                signingInfo = signing
            }
            packages.setPackageArchiveInfo(file.absolutePath, PackageInfo().apply {
                packageName = app.packageName
                longVersionCode = candidate.versionCode
                versionName = candidate.versionName
                signingInfo = signing
            })
            test(NativeUpdateApk(release, file))
        } finally {
            file.delete()
        }
    }

    @Suppress("DEPRECATION")
    private fun registerSystemInstaller(file: File) {
        val intent = Intent(Intent.ACTION_INSTALL_PACKAGE)
            .setDataAndType(Uri.parse("content://${app.packageName}.updates/verified-update/${file.name}"), NATIVE_UPDATE_MIME)
        shadowOf(app.packageManager).addResolveInfoForIntent(intent, ResolveInfo().apply {
            isDefault = true
            activityInfo = ActivityInfo().apply {
                name = "SystemInstaller"
                packageName = "android.fixture.installer"
                applicationInfo = ApplicationInfo().apply {
                    packageName = "android.fixture.installer"
                    flags = ApplicationInfo.FLAG_SYSTEM
                }
            }
        })
    }
}
