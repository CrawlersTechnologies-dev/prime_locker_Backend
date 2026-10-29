# Prime Locker — Enterprise Device Locking & EMI Management Platform

A high-performance Django REST Framework (DRF) backend engineered for Mobile Device Management (MDM), installment-based EMI financing, device security locking, and hierarchical distribution networks.

---

## 🌟 Core Modules & Capabilities

### 1. Multi-Tier User Hierarchy & Role-Based Access Control (RBAC)
*   **Roles:** `SUPER_ADMIN` ➔ `SUPER_DISTRIBUTOR` ➔ `DISTRIBUTOR` ➔ `SUB_DISTRIBUTOR` ➔ `RETAILER` ➔ `CUSTOMER`.
*   **Security & Deletion Policy:** Only `SUPER_ADMIN` has permission to permanently delete users.
*   **Auto-Activation:** All newly created users default to `is_active: true`.
*   **Audit Logging:** Every user creation, edit, status toggle, and deletion is recorded in a master `AuditLog` table with IP addresses and timestamps.

### 2. Customer Onboarding & KYC
*   Full profile onboarding for financing end-users.
*   Captures `full_name`, `mobile_number`, **`alternate_mobile_number`** (crucial for loan recovery), **`aadhar_number`** (12-digit national ID), address, and KYC document uploads.

### 3. Key Inventory & Distribution Engine
*   **Auto-Generation:** Super Admin generates unique 8-character alphanumeric activation keys with device prefixes (e.g. `MOB-XXXXXXXX`, `TV-XXXXXXXX`).
*   **Multi-Tier Direct Allocation (Top-Down):** Super Admin can allocate keys directly to any tier (including Retailers). Super Distributors and Distributors can allocate directly to any downstream node in their organization.
*   **2-Step Role-Wise Allocation:** Frontend can fetch eligible recipient roles (`/api/keys/recipients/`) and role-filtered users (`/api/keys/recipients/?role=RETAILER`) with live key balances.
*   **Key Requests (Bottom-Up):** Retailers and Sub-Distributors can request keys from their parent, with complete `approve` and `reject` workflows.
*   **Reclaim Engine:** Permitted upstream users can pull unactivated keys back from downstream nodes.

### 4. EMI & Payments Management Suite (Screens A-19 to A-22)
*   **Master EMI Plans (Screen A-19):** Super Admin creates and manages master plan templates (e.g., *Standard Phone 12M*, *Smart TV 12M*, *Festive Offer 9M*). Fully editable via `PATCH` / `PUT`, with one-click active/inactive toggles.
*   **Set up EMI (Screen A-20):** Connects Customer, Device IMEI, Brand, Model, and Plan Terms. Auto-calculates monthly EMI amounts and **automatically generates all monthly installment rows with calendar due dates**.
*   **EMI Schedule & Payment Recording (Screen A-21):**
    *   Detailed summary bar (*Total amount, Financed amount, Total paid, Total remaining, Paid count, Remaining count*).
    *   Record payments via `CASH`, `UPI`, `CARD`, or `ONLINE_GATEWAY`.
    *   **Auto-Unlock:** Recording an overdue payment automatically restores a locked device back to `ACTIVE`!
*   **Overdue EMIs Dashboard (Screen A-22):**
    *   Automated Ageing KPI Cards: `1–7 days`, `8–15 days`, `16–30 days`, and `30+ days` overdue.
    *   Filtered overdue table with customer phone, device IMEI, overdue amount, and days overdue.
    *   Manual **Lock** and **Unlock** override endpoints.

### 5. Offers & Promotions Engine
*   Super-Admin creates promotional campaigns targeted to `ALL` users or specific tiers (`RETAILER`, `DISTRIBUTOR`, etc.).
*   Supports Key Bonuses (e.g. *Buy 50 keys, get 5 free*), Fee Discounts, and Seasonal Schemes.
*   Role-aware visibility: Retailers only see Retailer-targeted or `ALL` offers; Distributors only see Distributor offers.
*   Automated validity windows (`valid_from`, `valid_to`) and instant pause/resume toggles.

### 6. Analytics & Reports Suite (with CSV/Excel Export)
*   **Collections Summary:** Amount due vs. collected, filterable by date range, retailer, and payment mode.
*   **Overdue Ageing:** Overdue EMIs bucketed into `1–7 days`, `8–15 days`, `16–30 days`, and `30+ days`.
*   **Retailer Performance:** Per-store ranking with devices sold, collection rate %, and overdue counts.
*   **Payments by Mode:** Revenue breakdown across `UPI`, `CASH`, `CARD`, and `ONLINE_GATEWAY`.
*   **Locks & Unlocks Audit:** Complete device lock and unlock status log with auto-lock and grace period tracking.
*   **Customer KYC:** Identifies customers with missing Aadhar numbers, missing alternate phones, or unverified documents.
*   **One-Click Spreadsheet Download:** Adding `?export=csv` to any report URL instantly streams an Excel-formatted `.csv` file!

### 7. Mobile & TV Device Management & MDM Control Suite
*   **Device Registry:** Complete device lifecycle management for **Mobile Phones** (Android & iOS) and **Smart TVs**.
*   **Two Scanners Support:** Supports both `NEW` purchased devices and `RUNNING` devices (customer personal phone held as collateral).
*   **Auto IMEI Lookup:** Auto-identifies brand, model, OS type, and category from the 15-digit IMEI.
*   **Remote MDM Commands:** Instant Lock/Unlock, Remote Reboot, Camera block, Social media block, Wallpaper control, Screen reminders, and Emergency 8-digit offline unlock codes.
*   **Silent Heartbeat API:** Pings server periodically from Android/TV APK to report GPS location, battery level, and sync lock policies.
*   **Device Alerts Status Board:** Real-time visibility into tamper events, offline devices (> 3 days), and provider command failures (`/api/devices/alerts/`).
*   **Retailer Emergency Offline Code:** In-app and WhatsApp dispatch of offline 8-digit unlock codes to retailers (`/api/devices/{id}/send-offline-code/`).

### 8. RocketPay DLC + OEM Hardware-Level Locking
*   **Zero-Touch OEM Locking:** Integrates with RocketPay SuperKey API (`/super-key/v2/only-key` and `/superkey/keys/{key_id}/control/LOCK/apply/LOCK|UNLOCK`) for direct Knox/hardware-level device enforcement.
*   **Strict Security & Signature Verification:** Webhooks require authentic HMAC-SHA256 signatures with timestamp replay protection; simulated bypasses are strictly prevented.
*   **Database-First Enrollment & Atomic Rollback:** Enrolls the device in the local database first and coordinates with RocketPay; if either step fails, transactions are atomically rolled back.
*   **RBI Compliance:** Overdue loan payment auto-unlocks are fired via `transaction.on_commit`, eliminating payment gateway timeouts while guaranteeing unlock execution within the 1-hour regulatory guideline.
*   **Resilience & Manual Retry:** Captures `provider_status="FAILED"` and detailed `provider_error` logs, enabling one-click retailer command retries.

### 9. TV & Appliance Financing (Phone Collateral Security)
*   **Dual-Asset Separation:** EMI contracts maintain independent tracking for the financed asset (`TV`, `APPLIANCE`, etc.) and the collateral lock device (`lock_device`).
*   **Multi-Contract Collateral:** Allows a single high-trust customer smartphone to secure multiple installment contracts simultaneously.
*   **Collateral Hot-Swap:** Dedicated endpoint (`/api/emi-contracts/{id}/swap-lock-device/`) to transfer security to a replacement phone with audit logging (automatically rejects iPhones/iOS devices).
*   **Automated Lock Redirection:** Daily automated overdue locks and payment unlocks directly target the associated collateral smartphone.

### 10. Approved Phone Models & TAC Whitelist Governance
*   **Hardware Compatibility Check:** Validates customer device TAC (first 8 digits of IMEI) against an approved device registry before enrollment.
*   **Environment Segregation:** Separate staging and production whitelists for brands, models, and marketing names.
*   **Pre-Flight Verification API:** Frontend calls `/api/tac-list/check/?imei=` for instant eligibility feedback prior to KYC onboarding.

### 11. App Versions & APK Management
*   **Centralized APK Repository:** Super Admin uploads and manages Android Locker APKs, TV APKs, and Retailer Apps.
*   **QR Provisioning & Direct Download:** Automatically produces direct download links and dynamic QR code generation URLs for instant installation on customer devices.
*   **Mandatory Update Flags:** Enforces mandatory app updates on remote devices.

### 12. Installation & Training Videos Suite
*   **Retailer Onboarding Tutorials:** Super Admin uploads or links step-by-step video guides for phone activations.
*   **Category-Aware Filtering:** Filter guides directly by device enrollment options: `ANDROID_DPC` (Android App), `DLC_LOCKER` (Google / OEM Hardware Lock), `IPHONE` (Apple iOS MDM), and `GENERAL`.
*   **Dual Video Source Engine:** Supports direct MP4 video file uploads as well as high-speed external streaming URLs (YouTube, Vimeo, Cloudflare Stream, AWS S3).
*   **Role-Based Targeting & Ordering:** Target videos to Retailers, Distributors, or All roles with custom display ordering (`1, 2, 3...`) and instant active/inactive toggles.

---

## 🛠️ Tech Stack

*   **Language & Framework:** Python 3.11+, Django 5.x, Django REST Framework (DRF)
*   **Authentication:** `djangorestframework-simplejwt` (JWT with dynamic "Keep me signed in" 30-day refresh)
*   **Image & File Processing:** `Pillow`
*   **Database:** PostgreSQL (Production) / SQLite (Local development)

---

## ⚙️ Local Setup Instructions

**1. Clone the repository**
```bash
git clone <your-repository-url>
cd Prime-Locker
```

**2. Create & Activate Virtual Environment**
```bash
# Windows
python -m venv venv
venv\Scripts\activate

# macOS / Linux
python3 -m venv venv
source venv/bin/activate
```

**3. Install Dependencies**
```bash
pip install -r requirements.txt
```

**4. Environment Variables (`.env`)**
Create a `.env` file in the root directory:
```ini
SECRET_KEY=your-django-secret-key-here
DEBUG=True
# Add PostgreSQL credentials if not using SQLite:
# DB_NAME=prime_locker
# DB_USER=postgres
# DB_PASSWORD=your_password
# DB_HOST=localhost
# DB_PORT=5432
```

**5. Apply Database Migrations**
```bash
python manage.py makemigrations
python manage.py migrate
```

**6. Create Super Admin Account**
```bash
python manage.py createsuperuser
```

**7. Run the Development Server**
```bash
python manage.py runserver
```

**8. Run the Automated Test Suite**
```bash
python manage.py test
```

---

## 🚀 Production Deployment & Automation

For staging and production deployments on Ubuntu/Linux servers, use the built-in deployment script:
```bash
chmod +x deploy.sh
./deploy.sh
```

### What `deploy.sh` Automates:
1. **Code Synchronization:** Pulls the latest commits from GitHub (`git pull origin main`).
2. **Environment Activation:** Automatically detects and activates `venv` or `.venv`.
3. **Dependency Sync:** Installs any new packages from `requirements.txt`.
4. **Database Migrations:** Applies all pending schema migrations (`python manage.py migrate`).
5. **Static Asset Compilation:** Compiles static files into the web root (`python manage.py collectstatic --noinput`).
6. **Automated Daily Cron Job:** Registers a midnight cron job (`0 0 * * *`) that executes `python manage.py auto_lock_overdue` and logs output to `/var/log/prime_locker_auto_lock.log`.
7. **Zero-Downtime Server Reload:** Restarts the Gunicorn / systemd service (`systemctl restart prime-locker`).

---

## 📡 API Endpoints Reference

### 🔐 Authentication & Accounts
| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `POST` | `/api/auth/login/` | JWT token obtain (`username` / `email`, `password`, optional `keep_me_signed_in`) |
| `POST` | `/api/auth/token/refresh/` | Refresh JWT access token |
| `POST` | `/api/auth/logout/` | Blacklist JWT refresh token (`{"refresh": "<token>"}`) and record logout audit |
| `POST` | `/api/auth/change-password/` | Change password (Screen A-42: `old_password`, `new_password`, `confirm_password`) |
| `GET`/`PATCH` | `/api/auth/me/` | Current user profile, role, parent, key counts, permissions. `PATCH` allows partners and retailers to update their business profile (`name`, `business_name`, `address`, `city`, `store_phone`, and `logo` image) |
| `POST` | `/api/auth/password-reset-request/` | Request 6-digit OTP for password reset |
| `POST` | `/api/auth/password-reset-confirm/` | Verify OTP & set new password |
| `GET`/`POST` | `/api/super-distributors/` | Manage Super Distributors (returns key balances, children counts, key flow stats) |
| `GET`/`POST` | `/api/distributors/` | Manage Distributors (returns key balances, children counts, key flow stats) |
| `GET`/`POST` | `/api/sub-distributors/` | Manage Sub-Distributors (returns key balances, children counts, key flow stats) |
| `GET`/`POST` | `/api/retailers/` | Manage Retailers (returns key balances, children counts, key flow stats) |
| `GET`/`POST` | `/api/customers/` | Onboard and view Customers (`first_name`, `last_name`, `name`, photo, signature, alt phone) |
| `GET` | `/api/customers/loan-providers/` | Get list of loan providers/financiers for mobile app dropdown |
| `POST` | `/api/customers/enroll/` | Step 1: Customer & Device Onboarding (Screen: Customer Device Details) with Photo, Signature, IMEI & Token Key |
| `GET` | `/api/dashboard/` | Role-aware Overview Dashboard (`?range=today\|7d\|30d\|this_month` or `?start_date=&end_date=`) |
| `GET` | `/api/audit-logs/` | Paginated, filterable Security Audit Logs (`?action=`, `?role=`, `?search=`, `?export=csv`) |
| `GET` | `/media/<path>` | Secure, authenticated media file server for KYC docs, photos, and signatures |

### 🔑 Key Management
| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `POST` | `/api/keys/generate/` | *(Super Admin)* Generate batches of keys with prefix (`MOB-`, `TV-`, `AND-`, `IOS-`) and optional `reference_note` |
| `GET` | `/api/keys/` | Paginated list of keys owned by current user (50/page) |
| `GET` | `/api/keys/dashboard/` | Inventory summary grouped by device type & status |
| `GET` | `/api/keys/recipients/` | Step 1: Get selectable recipient roles |
| `GET` | `/api/keys/recipients/?role=RETAILER` | Step 2: Get active users of selected role with key balances |
| `POST` | `/api/keys/allocate/` | Transfer keys directly down the hierarchy (by `target_user_id`, `quantity`, optional `reference_note`) |
| `POST` | `/api/keys/reclaim/` | Unilateral Key Recall: Higher authorities reclaim unactivated keys from downline nodes (supports optional `reason`) |
| `POST` | `/api/keys/activate/` | Activate key against customer device IMEI |
| `GET` | `/api/keys/transactions/` | Key history (Screen N-07): lists generated, allocated, reclaimed, activated transactions with `from_user`, `to_user`, date, type (`?type=`, `?start_date=`, `?end_date=`, `?search=`, `?export=csv`) |
| `GET`/`POST` | `/api/key-requests/` | Bottom-up key requests (Retailer ➔ Parent). Returns requester key balance, keys used this month, platform display (`?search=`) |
| `POST` | `/api/key-requests/{id}/approve/` | Parent approves request & auto-transfers keys (supports `{quantity}` for partial approval) |
| `POST` | `/api/key-requests/{id}/reject/` | Parent rejects key request (supports `{reason}` stored in notes) |

### 💳 EMI & Payments Management
| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET`/`POST` | `/api/emi-plans/` | Master EMI Plan Templates (CRUD Super Admin only; read-only for others) |
| `PATCH`/`PUT`| `/api/emi-plans/{id}/` | *(Super Admin)* Edit master plan terms |
| `POST` | `/api/emi-plans/{id}/toggle-status/` | *(Super Admin)* Activate / Deactivate plan |
| `GET`/`POST` | `/api/emi-contracts/` | Set up EMI contract (auto-generates installment schedule; supports `?customer=`, `?retailer=`, `?status=`, `?search=`). For TV/Appliance contracts, captures product details and collateral `lock_device`. |
| `GET` | `/api/emi-contracts/{id}/schedule/` | View financial summary bar + all installment rows |
| `POST` | `/api/emi-contracts/{id}/lock/` | Manual device lock override (targets collateral phone for TV contracts) |
| `POST` | `/api/emi-contracts/{id}/unlock/` | Manual device unlock override (targets collateral phone for TV contracts) |
| `POST` | `/api/emi-contracts/{id}/swap-lock-device/` | Hot-swap collateral phone securing a TV or appliance contract (`{"new_device_id": <int>, "reason": "string"}`). Refuses iOS/iPhone devices. |
| `GET` | `/api/payments/` | Paid installments list (Screen A-23) with customer, device, retailer, receipt number, mode, ref (`?date=`, `?start_date=`, `?end_date=`, `?mode=`, `?retailer=`, `?search=`, `?export=csv`) |
| `GET` | `/api/payments/{id}/` | Payment receipt (Screen A-25) with receipt number (`REC-XXXXXX`), customer/retailer addresses, and contract financial breakdown |
| `POST` | `/api/emi-installments/{id}/record-payment/` | Record in-store offline payment (Cash/UPI/Card) & auto-unlock device via `transaction.on_commit` if overdue |
| `POST` | `/api/emi-installments/{id}/create-payment-order/` | Create Cashfree PG (v2023-08-01) payment order (returns `payment_session_id`, `order_id`, and `order_amount` formatted to 2 decimals; charges `total_due` including late fees) |
| `POST` | `/api/emi-installments/{id}/verify-online-payment/` | Verify Cashfree order status with gateway & mark installment as `PAID` via `ONLINE_GATEWAY` (auto-unlocks overdue device) |
| `POST` | `/api/cashfree/webhook/` | External Cashfree PG webhook with timestamped HMAC-SHA256 signature verification (`PAYMENT_SUCCESS_WEBHOOK`) |
| `GET` | `/api/emi-overdue/` | Overdue dashboard with Ageing KPIs (`?bucket=1-7`, `8-15`, `16-30`, `30+`), per-row `late_fee`, and calculated `total_due` |

### 🎁 Offers & Promotions
| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET`/`POST` | `/api/offers/` | Create offer (Super Admin) / View active role-targeted offers (All users) |
| `PATCH`/`PUT`| `/api/offers/{id}/` | *(Super Admin)* Edit offer details |
| `DELETE` | `/api/offers/{id}/` | *(Super Admin)* Remove offer |
| `POST` | `/api/offers/{id}/toggle-status/` | *(Super Admin)* Quick pause / resume offer |

### 📊 Reports & Analytics (Supports `?export=csv`)
| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/api/reports/collections-summary/` | Total due vs collected, with breakdown by retailer & payment mode |
| `GET` | `/api/reports/overdue-ageing/` | Overdue amounts bucketed into 1-7, 8-15, 16-30, and 30+ days |
| `GET` | `/api/reports/retailer-performance/` | Per-store ranking: devices sold, collection rate %, overdue counts |
| `GET` | `/api/reports/payments-by-mode/` | Revenue breakdown across UPI, Cash, Card, Gateway with percentage share |
| `GET` | `/api/reports/locks-unlocks/` | Complete remote device lock/unlock audit log |
| `GET` | `/api/reports/customer-kyc/` | Customer KYC compliance (missing Photo, Signature, or Alternate phone) |

### 📱 Mobile & TV Device Management & MDM Suite
| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET`/`POST` | `/api/devices/` | Register device (`NEW` vs `RUNNING` collateral, Android/iOS/Smart TV) & filter list |
| `GET` | `/api/devices/imei-lookup/?imei=...` | Auto-detect brand, model, OS & category from 15-digit IMEI |
| `GET` | `/api/devices/alerts/` | Status Board Device Alerts: aggregates tamper alerts, devices offline > X days (`?days=3`), and failed RocketPay commands |
| `POST` | `/api/devices/{id}/lock/` | Remote lock device instantly (coordinates with RocketPay DLC if enrolled) |
| `POST` | `/api/devices/{id}/unlock/` | Remote unlock device instantly (coordinates with RocketPay DLC if enrolled) |
| `POST` | `/api/devices/{id}/retry-provider-command/` | Manually retry a failed RocketPay command (`{"command": "LOCK"|"UNLOCK"}`) |
| `POST` | `/api/devices/{id}/reboot/` | Queue remote reboot command |
| `POST` | `/api/devices/{id}/toggle-camera/` | Remotely enable/disable camera (native MDM only; returns 400 on RocketPay) |
| `POST` | `/api/devices/{id}/toggle-social-media/` | Remotely enable/disable WhatsApp, Instagram, Facebook (native MDM only; returns 400 on RocketPay) |
| `POST` | `/api/devices/{id}/toggle-reset-block/` | Reset Option Block & FRP Protection (block unauthorized factory reset) |
| `POST` | `/api/devices/{id}/set-wallpaper/` | Wallpaper Control (change, lock or customize wallpaper on device) |
| `POST` | `/api/devices/{id}/push-reminder/` | Device Reminders (push custom reminder message to device screen) |
| `GET` | `/api/devices/{id}/contacts/` | Get Contact Details (owner contact info & synced address book) |
| `POST` | `/api/devices/{id}/generate-offline-code/` | Generate emergency 8-digit offline unlock code |
| `POST` | `/api/devices/{id}/send-offline-code/` | Dispatch emergency 8-digit offline unlock code directly to retailer via In-App notification and WhatsApp |
| `GET` | `/api/devices/{id}/location-history/` | Live Location Tracking & historical GPS breadcrumbs |
| `POST` | `/api/devices/{id}/attach-emi/` | Step 2: Attach EMI Plan to device (via Master Plan Template, Custom Terms, or Existing Contract) |
| `POST` | `/api/devices/ping/` | Silent Heartbeat API for Android Locker APK / TV app / iOS agent |
| `POST` | `/api/devices/webhook/rocketpay/` | Hardware webhook endpoint for RocketPay OEM events (`device.locked`, `device.unlocked`, `device.tamper`) with HMAC-SHA256 verification |

### 📋 Approved Device Models (TAC Whitelist Governance)
| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET`/`POST` | `/api/tac-list/` | Manage approved TAC list (`tac` [8 digits], `brand`, `model_number`, `marketing_name`, `environment` [`staging`/`production`], `active`). Supports search and environment filters. |
| `GET`/`PATCH`/`DELETE` | `/api/tac-list/{id}/` | Retrieve, update, or remove approved model entry |
| `GET` | `/api/tac-list/check/?imei=...` | Pre-flight verification endpoint: checks whether a device IMEI TAC is authorized before proceeding with enrollment |

### 📦 App Versions & APK Management
| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET`/`POST` | `/api/app-versions/` | Upload and manage Android Locker / Smart TV APK releases |
| `GET` | `/api/app-versions/latest/?app_type=ANDROID_LOCKER` | Get latest active APK release with download link & QR code |

### 🎬 Installation & Training Videos
| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/api/installation-videos/` | List active tutorial & training videos for retailers/distributors (supports category filter `?category=DLC_LOCKER`, `ANDROID_DPC`, `IPHONE`, `GENERAL`) |
| `POST` | `/api/installation-videos/` | *(Super Admin)* Create/upload installation video (direct MP4 file upload or YouTube/CDN link, thumbnail, category, order) |
| `GET` | `/api/installation-videos/{id}/` | Retrieve single installation video details & streaming URL |
| `PATCH`/`PUT` | `/api/installation-videos/{id}/` | *(Super Admin)* Update video metadata, category, active state, or display order |
| `DELETE` | `/api/installation-videos/{id}/` | *(Super Admin)* Remove installation video |

### 🔔 Notifications, Staff & Advanced Administration
| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/api/notifications/` | In-app alerts list for Bell Icon A-41 (`?unread=true`, `?type=`) |
| `GET` | `/api/notifications/unread-count/` | Live badge count of unread notifications |
| `POST` | `/api/notifications/{id}/mark-read/` | Mark individual notification as read |
| `POST` | `/api/notifications/mark-all-read/` | Mark all notifications as read |
| `GET`/`POST` | `/api/staff/` | Manage internal staff roles (`STAFF_OPERATIONS`, `STAFF_COLLECTIONS`, `STAFF_SUPPORT`, `STAFF_VIEWER`) |
| `PATCH`/`DELETE` | `/api/staff/{id}/` | Update or deactivate staff member |
| `GET`/`PATCH` | `/api/settings/` | Platform & Company Settings (Support phone, email, grace period, auto-lock toggle, gateway keys) |
| `GET` | `/api/whatsapp/campaigns/` | View available message templates and recent delivery logs |
| `POST` | `/api/whatsapp/campaigns/` | Dispatch automated or bulk WhatsApp reminder campaigns (`OVERDUE_ALL`, `LOCKED_DEVICES`, custom numbers) |

