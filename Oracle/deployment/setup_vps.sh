#!/bin/bash
# Oracle cTrader Demo Trader — VPS setup script
# ================================================
# Run this ONCE on a fresh Ubuntu VPS (22.04 or newer; works on Oracle
# Cloud's free ARM tier, or any cheap Linux VPS — cTrader needs no Windows
# terminal, unlike MT5, which is why this is possible at all).
#
# Usage:
#   scp this file to your VPS, then:
#   chmod +x setup_vps.sh
#   ./setup_vps.sh
#
# What it does:
#   1. Installs Python, git, and build tools
#   2. Creates a dedicated non-root user ("oracle") to run the bot as
#   3. Clones your repo (or you can rsync/scp it over instead — see note)
#   4. Sets up a Python virtual environment with all dependencies
#   5. Prompts you to create the .env file with your credentials
#   6. Installs the systemd service so it runs continuously
#
# IMPORTANT: this script does NOT include your Client ID / Secret / Access
# Token / Account ID — you'll be prompted to create the .env file yourself
# in step 5, so those never end up copy-pasted into a shared script.

set -e   # stop on any error, rather than silently continuing after a failure

echo "=================================================="
echo " Oracle cTrader Demo Trader — VPS Setup"
echo "=================================================="
echo

# ---- 1. System packages ----
echo "[1/6] Installing system packages..."
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip git build-essential

# ---- 2. Dedicated user ----
echo "[2/6] Setting up a dedicated 'oracle' user (not root, for safety)..."
if ! id "oracle" &>/dev/null; then
    sudo useradd -m -s /bin/bash oracle
    echo "  Created user 'oracle'."
else
    echo "  User 'oracle' already exists, skipping."
fi

# ---- 3. Get the code onto the VPS ----
echo "[3/6] Getting your code onto this VPS..."
echo "  This script does NOT clone a repo for you automatically, since your"
echo "  project may not be in a public git repo. Options:"
echo "    a) If it IS in a git repo you can clone:"
echo "       sudo -u oracle git clone <your-repo-url> /home/oracle/Universe"
echo "    b) Otherwise, from YOUR OWN machine (not this VPS), run:"
echo "       scp -r Universal_AI oracle@<this-vps-ip>:/home/oracle/Universe/"
echo "  Press Enter once your code is in /home/oracle/Universe/Universal_AI ..."
read -r

if [ ! -d "/home/oracle/Universe/Universal_AI/Oracle" ]; then
    echo "  ERROR: /home/oracle/Universe/Universal_AI/Oracle not found."
    echo "  Copy your code there first, then re-run this script."
    exit 1
fi

# ---- 4. Python environment ----
echo "[4/6] Setting up Python virtual environment..."
sudo -u oracle python3 -m venv /home/oracle/Universe/Universal_AI/Oracle/venv
sudo -u oracle /home/oracle/Universe/Universal_AI/Oracle/venv/bin/pip install --upgrade pip
sudo -u oracle /home/oracle/Universe/Universal_AI/Oracle/venv/bin/pip install \
    -r /home/oracle/Universe/Universal_AI/requirements.txt

# ---- 5. Credentials ----
echo "[5/6] Setting up your .env file with cTrader credentials..."
ENV_PATH="/home/oracle/Universe/Universal_AI/Oracle/.env"
if [ -f "$ENV_PATH" ]; then
    echo "  .env already exists at $ENV_PATH — leaving it as-is."
    echo "  Edit it manually if you need to update credentials:"
    echo "    nano $ENV_PATH"
else
    echo "  Creating $ENV_PATH — enter your cTrader credentials now"
    echo "  (these are the same ones from openapi.ctrader.com/apps):"
    read -rp "  CTRADER_CLIENT_ID: " CID
    read -rp "  CTRADER_CLIENT_SECRET: " CSECRET
    read -rp "  CTRADER_ACCESS_TOKEN: " CTOKEN
    read -rp "  CTRADER_ACCOUNT_ID (the numeric ctidTraderAccountId, not the account number): " CACC
    sudo -u oracle bash -c "cat > $ENV_PATH" << EOF
CTRADER_CLIENT_ID=$CID
CTRADER_CLIENT_SECRET=$CSECRET
CTRADER_ACCESS_TOKEN=$CTOKEN
CTRADER_ACCOUNT_ID=$CACC
EOF
    sudo chmod 600 "$ENV_PATH"   # only the oracle user can read this file
    echo "  Saved. File permissions locked to owner-only (chmod 600)."
fi

# ---- 6. systemd service ----
echo "[6/6] Installing the systemd service (keeps the bot running forever)..."
SERVICE_SRC="/home/oracle/Universe/Universal_AI/Oracle/deploy/oracle-ctrader.service"
if [ -f "$SERVICE_SRC" ]; then
    sudo cp "$SERVICE_SRC" /etc/systemd/system/oracle-ctrader.service
    sudo systemctl daemon-reload
    sudo systemctl enable oracle-ctrader
    echo
    echo "=================================================="
    echo " Setup complete!"
    echo "=================================================="
    echo " Start it now with:"
    echo "   sudo systemctl start oracle-ctrader"
    echo " Watch the logs with:"
    echo "   sudo journalctl -u oracle-ctrader -f"
    echo "=================================================="
else
    echo "  WARNING: oracle-ctrader.service not found at $SERVICE_SRC"
    echo "  Copy it there and re-run steps 6, or install it manually."
fi