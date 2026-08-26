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

---

## 8. "App content" — answer sheet

The Play Console's App content checklist, in the order it lists them.
Every answer below is checked against what the code actually does, not
against what the app is described as doing.

### Set privacy policy

```
https://marsoud.com/privacy
```

⚠️ **Fix the policy before submitting this.** The published text never
mentions location, but Data safety (below) must declare precise and
approximate location because attendance check-in sends GPS coordinates.
Google cross-checks the declaration against the policy and rejects the
mismatch — it is the most common rejection for apps like this.

Paste `privacy-addendum-ar.html` from this folder into
**/admin/legal → سياسة الخصوصية**, appended to the existing text (do not
replace it). It covers location, the FCM device identifier, employment
data, encryption in transit, retention/deletion, and the no-ads
statement — i.e. exactly the set Data safety declares.

### Sign-in details

**This is the one that will get the app rejected if you skip it.** The
app is login-only: a reviewer who opens it sees nothing but the login
screen and no way past it, and "we couldn't access the functionality"
is a rejection, not a query.

Create a **real account on production** (`marsoud.com`) for Google to
use, and give it data worth looking at — an employee record, a few
tasks, a lead or two, some attendance history. An empty account looks
broken. Then fill in:

- Username / password of that account
- Any instructions: the app is Arabic/RTL; sign in, and the drawer
  (top-right ☰) reaches every screen

Keep the account alive — Google re-reviews on later updates too.

### Ads

**No**, this app contains no ads. Verified: no ad SDK in `pubspec.yaml`
(no AdMob, google_mobile_ads, AppLovin, Unity Ads, Facebook Audience).

### Content rating

Answer the questionnaire; category **Utility / Productivity / Other**.
Everything is No: no violence, sexual content, profanity, controlled
substances, gambling, or horror. Expected outcome: **Everyone / 3+**.

Two questions worth reading carefully rather than clicking through:

- *Do users interact or exchange content?* Users are assigned tasks and
  send daily reports to their own manager. It is a closed company
  workspace with no public feed and no stranger-to-stranger contact —
  but answer it for what the questionnaire actually asks, since a wrong
  answer here changes the rating.
- *Does the app share user location with other users?* Attendance
  coordinates are visible to the **employer**. Not to other users
  generally, and not publicly.

### Target audience

**18 and over** only. Do not tick any under-18 bracket: it pulls the app
into the Families policy programme, which brings much stricter
requirements the app is not built for. This is a workplace tool for
employed adults, so 18+ is both accurate and the easy path.

Then: *"Do you want your app to appeal to children?"* → **No**.

### Data safety

The table in §3 above. In the form:

- **Data is encrypted in transit** → Yes (HTTPS/TLS to marsoud.com)
- **Users can request data deletion** → Yes, and the addendum documents
  the route (HR, or the contact address in the policy)
- **Is data shared with third parties?** → No. Firebase Cloud Messaging
  is a service provider processing on your behalf, not third-party
  sharing.
- Location must be marked **required**, not optional, with the purpose
  "App functionality" — it gates attendance.

### Government apps

**No.** Marsoud is a private commercial product, not published by or on
behalf of a government body.

### Financial features

**None of the above.**

The one that looks like it might qualify is السلف (advances): an
employee submits an `AdvanceRequest` and their employer approves it.
There is no lender, no credit product, no interest, and no money moves
through the app — it is an internal HR approval workflow, and the app
carries no payment gateway of any kind (verified: no Stripe, PayPal,
Paymob, Fawry or card handling anywhere in `mobile/lib`). Displaying an
employee their own salary figure is not a financial feature either.

### Health

**No.** No health, medical, fitness or wellness features. Sick leave is
a leave-request type with a day count; the app stores no medical
information.

### App category and contact details

| field | value |
|---|---|
| Category | **Business** |
| Tags | Business, Productivity, Human Resources |
| Email | a monitored public address — Google shows it on the listing |
| Website | `https://marsoud.com` |
| Phone | optional |

### Store listing

Everything is in §1 and §2 — title, both descriptions, icon, feature
graphic, and screenshots from `screenshots-9x16/` or `framed/`.

---

## 9. Order to do them in

1. Privacy policy addendum via /admin/legal — **before** Data safety,
   so the two agree
2. Create the reviewer account on production, with real data in it
3. App content checklist (the nine items above)
4. Store listing
5. Upload the `.aab` to internal testing and install it yourself
6. Promote
