# Xcode Cloud setup for Marsoud

Step-by-step to build + submit the app to the App Store using Xcode
Cloud, starting from the state of this repo after `MARSOUD-MOBILE-
XCODE-CLOUD-01`.

## 1. Before you open Xcode Cloud

You need three things in place on Apple's side:

  1. **Apple Developer Program membership** (paid, USD 99/yr) —
     Xcode Cloud is free but only becomes usable inside a paid
     team account.
  2. **App Store Connect app record**
     - Bundle ID: `com.manasety.marsoud` (must match
       `PRODUCT_BUNDLE_IDENTIFIER` in
       `ios/Runner.xcodeproj/project.pbxproj`)
     - Name: Marsoud (or your localised variant)
     - Primary language: Arabic
  3. **Push Notification key** (`.p8`, one per team) — upload it to
     Firebase Console → Project Settings → Cloud Messaging → iOS app.
     This is what makes production APNs work.

## 2. Drop the Firebase iOS config file into the repo

The repo does NOT ship `ios/Runner/GoogleService-Info.plist` on purpose
— it's Firebase-project-specific and every environment (dev / staging /
prod) may want its own. Ship it before the first archive:

  1. Firebase Console → Project Settings → Your apps
  2. Add an iOS app with bundle ID `com.manasety.marsoud`
  3. Download `GoogleService-Info.plist`
  4. Drop it into `ios/Runner/GoogleService-Info.plist`
  5. Open Xcode, drag the file into the `Runner` group under
     Xcode's Project Navigator so it's registered as a Bundle
     Resource. (Alternatively edit `project.pbxproj` by hand —
     Xcode is easier.)
  6. Commit + push. GoogleService-Info.plist is NOT a secret; it's
     safe to commit.

## 3. Wire up Xcode Cloud

From Xcode locally, or from App Store Connect:

  1. Click your Runner project → Report Navigator (⌘9) → **Cloud**
     tab → **Get Started**
  2. Connect the GitHub repo `ibrahimfakhrey/marsoud-mobile`
  3. Create a workflow named **"App Store"**:
     - **Environment:** latest Xcode + macOS Sequoia
     - **Start Conditions:** on-demand *and* push to `main`
     - **Actions:**
        - Archive → **iOS App Store**
     - **Post-Actions:**
        - TestFlight (Internal Testing → your test group)
        - Deliver to App Store Connect (manual review submit)
  4. Add build environment variables (Xcode Cloud calls these
     "Environment Variables"; some must be flagged **Secret** so
     they don't appear in the build log):

     | Variable          | Value                                    | Secret? |
     |-------------------|------------------------------------------|---------|
     | `MARSOUD_API`     | `https://<your prod API host>`           | no      |
     | `MARSOUD_WEB`     | `https://<your prod web host>` (optional)| no      |

     These flow into `flutter build ipa --dart-define=$MARSOUD_API=…`
     via the ci_post_clone hook. **Do NOT ship the app without both
     set** — `Env.assertConfigured()` will throw on launch.

## 4. First build

Push to `main`. Xcode Cloud will:

  1. Clone the repo → run `ios/ci_scripts/ci_post_clone.sh` — this
     installs Flutter, hydrates `pub get`, runs `pod install`
  2. Archive → signs with your Apple Development / Distribution
     profile (Automatic signing works because Xcode Cloud manages
     the certs)
  3. Upload to TestFlight → your testers get the build within
     minutes; App Store submission is a click from there.

## 5. Troubleshooting

### `no such module Flutter`
Xcode Cloud did not run the post-clone hook. Make sure:
  - The script lives at `ios/ci_scripts/ci_post_clone.sh` exactly
  - It's executable (`git update-index --chmod=+x`)
  - It's committed (`git ls-files ios/ci_scripts/ci_post_clone.sh`)

### `MARSOUD_API not defined at build time`
The `Env.assertConfigured` check fired. The workflow's Environment
Variables don't include `MARSOUD_API`, or the ci hook isn't passing
them through to `flutter build`. Check the Xcode Cloud build log for
the `flutter pub get` line — the runner's shell should have it set.

### Push notifications don't arrive on TestFlight
  - Check `Runner.entitlements` → `aps-environment` = `development`
    (Xcode Cloud auto-switches to `production` for App Store builds)
  - Verify Firebase → Cloud Messaging → iOS APNs auth key is uploaded
  - Verify the app requested notification permission at least once

### `GoogleService-Info.plist not found`
See §2. Firebase can't initialize without it; the app currently
tolerates the miss silently, so pushes just no-op instead of
crashing — but you'll ship an app with dead push.

## What this ticket added

  * `ios/Podfile` — CocoaPods integration (standard Flutter template)
  * `ios/ci_scripts/ci_post_clone.sh` — Xcode Cloud → Flutter hook
  * `ios/Runner/Runner.entitlements` — `aps-environment` for push
  * `ios/Runner/Info.plist` → `UIBackgroundModes` = `[fetch,
    remote-notification]` for FCM background delivery
  * `Runner.entitlements` wired into the three Runner build configs
    via `CODE_SIGN_ENTITLEMENTS`
  * This document.
