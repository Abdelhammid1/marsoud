#!/bin/sh
# MARSOUD-MOBILE-XCODE-CLOUD-01 (2026-09-09) — Xcode Cloud post-clone
# hook. Xcode Cloud has NO knowledge of Flutter; it clones the repo,
# opens Runner.xcworkspace, and runs xcodebuild.  Without this hook
# the very first build fails with "no such module Flutter" because
# the plugin xcframeworks were never generated.
#
# MARSOUD-MOBILE-XCODE-CLOUD-TRACE-01 (2026-09-20) — this file was
# rewritten twice today because Xcode Cloud's UI shows only "script
# failed (exited with code 1)" with no way to inspect the actual
# failing line from the outside.  Now aligned as closely as possible
# to the official Flutter + Xcode Cloud template so we minimise the
# surface of things that can go wrong:
#   · `-b stable` instead of `-b 3.24.5` — a pinned tag can silently
#     disappear from Flutter's repo, and the specific version was
#     buying us nothing (we never depended on 3.24.5-only features).
#   · Removed `flutter precache --ios` — `flutter pub get` +
#     xcodebuild's Flutter phase pull what they need on demand, and
#     precache was one more failure surface with a network hop.
#   · Removed `--repo-update` from `pod install` — the fresh CocoaPods
#     runner already has an up-to-date spec repo, and `--repo-update`
#     was adding 2-3 minutes of network + occasional 401s.
#   · `#!/bin/sh` instead of `#!/bin/bash` — Xcode Cloud's runner has
#     both, but `sh` matches Apple's own example verbatim so we don't
#     rely on runner shell defaults we can't inspect.
#
# The script MUST be located at ios/ci_scripts/ci_post_clone.sh
# exactly (Xcode Cloud is picky about the path + name), and must
# be executable.  Reference:
#   https://developer.apple.com/documentation/xcode/writing-custom-build-scripts
#   https://docs.flutter.dev/deployment/ios#xcode-cloud

set -e

echo "==> Marsoud · Xcode Cloud post-clone hook"
echo "==> Runner: $(uname -a)"
echo "==> HOME=$HOME  CI_PRIMARY_REPOSITORY_PATH=${CI_PRIMARY_REPOSITORY_PATH:-<unset>}"

# ─── 1. Install Flutter (stable channel) ────────────────────────
git clone --depth 1 -b stable https://github.com/flutter/flutter.git "$HOME/flutter"
export PATH="$HOME/flutter/bin:$PATH"
flutter --version

# ─── 2. Hydrate the project ─────────────────────────────────────
cd "$CI_PRIMARY_REPOSITORY_PATH"
flutter pub get

# ─── 3. Install pods ─────────────────────────────────────────────
cd ios
pod install

echo "==> Post-clone hook done"
