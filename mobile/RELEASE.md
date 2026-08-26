# Marsoud mobile — release build

MARSOUD-MOBILE-RELEASE-SIGNING (2026-08-25)

## Why this exists

`android/app/build.gradle.kts` shipped with the Flutter template default:

```kotlin
release {
    // TODO: Add your own signing config for the release build.
    signingConfig = signingConfigs.getByName("debug")
}
```

`apksigner` on the artifact that was ready to upload confirmed it:

```
Signer #1 certificate DN:  C=US, O=Android, CN=Android Debug
```

Google Play refuses debug-signed uploads, so the build could not ship.
The signing config now reads `android/key.properties`, and a release
build **fails loudly** if that file is missing rather than quietly
falling back to the debug key — silence is how this got as far as it did.

## One-time: create the upload key

Run this yourself. It prompts for passwords, which is why it is not
scripted and must not be pasted into a shared terminal or a chat.

```bash
keytool -genkey -v \
  -keystore mobile/android/upload-keystore.jks \
  -storetype PKCS12 \
  -keyalg RSA -keysize 2048 -validity 10000 \
  -alias upload
```

On Windows, `keytool` ships with Android Studio's bundled JDK:

```
"C:\Program Files\Android\Android Studio\jbr\bin\keytool.exe" -genkey -v ^
  -keystore mobile\android\upload-keystore.jks ^
  -storetype PKCS12 -keyalg RSA -keysize 2048 -validity 10000 -alias upload
```

`PKCS12`, not `JKS`: keytool now prints
"The JKS keystore uses a proprietary format… migrate to PKCS12" on every
JKS operation. Gradle reads either, so there is no reason to start on the
deprecated one. The `.jks` file extension is just a name — the format is
what `-storetype` says.

It asks for a keystore password, then name / organisation / country, then
a key password (pressing Enter reuses the keystore password). `-validity
10000` is ~27 years; Play requires a key valid past 22 October 2033.

Then:

```bash
cp mobile/android/key.properties.example mobile/android/key.properties
# edit it with the passwords you just chose
```

### Back this up before you publish

**The upload key cannot be rotated by you once the app is live** — only
Google can reset it, through a support request that takes days. If the
keystore is lost you cannot ship another update under the same listing.

- keep `upload-keystore.jks` and its passwords in a password manager
- keep a second copy somewhere off this machine
- never commit either file — `android/.gitignore` and `mobile/.gitignore`
  both block them, and that is deliberate

## Build for upload

Play requires an **App Bundle** (`.aab`) for new apps, not an APK:

```bash
cd mobile
flutter build appbundle --release \
  --dart-define=MARSOUD_API=https://marsoud.com
```

`--dart-define` is not optional. `Env.apiBaseUrl`
(`lib/app/env.dart`) is a compile-time `String.fromEnvironment` with an
empty default, and `Env.assertConfigured()` throws at first launch if it
was omitted — a deliberate crash, chosen over an app that silently 401s
against nothing.

Output: `build/app/outputs/bundle/release/app-release.aab`

### Verify before uploading

```bash
# 1. signed with YOUR key, not the debug key
"$ANDROID_HOME/build-tools/<ver>/apksigner" verify --print-certs -v \
  build/app/outputs/apk/release/app-release.apk

# expected: Signer #1 certificate DN: CN=<your CN>, ...
# NOT:      CN=Android Debug
```

(`apksigner` reads APKs, not bundles — build an APK alongside the bundle
if you want to check the certificate directly, or check the bundle in the
Play Console after upload.)

Checks worth repeating each release:

- `google-services.json` present at `android/app/` — gitignored, so it
  will be missing on a fresh clone and the build fails at the
  `com.google.gms.google-services` plugin
- its `project_id` matches the server's
  `instance/firebase-service-account.json` (both `marsoud-5e3e1`),
  otherwise push tokens register and notifications silently never arrive
- `version:` in `pubspec.yaml` bumped — Play rejects a versionCode it has
  already seen

## Known gaps

- `android:label` in `AndroidManifest.xml` is `marsoud`, lowercase and
  English. It is what shows under the icon on the phone; everything else
  in the product is مرصود.
- `pubspec.yaml` is at `0.1.0+1`.
- No iOS signing is set up; this covers Android only.
