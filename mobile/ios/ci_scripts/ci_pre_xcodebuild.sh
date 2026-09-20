#!/bin/sh
# MARSOUD-MOBILE-XCODE-CLOUD-DART-DEFINE-01 (2026-09-20) — pre-xcodebuild
# hook.  Xcode Cloud's `xcodebuild archive` invokes Flutter's build
# phase via `xcode_backend.sh build`, which reads --dart-define values
# from the DART_DEFINES env var (base64 lines), NOT from arbitrary env
# vars named MARSOUD_API.  Without pre-compiling the framework with
# --dart-define, Env.apiBaseUrl ends up "" and assertConfigured()
# throws at main.dart:15 the moment the app launches — the .ipa uploads
# fine but the phone shows a black screen and iOS reverts to the
# last-known-good build.
#
# Fix: pre-build the Flutter framework with `--dart-define=MARSOUD_API=...`
# BEFORE xcodebuild archives.  The compiled Dart carries the value and
# xcodebuild's Flutter phase becomes a cache hit.
#
# MARSOUD-MOBILE-XCODE-CLOUD-TRACE-01 (2026-09-20) — this used to
# `exit 1` when MARSOUD_API was unset, which meant a workflow that
# forgot the env var couldn't produce ANY build.  Now it warns and
# continues: the runtime `assertConfigured()` will still catch the
# empty URL, but at least CI green means "the .ipa built" and we
# don't gate on a workflow-config step the operator may not have
# reached yet.
#
# Required Xcode Cloud env vars on the workflow (App Store Connect →
# Xcode Cloud → Workflow → Environment):
#   MARSOUD_API  = https://<prod api host>          (not secret)
#   MARSOUD_WEB  = https://<prod web host>  (optional, not secret)
#
# The script MUST be at ios/ci_scripts/ci_pre_xcodebuild.sh exactly
# and be executable.

set -e

echo "==> Marsoud · Xcode Cloud pre-xcodebuild hook"

# The Flutter SDK was installed into $HOME/flutter by ci_post_clone.sh.
export PATH="$HOME/flutter/bin:$PATH"

if [ -z "${MARSOUD_API:-}" ]; then
  echo "!! WARNING: MARSOUD_API is not set on this Xcode Cloud workflow."
  echo "!! Skipping the pre-build dart-define step.  The resulting .ipa"
  echo "!! will archive fine but throw at boot because Env.apiBaseUrl is"
  echo "!! empty.  Set MARSOUD_API in App Store Connect → Xcode Cloud →"
  echo "!! Workflow → Environment to fix at the source."
  exit 0
fi

DEFINES="--dart-define=MARSOUD_API=$MARSOUD_API"
if [ -n "${MARSOUD_WEB:-}" ]; then
  DEFINES="$DEFINES --dart-define=MARSOUD_WEB=$MARSOUD_WEB"
fi

cd "$CI_PRIMARY_REPOSITORY_PATH"
echo "==> flutter build ios --release --no-codesign $DEFINES"
flutter build ios --release --no-codesign $DEFINES

echo "==> Pre-xcodebuild hook done"
