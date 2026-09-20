#!/bin/bash
# MARSOUD-MOBILE-XCODE-CLOUD-DART-DEFINE-01 (2026-09-20) — pre-xcodebuild
# hook.  Xcode Cloud runs `xcodebuild archive` on Runner.xcworkspace,
# which invokes Flutter's build phase via `xcode_backend.sh build`.
# That phase reads --dart-define values from the DART_DEFINES env var
# (base64 lines), NOT from arbitrary env vars named MARSOUD_API.
#
# Without this, `Env.apiBaseUrl` compiles to "" and `assertConfigured()`
# throws at main.dart:15 the moment the app launches — the .ipa builds
# fine, uploads fine, but the phone shows a black screen on open, and
# App Store shipping of the update looks like "nothing changed" from
# the user's side (because the update crashed silently and iOS keeps
# the last-known-good binary running for the "Open" button).
#
# Fix: pre-compile the Flutter framework with `--dart-define` explicitly,
# so the compiled Dart already carries the value by the time xcodebuild
# runs its own build phase.  The subsequent `xcode_backend.sh build`
# call becomes a no-op because Flutter's assemble cache is already
# populated.
#
# Required Xcode Cloud env vars on the workflow (App Store Connect →
# Xcode Cloud → Workflow → Environment):
#   MARSOUD_API  = https://<prod api host>          (not secret)
#   MARSOUD_WEB  = https://<prod web host>  (optional, not secret)
#
# The script MUST be at ios/ci_scripts/ci_pre_xcodebuild.sh exactly
# and be executable.  Xcode Cloud runs it AFTER ci_post_clone.sh and
# BEFORE xcodebuild.

set -euxo pipefail

trap 'echo "!! ci_pre_xcodebuild.sh FAILED at line $LINENO — see the traced command above."' ERR

echo "==> Marsoud · Xcode Cloud pre-xcodebuild hook"

# ci_post_clone.sh already installed Flutter into $HOME/flutter.
FLUTTER_HOME="$HOME/flutter"
if [ ! -d "$FLUTTER_HOME/bin" ]; then
  echo "!! Flutter SDK not found at $FLUTTER_HOME — did ci_post_clone.sh run?"
  exit 1
fi
export PATH="$FLUTTER_HOME/bin:$PATH"

# The Xcode Cloud workflow's env vars flow into this shell.  Refuse
# to build without MARSOUD_API — a silent-empty build ships a brick
# to App Store and the user has no way of telling why "nothing
# changed" after installing the update.
if [ -z "$MARSOUD_API" ]; then
  echo "!! MARSOUD_API is not set on the Xcode Cloud workflow."
  echo "!! Set it in App Store Connect → Xcode Cloud → Workflow →"
  echo "!! Environment Variables, then re-run this build."
  echo "!! See mobile/XCODE_CLOUD_SETUP.md §3 for the full list."
  exit 1
fi

# MARSOUD_WEB is optional; the app falls back to MARSOUD_API when
# unset (env.dart::webBaseUrl).
DEFINES="--dart-define=MARSOUD_API=$MARSOUD_API"
if [ -n "$MARSOUD_WEB" ]; then
  DEFINES="$DEFINES --dart-define=MARSOUD_WEB=$MARSOUD_WEB"
fi

cd "$CI_PRIMARY_REPOSITORY_PATH"

echo "==> flutter build ios --release --no-codesign $DEFINES"
# --no-codesign because Xcode Cloud handles signing during archive.
# `flutter build ios` populates .dart_tool/flutter_build/… with the
# compiled Dart carrying the dart-define values baked in; xcodebuild's
# Flutter phase then just picks that up.
flutter build ios --release --no-codesign $DEFINES

echo "==> Pre-xcodebuild hook done"
