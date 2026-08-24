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

android {
    namespace = "com.marsoud.marsoud"
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
        // TODO: Specify your own unique Application ID (https://developer.android.com/studio/build/application-id.html).
        applicationId = "com.marsoud.marsoud"
        // You can update the following values to match your application needs.
        // For more information, see: https://flutter.dev/to/review-gradle-config.
        minSdk = flutter.minSdkVersion
        targetSdk = flutter.targetSdkVersion
        versionCode = flutter.versionCode
        versionName = flutter.versionName
    }

    buildTypes {
        release {
            // TODO: Add your own signing config for the release build.
            // Signing with the debug keys for now, so `flutter run --release` works.
            signingConfig = signingConfigs.getByName("debug")
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
