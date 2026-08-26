import java.util.Properties
import java.io.FileInputStream

plugins {
    id("com.android.application")
    id("kotlin-android")
    // The Flutter Gradle Plugin must be applied after the Android and Kotlin Gradle plugins.
    id("dev.flutter.flutter-gradle-plugin")
    // MARSOUD-MOBILE-TKT-05 (2026-08-18) — google-services
    // must be applied AFTER the Android plugin. Requires
    // mobile/android/app/google-services.json present at
    // build time (dropped in from the Firebase console —
    // gitignored).
    id("com.google.gms.google-services")
}

// ── MARSOUD-MOBILE-RELEASE-SIGNING (2026-08-25) ──────────────────────
// Release builds were signed with the Android DEBUG key: build.gradle
// still carried the Flutter template's
//   signingConfig = signingConfigs.getByName("debug")
// and apksigner on the built artifact reported
//   Signer #1 certificate DN: C=US, O=Android, CN=Android Debug
// Google Play refuses debug-signed uploads, so the app could not ship.
//
// The upload key now comes from `android/key.properties`, which is
// gitignored — a keystore in the repo is a keystore in everyone's hands,
// and losing control of it means losing the ability to update the app.
// See android/key.properties.example for the shape and RELEASE.md for
// how to generate one.
val keystorePropertiesFile = rootProject.file("key.properties")
val keystoreProperties = Properties()
val hasKeystore = keystorePropertiesFile.exists()
if (hasKeystore) {
    keystoreProperties.load(FileInputStream(keystorePropertiesFile))
}

// Fail LOUDLY instead of quietly falling back to the debug key. Silence
// is exactly how a debug-signed build got as far as being ready to
// upload. Only trips when a release artifact is actually being
// assembled, so `flutter run` and debug builds are unaffected.
val buildingRelease = gradle.startParameter.taskNames.any {
    it.contains("Release", ignoreCase = true)
}
if (buildingRelease && !hasKeystore) {
    throw GradleException(
        "\n\n  Release build requested but android/key.properties is missing.\n" +
        "  An unsigned or debug-signed bundle is REJECTED by Google Play.\n\n" +
        "  Create the upload keystore, then key.properties:\n" +
        "    see mobile/RELEASE.md  (template: android/key.properties.example)\n"
    )
}

android {
    namespace = "com.manasety.marsoud"
    compileSdk = flutter.compileSdkVersion
    ndkVersion = flutter.ndkVersion

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
        // MARSOUD-MOBILE-TKT-05 (2026-08-18) — flutter_local_notifications
        // uses java.time.LocalDateTime + java.util.concurrent.Flow which
        // require core-library desugaring on Android < API 26. Without
        // this flag the release build fails with:
        //   "Dependency ':flutter_local_notifications' requires core
        //    library desugaring to be enabled".
        isCoreLibraryDesugaringEnabled = true
    }

    kotlinOptions {
        jvmTarget = JavaVersion.VERSION_17.toString()
    }

    defaultConfig {
        // MARSOUD-MOBILE-APPID (2026-08-26) — reverse-DNS of the
        // company domain, then the product: Manasety is the org,
        // Marsoud is this app. LOCKED once Play accepts the first
        // bundle — a different id is a different listing with no
        // installs, reviews or update path. Do not "tidy" it.
        applicationId = "com.manasety.marsoud"
        // You can update the following values to match your application needs.
        // For more information, see: https://flutter.dev/to/review-gradle-config.
        minSdk = flutter.minSdkVersion
        targetSdk = flutter.targetSdkVersion
        versionCode = flutter.versionCode
        versionName = flutter.versionName
    }

    signingConfigs {
        // Only declared when key.properties is present. Referencing a
        // config with null paths makes Gradle emit an UNSIGNED artifact
        // rather than fail, which would put us back where we started.
        if (hasKeystore) {
            create("release") {
                storeFile = file(keystoreProperties["storeFile"] as String)
                storePassword = keystoreProperties["storePassword"] as String
                keyAlias = keystoreProperties["keyAlias"] as String
                keyPassword = keystoreProperties["keyPassword"] as String
            }
        }
    }

    buildTypes {
        release {
            signingConfig = if (hasKeystore) {
                signingConfigs.getByName("release")
            } else {
                // Unreachable for release tasks — the GradleException
                // above stops those. This branch only keeps the script
                // configurable for debug work with no keystore present.
                signingConfigs.getByName("debug")
            }
        }
    }
}

flutter {
    source = "../.."
}

// MARSOUD-MOBILE-TKT-05 (2026-08-18) — desugaring runtime library.
// Version pinned to what flutter_local_notifications v18 requires
// (2.1.4+). Google publishes the coordinate; safe to bump but
// this floor is the tested one.
dependencies {
    coreLibraryDesugaring("com.android.tools:desugar_jdk_libs:2.1.4")
}
