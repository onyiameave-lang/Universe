# VPS and GitHub Actions Deployment Guide

This guide is for the person administering the Linux VPS for the Oracle cTrader trader.

The repository is:

```text
https://github.com/onyiameave-lang/Universe.git
```

The deployment branch is:

```text
oracle-v1
```

The systemd service is:

```text
oracle-ctrader
```

## 1. VPS requirements

Use an Ubuntu 22.04 or newer Linux VPS with:

- A public IP address
- SSH access
- Outbound HTTPS access
- A user with `sudo` access for initial setup

The trader itself runs as the unprivileged `oracle` user after setup.

## 2. Copy and run the installer

From the administrator's computer, copy the installer to the VPS. Replace the SSH user and host as necessary:

```bash
scp Universal_AI/Oracle/deployment/setup_vps.sh \
  ubuntu@YOUR_VPS_IP:/tmp/setup_vps.sh
```

Connect to the VPS:

```bash
ssh ubuntu@YOUR_VPS_IP
```

Run the installer:

```bash
chmod +x /tmp/setup_vps.sh
sudo /tmp/setup_vps.sh
```

The installer installs Python, Git, build tools, creates the `oracle` service user, creates a Python virtual environment, and installs the repository dependencies.

## 3. Clone the repository when requested

The installer pauses while waiting for the code to be placed at:

```text
/home/oracle/Universe/Universal_AI
```

While the installer is paused, open a second SSH session to the VPS and run:

```bash
sudo -u oracle mkdir -p /home/oracle/Universe
sudo -u oracle git clone --branch oracle-v1 --single-branch \
  https://github.com/onyiameave-lang/Universe.git \
  /home/oracle/Universe
```

Then return to the installer session and press Enter.

If the repository is private, configure a GitHub deploy key for the `oracle` user before cloning. Do not put a GitHub password, personal access token, or private key in this document or in the repository.

## 4. Configure the VPS environment file

The installer asks for the cTrader credentials and creates:

```text
/home/oracle/Universe/Universal_AI/Oracle/.env
```

This file must remain on the VPS and must never be committed to GitHub. Set owner-only permissions:

```bash
sudo chown oracle:oracle /home/oracle/Universe/Universal_AI/Oracle/.env
sudo chmod 600 /home/oracle/Universe/Universal_AI/Oracle/.env
```

Edit the file and confirm the runtime settings. Use the values approved by the account owner:

```bash
sudo -u oracle nano /home/oracle/Universe/Universal_AI/Oracle/.env
```

At minimum, the file should contain the four cTrader credentials plus the runtime policy keys:

```dotenv
CTRADER_CLIENT_ID=REPLACE_WITH_REAL_VALUE
CTRADER_CLIENT_SECRET=REPLACE_WITH_REAL_VALUE
CTRADER_ACCESS_TOKEN=REPLACE_WITH_REAL_VALUE
CTRADER_ACCOUNT_ID=REPLACE_WITH_REAL_VALUE
CTRADER_USE_DEMO=true
BROKER_SYMBOL_MAP=REPLACE_WITH_APPROVED_MAPPING
TRADER_ID=ctrader_demo

ORACLE_PAPER_TRADING=true
ORACLE_ALLOW_LIVE=false
ORACLE_SESSION_MAX_LOSS_PCT=0.05
ORACLE_MAX_TRADES_PER_SESSION=
ORACLE_RECONNECT_INTERVAL_SEC=60
ORACLE_MANAGE_INTERVAL_SEC=15

RL_RISK_PER_TRADE=0.01
RL_MAX_POSITIONS=1
RL_MAX_DRAWDOWN_PCT=0.20
ORACLE_SYMBOL_TIMEOUT_SEC=120
KILL_SWITCH_EQUITY_CACHE_SEC=60
```

Keep `ORACLE_PAPER_TRADING=true` and `ORACLE_ALLOW_LIVE=false` until the administrator has verified the account, symbols, credentials, and logs. Only the account owner should approve live trading.

## 5. Enable and start the service

After the installer finishes, run:

```bash
sudo systemctl daemon-reload
sudo systemctl enable oracle-ctrader
sudo systemctl start oracle-ctrader
sudo systemctl is-active oracle-ctrader
```

The expected result from the final command is:

```text
active
```

The service runs:

```text
/home/oracle/Universe/Universal_AI/Oracle/venv/bin/python \
  execution/ctrader_demo_trader.py --preset live
```

Check status and logs with:

```bash
sudo systemctl status oracle-ctrader
sudo journalctl -u oracle-ctrader -n 100 --no-pager
sudo journalctl -u oracle-ctrader -f
```

Press `Ctrl+C` to stop viewing logs. It does not stop the service.

## 6. Configure GitHub Actions deployment

In the GitHub repository, open:

```text
Settings -> Environments -> production
```

Create the `production` environment if it does not exist. Add these environment secrets:

| Secret | Value |
|---|---|
| `VPS_HOST` | VPS IP address or DNS hostname |
| `VPS_USER` | `oracle` |
| `VPS_REPO_DIR` | `/home/oracle/Universe/Universal_AI` |
| `VPS_SSH_PRIVATE_KEY` | Private key whose public key is authorized for the VPS SSH account |
| `VPS_KNOWN_HOSTS` | Verified output of `ssh-keyscan -H YOUR_VPS_IP` |

The public key matching `VPS_SSH_PRIVATE_KEY` must be present in the SSH authorized keys for `VPS_USER`.

The host key must be verified before placing it in `VPS_KNOWN_HOSTS`; do not blindly trust an unexpected fingerprint.

## 7. How automatic deployment works

The workflow is located at:

```text
.github/workflows/deploy-vps.yml
```

A push to `oracle-v1` causes GitHub Actions to:

1. Check out the branch.
2. Compile the trading runtime modules.
3. Run the test command.
4. Connect to the VPS over SSH.
5. Fetch and reset the VPS repository to the pushed commit.
6. Confirm that `Oracle/.env` still exists.
7. Restart `oracle-ctrader`.
8. Confirm that systemd reports the service as active.

The VPS `.env` is ignored by Git and is not overwritten by deployment.

After the one-time setup, normal deployment is simply:

```bash
git push origin oracle-v1
```

The deployment result is visible in the repository's GitHub Actions tab.

## 8. Manual deployment or rollback checks

To deploy the current branch manually from the VPS:

```bash
cd /home/oracle/Universe/Universal_AI
git fetch --prune origin oracle-v1
git reset --hard origin/oracle-v1
sudo systemctl restart oracle-ctrader
sudo systemctl is-active oracle-ctrader
```

Do not run `git clean -fd` on the VPS because it could remove local runtime files. Do not delete `Oracle/.env`.

If a deployment fails, inspect:

```bash
sudo systemctl status oracle-ctrader
sudo journalctl -u oracle-ctrader -n 200 --no-pager
cd /home/oracle/Universe/Universal_AI
git status
```

## 9. Security rules

- Never commit `.env`, broker credentials, SSH private keys, or GitHub tokens.
- Keep `/home/oracle/Universe/Universal_AI/Oracle/.env` at permission mode `600`.
- Run the trader as `oracle`, not as root.
- Use GitHub environment secrets, not plaintext values in workflow files.
- Treat cTrader access tokens as credentials and rotate them when they expire or are exposed.
- Keep paper trading enabled until live trading is explicitly approved.
