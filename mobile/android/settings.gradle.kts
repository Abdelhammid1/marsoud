pluginManagement {
    val flutterSdkPath =
        run {
            val properties = java.util.Properties()
            file("local.properties").inputStream().use { properties.load(it) }
            val flutterSdkPath = properties.getProperty("flutter.sdk")
            require(flutterSdkPath != null) { "flutter.sdk not set in local.properties" }
            flutterSdkPath
        }

    includeBuild("$flutterSdkPath/packages/flutter_tools/gradle")

    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}

plugins {
    id("dev.flutter.flutter-plugin-loader") version "1.0.0"
    id("com.android.application") version "8.11.1" apply false
    id("org.jetbrains.kotlin.android") version "2.2.20" apply false
    // MARSOUD-MOBILE-TKT-05 (2026-08-18) — google-services
    // Gradle plugin. Reads mobile/android/app/google-services.json
    // and generates a per-project resource so FirebaseApp can
    // initialize without hard-coded API keys.
    id("com.google.gms.google-services") version "4.4.2" apply false
}

include(":app")
