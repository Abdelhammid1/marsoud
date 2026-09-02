# مرصود — Mobile App

A Flutter mobile app for Marsoud. Consumes the JSON API at
`/api/v1/*` on the Flask backend (see `../app/routes/api_v1*.py`).

## Requirements

- Flutter 3.41+ (Dart 3.11+)
- Android Studio (for Android builds) or Xcode (iOS)
- A running Marsoud backend reachable from the device

## Environment

The base API URL is a compile-time constant, injected via
`--dart-define`:

```bash
# Android emulator against localhost Flask (10.0.2.2 is host from AVD)
flutter run --dart-define=MARSOUD_API=http://10.0.2.2:5050

# iOS simulator against localhost
flutter run --dart-define=MARSOUD_API=http://localhost:5050

# Physical device on same WiFi
flutter run --dart-define=MARSOUD_API=http://<your-ip>:5050

# Staging / production
flutter build apk --dart-define=MARSOUD_API=https://api.marsoud.example
```

If `MARSOUD_API` isn't set at build time the app aborts at first launch
with a fatal error — that's deliberate. A release APK with an empty URL
is a silent-401 bug factory.

## Auth

Login is `POST /api/v1/auth/login` with `{email, password, device_name?}`.
The response contains a bearer token that the app stores in
`flutter_secure_storage`. Every subsequent request attaches
`Authorization: Bearer <token>` via a Dio interceptor. Logout revokes the
token server-side by hitting `POST /api/v1/auth/logout`.

## Architecture

```
lib/
├── main.dart              — entry point
├── app/
│   ├── app.dart           — root MaterialApp (Arabic + RTL + Cairo theme)
│   ├── env.dart           — MARSOUD_API constant
│   ├── router.dart        — go_router + auth-guard redirect
│   └── theme.dart         — brand palette / typography
├── data/
│   ├── api_client.dart    — Dio client + bearer interceptor
│   ├── auth_state.dart    — Riverpod session + secure storage
│   ├── auth_repository.dart
│   └── my_account_repository.dart
└── features/
    ├── splash/            — while auth state loads
    ├── auth/              — login screen
    ├── home/              — persona-aware bottom-nav shell
    ├── my_account/        — /home tab
    ├── attendance/        — check-in/out with GPS
    └── notifications/     — feed + mark-read
```

## Personas

The bottom-nav tabs are picked from the caller's role on the active
company (returned by `/api/v1/auth/login`'s `companies[i].role`).
Current implementation ships the Employee lane; the Manager and Sales
lanes are follow-up tickets that add their tabs to `home_shell.dart`.

## Feature status

- Employee — my account, attendance check-in/out, notifications: **shipped**
- Employee — leave / permission / advance submit forms: **backend ready, screens TBD**
- Employee — daily reports, custody, my archive: **backend ready, screens TBD**
- Manager — leave/permission/advance approvals: **backend TBD, screens TBD**
- CRM (sales) — leads, activities, contacts: **backend TBD, screens TBD**
- Push notifications — **shipped** (FCM foreground + background,
  Android 13+ POST_NOTIFICATIONS wired, iOS pending Firebase project
  onboarding). Unread bell in the top bar polls `/api/v1/my/
  notifications/unread-count` every 30s as a backup.

## Verify

```bash
flutter analyze     # must pass with 0 issues
flutter test        # runs the placeholder smoke test
```

Backend audit: `python ../tests/audit_api_v1_mobile.py` from the repo
root — 14/14 covering login, auth guard, cross-tenant scope, and the
main `/my/*` endpoints the app calls.
