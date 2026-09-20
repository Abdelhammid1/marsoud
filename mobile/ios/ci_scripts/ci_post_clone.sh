#!/bin/bash
# MARSOUD-MOBILE-XCODE-CLOUD-01 (2026-09-09) — Xcode Cloud post-clone
# hook. Xcode Cloud has NO knowledge of Flutter; it clones the repo,
# opens Runner.xcworkspace, and runs xcodebuild.  Without this hook
# the very first build fails with "no such module Flutter" because
# the plugin xcframeworks were never generated.
#
# MARSOUD-MOBILE-XCODE-CLOUD-DART-DEFINE-01 (2026-09-20) — the
# previous version of this script silently failed the whole build
# with a generic "ci_post_clone.sh script failed (exited with code 1)"
# and no way of telling WHICH step died.  Rewritten with:
#   · `set -euxo pipefail` — strict mode + trace every command
#   · `trap` on ERR that prints the exact failing line
#   · bash (not /bin/sh) so pipefail actually works
#   · a "print system info" preamble so future failures are easier
#     to diagnose from the Xcode Cloud log alone
#
# What this script does — all inside the Xcode Cloud runner:
#   1. Installs the Flutter SDK on the ephemeral macOS runner
#   2. Runs `flutter precache --ios` for iOS artifacts
#   3. Runs `flutter pub get` to hydrate the plugins list
#   4. Runs `pod install` so all plugin frameworks land in the
#      workspace by the time xcodebuild starts
#
# The script MUST be located at ios/ci_scripts/ci_post_clone.sh
# exactly (Xcode Cloud is picky about the path + name), and must
# be executable.  Reference:
#   https://developer.apple.com/documentation/xcode/writing-custom-build-scripts
#   https://docs.flutter.dev/deployment/ios#xcode-cloud

set -euxo pipefail

trap 'echo "!! ci_post_clone.sh FAILED at line $LINENO — see the traced command above."' ERR

echo "==> Marsoud · Xcode Cloud post-clone hook"
echo "==> Runner env: $(uname -a)"
echo "==> HOME=$HOME  PWD=$PWD  CI_PRIMARY_REPOSITORY_PATH=${CI_PRIMARY_REPOSITORY_PATH:-<unset>}"
echo "==> git version: $(git --version)"
echo "==> Ruby: $(ruby --version)  CocoaPods: $(pod --version 2>/dev/null || echo 'not-installed-yet')"

# ─── 1. Install Flutter ─────────────────────────────────────────
# Xcode Cloud runners are ephemeral and clean; we grab Flutter
# from GitHub, drop it into the runner's HOME, and put it on PATH.
FLUTTER_VERSION="3.24.5"
FLUTTER_HOME="$HOME/flutter"

if [ ! -d "$FLUTTER_HOME" ]; then
  echo "==> Installing Flutter $FLUTTER_VERSION"
  git clone --depth 1 --branch "$FLUTTER_VERSION" \
    https://github.com/flutter/flutter.git "$FLUTTER_HOME"
else
  echo "==> Flutter already present at $FLUTTER_HOME"
fi

export PATH="$FLUTTER_HOME/bin:$PATH"

echo "==> flutter --version"
flutter --version

echo "==> flutter precache --ios"
flutter precache --ios

# ─── 2. Hydrate the project ─────────────────────────────────────
# Xcode Cloud clones into $CI_PRIMARY_REPOSITORY_PATH; this script
# sits at ios/ci_scripts/, so the pubspec.yaml lives two levels up.
# `${VAR:?msg}` fails loudly + names the missing var in the log
# instead of `cd` succeeding to $HOME by accident.
cd "${CI_PRIMARY_REPOSITORY_PATH:?CI_PRIMARY_REPOSITORY_PATH not set by Xcode Cloud}"

echo "==> flutter pub get (in $PWD)"
flutter pub get

# ─── 3. Install pods ─────────────────────────────────────────────
echo "==> pod install"
cd ios
pod install --repo-update

echo "==> Post-clone hook done"
