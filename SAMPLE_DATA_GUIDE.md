# Sample Data Guide

This guide shows how to fill the Gym Management System with realistic **sample data** for development and testing, and how to remove it again safely.

> Sample data is for your own computer only. The commands refuse to run when `DEBUG` is off.

## 1. What is included

Everything is created through the application's normal services, so the numbers, balances, receipts and audit entries are real and consistent. All sample names start with **Demo** so they are easy to recognise.

| Area | Sample data |
|---|---|
| Membership plans | Demo Monthly (5,000), Demo Quarterly (13,500), Demo Off-peak (3,500, weekdays 09:00–17:00 only) |
| Members (9) | Alice, Bob, Cara, Dan, Eve, Fiona, George, Hana, Ian — in different states: paid in full, partly paid, paid with a refund, pending payment, expiring in 5 days, expired, suspended, and one deactivated member (Ian) |
| QR codes | Every member gets a unique private QR token (the same mechanism real members use) |
| Payments | Cash, card and bank-transfer payments, a partial payment, a pending payment, a refund, a discount adjustment, and a session fee |
| Attendance | A week of earlier visits (both QR and front-desk), Alice checked in, and George checked in by a real QR scan |
| Training | One trainer (Demo Coach) with weekly availability, two assigned members, an upcoming session, a cancelled session, a completed session |
| Workouts, progress, nutrition | A workout plan with a logged session, a progress record, a nutrition profile and a meal plan (all for Alice) |
| Operations | A supplier, a treadmill with a fault and a repair, towels with stock movements, a rent expense |
| Other | An announcement, plus the notifications the system generates |

## 2. Sample accounts

All sample accounts use the same development-only password:

**`DemoPass-12345!`**

| Username | Role | What it is for |
|---|---|---|
| `demo_manager` | Gym Manager | Full day-to-day management |
| `demo_reception` | Receptionist | Members, front desk, QR scanning, payments |
| `demo_accountant` | Accountant | Payments, expenses, reports |
| `demo_trainer` | Trainer | Sees only Alice and Bob (assigned members), workouts, sessions |
| `demo_alice` | Member | Member portal for "Demo Alice" |
| `demo_bob` | Member | Member portal for "Demo Bob" (has an outstanding balance) |

No sample account is a Super Admin or superuser. The loader never creates, changes or deletes the Super Admin. Passwords are stored hashed with Django's normal password hashing.

**How to log in:** start the server (section 3), open http://127.0.0.1:8000/login/, enter a username and the password above.

## 3. Load the sample data

Open a terminal in the project folder (`D:\Github-Projects\Gym_system`).

```bash
# 1. Activate the virtual environment
.venv\Scripts\activate            # Windows (Linux/macOS: source .venv/bin/activate)

# 2. Make sure the database is up to date (needed once after updating the code)
python manage.py migrate

# 3. Load the sample data (your .env must have DEBUG=True)
python manage.py load_sample_data

# 4. Start the application
python manage.py runserver
```

You need your own Super Admin account first (`python manage.py createsuperuser`) only if you want to sign in as an administrator; the loader itself does not need one and never creates one.

Running `load_sample_data` a second time does **not** duplicate anything. It stops with the message *"Sample data is already loaded"*. To start fresh, clean up (section 6) and load again.

(`python manage.py seed_demo` still works; it is now just another name for `load_sample_data`.)

## 4. Try each role

* **Manager (`demo_manager`)** – open *Members*, *Memberships*, *Payments*, *Reports* and *Audit log*. Open "Demo Bob" to see a partly paid membership.
* **Receptionist (`demo_reception`)** – open *Attendance*: search for a member, check them in and out, open the QR scanner at `/attendance/scan/`.
* **Accountant (`demo_accountant`)** – open *Payments*, *Outstanding*, *Expenses* and *Reports*. Attendance and member editing are not available.
* **Trainer (`demo_trainer`)** – you will see only Demo Alice and Demo Bob. Other members (for example Demo Cara) return "not found" because they are not assigned to this trainer.
* **Members (`demo_alice`, `demo_bob`)** – you land in the member portal: your membership, sessions, workouts, progress, payments and receipts, and **My QR code**. A member cannot open staff pages or anybody else's records.

## 5. Test QR-code attendance

1. Sign in as `demo_alice` and open **My QR code** (sidebar). Use **Download PNG** to save the code.
   (Staff can also open a member's QR from the member's profile.)
2. Sign in as `demo_reception` in another browser (or a private window) and open **Attendance → QR scan** (`/attendance/scan/`).
3. Scan the code with a scanner or a camera (browser camera scanning needs HTTPS or `localhost`), or paste the scanned text into the box.
4. Check the results:
   * A valid member with a current membership is **checked in** and the time is recorded.
   * Scanning the same code again within 60 seconds is ignored with a message ("just checked in … Ignoring the repeated scan"). Scanning again after that **checks the member out**.
   * **Demo Alice** has an open visit already; **Demo George** was checked in by a scan when you loaded the data.
   * **Demo Dan** (expired membership), **Demo Eve** (suspended), **Demo Hana** (expired) and **Demo Ian** (deactivated) are refused with a clear message.
   * An unknown code shows "Unrecognised QR code."
5. See the records under **Attendance → History** (filter by date, method or member).

## 6. Remove the sample data

```bash
python manage.py cleanup_sample_data
```

It tells you how many records it will delete and asks you to type `yes`. (`--yes` skips the question.) Like loading, it refuses to run if `DEBUG` is off.

**What is removed:** only the records that `load_sample_data` created — the sample members, plans, memberships, payments and their audit rows, attendance, sample logins, trainer, sessions, workouts, equipment, stock, expense, announcement and notifications. The loader keeps a register of them (`SampleRecord`), and nothing outside the register is ever deleted.

**What is kept:** your Super Admin and every other account, all real members, plans, payments, attendance and other records. If a real record depends on a sample record (for example you recorded a real payment on a sample membership), that sample record is **kept** and listed in the message, because deleting it would damage real data.

## 7. Safety precautions

* The commands work only with `DEBUG=True`.
* The Super Admin is never created, modified, reset or deleted. Existing usernames are never overwritten, and no sample user is a superuser or staff-flag account.
* There is no `flush`, no table truncation and no "delete all users" anywhere in the loader or cleanup.
* Loading happens in one database transaction: if anything fails, nothing is kept.
* If records named "Demo …" or `demo_…` already exist but were not created by the loader, loading stops and changes nothing, because they could not be removed safely later.
* The password is public in this file. Never use these accounts, or `DEBUG=True`, on a live system.

## 8. Troubleshooting

| Message | What to do |
|---|---|
| `Refusing to load sample data while DEBUG is off` | Set `DEBUG=True` in `.env` on your development machine and try again. |
| `Sample data is already loaded` | Nothing to do, or run `cleanup_sample_data` and then load again. |
| `... already exist but were not created by this loader` | Records called "Demo …" exist from an older manual setup. Rename or remove them yourself, then load again. |
| `no such table: core_samplerecord` | Run `python manage.py migrate`. |
| `Kept N record(s) because real data still depends on them` | Expected when you added real records to sample ones. Delete or void your real records first, then run cleanup again. |
| A sample login says "Please enter a correct username and password" | The sample data may have been cleaned up, or the account is locked after 5 failed attempts for 15 minutes — wait, or reload the sample data. |
| Camera scanning does not start | Browsers allow the camera only on HTTPS or `localhost`; use a USB scanner or paste the code instead. |
