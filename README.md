# Gym Management System

A complete gym management application built with Django 5.2: members and memberships, attendance (manual and QR), payments and
receipts, personal-training appointments, workout plans and fitness progress, optional nutrition, equipment, inventory, expenses,
notifications and announcements, business reports, gym settings, an audit log and a member portal.

Stack: Python 3.10+, Django 5.2 LTS, Bootstrap 5, Bootstrap Icons and Chart.js (loaded from the jsDelivr CDN), SQLite for development,
PostgreSQL for production, WhiteNoise for static files, optional Redis/Celery.

| Area | What it does |
|---|---|
| Access | Six roles (Super Admin, Gym Manager, Receptionist, Trainer, Accountant, Member); permissions per role editable by the Super Admin; trainers see only their assigned members; members see only their own records |
| Members | Registration, profiles with photo, unique IDs, duplicate prevention, deactivation that keeps history, optional portal login |
| Memberships | Plans (monthly … custom, LKR), assignment, renewal without rewriting history, plan changes, suspension, cancellation, expiry tracking |
| Attendance | Front desk and QR check-in validated against membership status and plan access rules, duplicate prevention, history and reports |
| Payments | Append-only ledger, partial payments, refunds, adjustments, pending confirmation, receipts (HTML/PDF), one balance calculation |
| Training | Trainers, availability, assignments, session booking with conflict checks, workout plans, fitness progress, optional meal plans |
| Operations | Equipment and maintenance, inventory with stock ledger, expenses with private receipts, suppliers |
| Communication | Notification centre, de-duplicated reminders, announcements, optional e-mail/SMS/WhatsApp with honest delivery status |
| Insight | Role-aware dashboard, 14 CSV/PDF/printable reports, audit log |

## Quick start (local development)

```bash
python -m venv .venv
.venv\Scripts\activate                 # Windows   (Linux/macOS: source .venv/bin/activate)
pip install -r requirements.txt

copy .env.example .env                 # Linux/macOS: cp .env.example .env   – then set SECRET_KEY (command is in the file)
python manage.py migrate
python manage.py createsuperuser       # always gets the Super Admin role
python manage.py runserver
```

Open http://127.0.0.1:8000/ and sign in. Optional: `python manage.py seed_demo` adds demo members, payments, sessions, equipment and so on so you
can try every screen (it only adds data, never removes any, and refuses to run when `DEBUG=False`).

Password-reset e-mails are printed to the console by default; configure `EMAIL_*` in `.env` to send real mail.

### Running the tests

```bash
python manage.py test                    # about 490 tests, 1-2 minutes
python manage.py test apps.payments      # one module
python manage.py check --deploy          # production configuration check (with production .env values)
python manage.py makemigrations --check  # fails if a model change has no migration
```

Besides each module's unit tests, `apps/core` holds cross-cutting suites: `test_workflows.py` (eight end-to-end business workflows), `test_crawl.py`
(follows every link for every role, checks forms carry CSRF tokens and post to real URLs), `test_security.py` (direct URL access for every route, member
isolation, CSRF, upload handling, error leakage, audit of failed logins) and `test_performance.py` (query counts must not grow with the number of rows).

## Configuration (`.env`)

| Variable | Purpose |
|---|---|
| `SECRET_KEY` | **Required.** The app refuses to start without it. Use a long random value. |
| `DEBUG` | `True` for development only. Defaults to `False`. |
| `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS` | Comma-separated host names / `https://` origins. |
| `DB_ENGINE` | `sqlite` (default) or `postgresql`, with `DB_NAME/USER/PASSWORD/HOST/PORT` (+ optional `DB_SSLMODE`). `SQLITE_PATH` relocates the SQLite file. |
| `REDIS_URL` | Optional shared cache so login throttling works across several worker processes. |
| `SECURE_SSL_REDIRECT`, `SECURE_HSTS_PRELOAD`, `USE_X_FORWARDED_FOR` | HTTPS and proxy behaviour (see *Deployment*). |
| `EMAIL_*`, `DEFAULT_FROM_EMAIL` | SMTP settings for password reset and notifications. |
| `GYM_NAME`, `TIME_ZONE` | Initial gym name (edit under *Gym settings* afterwards) and time zone (default Asia/Colombo). |
| `NUTRITION_ENABLED` | `True` to enable the optional nutrition module (default off). |
| `INVENTORY_ENABLED` | `False` hides the optional inventory module and its report (default on). |
| `SMS_PROVIDER`, `WHATSAPP_PROVIDER` | `none` (default) or `twilio`. See *Notifications and external messaging*. |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_SMS_FROM`, `TWILIO_WHATSAPP_FROM`, `TWILIO_WEBHOOK_URL` | Twilio credentials / sender numbers / public callback URL. |
| `DEFAULT_PHONE_COUNTRY_CODE` | Prefix for local numbers that start with 0 (default `+94`). |
| `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND` | Optional Celery + Redis (see *Scheduled tasks*). |
| `LOG_LEVEL` | Console log level (default `INFO`). |

`deploy/env.production.example` lists every value to set for production.

## Deployment (Linux, PostgreSQL, nginx, gunicorn)

Example for Ubuntu with a `gym` service user; adapt paths as needed. Templates for every file mentioned are in `deploy/`.

1. **System packages:** `sudo apt install python3 python3-venv postgresql nginx redis-server certbot python3-certbot-nginx`
2. **Database:**
   ```bash
   sudo -u postgres psql -c "CREATE USER gym WITH PASSWORD 'choose-a-strong-password';"
   sudo -u postgres psql -c "CREATE DATABASE gym_system OWNER gym ENCODING 'UTF8';"
   ```
3. **Code and dependencies:**
   ```bash
   sudo useradd --system --create-home --home-dir /srv/gym gym
   sudo -u gym git clone <your repository> /srv/gym/app
   sudo -u gym python3 -m venv /srv/gym/venv
   sudo -u gym /srv/gym/venv/bin/pip install -r /srv/gym/app/requirements-prod.txt
   ```
4. **Configuration:** copy `deploy/env.production.example` to `/srv/gym/.env`, `chmod 600`, set `SECRET_KEY`, `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, the database and
   e-mail values. Secrets live only in this file / the environment – never in the repository.
5. **Initialise** (run from `/srv/gym/app` with the `.env` loaded, as the `gym` user):
   ```bash
   /srv/gym/venv/bin/python manage.py migrate
   /srv/gym/venv/bin/python manage.py collectstatic --noinput
   /srv/gym/venv/bin/python manage.py createsuperuser          # your Super Admin account
   /srv/gym/venv/bin/python manage.py check --deploy           # should report no problems except the optional HSTS-preload note
   ```
6. **Application service:** install `deploy/gym.service.example` as `/etc/systemd/system/gym.service`, then `sudo systemctl enable --now gym`.
7. **Web server and HTTPS:** install `deploy/nginx.conf.example` as the site, run `sudo certbot --nginx -d gym.example.com`, `sudo nginx -t && sudo systemctl reload nginx`.
   Do **not** map `private_media/` in nginx (certificates and expense receipts must only be served by the application).
8. **Scheduled notifications:** `0 * * * * gym cd /srv/gym/app && /srv/gym/venv/bin/python manage.py run_scheduled_notifications` (or Celery, see *Scheduled tasks*).
9. **Backups:** schedule `deploy/backup.sh` nightly and copy the output off the server (see *Backups*).
10. **Smoke test:** open `https://<host>/healthz/` (returns `{"status": "ok"}`), sign in as the Super Admin, open *Gym settings* and enter your gym's details, then create staff
    accounts under *Users* and review *Roles & permissions*.

What the application does automatically when `DEBUG=False`: redirects HTTP to HTTPS, sets secure/HttpOnly/SameSite cookies, sends HSTS (30 days), refuses unknown `Host`
headers, serves hashed static files through WhiteNoise, and shows generic error pages without technical details. Windows hosting works the same way with `waitress` or IIS
in place of gunicorn/nginx (gunicorn itself does not run on Windows).

### Upgrading
```bash
git pull
/srv/gym/venv/bin/pip install -r requirements-prod.txt
/srv/gym/venv/bin/python manage.py migrate        # migrations are additive and preserve existing data
/srv/gym/venv/bin/python manage.py collectstatic --noinput
sudo systemctl restart gym
```
Take a backup first. Every release's migrations were verified to preserve existing data (members, memberships, payments, attendance and audit history carry through all later schemas).

### Backups
`deploy/backup.sh` writes a PostgreSQL dump (or an SQLite online backup) and an archive of `media/` and `private_media/` to `/srv/gym/backups`, keeping 14 days. Backups contain
personal and financial data: store them encrypted and off-site, and **test a restore** on a spare machine before you need it (restore commands are at the end of the script).
Because payments, stock movements and audit entries are append-only, a restore from the most recent backup never leaves them inconsistent.

### Superuser and first users
`python manage.py createsuperuser` creates the Super Admin (the only role that can edit permissions and create Gym Manager accounts). Create everyone else in *Users*, then link member
logins from a member's profile (*Member portal access*) and trainer logins on the trainer's form.

## Security overview
* Server-side authorisation on every view (`GymPermissionMixin`); a site-wide login requirement (`LoginRequiredMiddleware`) with an explicit public allow-list; navigation hides what a role cannot use, but hiding is never the only control.
* Data isolation: trainers are limited to assigned members (members, attendance, memberships, workouts, progress, nutrition, sessions); members only reach `/me/...` and only their own rows.
* CSRF on every form (logout is POST-only); login throttling with lockout; generic login/reset messages; failed sign-ins and lockouts are written to the audit log (never the password).
* Passwords: Django's PBKDF2 hashing, 10-character minimum, common/numeric/similarity validators. Privilege-escalation guards on user management.
* Money: append-only payment ledger, idempotent submissions, row locking in transactions, refund/adjustment/void permissions, audit trail, no card data stored.
* Uploads: type and size checks by content (not just extension), random file names, images only as JPEG/PNG/WebP, certificates and receipts stored outside the public media folder and streamed only to authorised users.
* Production headers/cookies: HTTPS redirect, HSTS, secure + HttpOnly + SameSite cookies, `X-Frame-Options: DENY`, `nosniff`, strict referrer policy. Secrets only in environment variables.
* Known gaps (see *Limitations*): no Content-Security-Policy (templates use inline scripts), no multi-factor authentication.

## Limitations
* Static assets (Bootstrap, Bootstrap Icons, Chart.js, the Inter font, and the optional QR-camera library) load from CDNs; an offline installation needs them vendored into `static/`.
* Browser-based QR scanning needs HTTPS and camera permission; USB/Bluetooth scanners that type the code work everywhere.
* SMS/WhatsApp delivery is implemented for Twilio only; e-mail delivery can be reported as *sent* but never *delivered* (SMTP gives no receipt).
* PDF documents use a built-in Latin font; names in other scripts (e.g. Sinhala, Tamil) print incorrectly in PDFs (HTML views are fine).
* Row-level locking for simultaneous payments/bookings/stock moves is effective on PostgreSQL; SQLite serialises writers instead and is meant for development only.
* Photos and exercise images are served from `/media/` by the web server without a login (their names are random 128-bit values); private documents are never served that way.
* Everything is single-gym, single-currency (LKR), single-time-zone.

## Project layout

```
config/            settings, root URLs, wsgi/asgi
apps/accounts/     custom User (role), RolePermission, auth/user-management/permission views, forms, tests
apps/members/      Member model (profile, photo, unique member ID), list/register/detail/edit/deactivate
apps/memberships/  MembershipPlan, Membership (history), business rules in services.py, status queries
apps/attendance/   Attendance model, check-in rules (services.py), desk / QR scan / history / daily report
apps/payments/     append-only Payment ledger + PaymentAudit, billing.py (balances), services.py, receipts (HTML + PDF)
apps/trainers/     Trainer profiles, certificates (private files), assignments (history), availability, time off, compensation, access scoping
apps/appointments/ Appointment + event log, booking rules/slots/reminders (services.py), reminder command
apps/workouts/     Exercise library, workout plans (per-day exercises, change log), recorded workouts
apps/progress/     Body weight/height + configurable measurements, charts
apps/nutrition/    OPTIONAL meal plans and nutrition profiles (off unless NUTRITION_ENABLED=True)
apps/portal/       Member portal (/me/...): own trainers, workouts, sessions, progress, nutrition
apps/suppliers/    Shared supplier directory (equipment, inventory, expenses)
apps/expenses/     Expenses + categories, private receipts, void (never delete)
apps/equipment/    Equipment register, fault reports, maintenance/repair history, schedule
apps/inventory/    OPTIONAL stock items and append-only stock-movement ledger
apps/announcements/ Announcements with audience and publication window
apps/notifications/ Reminder periods, user preferences, external delivery log/providers, scheduled generators, Celery tasks
apps/reports/      Report engine (CSV / print / PDF), 14 reports, finance.py = the single set of money rules
apps/core/         dashboard, gym settings, audit log, notification centre, health check, activity log, template tags, demo seeder, cross-cutting test suites
deploy/            production templates: environment file, systemd unit, nginx site, backup script
templates/         base.html, base_auth.html, components/ (reusable UI), registration/, accounts/, core/, errors/
static/            css/app.css, js/app.js
```

## Roles and permissions

Roles: Super Admin, Gym Manager, Receptionist, Trainer, Accountant, Member.

* Every view requires login (Django's `LoginRequiredMiddleware`); only the login and password-reset pages are public.
* Authorisation is enforced server-side with `GymPermissionMixin` (`required_permission = "users.manage"`).
  Sidebar links are hidden using the same check (`{% if request.user|can:"users.view" %}`), but hiding is cosmetic only.
* Permissions are listed in `apps/accounts/permissions.py`. Which role holds which permission is stored in the
  database, seeded with sensible defaults, and editable by the Super Admin at **Roles & permissions**.
  Super Admin always has everything; `permissions.manage` cannot be granted to anyone else.
* **Privilege escalation guards:** only a Super Admin can create/edit Super Admins and Gym Managers, even if
  `users.manage` is granted to another role; nobody can change their own role or deactivate themselves;
  `is_staff`/`is_superuser` are derived from the role on every save, so they cannot be set through a form.
* **Members** have no dashboard or admin access: they see their own account page and can edit only their own
  contact details.
* Accounts are deactivated rather than deleted; deactivated users cannot sign in and existing sessions stop working.

To add a module in a later phase: add its permission codes to `PERMISSIONS`/`PERMISSION_GROUPS` (and defaults),
protect views with `GymPermissionMixin`, and add a sidebar link in `templates/base.html`.

## Members (Phase 2)

* Member IDs are generated as `GM000123` (prefix `MEMBER_ID_PREFIX`) from the primary key and never change.
* Required: name, phone, date of birth, emergency contact. Optional: email, address, photo (JPEG/PNG/WebP, 2 MB), fitness goals.
* Duplicates are blocked in the form **and** by database constraints: phone (compared on its last 9 digits, so
  `0771234567` equals `+94771234567`) and email (case-insensitive).
* Search by name, member ID, phone or email; filter by status and registration date; sortable columns; pagination.
* Members are deactivated, never deleted (deleting a member with memberships is blocked by `PROTECT`). Deactivation keeps
  every membership and record; it blocks new assignments/renewals until the member is reactivated.
* Member status shown everywhere is derived from memberships: Active, Suspended, Scheduled, Expired, No membership, Inactive.
* Payments / attendance / workouts panels on the profile are placeholders until those modules exist.
* Uploaded photos are served by Django only when `DEBUG=True`; in production serve `MEDIA_ROOT` from the web server.

## Membership plans and memberships (Phase 2)

Plans: monthly, 3-month, 6-month, annual or custom (number of days); price in LKR; benefits; access restrictions; active flag.
Only roles with `plans.price` (Gym Manager by default) can set or change a price; others can edit the rest of a plan and the
price field is ignored server-side. Deactivating a plan stops new assignments only. A price change never alters existing
memberships, because each membership stores a snapshot of the price it was sold at.

All rules live in `apps/memberships/services.py` (each runs in a transaction and locks the member row):

| Rule | Behaviour |
|---|---|
| Period | `start_date`..`end_date` inclusive. Month plans end the day before the next anniversary (1 Jan + 1 month → 31 Jan; 31 Jan → 27 Feb, day clamped to month length). Custom plans cover exactly N days including the first. |
| Expiry | Derived from dates (no scheduled job): expired when `end_date < today`. |
| New membership | Starts today up to `MEMBERSHIP_MAX_ADVANCE_START_DAYS` (60) ahead, never in the past; rejected if the member already has a current, scheduled or suspended membership; no overlapping periods. |
| Renewal | Never edits the old period; creates a new row. Renewed before expiry (or on the last day) → starts the day after the current one ends (no days lost). Renewed after expiry → starts today (no back-dating). One queued renewal at a time. Plan may differ; the price is the plan's price at renewal time. |
| Upgrade / downgrade | Only on a currently active membership. Old row becomes `superseded` (dates and price kept), a new full period on the new plan starts today. No proration; the price difference is settled in Phase 3. Blocked while a renewal is queued. |
| Suspension | Only while active. Reactivating adds the suspended days (capped at `MEMBERSHIP_MAX_SUSPENSION_DAYS`, 90) to the end date and shifts a queued renewal by the same amount. A suspended member has no access. |
| Cancellation | Needs a reason and the `memberships.cancel` permission (Gym Manager). Access ends immediately, the record stays; scheduled renewals can be cancelled; expired ones cannot. Refunds belong to Phase 3. |
| Constraints | DB checks: `end_date >= start_date`, non-negative prices, custom days only on custom plans, suspended rows carry a suspension date; unique plan names (case-insensitive). |

Pages: Members, Register/Edit member, Member profile, Membership plans (+ create/edit/detail), Memberships list,
Membership detail and history, Assign, Renew, Upgrade/downgrade, Suspend, Cancel, Expired, Expiring soon.
Permissions added: `plans.view/manage/price`, `memberships.view/manage/cancel` (editable per role by the Super Admin).

**Phase 3 integration:** payments should reference a `Membership` and use its stored `price`; a membership's
payment state should be derived from payments, not stored on the membership.

## Attendance (Phase 3)

* **Front desk** (`/attendance/`): search by member ID, name or phone; check in / out; see who is inside. **QR kiosk**
  (`/attendance/scan/`): a scanner or the browser camera submits the code; a scan checks the member in, scanning again
  checks them out. (Browser cameras need HTTPS in production.)
* **A check-in is accepted only if** the member is active, has a membership covering today (suspended, expired, scheduled,
  cancelled or missing memberships are refused with the reason), the plan's access rules allow this moment, and the member is not
  already inside. Optional: `ATTENDANCE_BLOCK_UNPAID=True` also refuses memberships with no payment recorded.
* **Access rules** are now structured on each plan: allowed weekdays and opening hours (evaluated in the gym's local time zone,
  same-day windows only). The free-text "access restrictions" remains as a note for staff.
* **Duplicate prevention** is enforced by the service *and* a partial unique index (one open visit per member). A forgotten
  check-out is closed automatically after `ATTENDANCE_MAX_SESSION_HOURS` (12) and flagged "auto".
* **QR codes** encode only `GYMQR:<256-bit random token>` – no name, phone or ID. The token is a credential: the QR image is served
  `no-store` to staff with `attendance.manage` only, the token is never put in a URL, "Reset code" invalidates a lost card, and
  unrecognised scans are rate-limited per staff user (`QR_SCAN_MAX_FAILURES` per `QR_SCAN_WINDOW_SECONDS`).
  Members see and download their own code under **My QR code** in the portal (`/me/qr/`); staff open it from the member's profile.
  A second scan within `ATTENDANCE_SCAN_COOLDOWN_SECONDS` (60) of a check-in is rejected as accidental instead of checking the member out.
  `python manage.py generate_member_qrs` (`--dry-run` to preview) gives a token to any member that lacks one and never changes existing codes.
* **Reports:** attendance history (search, date range, method, sorting, pagination), per-member history with the same filters,
  and a daily report (totals, unique members, QR share, average visit, check-ins by hour). The dashboard shows today's check-ins,
  who is inside now and a 7-day chart.

## Payments, billing and receipts (Phase 3)

Every money movement is a row in an **append-only ledger** (`Payment`): `payment` (money in), `refund` (money out, linked to the
payment it reverses) or `adjustment` (staff-approved change to what is owed; negative = discount). Rows are never edited or
deleted – only a status can change (pending → completed/failed, completed/pending → voided), through services that write an
immutable `PaymentAudit` row (who, what, old/new status) and an activity-log entry. The Django admin shows these tables read-only.

**Balances** (one implementation, `apps/payments/billing.py`, used by every page, receipt and the dashboard):

    amount due  = membership price (snapshot) + completed adjustments
    net paid    = completed payments − completed refunds
    outstanding = max(amount due − net paid, 0)      (0 for cancelled memberships: cancelling writes off what was unpaid)

Status: *Unpaid*, *Partially paid*, *Paid*, *Partially refunded*, *Refunded*; a payment itself is *Pending confirmation*, *Completed*,
*Failed* or *Voided*. Pending, failed and voided entries never affect a balance. A pending amount also reserves part of the
payable balance, so the same transfer cannot be recorded twice.

* **Recording:** cash, bank transfer or card; payment date (not future, at most `PAYMENT_MAX_BACKDATE_DAYS` back); optional
  bank/terminal reference; amount may not exceed the balance still payable. Bank/card payments can be recorded as *awaiting
  confirmation* and later confirmed or rejected by staff.
* **Unique references:** `TXN-YYYYMMDD-XXXXXXXX` (random) and receipt numbers `RC-<year>-<id>` / `RF-<year>-<id>` for refunds.
* **Duplicate submissions:** every form carries a one-time key backed by a unique constraint; a double click or resubmit returns
  the original entry. Each operation locks the membership row inside a transaction (PostgreSQL row locks) and re-reads the
  balance, so concurrent requests cannot overpay; a failure rolls back the payment *and* its audit row together.
* **Refunds** (`payments.refund`): need a reason, are capped at what remains refundable on the original payment, and leave the
  original untouched. **Adjustments / voids** (`payments.adjust`): need a reason; an adjustment cannot make the amount due negative or
  fall below what is already paid (refund first). Defaults: Gym Manager and Accountant; Receptionists can record and confirm payments only.
* **Card safety:** no card numbers, expiry dates or CVV are collected or stored. The reference field rejects values that look like
  a card number (13–19 digits passing the Luhn check). Only a terminal approval code or bank slip number is stored.
* **Online payments:** not integrated. If a gateway is added later, a payment must only become *completed* after the provider's
  server-side verification (webhook or API lookup) – never from a browser redirect. The *pending* status and `confirm_payment()`
  exist for that purpose today; the only caller is an authenticated staff action.
* **Receipts:** printable page and PDF download (`payments.view`) with receipt number, member, membership, amount due, amount paid,
  balance, method, date and transaction reference. Balances are shown as at that receipt. Pending/failed/voided entries print with
  a "NOT A VALID RECEIPT" notice. PDF downloads are logged and sent `no-store`. The built-in PDF font covers Latin text only.
* **Pages:** Payments list (search by receipt, reference, member name/ID/phone, bank reference; filter by type, method, status, dates;
  sort; paginate; received / refunded / net totals), Outstanding balances, Payment detail with audit trail, Record payment,
  Refund, Adjust, Void, Reject. Payment history also appears on the membership and member pages.

**Dashboard finance:** *Revenue collected* = completed payments − refunds by payment date (today / this month / 6-month chart).
*Outstanding (unpaid fees)* is shown separately because it is money owed, not revenue. Also: membership payment status
breakdown (unpaid / partial / paid), amount awaiting confirmation, and recent payments.

**New permissions:** `payments.refund`, `payments.adjust` (plus existing `payments.view/manage`, `attendance.view/manage`).

## Trainers, appointments, workouts, progress and nutrition (Phase 4)

**Who sees what.** Permissions decide which *modules* a role may use (`trainers.*`, `appointments.*`, `workouts.*`,
`progress.*`, `nutrition.*`, editable by the Super Admin). On top of that, the **Trainer** role is limited to *its own*
data: members with an open assignment to that trainer (members list/profile, workout plans, progress, meal plans) and the
trainer's own appointments. Other staff roles holding a permission see everyone. Ending an assignment withdraws a trainer's access
immediately; the assignment row stays as training history. Members only ever reach their own records (`/me/...`): every portal query
is filtered on the signed-in member's record, so another member's plan, session or progress id is a plain 404.

**Trainers** – profile (contact, qualifications, specialties, employment status, session rate), photo, weekly availability windows,
time off, certificates, assignments (a member may have several trainers; no duplicate open pair) and training history.
Certificates are stored **outside** the public media folder (`PRIVATE_MEDIA_ROOT`) and streamed only to `trainers.manage` users
(PDF/JPEG/PNG up to 5 MB, content-checked, downloads are logged). Salary/commission records (`trainers.compensation`, Gym Manager and
Accountant by default) are append-only and hidden from everyone else. Marking a trainer as "left" ends their assignments and cancels
their upcoming sessions (history kept).

**Appointments** – every booking is validated on the server inside a transaction that locks the trainer and the member:
allowed length (`APPOINTMENT_DURATIONS`), start on the `APPOINTMENT_SLOT_STEP_MINUTES` grid, within the booking horizon, trainer employed
and inside a weekly availability window and not on leave, no overlap for the trainer *or* the member, membership covering the day
(`APPOINTMENT_REQUIRE_MEMBERSHIP`), and for self-booking members: an assigned trainer and `APPOINTMENT_MIN_LEAD_MINUTES` notice.
A partial unique index (trainer, start) for scheduled sessions is the last line of defence if two requests race. Members may
reschedule/cancel with `APPOINTMENT_CHANGE_NOTICE_HOURS` notice (staff may always). Rescheduling edits the same row (a payment stays
attached) and every change is written to the appointment's event log. Outcomes: completed / no-show (only after the start).
**Reminders:** run `python manage.py send_appointment_reminders` every 15–60 minutes from cron / Windows Task Scheduler. It creates an
in-app notification for the member and trainer (and an e-mail if the member has an address) once per session; rescheduling re-arms it.
Booking, rescheduling and cancelling also notify the other party. Notifications appear under the bell icon and at `/notifications/`.

**Session fees use the existing payment ledger – nothing is duplicated.** A trainer's rate × length is copied onto the appointment as its
fee. A `Payment` row now points to *either* a membership *or* an appointment (a database constraint enforces exactly one), so payments,
refunds, adjustments, pending confirmation, idempotency keys, audit trail and receipts work unchanged for sessions. Balances come from
the same `billing.py` function. A cancelled session has no outstanding balance (an existing payment is refunded manually from Payments).
Unpaid session fees count towards "Outstanding" and appear on the Outstanding page; they are never revenue until paid.

**Workouts** – exercise library (muscle group, category, instructions, image, https video link), plans per member with start/end dates and
status, exercises organised by weekday with sets / reps / weight / duration / rest, copy a plan to another member, and recorded workouts.
*History is preserved*: a recorded workout stores the numbers actually done (a snapshot), exercises that already have recorded workouts are
archived instead of deleted, and every change to a plan is logged. Staff can record a workout on a member's behalf; members can record their own.

**Progress** – dated records of body weight, height and configurable measurements (Body fat, Waist, Chest, Hips, Arm, Thigh are seeded; staff can
add more). Members add their own; trainers add/view for assigned members. Charts (Chart.js) for weight and measurements and a history table. BMI
is shown only as an indicative ratio with a disclaimer – no "underweight/overweight" labels or other automated judgements.

**Nutrition (optional)** – set `NUTRITION_ENABLED=True` to turn it on. When off, every nutrition URL answers 404 and no menu entry is shown; the
tables still exist so it can be enabled later. Meal plans per member organised by day and meal type with descriptions and ingredients, plus a
nutrition profile (food preferences, allergies, general goals) that members can edit themselves. Allergies are highlighted on the plan. No
calorie or nutrient calculations are produced, and a "general guidance, not medical advice" notice is shown on every page.

**Member portal** – a member's login (role *Member*) must be linked to their member record. Staff with `users.manage` do this from the member
profile (*Member portal access*): create a login (a password-setup email is sent) or link/unlink an existing account. An unlinked member sees a
friendly "not linked yet" page. Portal pages: account summary, my trainers, workout plans (+ record a workout), sessions (book from free
slots, reschedule, cancel), progress (add + charts), nutrition (if enabled) and notifications.

**Dashboard** – every block is now shown only to roles holding the matching permission (a trainer no longer sees revenue). New figures: active
trainers, upcoming and completed sessions, active workout plans – for trainers these are limited to their own sessions/assigned members, and
gym-wide member counts and the expiring-members list are hidden from the Trainer role.

## Equipment, inventory, expenses, announcements and notifications (Phase 5)

**Equipment** – register with a unique code (blank = `EQ-0001`…), category, purchase date/cost, supplier, warranty, location, condition and
status (*Available, In use, Under maintenance, Out of service*). Anyone who can view equipment (including trainers) can **report a fault**; a
*major* fault puts the item out of service immediately and notifies maintenance staff. **Recording maintenance** (`equipment.manage`) logs a service,
inspection or repair with cost, technician and supplier, sets the last/next maintenance dates (explicit date, else the item's service interval),
optionally returns the item to service and resolves the fault, all in one transaction. Maintenance history is permanent. Reminders for upcoming and
overdue maintenance go to maintenance staff (see below). A cost can create **one** linked *Maintenance* expense; the equipment purchase cost can
optionally create one *Equipment* expense at registration.

**Inventory (optional)** – items with unique SKU (blank = `SKU-00001`…), category, unit, minimum level and supplier. Quantity changes **only** through
stock movements (opening stock, purchase, usage/sale, counted adjustment with a reason). Each movement stores the signed change and the quantity after,
rows cannot be edited or deleted, an item row is locked while it changes, and stock can never go negative (service check plus a database constraint).
A purchase can create one linked *Inventory & supplies* expense. Low-stock notifications go to inventory managers **once per running-low episode**
(a restock starts a new episode). Set `INVENTORY_ENABLED=False` to switch the module off.

**Expenses** – categories (Rent, Electricity, Water, Salaries, Equipment, Maintenance, Cleaning supplies, Inventory & supplies, Marketing, Other; you can add
more), amount, date, description, supplier, reference, private receipt (PDF/JPEG/PNG ≤ 5 MB, stored outside the public media folder, downloads logged),
and the user who entered it. Search by text/supplier/category/dates, sort, paginate. Edits log the old and new amount; mistakes are **voided** with a
reason (`expenses.void`, Gym Manager) and excluded from totals – expenses are never deleted. Expense data needs `expenses.view`/`expenses.manage`
(Gym Manager and Accountant by default) and is invisible to everyone else, including on the dashboard and in reports.

### Financial rules (one definition, `apps/reports/finance.py`)

| Term | Meaning |
|---|---|
| **Invoiced** | Fees billed in the period: memberships *starting* in it + training-session fees for sessions in it + completed adjustments. Cancelled items are not invoiced. |
| **Collected** | Completed payments by payment date. Pending, failed and voided entries never count. |
| **Refunds** | Completed refunds by payment date. |
| **Revenue** | Collected − refunds (“net collected”). A payment is one ledger row, so it is counted once; the fee it pays is never added on top. |
| **Outstanding** | What members still owe *now* on non-cancelled memberships and sessions. A receivable – not revenue – and independent of the report period. |
| **Operating expenses** | Non-void `Expense` rows by expense date. Maintenance records and stock purchases create *one* linked expense; their own cost fields are informational and not added again. Equipment purchase cost counts only if recorded as an expense. |
| **Estimated operating profit** | Revenue − operating expenses for the same period (cash basis). It ignores unpaid fees, depreciation, tax and trainer compensation records unless the pay was entered as a *Salaries* expense. |

### Notifications and external messaging

The **notification centre** (bell icon, `/notifications/`) always works: history, read/unread, filters by type, and for each notification the status of any
external copies. Notifications are generated for: memberships expiring soon (member + staff digest), expired memberships, payment due / overdue,
successful payment recording and confirmation, membership start and renewal, upcoming training sessions, appointment changes (booked / rescheduled /
cancelled), low inventory, equipment faults and maintenance due, and gym announcements. **Reminder periods are configurable** at *Notification settings*
(Gym Manager): days before membership expiry (default 14,7,1), payment due grace and offsets (default due 7 days after start; reminders 3 days before, on the
day, 3 and 7 days overdue), appointment reminder hours (24), maintenance reminder days (7,1,0). Each reminder has a deterministic *dedupe key* (e.g.
`membership-expiring:42:7`) with a database unique constraint per user, so repeated, overlapping or retried runs **never send the same reminder twice**; if a
run is missed the next run sends only the most urgent period that is due.

**E-mail, SMS and WhatsApp are optional and need external credentials.** With none configured nothing external is attempted and the app is unaffected.
Each external copy is tracked honestly (shown under the notification and in *Notification settings*):

* `queued` – waiting for a delivery run; `sent` – the provider *accepted* it (SMTP server / Twilio API) – **not proof it arrived**;
  `delivered` – set **only** when the provider confirms it; `failed` – rejected or all 3 attempts failed; `skipped` – not attempted (e.g. no address).
* **E-mail**: set `EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend` and the `EMAIL_*` settings. E-mail can only reach `sent` (SMTP gives no delivery receipt).
  The development console backend never creates deliveries.
* **SMS / WhatsApp via Twilio**: set `SMS_PROVIDER=twilio` and/or `WHATSAPP_PROVIDER=twilio`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, and `TWILIO_SMS_FROM` /
  `TWILIO_WHATSAPP_FROM`. For delivery confirmation expose `https://<your-host>/notifications/webhooks/twilio/` publicly and set it as `TWILIO_WEBHOOK_URL`; callbacks are
  accepted only with a valid `X-Twilio-Signature`. WhatsApp needs an approved sender/template under Twilio's rules. Without the callback URL messages stay at `sent`.
* Consent: the business switches each channel on in *Notification settings*, and every user opts in to SMS/WhatsApp (and can turn e-mail off) under *Preferences*.
  Only selected reminder types (membership, payment, appointment, announcement) are copied externally.

### Scheduled tasks (cron, or optionally Celery + Redis)

All recurring work is one idempotent job; run it hourly (or at least daily):

    python manage.py run_scheduled_notifications

It publishes due announcements, creates membership/payment/appointment/maintenance/low-stock notifications and attempts queued external deliveries. Use cron
(`0 * * * * cd /srv/gym && .venv/bin/python manage.py run_scheduled_notifications`) or Windows Task Scheduler. `send_appointment_reminders` still exists for session reminders alone.

*Optional Celery:* the same jobs are registered as Celery tasks (`notifications.run_scheduled` hourly, `notifications.deliver_queued` every 10 minutes, see `config/celery.py`).

    pip install -r requirements-celery.txt          # celery + redis client
    # .env:  CELERY_BROKER_URL=redis://localhost:6379/0   (optionally CELERY_RESULT_BACKEND)
    redis-server                                    # or a managed Redis
    celery -A config worker -l info
    celery -A config beat -l info                   # the scheduler

Run **either** cron **or** Celery beat; running both is harmless (duplicates are impossible) but unnecessary. Celery is not required for any feature.

**Announcements** – staff with `announcements.manage` write a notice (information, *gym closure*, operational update, offer), choose an audience (everyone, all members,
active-membership members, expired members, all staff, trainers), set publication and expiry times, and can edit or deactivate it. Live announcements appear as a banner on the
dashboard / member portal; if “also notify” is ticked the audience gets a notification (and e-mail/SMS/WhatsApp copies where configured) once at publication – editing later does not
re-send. Members without a portal login cannot be reached by in-app notification (create a login from the member profile).

### Business reports (`/reports/`)

Fourteen reports, all computed from live records: member registrations; active/expired memberships; renewals; plan popularity; daily/monthly attendance; member attendance
history; daily/monthly payments; outstanding balances; revenue collected (invoiced vs collected vs refunds vs outstanding, by method); expenses (by category); revenue vs
expenses (monthly, estimated profit); training appointments; equipment maintenance; inventory & low stock. Every report has date-range filters (where meaningful), search within
results, pagination, a **printable view**, **CSV** (UTF-8 with BOM, spreadsheet-formula injection neutralised) and **PDF** export; exports include every row, not just the visible
page, and are logged. Access needs `reports.view` **plus** the permission of the data shown (e.g. `expenses.view`), so a role without expense access cannot read expense figures.

## Dashboard

`apps/core/stats.py` computes every figure from the database (no placeholders), including training statistics: member counts, new registrations, upcoming
expirations (next `MEMBERSHIP_EXPIRING_SOON_DAYS`, default 14, excluding already-renewed members), attendance, revenue collected,
outstanding balances and recent payments. Money is shown in LKR.

## Security summary

CSRF on all forms (logout is POST-only), server-side validation via Django forms and password validators
(min. 10 chars), login throttling (5 failures per username+IP → 15 min lockout, cache-based), generic login and
password-reset messages (no account enumeration), HttpOnly/SameSite cookies, clickjacking and content-type
protection, custom 403/404/500 pages with no technical detail, and all secrets in the environment.

Note: login throttling uses Django's default per-process cache. With several worker processes configure a shared
cache (Redis/Memcached) so counters are shared.
