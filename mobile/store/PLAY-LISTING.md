# Google Play listing — مرصود (Marsoud)

MARSOUD-MOBILE-BRANDING (2026-08-26)

Everything the Play Console asks for, with the assets in this folder.
Character limits are Google's, and the counts below are the actual
lengths of the strings as written.

---

## 1. Store listing

### App name — max 30

```
مرصود – Marsoud
```

15 characters. This is the store title only. The launcher label is
deliberately different — see §5.

### Short description — max 80

```
تطبيق الموظف من مرصود: حضور وانصراف، مهام، عملاء محتملين، واجتماعاتك اليومية.
```

77 characters.

### Full description — max 4000

```
مرصود هو نظام إدارة الأعمال المتكامل للشركات. هذا التطبيق هو واجهة الموظف
منه: كل ما تحتاجه من عملك اليومي في جوالك، بالعربية وبواجهة من اليمين إلى
اليسار.

⏱ الحضور والانصراف
سجّل حضورك وانصرافك من جوالك مع تحديد الموقع، وتابع سجل الشهر كاملاً مع
عدد ساعات كل يوم.

✅ المهام
كل مهمة أنت مسؤول عنها أو ضمن فريقها، مقسّمة حسب الحالة: جديدة، قيد
التنفيذ، مراجعة، منجزة، متوقفة — مع الأولوية وتاريخ التسليم.

🎯 العملاء المحتملون
تابع عملاءك المحتملين ومراحلهم: عميل جديد، تم التواصل، اجتماع مجدول،
تفاوض، أُرسل العرض. مع بيانات التواصل والقيمة المتوقعة لكل فرصة.

📅 الاجتماعات والجدول
اجتماعاتك القادمة ومواعيدك المتكررة في مكان واحد، مع إمكانية إضافة اجتماع
جديد.

📮 الطلبات
قدّم طلبات الإجازة والسلفة والإذن، وتابع حالتها أولاً بأول.

📝 التقارير اليومية
اكتب تقريرك اليومي وأرسله لمديرك مباشرة من التطبيق.

💵 العهد
تابع عهدتك النقدية وعهدك العينية، واطلب صرف عهدة جديدة.

📁 ملفاتي والأرشيف
مستنداتك ومرفقاتك متاحة دائمًا.

🔔 إشعارات فورية
تنبيه فوري عند إسناد مهمة جديدة، أو قرب موعد اجتماع، أو تحديث على أحد
عملائك.

—

التطبيق يتطلب حساب موظف على منصة مرصود لدى شركتك. إذا لم يكن لديك حساب،
تواصل مع إدارة الموارد البشرية في شركتك.

الموقع الإلكتروني: https://marsoud.com
سياسة الخصوصية: https://marsoud.com/privacy
```

### Category and tags

| field | value |
|---|---|
| App or game | App |
| Category | **Business** |
| Tags | Business, Productivity, Human Resources |
| Contact email | *(your support address — required, shown publicly)* |
| Website | `https://marsoud.com` |
| Privacy policy | `https://marsoud.com/privacy` ← already live, returns 200 |

---

## 2. Graphics — all in this folder

| asset | spec | file | status |
|---|---|---|---|
| App icon | 512×512 PNG, 32-bit, **no alpha** | `play-icon-512.png` | ✅ alpha stripped |
| Feature graphic | 1024×500, no alpha | `feature-graphic-1024x500.png` | ✅ |
| Phone screenshots — plain | 9:16 | `screenshots-9x16/` (8 × 1242×2208) | ✅ **upload this** |
| Phone screenshots — captioned | 9:16 | `framed/` (8 × 1242×2208) | ✅ or this |
| Raw captures | 1080×2400 | `screenshots/` | ⚠️ **do not upload** |

**Pick `screenshots-9x16/` or `framed/` — not both**, and not
`screenshots/`. All three hold the same eight screens.

The raw captures are 1080×2400, which is 9:20. Play requires a phone
screenshot's long side to be at most twice its short side (9:16), so the
raw files are rejected on dimensions at upload. `screenshots-9x16/` is
the same image scaled to fit a 9:16 canvas — nothing cropped, just side
margins — and `framed/` adds an Arabic caption and a device bezel, which
reads better in the store carousel. `screenshots/` is kept only as the
untouched original.

Suggested carousel order (the first two are what most users actually
see):

1. `02-attendance` — the most distinctive feature
2. `04-tasks`
3. `05-leads`
4. `06-meetings`
5. `01-account`
6. `07-notifications`
7. `03-menu`
8. `08-login`

Tablet screenshots are optional. Without them Play shows a "not
optimised for tablets" note on tablet devices but still lists the app.

---

## 3. Data safety — declare these

The app **does** collect data, so "No data collected" is not a valid
answer. Permissions actually shipped in the bundle:

`INTERNET`, `ACCESS_NETWORK_STATE`, `ACCESS_FINE_LOCATION`,
`ACCESS_COARSE_LOCATION`, `POST_NOTIFICATIONS`, `VIBRATE`, `WAKE_LOCK`,
`com.google.android.c2dm.permission.RECEIVE`

| data type | collected | shared | why |
|---|---|---|---|
| Name, email address | Yes | No | account identity |
| **Approximate + precise location** | Yes | No | stamped on attendance check-in/out |
| Employment info (job title, employee no., salary fields) | Yes | No | the "my account" screen |
| Device ID (FCM token) | Yes | No | push notifications |
| App activity (tasks, leads, meetings) | Yes | No | core function |

Location is the one that gets listings rejected when it is left
undeclared — attendance check-in sends coordinates, so it must be
declared, and the in-app text already tells the user
("الموقع مطلوب لتسجيل الحضور"). Answer *"Data is encrypted in transit"*
= Yes (HTTPS to marsoud.com), and *"Users can request data deletion"*
according to your actual policy.

---

## 4. Content rating

Business tool, no user-generated public content, no ads, no purchases.
The questionnaire should come out **Everyone / 3+**. Answer honestly:
no violence, no sexual content, no gambling, no user-to-user
communication that is publicly visible.

---

## 5. App identity — and one deliberate difference

| where | string |
|---|---|
| Play listing title | **مرصود – Marsoud** |
| Launcher label (`android:label`) | **مرصود** |

These are intentionally not the same. Android gives a home-screen label
roughly 11–12 characters before it ellipsises, so the full
"مرصود – Marsoud" (15) would render on the phone as "مرصود – Mars…".
The store title has 30 characters to play with and is what users read
when deciding to install, so the full bilingual name lives there.

If you would rather have them identical, change `app_name` in
`android/app/src/main/res/values/strings.xml` — the truncation is the
only reason it is short.

| field | value |
|---|---|
| Package name | `com.manasety.marsoud` — **locked** once Play accepts the first bundle |
| Current version | `1.0.0+1` |
| Target SDK | 36 |
| Signing | upload keystore via `android/key.properties` — see `../RELEASE.md` |

---

## 6. Before you upload

1. **Bump the version.** `pubspec.yaml` is still `0.1.0+1`. Play rejects
   a versionCode it has already accepted, so this must increase on every
   upload. Suggested: `1.0.0+1` for the first public release.
2. **Create the upload keystore** if you have not — `../RELEASE.md`.
   Without `android/key.properties` the release build now fails loudly
   rather than silently producing a debug-signed bundle Play would
   reject.
3. **Build the bundle** (not an APK):
   ```bash
   cd mobile
   flutter build appbundle --release \
     --dart-define=MARSOUD_API=https://marsoud.com
   ```
   `--dart-define` is required: `Env.apiBaseUrl` is a compile-time
   constant with an empty default and the app throws at first launch
   without it.
4. **Confirm `google-services.json`** is at `android/app/` and its
   `project_id` matches the server's `instance/firebase-service-account.json`
   (both `marsoud-5e3e1`), or push tokens register and notifications
   never arrive.
5. First release goes to **internal testing**, not production — it is
   the fastest track and lets you verify the bundle installs and reaches
   `https://marsoud.com` before anyone else sees it.

---

## 7. How the screenshots were produced

Real captures from the app running on an Android 16 emulator at
1080×2400, built in **profile** mode so there is no debug banner, and
pointed at a local Marsoud server through `10.0.2.2`. They are genuine
app screens, not mockups of a design — Play requires screenshots to
represent the actual app.

The data behind them is a seeded demo employee ("سارة عبد الرحمن",
EMP-1042) in the existing demo company. The seed script is
`tools/seed_demo.py` and it has an `--undo` flag; the rows live only in
the local dev database.
