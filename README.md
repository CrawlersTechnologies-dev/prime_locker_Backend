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

---

## 📡 API Endpoints Reference

### 🔐 Authentication & Accounts
| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `POST` | `/api/auth/login/` | JWT token obtain (`username` / `email`, `password`, optional `keep_me_signed_in`) |
| `POST` | `/api/auth/token/refresh/` | Refresh JWT access token |
| `POST` | `/api/auth/password-reset-request/` | Request 6-digit OTP for password reset |
| `POST` | `/api/auth/password-reset-confirm/` | Verify OTP & set new password |
| `GET`/`POST` | `/api/super-distributors/` | Manage Super Distributors |
| `GET`/`POST` | `/api/distributors/` | Manage Distributors |
| `GET`/`POST` | `/api/sub-distributors/` | Manage Sub-Distributors |
| `GET`/`POST` | `/api/retailers/` | Manage Retailers |
| `GET`/`POST` | `/api/customers/` | Onboard and view Customers (with Aadhar, alt phone, KYC) |

### 🔑 Key Management
| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `POST` | `/api/keys/generate/` | *(Super Admin)* Generate batches of keys with prefix (`MOB-`, `TV-`) |
| `GET` | `/api/keys/` | Paginated list of keys owned by current user (50/page) |
| `GET` | `/api/keys/dashboard/` | Inventory summary grouped by device type & status |
| `GET` | `/api/keys/recipients/` | Step 1: Get selectable recipient roles |
| `GET` | `/api/keys/recipients/?role=RETAILER` | Step 2: Get active users of selected role with key balances |
| `POST` | `/api/keys/allocate/` | Transfer keys directly down the hierarchy (by `target_user_id` and `target_role`) |
| `POST` | `/api/keys/reclaim/` | Reclaim unactivated keys from lower-tier nodes |
| `POST` | `/api/keys/activate/` | Activate key against customer device IMEI |
| `GET`/`POST` | `/api/key-requests/` | Bottom-up key requests (Retailer ➔ Parent) |
| `POST` | `/api/key-requests/{id}/approve/` | Parent approves request & auto-transfers keys |
| `POST` | `/api/key-requests/{id}/reject/` | Parent rejects key request |

### 💳 EMI & Payments Management
| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET`/`POST` | `/api/emi-plans/` | Master EMI Plan Templates (CRUD Super Admin only; read-only for others) |
| `PATCH`/`PUT`| `/api/emi-plans/{id}/` | *(Super Admin)* Edit master plan terms |
| `POST` | `/api/emi-plans/{id}/toggle-status/` | *(Super Admin)* Activate / Deactivate plan |
| `GET`/`POST` | `/api/emi-contracts/` | Set up EMI contract (auto-generates installment schedule) |
| `GET` | `/api/emi-contracts/{id}/schedule/` | View financial summary bar + all installment rows |
| `POST` | `/api/emi-contracts/{id}/lock/` | Manual device lock override |
| `POST` | `/api/emi-contracts/{id}/unlock/` | Manual device unlock override |
| `POST` | `/api/emi-installments/{id}/record-payment/` | Record payment (Cash/UPI) & auto-unlock device if overdue |
| `GET` | `/api/emi-overdue/` | Overdue dashboard with Ageing KPIs (`?bucket=1-7`, `8-15`, `16-30`, `30+`) |

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
| `GET` | `/api/reports/customer-kyc/` | Customer KYC compliance (missing Aadhar, alternate phone, or doc) |
