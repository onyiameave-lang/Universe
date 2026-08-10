# Deploying the cTrader Demo Trader to a VPS

## Why this is possible now (and wasn't for MT5)

MT5 needs an actual Windows desktop terminal running in the background —
that ruled out free/cheap Linux hosting entirely (see the earlier
conversation about Oracle Cloud's free tier: generous ARM instances, but
no practical Windows option). cTrader's Open API is a real network API —
no terminal, no GUI, runs on plain Linux. This reopens the free-hosting
door that MT5 closed.

## Option A: Oracle Cloud "Always Free" tier (genuinely free, not a trial)

1. Sign up at cloud.oracle.com (needs a credit card for verification, but
   the Always Free resources are never charged).
2. Create a Compute Instance:
   - Shape: **VM.Standard.A1.Flex** (the free ARM shape) — up to 4 OCPUs /
     24GB RAM total across your free instances, genuinely free forever.
   - Image: **Ubuntu 22.04** (or newer) — ARM-compatible.
   - Add your SSH key (or let Oracle Cloud generate one for you to download).
3. Under "Networking", note the instance's public IP address.
4. **Open the firewall for SSH** (usually already open by default for port
   22, but double check the instance's attached Security List/Network
   Security Group allows inbound TCP 22 from your IP).
5. SSH in:
   ```bash
   ssh -i /path/to/your-key.pem ubuntu@<instance-public-ip>
   ```

## Option B: Any cheap Linux VPS (if you'd rather not deal with Oracle Cloud's setup)

Any $5-10/month Ubuntu VPS (Contabo, Vultr, DigitalOcean, etc.) works
identically from here on — just SSH in and skip to the steps below.

## Getting your code onto the VPS

Two ways:
- **If your project is in a git repo** (even a private one, with an SSH
  deploy key or personal access token): `git clone` it directly on the VPS.
- **If it's not in a repo**: from your OWN machine (not the VPS), run:
  ```bash
  scp -r "C:\Users\HP\Documents\Universe\Universal_AI" oracle@<vps-ip>:/home/oracle/Universe/
  ```
  (adjust the local path to match wherever your project actually lives)

## Running the setup script

Once your code is on the VPS:
```bash
cd /home/oracle/Universe/Universal_AI/Oracle/deploy
chmod +x setup_vps.sh
./setup_vps.sh
```

This installs Python, creates a dedicated non-root `oracle` user, sets up
a virtual environment with all dependencies (including `ctrader-open-api`,
`twisted`, `service_identity` — the ones needed for cTrader specifically),
prompts you for your credentials to create `.env`, and installs the
systemd service.

## Starting it

```bash
sudo systemctl start oracle-ctrader
```

## Checking on it (this is your "check in occasionally" workflow)

```bash
# Is it running?
sudo systemctl status oracle-ctrader

# Watch live logs (Ctrl+C to stop watching, doesn't stop the bot)
sudo journalctl -u oracle-ctrader -f

# Last 100 log lines
sudo journalctl -u oracle-ctrader -n 100
```

## What "stop running this every day" actually means now

Once `systemctl enable oracle-ctrader` has run (the setup script does this
automatically), the bot:
- Starts automatically if the VPS reboots (e.g. after a provider maintenance
  restart)
- Restarts automatically if it crashes (waits 30s, tries again, gives up
  after 5 failures in 10 minutes so it doesn't hammer cTrader's servers
  with bad requests if something is fundamentally broken)
- Keeps running whether or not you're connected via SSH — closing your
  terminal/laptop does NOT stop it

You genuinely don't need to run anything manually again after this setup —
just check in via `journalctl` or the Telegram notifier (from earlier in
this conversation) whenever you want a status update.

## Chronicle Research Director (Chronicle + Forge + Atlas, once a day)

**This replaces an earlier, abandoned version** (`oracle-nightly-research` —
if you deployed that already, remove it: `sudo systemctl disable --now
oracle-nightly-research.timer`). The old version had Oracle asking
research questions about itself, which got the direction backwards.

The real version lives in `Chronicle/deploy/` and `Chronicle/tools/`:
Chronicle collects the day's trades, registers a hypothesis for each
signal stream ("does the news stream actually predict wins?") in Forge's
Hypothesis Queue, Forge runs a Sensitivity Analysis experiment against the
real data, and Chronicle stores the conclusion — with a strict rule that
nothing gets marked "confirmed" or "rejected" until at least 30 trades of
evidence exist, no matter how convincing a smaller sample looks. Atlas
adds qualitative context, best-effort, but the loop doesn't depend on it.

Install:
```bash
cd Chronicle/deploy
sudo cp chronicle-research-director.service /etc/systemd/system/
sudo cp chronicle-research-director.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now chronicle-research-director.timer
```

Check on it:
```bash
systemctl list-timers chronicle-research-director.timer   # when's it next due?
sudo journalctl -u chronicle-research-director -n 100      # last run's output
sudo systemctl start chronicle-research-director           # run it right now, manually
```

This deliberately never auto-applies anything — it's a report for you to
read, same "human stays in the loop" principle as Tier 0's suggestion
queue and Champion Retirement.

## Updating the code later

When you make changes (like the bug fixes from this session):
```bash
# From your own machine, re-copy the changed files:
scp execution/ctrader_broker.py oracle@<vps-ip>:/home/oracle/Universe/Universal_AI/Oracle/execution/

# Then on the VPS, restart to pick up the change:
sudo systemctl restart oracle-ctrader
```

## Security notes

- The `.env` file is chmod'd to 600 (owner-only readable) by the setup
  script — don't loosen this.
- The systemd service runs as the dedicated `oracle` user, not root — if
  anything ever went wrong, the blast radius is limited to that user's
  permissions.
- Your access token expires in ~30 days (per the Sandbox token you
  generated) — you'll need to regenerate it and update `.env` + restart
  the service when that happens. Nothing automatic here yet; a reminder
  is worth setting on your calendar.
