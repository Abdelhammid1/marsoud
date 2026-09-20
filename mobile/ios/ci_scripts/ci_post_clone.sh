#!/bin/sh
# MARSOUD-MOBILE-XCODE-CLOUD-01 (2026-09-09) — Xcode Cloud post-clone
# hook. Xcode Cloud has NO knowledge of Flutter; it clones the repo,
# opens Runner.xcworkspace, and runs xcodebuild.  Without this hook
# the very first build fails with "no such module Flutter" because
# the plugin xcframeworks were never generated.
#
# What this script does — all inside the Xcode Cloud runner:
#   1. Installs the Flutter SDK on the ephemeral macOS runner
#   2. Pins to a stable channel
#   3. Runs `flutter pub get` to hydrate the .flutter-plugins-
#      dependencies file the pods integration reads
#   4. Runs `pod install` so all plugin frameworks land in the
#      workspace by the time xcodebuild starts
#
# The script MUST be located at ios/ci_scripts/ci_post_clone.sh
# exactly (Xcode Cloud is picky about the path + name), and must
# be executable.  Reference:
#   https://developer.apple.com/documentation/xcode/writing-custom-build-scripts
#   https://docs.flutter.dev/deployment/ios#xcode-cloud

set -e

echo "==> Marsoud · Xcode Cloud post-clone hook"

# ─── 1. Install Flutter ─────────────────────────────────────────
# Xcode Cloud runners are ephemeral and clean; we grab Flutter
# from GitHub, drop it into the runner's HOME, and put it on PATH.
FLUTTER_VERSION="3.24.5"
FLUTTER_HOME="$HOME/flutter"

if [ ! -d "$FLUTTER_HOME" ]; then
  echo "==> Installing Flutter $FLUTTER_VERSION"
  git clone --depth 1 --branch "$FLUTTER_VERSION" \
    https://github.com/flutter/flutter.git "$FLUTTER_HOME"
fi

export PATH="$FLUTTER_HOME/bin:$PATH"

flutter --version
flutter precache --ios

# ─── 2. Hydrate the project ─────────────────────────────────────
# Xcode Cloud clones into $CI_PRIMARY_REPOSITORY_PATH; this script
# sits at ios/ci_scripts/, so the pubspec.yaml lives two levels up.
cd "$CI_PRIMARY_REPOSITORY_PATH"

echo "==> flutter pub get"
flutter pub get

# ─── 3. Install pods ─────────────────────────────────────────────
echo "==> pod install"
cd ios
pod install --repo-update

echo "==> Post-clone hook done"
