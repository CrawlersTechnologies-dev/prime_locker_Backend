# Prime Locker - Backend API

A robust Django REST API backend designed for Mobile Device Management (MDM), EMI financial tracking, and secure device locking. The platform features a deep user hierarchy system and an automated activation key pipeline.

## 🚀 Key Features

*   **Role-Based Hierarchy (RBAC):** Strict top-down management (Super Admin ➔ Super Distributor ➔ Distributor ➔ Sub-Distributor ➔ Retailer ➔ Customer).
*   **JWT Authentication:** Secure login with extended "keep me signed in" capabilities and OTP-based password resets.
*   **Key Management System:** 
    *   Generate secure alphanumeric activation keys with device-specific prefixes (e.g., `MOB-`, `TV-`).
    *   Top-down key allocation (Push) and bottom-up key requests (Pull).
    *   Reclaim capabilities for unused inventory.
*   **Device & EMI Tracking (Phase 4 - Upcoming):** Automated device locking/unlocking API based on customer EMI payment due dates.

## 🛠️ Tech Stack

*   **Backend:** Python 3, Django, Django REST Framework (DRF)
*   **Authentication:** djangorestframework-simplejwt
*   **Database:** PostgreSQL / SQLite (Configurable via `.env`)

## ⚙️ Local Setup Instructions

**1. Clone the repository**
```bash
git clone <your-repo-url>
cd Prime-Locker
```

**2. Create a virtual environment**
```bash
python -m venv venv
# Windows:
venv\Scripts\activate
# Mac/Linux:
source venv/bin/activate
```

**3. Install dependencies**
```bash
pip install -r requirements.txt
```

**4. Set up Environment Variables**
Create a `.env` file in the root directory (where `manage.py` is located) and add your secrets:
```ini
SECRET_KEY=your_very_secret_django_key_here
DEBUG=True
# Add DB credentials here if using PostgreSQL
```

**5. Apply Migrations**
```bash
python manage.py makemigrations
python manage.py migrate
```

**6. Create a Super Admin**
```bash
python manage.py createsuperuser
```

**7. Run the Server**
```bash
python manage.py runserver
```

## 📚 API Endpoints Overview

*   `/api/auth/login/` - JWT Login
*   `/api/super-distributors/` - Manage State-level distributors
*   `/api/retailers/` - Manage Shop-level retailers
*   `/api/keys/generate/` - Super Admin key generation
*   `/api/key-requests/` - Downstream key requests
