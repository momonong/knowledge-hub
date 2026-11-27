plugins {
    id("com.android.application") version "9.4.1"
    id("org.jetbrains.kotlin.plugin.compose") version "2.4.21"
}

android {
    namespace = "com.momonong.knowledgehub"
    compileSdk = 37

    defaultConfig {
        applicationId = "com.momonong.knowledgehub"
        minSdk = 26
        targetSdk = 37
        versionCode = 10
        versionName = "1.0.8"
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    buildFeatures.compose = true
    testOptions.unitTests.isIncludeAndroidResources = true
    packaging.resources.excludes += "/META-INF/{AL2.0,LGPL2.1}"
}

dependencies {
    val bom = platform("androidx.compose:compose-bom:2026.09.00")
    implementation(bom)
    implementation("androidx.activity:activity-compose:1.13.0")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.core:core-ktx:1.19.1")
    implementation("androidx.credentials:credentials:1.6.0")
    implementation("androidx.credentials:credentials-play-services-auth:1.6.0")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.11.0")
    implementation("androidx.webkit:webkit:1.14.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.11.0")
    constraints {
        implementation("androidx.annotation:annotation-experimental:1.5.1")
        implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.11.0")
        implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.11.0")
        testImplementation("com.google.errorprone:error_prone_annotations:2.48.0")
    }

    testImplementation(bom)
    testImplementation("androidx.compose.ui:ui-test-junit4")
    testImplementation("androidx.test:core:1.7.0")
    testImplementation("androidx.test:runner:1.7.0")
    testImplementation("androidx.test.ext:junit:1.3.0")
    testImplementation("com.google.code.findbugs:jsr305:3.0.2")
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.robolectric:robolectric:4.17")
}

tasks.withType<Test>().configureEach {
    providers.environmentVariable("KNOWLEDGE_HUB_ROBOLECTRIC_DIR").orNull?.let {
        systemProperty("robolectric.offline", "true")
        systemProperty("robolectric.dependency.dir", it)
    }
    providers.environmentVariable("KNOWLEDGE_HUB_SCREENSHOTS").orNull?.let {
        systemProperty("knowledgehub.screenshots", it)
    }
    maxHeapSize = "2g"
}
