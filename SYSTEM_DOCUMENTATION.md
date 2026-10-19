# Gym Management System – System Documentation

This document describes what the system **actually does today**, based on the source code in this repository. Routes, model names, permission codes and management commands were read from the code (URL table and model list were generated from the running project). Where something could not be fully verified, or is not implemented, it says so.

**Status legend**

| Mark | Meaning |
|---|---|
| ✅ Implemented | Present in the code and covered by automated tests |
| ◐ Partial | Present, but with limits or not verifiable without outside services |
| ⏳ Not implemented | Mentioned only to avoid confusion; **not** part of the system |

---

## Table of contents

1. [System overview](#1-system-overview)
2. [User roles and permissions](#2-user-roles-and-permissions)
3. [Authentication and user management](#3-authentication-and-user-management)
4. [Dashboard](#4-dashboard)
5. [Member management](#5-member-management)
6. [Membership plans and memberships](#6-membership-plans-and-memberships)
7. [Trainer management](#7-trainer-management)
8. [Workout plans](#8-workout-plans)
9. [Workout session tracking (guided workouts)](#9-workout-session-tracking-guided-workouts)
10. [QR code attendance](#10-qr-code-attendance)
11. [Payments and transactions](#11-payments-and-transactions)
12. [Reports and analytics](#12-reports-and-analytics)
13. [System settings and customization](#13-system-settings-and-customization)
14. [Notifications and other modules](#14-notifications-and-other-modules)
15. [Database structure](#15-database-structure)
16. [Installation and configuration](#16-installation-and-configuration)
17. [Sample data management](#17-sample-data-management)
18. [Testing and troubleshooting](#18-testing-and-troubleshooting)
19. [Implemented, partial and not implemented – summary](#19-implemented-partial-and-not-implemented--summary)

---

## 1. System overview

**Purpose.** A single-gym management application: it registers members, sells and tracks memberships, records attendance (front desk and QR), takes payments and issues receipts, manages trainers and personal-training sessions, workout plans and fitness progress, equipment, inventory and expenses, sends notifications, produces reports, and gives members a self-service portal.

**Architecture.** A server-rendered Django application (monolith). Business rules live in service modules (`services.py`, `runs.py`, `billing.py`, `finance.py`) that run inside database transactions; views are thin. Templates use Bootstrap 5. There is no separate API: all pages are HTML, with a handful of small JSON endpoints (booking slots, the theme preview, the health check).

**Technologies**

| Area | Technology |
|---|---|
| Language / framework | Python 3.10+, Django 5.2 (LTS) |
| Database | SQLite (development default), PostgreSQL (production, via `psycopg`) |
| Front end | Django templates, Bootstrap 5.3 and Bootstrap Icons (CDN), Chart.js 4 (CDN, only on chart pages), Inter font (Google Fonts) |
| Static files | WhiteNoise (hashed static files when `DEBUG=False`) |
| QR codes | `qrcode` + Pillow |
| PDF | `reportlab` (receipts and report PDFs) |
| Optional | Redis (shared cache for login throttling), Celery + Redis (scheduled jobs), Twilio (SMS / WhatsApp) |
| Production serving | gunicorn behind nginx (templates in `deploy/`) |

**Project structure**

```
config/              settings.py, urls.py, wsgi/asgi, celery.py
apps/accounts/       custom User (roles), RolePermission, login/logout/password flows, user management, throttle
apps/core/           dashboard, gym settings, customization (Appearance), audit log, notification centre,
                     health check, sample-data commands, shared test suites
apps/members/        Member records, portal-login linking, QR token, generate_member_qrs
apps/memberships/    MembershipPlan, Membership, lifecycle rules (services.py)
apps/attendance/     Attendance, desk / QR scan / history / daily report, QR cards
apps/payments/       Payment ledger, PaymentAudit, billing.py, receipts (HTML/PDF)
apps/trainers/       Trainer, assignments, availability, time off, certificates, compensation, access scoping
apps/appointments/   Personal-training sessions, booking rules, reminders
apps/workouts/       Exercises, workout plans, recorded workouts, guided workout runs (runs.py)
apps/progress/       Body weight/height and measurements
apps/nutrition/      OPTIONAL meal plans and nutrition profiles
apps/portal/         Member portal (/me/...)
apps/suppliers/ expenses/ equipment/ inventory/   Operations modules
apps/announcements/  Announcements with audience and publication window
apps/notifications/  Reminder settings, preferences, external delivery log and providers, scheduled generators
apps/reports/        Report engine (HTML / print / CSV / PDF), 14 reports, finance.py (money rules)
templates/  static/  deploy/  requirements*.txt  .env.example  README.md  SAMPLE_DATA_GUIDE.md
```

**Cross-module relationships (summary)**

* A **Member** has memberships, attendance, payments (through memberships/appointments), workout plans/runs, progress, meal plans, appointments and optionally a login (`User`).
* A **Payment** always points to exactly one of a **Membership** or an **Appointment**; balances come from one function (`billing.with_billing`).
* **Notifications** are produced by memberships, payments, appointments, equipment, inventory and announcements, de-duplicated by key.
* **Reports** read the same data through `finance.py` so every screen uses one definition of revenue, outstanding and expenses.

Status: ✅ Implemented.

---

## 2. User roles and permissions

### 2.1 Roles that exist

Defined in `apps/accounts/models.py` (`Role`): **Super Admin**, **Gym Manager**, **Receptionist**, **Trainer**, **Accountant**, **Member**. There is no other role (for example no separate "administrator" besides Super Admin and Gym Manager).

| Role | Typical use |
|---|---|
| Super Admin | Owner/IT. Always holds every permission; the only role that can edit the role/permission matrix, create other Super Admins/Gym Managers and use the Django admin. |
| Gym Manager | Day-to-day management (broad default permissions, see below). |
| Receptionist | Front desk: members, memberships, check-in, payments, bookings. |
| Trainer | Own assigned members, workouts, progress, nutrition, own appointments. |
| Accountant | Payments, refunds/adjustments, expenses, suppliers, financial reports. |
| Member | No staff access; uses the member portal (`/me/`). |

### 2.2 How permissions work

* Every page requires login (`LoginRequiredMiddleware`); only the sign-in and password-reset pages are public (plus `/healthz/`).
* Permission codes are listed in `apps/accounts/permissions.py`. Which role holds which code is stored in the database (`RolePermission`), seeded with defaults by migrations, and editable at **Roles & permissions** (`/accounts/permissions/`).
* Views declare `required_permission` through `GymPermissionMixin`. Hiding a sidebar link is cosmetic; the server still enforces the permission.
* `permissions.manage` is Super-Admin-only and cannot be granted.
* On top of permissions, the **Trainer** role is scoped to its assigned members and own appointments (`apps/trainers/access.py`); members only reach their own records through `/me/`.

### 2.3 Default permissions (from the code)

✔ = granted by default. Super Admin holds all. Members hold none (they use the portal).

| Permission | Manager | Receptionist | Trainer | Accountant |
|---|:-:|:-:|:-:|:-:|
| dashboard.view | ✔ | ✔ | ✔ | ✔ |
| users.view / users.manage | ✔ | | | |
| members.view | ✔ | ✔ | ✔ | ✔ |
| members.manage | ✔ | ✔ | | |
| plans.view | ✔ | ✔ | ✔ | ✔ |
| plans.manage / plans.price | ✔ | | | |
| memberships.view | ✔ | ✔ | ✔ | ✔ |
| memberships.manage | ✔ | ✔ | | |
| memberships.cancel | ✔ | | | |
| attendance.view | ✔ | ✔ | ✔ | |
| attendance.manage | ✔ | ✔ | | |
| payments.view / payments.manage | ✔ | ✔ | | ✔ |
| payments.refund / payments.adjust | ✔ | | | ✔ |
| reports.view | ✔ | | | ✔ |
| trainers.view | ✔ | ✔ | ✔ | ✔ |
| trainers.manage | ✔ | | | |
| trainers.compensation | ✔ | | | ✔ |
| appointments.view / appointments.manage | ✔ | ✔ | ✔ | |
| workouts.view / workouts.manage | ✔ | | ✔ | |
| progress.view / progress.manage | ✔ | | ✔ | |
| nutrition.view / nutrition.manage | ✔ | | ✔ | |
| suppliers.view | ✔ | ✔ | | ✔ |
| suppliers.manage | ✔ | | | ✔ |
| equipment.view | ✔ | ✔ | ✔ | ✔ |
| equipment.manage | ✔ | | | |
| inventory.view | ✔ | ✔ | | ✔ |
| inventory.manage | ✔ | ✔ | | |
| expenses.view / expenses.manage | ✔ | | | ✔ |
| expenses.void | ✔ | | | |
| announcements.manage | ✔ | | | |
| notifications.configure | ✔ | | | |
| settings.manage (gym settings) | ✔ | | | |
| settings.appearance (customization) | – | – | – | – |
| audit.view | ✔ | | | |
| permissions.manage | Super Admin only | | | |

`settings.appearance` is held by the Super Admin only until another role is granted it in Roles & permissions.

### 2.4 Escalation guards

Only a Super Admin can create or edit Super Admins and Gym Managers; nobody can change their own role or deactivate themselves; `is_staff` / `is_superuser` are derived from the role on every save and cannot be set through a form. `is_staff` (Django admin access) is true only for Super Admins.

Status: ✅ Implemented.

---

## 3. Authentication and user management

| Feature | URL | Who | Notes |
|---|---|---|---|
| Sign in | `/accounts/login/` | Anyone | Generic error message ("Invalid username or password"); throttled lockout after 5 failures for 15 minutes (`LOGIN_MAX_ATTEMPTS`, `LOGIN_LOCKOUT_SECONDS`); failed and locked attempts are written to the audit log (never the password). |
| Sign out | `/accounts/logout/` | Signed-in | POST only. |
| Password change | `/accounts/password/change/` | Signed-in | |
| Password reset | `/accounts/password/reset/` (+ `sent/`, `<uidb64>/<token>/`, `complete/`) | Anyone | Link valid 3 hours; same response whether or not the address exists. E-mail goes to the console unless SMTP is configured. |
| My profile | `/accounts/profile/` | Signed-in | A user can edit only first/last name, e-mail and phone – never role or status. |
| Users | `/accounts/users/` (+ `new/`, `<id>/edit/`, `<id>/toggle-active/`) | `users.view` / `users.manage` | List, create, edit, deactivate/reactivate. Accounts are deactivated, never deleted. |
| Roles & permissions | `/accounts/permissions/` | Super Admin | Permission matrix per role. |
| Django admin | `/django-admin/` | Super Admin (is_staff) | Registered for accounts, members, memberships, payments, trainers, attendance, appointments; ledgers are read-only. |

**Registration.** There is **no public self-registration**. Staff create user accounts (Users) and member records (Members). A member's login is created or linked from the member profile (*Member portal access*, `/members/<id>/portal-access/`, permission `users.manage`); a password-setup e-mail is sent when a login is created.

**Passwords.** Django's PBKDF2 hashing; validators: minimum 10 characters, not too similar to user data, not a common password, not entirely numeric. Sessions last 12 hours (`SESSION_COOKIE_AGE`); cookies are HttpOnly and SameSite=Lax (secure flag and HSTS when `DEBUG=False`).

**Access control summary.** Login required everywhere; permission check per view; trainer data scoping; member isolation (any id not belonging to the signed-in member returns 404 in `/me/...`).

Status: ✅ Implemented. Multi-factor authentication: ⏳ not implemented.

---

## 4. Dashboard

**URL** `/` (`core:home`). Staff with `dashboard.view` see the dashboard; Members are redirected to the portal (`/me/`). Each block appears only to roles holding the matching permission (for example a Trainer sees no revenue, and sees only own sessions / assigned members).

**Statistic tiles (role-dependent):** total members, active members, expired memberships, new members this month, today's attendance (and how many are inside now), revenue collected today and this month, outstanding (unpaid) fees, active trainers, upcoming sessions, completed sessions, active workout plans.

**Quick actions:** Check-in desk, Book session, Register member (shown to roles that may use them).

**Charts and panels:** revenue collected – last 6 months; attendance – last 7 days; revenue vs expenses and expenses by category (with expense permission); equipment status; maintenance due soon; low stock; recent payments; membership payment status breakdown (unpaid / partial / paid) and amount awaiting confirmation; upcoming membership expirations; recent activities; active accounts by role (with `users.view`). Announcements appear as a banner.

Charts use Chart.js (loaded from a CDN) with animation off and colours taken from the saved theme. Tile layout (three or four per row on wide screens) is a customization option.

Status: ✅ Implemented.

---

## 5. Member management

**Who:** `members.view` to see, `members.manage` to create/edit/deactivate. Trainers see only members with an open assignment to them.

| Page | URL |
|---|---|
| Members list | `/members/` |
| Register member | `/members/new/` |
| Member profile | `/members/<id>/` |
| Edit | `/members/<id>/edit/` |
| Deactivate / reactivate | `/members/<id>/toggle-active/` (POST) |
| Portal access (create/link login) | `/members/<id>/portal-access/` |
| Assign membership | `/members/<id>/assign/` |

**Record (`members.Member`):** member ID (`GM000123`, prefix `MEMBER_ID_PREFIX`, generated from the primary key, never changes), full name, phone, e-mail, address, date of birth, emergency contact, registration date, photo, fitness goals, `qr_token` (private check-in token), optional `user` login (one-to-one), `is_active`.

**Rules**
* Required: name, phone, date of birth, emergency contact. Optional: e-mail, address, photo (JPEG/PNG/WebP, 2 MB), fitness goals.
* Duplicate phone (compared on its last 9 digits) and duplicate e-mail (case-insensitive) are blocked by the form **and** database constraints.
* Search by name, member ID, phone or e-mail; filter by status and registration date; sortable columns; pagination.
* Members are deactivated, never deleted; deactivation keeps all history and blocks new assignments, renewals and check-ins.
* The status shown everywhere is derived from memberships: Active, Suspended, Scheduled, Expired, No membership, Inactive.
* The profile shows contact details, goals, current membership, membership history, payments, attendance (with link to full history and the QR card), and – for those allowed – training information.
* A new member automatically receives a unique QR token (see §10).

**Relationships:** Member → memberships, attendance, workout plans/sessions/runs, progress, meal plans, appointments, trainer assignments, notifications (through the linked login).

Status: ✅ Implemented.

---

## 6. Membership plans and memberships

### 6.1 Plans

**URLs** `/plans/`, `/plans/new/`, `/plans/<id>/`, `/plans/<id>/edit/`, `/plans/<id>/toggle-active/`. **Who:** `plans.view` to see; `plans.manage` to create/edit; `plans.price` to set or change a price (others can edit the rest and the price field is ignored server-side).

**Fields (`MembershipPlan`):** name (unique, case-insensitive), description, duration type (monthly, quarterly, semiannual, annual, custom with `custom_days`), price (LKR), benefits, free-text access restrictions, structured access rules (`allowed_weekdays`, `access_from`, `access_until`, evaluated in the gym's time zone), active flag. Deactivating a plan stops new assignments only. A price change never alters existing memberships because each membership stores a price snapshot.

### 6.2 Memberships

**URLs** `/memberships/` (list), `/memberships/<id>/` (detail and history), `/memberships/expiring/`, `/memberships/expired/`, `/members/<member_id>/assign/`, `/memberships/<id>/renew/`, `/change/` (upgrade/downgrade), `/suspend/`, `/reactivate/`, `/cancel/`. **Who:** `memberships.view`; actions need `memberships.manage`; cancellation needs `memberships.cancel`.

All rules are in `apps/memberships/services.py`, each in a transaction that locks the member row:

| Rule | Behaviour |
|---|---|
| Period | `start_date`..`end_date` inclusive; month-based plans end the day before the next anniversary (day clamped to month length); custom plans cover exactly N days. |
| Expiry | Derived from dates (no scheduled job): expired when `end_date < today`. "Expiring soon" window: `MEMBERSHIP_EXPIRING_SOON_DAYS` (14). |
| New membership | Starts today up to 60 days ahead (`MEMBERSHIP_MAX_ADVANCE_START_DAYS`), never in the past; refused if the member already has a current, scheduled or suspended membership. |
| Renewal | Creates a new row (history never edited). Before/at expiry it starts the day after the current one ends; after expiry it starts today. One queued renewal at a time. |
| Upgrade / downgrade | Only on a currently active membership: the old row becomes `superseded`, a new full period starts today; no proration. |
| Suspension | Only while active; reactivating adds the suspended days (capped at 90, `MEMBERSHIP_MAX_SUSPENSION_DAYS`) to the end date. A suspended member has no access. |
| Cancellation | Needs a reason; access ends immediately; the record stays; unpaid amounts are written off. |

Statuses: `active`, `suspended`, `cancelled`, `superseded` (plus derived Scheduled/Expired). Kinds: `new`, `renewal`, `upgrade`, `downgrade`, `change`.

**Relationships:** Membership → Member, Plan; payments and attendance reference the membership; the attendance check uses its plan's access rules.

Status: ✅ Implemented.

---

## 7. Trainer management

**URLs** `/trainers/`, `/trainers/new/`, `/trainers/<id>/`, `/trainers/<id>/edit/`, `/trainers/my-members/` (Trainer role), `/trainers/assign/`, `/trainers/assignments/<id>/end/`, availability (`/trainers/<id>/availability/add/`, `/trainers/availability/<id>/delete/`), time off, certificates (add / download / delete), compensation (`/trainers/<id>/compensation/add/`).

**Who:** `trainers.view` to see; `trainers.manage` to create/edit/assign; `trainers.compensation` (Gym Manager and Accountant by default) for salary/commission records.

**Features**
* **Profile** (`Trainer`): name, contact, photo, bio, qualifications, specialties, employment status, hire date, hourly (session) rate; optional login (`user`, one-to-one).
* **Assignments** (`TrainerAssignment`): a member may have several trainers; no duplicate open pair; ending an assignment keeps the row as history and **immediately withdraws** the trainer's access to that member.
* **Availability and time off** feed the appointment booking rules (§14.1).
* **Certificates** are stored outside the public media folder (`PRIVATE_MEDIA_ROOT`) and streamed only to authorised users (PDF/JPEG/PNG up to 5 MB, content-checked, downloads logged).
* **Compensation** records are append-only and hidden from everyone without `trainers.compensation`.
* Marking a trainer as left ends their assignments and cancels their upcoming sessions (history kept).
* **Data scoping:** a user with the Trainer role sees only members with an open assignment to them (members list/profile, attendance views, memberships, workout plans, progress, meal plans) and their own appointments.

Status: ✅ Implemented.

---

## 8. Workout plans

### 8.1 Exercise library

`/workouts/exercises/` (+ `new/`, `<id>/`, `<id>/edit/`). **Model `Exercise`:** name (unique), description, muscle group, category, instructions, image (JPEG/PNG/WebP, 2 MB), https video link, active flag. **Who:** `workouts.view` / `workouts.manage` (Trainer and Gym Manager by default).

### 8.2 Plans (staff side)

`/workouts/plans/` (list), `/new/`, `/<id>/` (detail), `/<id>/edit/`, `/<id>/status/`, `/<id>/copy/`, `/<id>/log/` (record a workout for a member), `/<id>/exercises/add/`, `/workouts/items/<id>/edit/`, `/workouts/items/<id>/remove/`.

* **Plan (`WorkoutPlan`):** member, trainer, name, description, goal, start/end date, status (active, completed, archived), copied-from link. Every change is written to a change log (`PlanChange`).
* **Exercises in a plan (`PlanExercise`):** the trainer adds as many exercises as needed, each with its **weekday**, **order on that day** (`position`; empty means "add last"), **number of sets**, **target repetitions**, optional **weight**, optional **duration (minutes)**, **rest between sets in seconds** (each exercise has its own value) and notes. Instructions come from the exercise in the library. A row must have reps or a duration. Exercises appear in `weekday, position, id` order.
* **Copy** a plan to another member; the original is untouched. Plans can be marked active/completed/archived.
* **History preserved:** exercises that already have recorded workouts are archived instead of deleted.
* Trainers only see and manage plans of their assigned members.
* Rest is entered in **seconds** (for example 90 = 1 min 30 s); there is no separate minutes input.

### 8.3 Member access

`/me/workouts/` lists the member's plans; `/me/workouts/<id>/` shows the plan by day with instructions, a "Start Workout" button per day (§9) and a "Record manually" option (`/me/workouts/<id>/log/`) to log numbers after the fact. Members cannot create or edit plans, and another member's plan returns 404.

Status: ✅ Implemented.

---

## 9. Workout session tracking (guided workouts)

Implemented in `apps/workouts/runs.py` (rules), `apps/portal/views.py` (pages) and `static/js/workout_run.js` (countdown and alarm).

**Who:** members only (portal, `/me/...`), for plans assigned to them. Staff cannot run a member's workout (403).

### 9.1 Flow

1. On a plan page, **Start Workout** on a day starts a run (`POST /me/workouts/<plan_id>/start/`). The plan must be active and the day must have exercises. Only **one active run per member** is allowed (database constraint); starting again resumes it.
2. The run page (`/me/workouts/run/<id>/`) lists the exercises in the trainer's order with their state: **Upcoming**, **In progress** (working or resting), **Completed**, **Not done**.
3. **Start Exercise** per exercise. An exercise can only start when the previous ones are completed and none is in progress. Starting it starts **set 1** immediately.
4. The page shows "Set *n* of *N*", target reps (or minutes), weight, configured rest, overall progress (sets done / total, exercises done / total) and a running set timer.
5. **Finish Set** records the set's completion time and elapsed time. Unless it was the last set, the **rest countdown** (e.g. `01:00`) starts using the exercise's configured rest.
6. When the countdown reaches zero the page shows "Rest finished", enables **Start Set *n*** and plays a short alarm. **The next set never starts by itself.**
7. After the last set the exercise is completed (its duration is shown). The next exercise starts only when the member presses **Start Exercise**.
8. After the last exercise the run is **completed** and a summary shows total duration, exercises and sets completed, progress and a per-set table (set time, configured vs actual rest). **End workout** (with confirmation) ends early; what was done is kept and the remaining exercises are marked "Not done".

### 9.2 Timer and alarm behaviour

* All timestamps are written by the **server**; the browser only displays. The countdown targets an absolute deadline calculated from the server clock, so a refresh, a throttled background tab or a wrong device clock does not change the rest period.
* A refresh never resets the rest or creates records; the page is rebuilt from stored timestamps (`Cache-Control: no-store`). An interrupted run is restored by opening it again (the plan page shows **Resume workout**).
* **Start Set** is disabled by the page until the rest ends **and** the server refuses an early start ("Rest is not finished yet", with a 1.5 s tolerance).
* The alarm (three short beeps via the Web Audio API) plays **once**, when the countdown crosses zero while the page is open. It does not play when a page loads after the rest is already over. A **Sound on/off** toggle is remembered in the browser. Audio starts only after the first user interaction; if the browser blocks it, the visible "Rest finished" banner, the changed tab title and (on phones) vibration are the fallbacks.
* Pause/resume is **not** provided.

### 9.3 Rules and records

* Targets (sets, reps, weight, rest) are copied from the plan when the run starts, so later plan edits do not change a run in progress.
* Duplicate clicks do not duplicate records (finishing an already finished set is refused; starting an already running set returns the existing one).
* A run left open for more than 12 hours is closed automatically as "Ended early" (`RUN_MAX_HOURS`).
* When a run ends, a normal **`WorkoutSession`** with **`SessionEntry`** rows is created from what was done, so existing workout history shows it.
* Recorded per run: start/end time (`WorkoutRun`), per exercise start/end and status (`ExerciseRun`), per set start/end, configured rest and actual rest (`SetRun`). Completed vs incomplete sets are derived (a set without a completion time is incomplete). Trainers see each plan's guided workouts (date, day, status, duration) on the plan page.

### 9.4 Verification note

Rules, permissions and refresh behaviour are covered by automated tests. The countdown, enabling of **Start Set**, tab-title change and the Rest-finished banner were checked in headless Edge. **The sound itself could not be verified** (no audio output in the test environment) and was not tested on real phones.

Status: ✅ Implemented (alarm audio ◐ not verified).

---

## 10. QR code attendance

### 10.1 Member QR codes

* Every member gets a unique, random 256-bit token (`Member.qr_token`) at registration. The QR image encodes only `GYMQR:<token>` – **no name, phone or ID**.
* **Members** open **My QR code** (`/me/qr/`) to view, **download** (`/me/qr.png?download=1`) or print their code. **Staff** with `attendance.manage` open it from the member profile (`/attendance/members/<id>/qr/`, image `…/qr.png`, download with `?download=1`) and can **reset** it (`…/qr/regenerate/`, POST) if a card is lost – the old code stops working.
* The image is served `Cache-Control: private, no-store`; the token is never put in a URL.
* **Existing members without a code:** `python manage.py generate_member_qrs` (`--dry-run` to preview) gives a token to members that lack one and never changes existing tokens.

### 10.2 Scanning and check-in

* **QR kiosk** `/attendance/scan/` (`attendance.manage`): a USB/Bluetooth scanner types the code, or the browser camera is used where available (needs HTTPS or localhost). A scan checks the member in; scanning again after the cooldown checks them out.
* The server looks the token up itself and **trusts nothing else from the code**. Unknown codes show "Unrecognised QR code" and reveal nothing; after `QR_SCAN_MAX_FAILURES` (10) unknown codes per staff user within `QR_SCAN_WINDOW_SECONDS` (300), scanning pauses.
* **A check-in is accepted only if:** the member is active; a membership covers today (suspended, expired, scheduled, cancelled and missing memberships are refused with the reason); the plan's access rules (allowed weekdays / hours) allow this moment; the member is not already inside. Optionally `ATTENDANCE_BLOCK_UNPAID=True` also refuses memberships with no payment recorded.
* **Duplicate prevention:** a service check plus a partial unique database index (one open visit per member). A second scan within `ATTENDANCE_SCAN_COOLDOWN_SECONDS` (60) of a check-in is rejected as accidental ("…was just checked in… Ignoring the repeated scan") instead of checking the member out. A forgotten check-out is closed automatically after `ATTENDANCE_MAX_SESSION_HOURS` (12) and flagged "auto".
* Messages are shown for success, deactivated member, no/expired/suspended membership, plan-hours restriction, already inside, repeated scan and unrecognised code.

### 10.3 Attendance management

| Page | URL | Who |
|---|---|---|
| Front desk (search by ID, name, phone; check in/out; who is inside) | `/attendance/` | `attendance.view` / `.manage` |
| Check in / out actions | `/attendance/check-in/<member_id>/`, `/attendance/<id>/check-out/` (POST) | `attendance.manage` |
| History (search, date range, method, sorting, pagination) | `/attendance/history/` | `attendance.view` |
| Member history | `/attendance/members/<id>/` | `attendance.view` |
| Daily report (totals, unique members, QR share, average visit, check-ins by hour) | `/attendance/report/` | `attendance.view` |

`attendance.manage` (create/modify attendance) is held by Gym Manager and Receptionist by default; Trainers can view only their assigned members' attendance; Members and Accountants cannot create or modify attendance. Attendance rows are linked to the member and (when known) the membership that granted access, and are never deleted.

Status: ✅ Implemented. Camera scanning in a real browser/device: ◐ not verified here.

---

## 11. Payments and transactions

**Who:** `payments.view` to see; `payments.manage` to record/confirm; `payments.refund` and `payments.adjust` for refunds, adjustments and voids (Gym Manager and Accountant by default; Receptionists can record and confirm payments only). Members see their own payments and receipts at `/me/payments/`.

### 11.1 The ledger

Every money movement is a row in the append-only `Payment` table: `payment` (money in), `refund` (money out, linked to the payment it reverses) or `adjustment` (staff-approved change to what is owed; negative = discount). Rows are never edited or deleted; only the status can change (pending → completed/failed; completed/pending → voided) through services that write an immutable `PaymentAudit` row and an activity-log entry. A payment points to exactly one **membership** or one **appointment** (database constraint).

Methods: cash, bank transfer, card (no online gateway). Statuses: pending confirmation, completed, failed, voided.

### 11.2 Balances (single implementation: `apps/payments/billing.py`)

```
amount due  = membership/session price (snapshot) + completed adjustments
net paid    = completed payments − completed refunds
outstanding = max(amount due − net paid, 0)     (0 for cancelled items)
```
Payment state shown to users: Unpaid, Partially paid, Paid, Partially refunded, Refunded. Pending, failed and voided entries never affect a balance; a pending amount reserves part of the payable balance.

### 11.3 Rules

* Payment date cannot be in the future or more than `PAYMENT_MAX_BACKDATE_DAYS` (90) back; the amount may not exceed what is still payable.
* Bank/card payments can be recorded as awaiting confirmation and later confirmed or rejected.
* Each submission carries a one-time idempotency key (unique constraint) – a double click returns the original entry. Operations lock the membership row inside a transaction and re-read the balance, so concurrent requests cannot overpay (row locks are effective on PostgreSQL).
* Refunds need a reason, are capped at what remains refundable on the original payment, and leave it untouched. Adjustments cannot make the amount due negative or lower than what is already paid.
* **No card data is stored:** the reference field rejects values that look like card numbers (13–19 digits passing the Luhn check).
* Unique references `TXN-YYYYMMDD-XXXXXXXX`; receipt numbers `RC-<year>-<id>` (payments) and `RF-<year>-<id>` (refunds).

### 11.4 Pages

| Page | URL |
|---|---|
| Payments list (search, filters, totals) | `/payments/` |
| Outstanding balances | `/payments/outstanding/` |
| Record payment | `/payments/membership/<id>/pay/`, `/payments/appointment/<id>/pay/` |
| Adjust charge | `/payments/membership/<id>/adjust/`, `/payments/appointment/<id>/adjust/` |
| Payment detail with audit trail | `/payments/<id>/` |
| Refund / confirm / reject / void | `/payments/<id>/refund/`, `/confirm/`, `/reject/`, `/void/` |
| Receipt (printable page / PDF) | `/payments/<id>/receipt/`, `/payments/<id>/receipt.pdf` |
| Member's own payments and receipts | `/me/payments/`, `/me/payments/<id>/receipt/`, `…/receipt.pdf` |

Receipts show the gym name, address, phone, e-mail and footer from Gym settings; pending/failed/voided entries print "NOT A VALID RECEIPT". The built-in PDF font covers Latin text only.

**Not implemented:** online payment gateways, payment links. (The "pending" status exists so a gateway could be added later.)

Status: ✅ Implemented.

---

## 12. Reports and analytics

**Who:** `reports.view` **plus** the permission of the module the report reads (for example `expenses.view` for expense reports); inventory reports appear only if inventory is enabled. Gym Manager and Accountant hold `reports.view` by default.

**URLs** `/reports/` (index) and `/reports/<key>/`. Every report supports on-screen view with pagination, a **printable page** (`?print=1`), **CSV** (`?export=csv`) and **PDF** (`?export=pdf`). Common filters: date range (`from`, `to`; default from the start of the month, range capped at five years) and, where relevant, `status` and `member` (ID or phone).

| Key | Report | Notes |
|---|---|---|
| `member-registrations` | Member registrations | Members registered in the period with current status |
| `membership-status` | Active and expired memberships | As of today, filter by status |
| `renewals` | Membership renewals | Renewals starting in the period |
| `plan-popularity` | Membership plan popularity | Memberships per plan started in the period |
| `attendance` | Daily and monthly attendance | Check-ins per day or month |
| `member-attendance` | Member attendance history | One member (ID or phone) |
| `payments` | Daily and monthly payments | Completed payments and refunds by payment date |
| `outstanding` | Outstanding balances | As of now, cancelled items written off |
| `revenue` | Revenue collected | Invoiced, collected, refunded, outstanding; by method |
| `expenses` | Expenses | Non-void expenses in the period |
| `revenue-vs-expense` | Revenue versus expenses | Monthly, estimated operating profit (cash basis) |
| `appointments` | Training appointments | Sessions in the period, filter by status |
| `equipment-maintenance` | Equipment maintenance | Records and costs in the period |
| `inventory` | Inventory and low stock | Current stock and movements (optional module) |

### Financial definitions (`apps/reports/finance.py`)

| Term | Meaning |
|---|---|
| Invoiced | Fees billed in the period: memberships starting in it + session fees + completed adjustments; cancelled items excluded |
| Collected | Completed payments by payment date |
| Refunds | Completed refunds by payment date |
| Revenue | Collected − refunds |
| Outstanding | What members still owe now (receivable, not revenue) |
| Operating expenses | Non-void expenses by expense date; linked maintenance/stock expenses counted once |
| Estimated operating profit | Revenue − operating expenses (ignores unpaid fees, depreciation, tax and trainer pay unless entered as a Salaries expense) |

Status: ✅ Implemented.

---

## 13. System settings and customization

### 13.1 Gym settings

`/settings/gym/` (`settings.manage`; Gym Manager by default). Single record (`GymSettings`): name, tagline, address, phone, e-mail and receipt footer. Used in the sidebar, page titles, receipts, reports, e-mails and the sign-in page. `GYM_NAME` in `.env` is only the initial default.

### 13.2 System customization

`/settings/appearance/` (`settings.appearance`; Super Admin by default; sidebar → Administration → **Customization**). Saved in the single-row `Appearance` table and applied immediately to every page, including the sign-in page, through CSS variables.

* **Branding:** gym/company name and tagline (the same values as Gym settings), logo (PNG/JPEG/WebP, 2 MB, ≤2000 px), favicon (PNG/ICO, 512 KB, ≤512 px), sign-in heading, welcome message and background image (JPEG/PNG/WebP, 4 MB, ≤5000 px). Each image shows a preview with replace and remove options; replaced or removed files are deleted from disk. SVG and files whose content is not a real image are refused. Stored under `media/branding/` with random names and served via `MEDIA_URL`.
* **Theme:** five presets – Graphite & Teal (default), Midnight Blue, Forest, Plum, Stone & Bronze – or custom primary, secondary, accent, background, surface, text and border colours; **light, dark or follow the device**. Custom neutral colours apply to light mode; dark mode uses a matching dark palette. Colours are validated (hex, text contrast ≥ 4.5:1 on background/surface, readable white button text on the primary colour, dark sidebar needs a dark secondary colour).
* **Layout and typography:** light or dark sidebar, expanded or compact sidebar width, comfortable or compact spacing, text size (small/default/large), font (Inter or system font), dashboard tiles per row (four or three on wide screens).
* **Live preview:** the page fetches CSS for the unsaved form values from `POST /settings/appearance/preview/` (the same generator the live system uses); nothing is saved until **Save customization**.
* **Reset:** `POST /settings/appearance/reset/` (confirmation required) restores the default theme and layout; logo, images, texts and gym name are kept.
* Changes are written to the audit log (`appearance_updated`, `appearance_reset`). No secrets are shown or stored here.
* The interface is intentionally static: there are no animations, transitions, parallax or moving backgrounds.

Status: ✅ Implemented. Not verified in a real browser: favicon display and the login background image (HTML output is tested).

### 13.3 Audit log and health check

* **Audit log** `/audit/` (`audit.view`): read-only list of the activity trail (sign-ins, failed sign-ins and lockouts, user/permission changes, financial and operational actions) with search, user, date and area filters, 50 per page.
* **Health check** `/healthz/` (public): returns `{"status":"ok"}` or HTTP 503 `{"status":"unavailable"}`; reveals nothing else.

---

## 14. Notifications and other modules

### 14.1 Appointments (personal training)

`/appointments/` (list), `/book/`, `/slots/` (free-slot lookup), `/<id>/`, `/reschedule/`, `/cancel/`, `/complete/`, `/no-show/`; members use `/me/appointments/` (+ `book/`, `slots/`, `<id>/reschedule/`, `<id>/cancel/`). **Who:** `appointments.view/manage`; Trainers see their own sessions.

Booking rules (server side, in a transaction locking trainer and member): allowed length (`APPOINTMENT_DURATIONS` 30/45/60/90 min), start on the 15-minute grid, within 60 days ahead, trainer employed, inside an availability window and not on leave, no overlap for the trainer or the member, a membership covering the day (`APPOINTMENT_REQUIRE_MEMBERSHIP`), and for self-booking members an assigned trainer and at least 60 minutes' notice. Members may reschedule/cancel with 12 hours' notice (staff may always). Outcomes: completed, no-show (only after the start), cancelled. The session fee (trainer rate × length) uses the normal payment ledger. A partial unique index (trainer, start) guards against races; every change is logged (`AppointmentEvent`).

### 14.2 Progress

`/progress/` (+ `members/<id>/`, `types/`) and `/me/progress/`: dated body weight and height and configurable measurements (Body fat, Waist, Chest, Hips, Arm, Thigh are seeded; staff can add types); charts and a history table. BMI is shown only as an indicative ratio with a disclaimer.

### 14.3 Nutrition (optional)

Enabled only with `NUTRITION_ENABLED=True`; otherwise every nutrition URL returns 404 and no menu entry appears. Meal plans by day and meal type, a nutrition profile (preferences, allergies, goals), members can edit their own profile (`/nutrition/...`, `/me/nutrition/`). No calorie calculations; a "general guidance, not medical advice" notice is shown.

### 14.4 Member portal

`/me/`: account summary, trainers, workouts (+ guided workouts), sessions, payments and receipts, **My QR code**, progress, nutrition (if enabled), notifications. A login must be linked to a member record; otherwise a "not linked yet" page is shown.

### 14.5 Equipment

`/equipment/` (+ `new/`, `<id>/`, `<id>/edit/`, `<id>/status/`, `<id>/fault/`, `<id>/maintenance/add/`, `maintenance/`). Register with unique code (`EQ-0001…` if blank), category, purchase data, supplier, warranty, location, condition and status (available, in use, maintenance, out of service). Anyone who can view equipment (including Trainers) can report a fault; a major fault sets the item out of service and notifies maintenance staff. Recording maintenance (`equipment.manage`) logs routine/repair/inspection with cost, sets next-due dates, may resolve the fault and may create one linked expense. Maintenance history is permanent.

### 14.6 Inventory (optional)

`INVENTORY_ENABLED` (default on). `/inventory/` (+ `new/`, `<id>/`, `edit/`, `purchase/`, `usage/`, `adjust/`, `movements/`). Quantity changes only through append-only stock movements (initial, purchase, usage, adjustment with reason); stock cannot go negative (service check plus database constraint); low-stock notifications once per running-low episode; a purchase can create one linked expense.

### 14.7 Expenses and suppliers

`/expenses/` (+ `new/`, `categories/`, `<id>/`, `edit/`, `void/`, `receipt/`) and `/suppliers/`. Categories (Rent, Electricity, Water, Salaries, Equipment, Maintenance, Cleaning supplies, Inventory & supplies, Marketing, Other; more can be added), private receipts (PDF/JPEG/PNG ≤ 5 MB, outside the public media folder, downloads logged). Mistakes are **voided** (`expenses.void`) with a reason; expenses are never deleted. Visible only to `expenses.view` / `.manage` holders.

### 14.8 Announcements

`/announcements/` (+ `new/`, `<id>/edit/`, `<id>/toggle/`, `announcements.manage`). Title, body, kind (info, closure, operational, promo), audience (everyone, all members, active members, expired members, staff, trainers), publication window, optional notification. Active announcements appear as a banner for the audience.

### 14.9 Notifications

* **Notification centre** (bell, `/notifications/`, `/notifications/read/`, `/notifications/<id>/read/`): in-app messages for memberships expiring/expired, payment due/overdue, payments recorded, membership start/renewal, upcoming sessions, appointment changes, low stock, equipment faults and maintenance due, announcements. Members receive in-app notifications only if they have a linked login.
* **De-duplication:** each reminder has a dedupe key with a database unique constraint per user, so repeated runs never send the same reminder twice.
* **Settings** (`/notifications/settings/`, `notifications.configure`): reminder periods (membership expiry days, payment due offsets and grace, appointment reminder hours, maintenance reminder days) and channel switches. **Preferences** (`/notifications/preferences/`, any user): opt in to SMS/WhatsApp, turn e-mail off.
* **External channels (optional, need credentials):** e-mail (SMTP), SMS and WhatsApp (Twilio). Each external copy is tracked honestly – `queued`, `sent` (provider accepted, not proof of arrival), `delivered` (only when the provider confirms), `failed` (after 3 attempts), `skipped`. A Twilio status webhook (`/notifications/webhooks/twilio/`) accepts only requests with a valid signature. With nothing configured, nothing external is attempted.
* **Scheduling:** `python manage.py run_scheduled_notifications` (one idempotent job; run hourly or at least daily from cron / Task Scheduler) publishes due announcements, creates reminders and attempts queued deliveries; `python manage.py send_appointment_reminders` sends session reminders only. Optional Celery configuration exists in `config/celery.py` and `apps/notifications/tasks.py` (`requirements-celery.txt`).

Status: in-app notifications and scheduling ✅ Implemented. E-mail, SMS, WhatsApp, Twilio webhook and Celery/Redis: ◐ implemented but **not tested against real services**.

---

## 15. Database structure

SQLite for development, PostgreSQL for production. All models live in the `apps.*` packages. Constraints mentioned are enforced by the database as well as by services.

### 15.1 Models by app

| App | Model | Important fields / notes |
|---|---|---|
| accounts | `User` | custom user; `role`, `phone`; `is_staff`/`is_superuser` derived from role |
| accounts | `RolePermission` | (`role`, `permission`) unique |
| core | `GymSettings` | single row: name, tagline, address, phone, email, receipt_footer |
| core | `Appearance` | single row: logo/favicon/login_background, login texts, theme preset/mode/colours, layout options |
| core | `ActivityLog` | audit trail: user, action, description, created_at |
| core | `Notification` | user, title, message, url, kind, `dedupe_key` (unique with user), read_at |
| core | `SampleRecord` | ownership register used by the sample-data commands |
| members | `Member` | member_id, contact data, `qr_token` (unique), `user` (one-to-one), is_active |
| memberships | `MembershipPlan` | duration_type, custom_days, price, access rules, is_active |
| memberships | `Membership` | member, plan, kind, previous, start/end date, price snapshot, status, suspension/cancellation fields |
| attendance | `Attendance` | member, membership, check_in/out, method (manual/qr), auto_closed; one open visit per member |
| payments | `Payment` | ledger row; membership **or** appointment; kind, method, status, amount, `refund_of`, `idempotency_key`, reference, receipt_number |
| payments | `PaymentAudit` | immutable status/amount history per payment |
| trainers | `Trainer`, `TrainerAssignment`, `TrainerAvailability`, `TrainerTimeOff`, `TrainerCertificate`, `TrainerCompensation` | see §7; one open assignment per trainer/member pair |
| appointments | `Appointment`, `AppointmentEvent` | member, trainer, start/end, status, fee; unique (trainer, start) while scheduled |
| workouts | `Exercise`, `WorkoutPlan`, `PlanExercise`, `PlanChange` | plan → exercises per weekday with position, sets, reps, weight, duration, rest_seconds |
| workouts | `WorkoutSession`, `SessionEntry` | history of what was done (manual records and finished guided workouts) |
| workouts | `WorkoutRun`, `ExerciseRun`, `SetRun` | guided workout tracking (§9); one active run per member, one active exercise per run |
| progress | `MeasurementType`, `ProgressRecord`, `ProgressMeasurement` | |
| nutrition | `NutritionProfile`, `MealPlan`, `MealItem` | optional module |
| suppliers | `Supplier` | |
| expenses | `ExpenseCategory`, `Expense` | receipt file, void handling, optional link to a maintenance record |
| equipment | `Equipment`, `FaultReport`, `MaintenanceRecord` | |
| inventory | `InventoryItem`, `StockMovement` | quantity only changes through movements |
| announcements | `Announcement` | audience and publication window |
| notifications | `NotificationSettings`, `NotificationPreference`, `NotificationDelivery` | channel settings and the external delivery log |

### 15.2 Key relationships

```
User 1─1 Member (optional login)         Member 1─* Membership *─1 MembershipPlan
Member 1─* Attendance  *─1 Membership    Membership 1─* Payment   Appointment 1─* Payment (exactly one target)
Payment 1─* PaymentAudit                 Payment 1─* Payment (refund_of)
Member *─* Trainer  (TrainerAssignment)  Member 1─* Appointment *─1 Trainer
Member 1─* WorkoutPlan 1─* PlanExercise *─1 Exercise
Member 1─* WorkoutRun 1─* ExerciseRun 1─* SetRun;  WorkoutRun 1─1 WorkoutSession 1─* SessionEntry
Equipment 1─* MaintenanceRecord; Equipment 1─* FaultReport; Expense 1─1 MaintenanceRecord (optional)
```

Most foreign keys to records with history use `PROTECT`, so history cannot be deleted by accident.

### 15.3 Migrations

Every app has its own migrations (`python manage.py migrate`). Migrations are additive and were verified to preserve existing data (members, memberships, payments, attendance, audit history). `python manage.py makemigrations --check` reports no pending model changes.

---

## 16. Installation and configuration

### 16.1 Local development

```bash
python -m venv .venv
.venv\Scripts\activate                # Windows  (Linux/macOS: source .venv/bin/activate)
pip install -r requirements.txt
copy .env.example .env                # Linux/macOS: cp .env.example .env  – then set SECRET_KEY
python manage.py migrate
python manage.py createsuperuser      # the account created this way is always a Super Admin
python manage.py runserver            # http://127.0.0.1:8000/
```

The application **refuses to start without `SECRET_KEY`**. `DEBUG` defaults to off.

### 16.2 Configuration (`.env`; values are never committed)

| Variable | Purpose |
|---|---|
| `SECRET_KEY` (required), `DEBUG`, `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS` | Core settings |
| `DB_ENGINE` (`sqlite` / `postgresql`), `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT`, `DB_SSLMODE`, `SQLITE_PATH` | Database |
| `REDIS_URL` | Optional shared cache so login throttling works across worker processes |
| `SECURE_SSL_REDIRECT`, `SECURE_HSTS_PRELOAD`, `USE_X_FORWARDED_FOR` | HTTPS and proxy behaviour |
| `EMAIL_BACKEND`, `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS`, `DEFAULT_FROM_EMAIL` | E-mail |
| `GYM_NAME`, `TIME_ZONE` (default `Asia/Colombo`) | Initial gym name and time zone |
| `NUTRITION_ENABLED`, `INVENTORY_ENABLED` | Optional modules |
| `SMS_PROVIDER`, `WHATSAPP_PROVIDER`, `TWILIO_*`, `DEFAULT_PHONE_COUNTRY_CODE` | Optional external messaging |
| `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND` | Optional Celery |
| `LOG_LEVEL` | Console log level |

Business-rule constants (reminder windows, booking limits, attendance limits, scan limits, payment back-date limit, etc.) are defined in `config/settings.py`.

### 16.3 Static and media files

* `static/` holds `css/app.css`, `js/app.js`, `js/appearance.js`, `js/workout_run.js`. `python manage.py collectstatic --noinput` writes to `staticfiles/`; WhiteNoise serves hashed files when `DEBUG=False`.
* `media/` holds public uploads (member/trainer/exercise photos, branding images). In development Django serves it when `DEBUG=True`; in production map `/media/` in the web server.
* `private_media/` holds certificates and expense receipts. It is **never** served directly – only through permission-checked views. Do not map it in nginx.

### 16.4 Production

`requirements-prod.txt` (adds gunicorn and redis), `deploy/env.production.example`, `deploy/gym.service.example` (systemd), `deploy/nginx.conf.example`, `deploy/backup.sh`. See `README.md` for the step-by-step guide: PostgreSQL, `migrate`, `collectstatic`, `createsuperuser`, `check --deploy`, gunicorn, nginx with TLS, a cron job for `run_scheduled_notifications`, nightly backups (test a restore), and `/healthz/` as the smoke test.

### 16.5 Management commands (verified in the code)

| Command | Purpose |
|---|---|
| `run_scheduled_notifications` | Idempotent recurring job (announcements, reminders, queued deliveries) |
| `send_appointment_reminders` | Session reminders only |
| `generate_member_qrs [--dry-run]` | Give QR tokens to members that lack one |
| `load_sample_data` / `cleanup_sample_data [--yes]` | Sample data (§17) |
| `seed_demo` | Deprecated alias of `load_sample_data` |

---

## 17. Sample data management

Full instructions are in `SAMPLE_DATA_GUIDE.md`. Summary:

* `python manage.py load_sample_data` – development only (refuses when `DEBUG` is off). Creates sample members (all named "Demo …"), plans, memberships in different states, payments (including partial, pending and refund), attendance (QR and front desk), a trainer with sessions, a workout plan, equipment, stock and an expense, an announcement and notifications, through the normal services, in one transaction.
* **Sample accounts** – all use the development-only password documented in `SAMPLE_DATA_GUIDE.md`: `demo_manager` (Gym Manager), `demo_reception` (Receptionist), `demo_accountant` (Accountant), `demo_trainer` (Trainer), `demo_alice` and `demo_bob` (Members). None is a superuser; the loader never creates, changes or deletes the Super Admin and never overwrites existing users.
* **Repeatable:** a second run stops with "already loaded" and adds nothing. If untracked "Demo …" records already exist, loading stops without changes.
* **Tracking:** every row created is registered in `SampleRecord`.
* `python manage.py cleanup_sample_data` – asks for confirmation (or `--yes`), deletes only registered rows, skips any user that is a superuser/Super Admin/staff, and **keeps** any sample record that real data still depends on (listed in the output). No flush, truncate or "delete all users" is used.

Status: ✅ Implemented and tested.

---

## 18. Testing and troubleshooting

### 18.1 Automated tests

```bash
python manage.py test                       # whole suite
python manage.py test apps.payments         # one app
python manage.py check --deploy             # production configuration check
python manage.py makemigrations --check     # fails if a model change has no migration
```

At the time of writing the full suite ran **529 tests, all passing**. (Counts change as tests are added; run the command for the current number.) Notable suites besides each app's own tests:

| Suite | Covers |
|---|---|
| `apps/core/test_workflows.py` | Eight end-to-end workflows (registration → plan → payment → receipt, attendance, renewal, partial payments, personal training, workout plans, expenses/reports/dashboard, notification reminders with de-duplication) |
| `apps/core/test_security.py` | Direct URL access for every route and role, member isolation, CSRF, uploads, error leakage, failed-login audit |
| `apps/core/test_crawl.py` | Follows every link and form for every role |
| `apps/core/test_performance.py` | Query counts do not grow with the number of rows |
| `apps/core/test_sample_data.py` | Loader / cleanup safety |
| `apps/core/test_appearance.py` | Customization permissions, validation, uploads, preview, reset, system-wide application |
| `apps/workouts/test_runs.py` | Guided workout rules, ownership, refresh restoration |

Tests run on a separate test database (in memory for SQLite) with a fast password hasher, so they never touch your development data. The browser JavaScript (countdown, alarm, live preview) has no automated test.

### 18.2 Troubleshooting

| Problem | Cause / fix |
|---|---|
| App refuses to start: "SECRET_KEY" | Set `SECRET_KEY` in `.env` (see `.env.example`). |
| `no such table: …` / missing column | Run `python manage.py migrate`. |
| "Invalid host header" / 400 in production | Add the host to `ALLOWED_HOSTS` (and the HTTPS origin to `CSRF_TRUSTED_ORIGINS`). |
| Static files missing or unstyled in production | Run `collectstatic`; check nginx `/static/` alias; the app also needs internet for the Bootstrap/Chart.js/font CDNs. |
| Uploaded images not showing in production | Map `/media/` in the web server (Django serves it only when `DEBUG=True`). |
| Sign-in "Invalid username or password" repeatedly, then locked | After 5 failures the account/IP is locked for 15 minutes; wait, or check the audit log. |
| Camera scanning does not start | Browsers allow the camera only on HTTPS or localhost; use a USB scanner or paste the code. |
| QR scan says "Ignoring the repeated scan" | A second scan within 60 seconds of check-in is deliberately ignored. |
| "Rest is not finished yet" when starting a set | The server enforces the rest period; wait for the countdown. |
| No alarm sound | Browsers block audio until the page has been interacted with; check the Sound toggle and the device volume. The visible banner still appears. |
| Notifications not delivered externally | E-mail/SMS/WhatsApp need credentials and enabled channels; see the delivery status under the notification. `sent` is not proof of arrival. |
| `load_sample_data`: "Refusing…" | Sample commands run only with `DEBUG=True`. |
| Page looks wrong after changing the theme | Use **Reset theme and layout** on the Customization page. |
| Login throttle not shared between workers | Set `REDIS_URL`. |
| Row locking and concurrent payments | Only effective on PostgreSQL; SQLite serialises writes and is for development only. |

---

## 19. Implemented, partial and not implemented – summary

| Area | Status | Notes |
|---|---|---|
| Roles, permissions, user management, password flows | ✅ | No self-registration, no multi-factor authentication ⏳ |
| Dashboard | ✅ | Role-aware blocks |
| Members, memberships, plans | ✅ | |
| Trainers, assignments, appointments | ✅ | |
| Workout plans, exercise library, manual workout recording | ✅ | Rest is entered in seconds |
| Guided workouts (sets, rest countdown, history) | ✅ | Alarm sound itself not verified; no pause/resume ⏳ |
| QR codes and attendance | ✅ | Real camera / device scanning not verified here ◐ |
| Payments, refunds, receipts (HTML/PDF) | ✅ | Online gateway ⏳; PDF font is Latin-only |
| Reports (14) with CSV / PDF / print | ✅ | |
| Gym settings and System customization | ✅ | Favicon and login background shown in a real browser not verified ◐ |
| Notifications (in-app, scheduling, de-duplication) | ✅ | |
| E-mail / SMS / WhatsApp / Twilio webhook / Celery + Redis | ◐ | Implemented; not tested against real services |
| Equipment, inventory (optional), expenses, suppliers, announcements | ✅ | |
| Nutrition (optional) | ✅ | Off by default |
| Sample data loader and cleanup | ✅ | |
| Audit log, health check | ✅ | |
| Deployment templates (nginx, systemd, backup) | ◐ | Provided and reviewed; not run on a real server here |
| Offline use (no CDN access) | ⏳ | Bootstrap, icons, Chart.js and fonts are loaded from CDNs |
| Content-Security-Policy, multi-gym, multi-currency, mobile app | ⏳ | Not implemented |

**Known limitations:** photos and branding images are served by URL without login (random file names; private documents are protected); the interface was visually checked only through headless-browser screenshots of a subset of pages; the PDF font cannot print non-Latin scripts; members without a portal login receive no in-app notifications; the application is single-gym, single-currency (LKR) and single-time-zone.
