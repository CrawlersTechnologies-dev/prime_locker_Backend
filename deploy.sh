#!/usr/bin/env bash
# ==============================================================================
# Prime Locker Backend Deployment Script
# Automatically pulls code, runs migrations, collects static, configures
# the daily auto_lock_overdue cron job, and restarts application services.
# Usage: ./deploy.sh
# ==============================================================================
set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

echo "=========================================================="
echo " Starting Prime Locker Deployment"
echo " Directory: $PROJECT_DIR"
echo " Time: $(date)"
echo "=========================================================="

# 1. Pull latest code from repository
echo "--> 1. Pulling latest commits from git origin main..."
git fetch origin main
git reset --hard origin/main

# 2. Activate Python Virtual Environment
echo "--> 2. Checking Python environment..."
if [ -d "venv" ]; then
    echo "Activating local venv..."
    source venv/bin/activate
elif [ -d "../venv" ]; then
    echo "Activating parent venv..."
    source ../venv/bin/activate
elif [ -n "$VIRTUAL_ENV" ]; then
    echo "Using active virtual environment: $VIRTUAL_ENV"
else
    echo "Notice: No virtual environment directory found. Using system python3."
fi

PYTHON_BIN="$(which python3 || which python)"
echo "Using Python executable: $PYTHON_BIN"

# 3. Install / update dependencies
if [ -f "requirements.txt" ]; then
    echo "--> 3. Installing dependencies from requirements.txt..."
    "$PYTHON_BIN" -m pip install --quiet -r requirements.txt
fi

# 4. Run database migrations
echo "--> 4. Running database migrations..."
"$PYTHON_BIN" manage.py migrate --noinput

# 5. Collect static files
echo "--> 5. Collecting static files..."
"$PYTHON_BIN" manage.py collectstatic --noinput

# 6. Configure daily auto-lock cron job (Item 2)
echo "--> 6. Configuring daily auto_lock_overdue cron job..."
CRON_SCHEDULE="0 0 * * *"
CRON_COMMAND="cd $PROJECT_DIR && $PYTHON_BIN manage.py auto_lock_overdue >> /var/log/prime_locker_auto_lock.log 2>&1"
CRON_JOB="$CRON_SCHEDULE $CRON_COMMAND"

# Ensure log directory exists
sudo mkdir -p /var/log 2>/dev/null || true

# Check if cron job is already installed, otherwise append it
if crontab -l 2>/dev/null | grep -q "auto_lock_overdue"; then
    echo "Daily auto-lock cron job is already registered in crontab."
else
    (crontab -l 2>/dev/null; echo "$CRON_JOB") | crontab -
    echo "Successfully registered daily auto-lock cron job (Runs daily at midnight 00:00)."
fi

# 7. Restart application server
echo "--> 7. Restarting application server service..."
if command -v systemctl >/dev/null 2>&1; then
    if systemctl is-active --quiet gunicorn; then
        sudo systemctl restart gunicorn
        echo "[✓] Gunicorn service restarted successfully."
    elif systemctl is-active --quiet prime_locker; then
        sudo systemctl restart prime_locker
        echo "[✓] Prime Locker systemd service restarted successfully."
    else
        echo "[!] Notice: Neither gunicorn nor prime_locker active service was found."
        echo "    If running via uwsgi, supervisor, or docker, please restart your process container."
    fi
else
    echo "[!] systemctl not found (e.g. Docker or non-systemd environment). Skipping systemctl restart."
fi

echo "=========================================================="
echo " Prime Locker Deployment Completed Successfully!"
echo " /api/banners/ and all new endpoints are active."
echo "=========================================================="
