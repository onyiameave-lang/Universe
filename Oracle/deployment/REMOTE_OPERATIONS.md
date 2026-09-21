# Private VPS Operations

Use your own SSH key and your own VPS user. Never share the private key or use another person's cloud account.

## Live logs

From your computer:

```bash
ssh -i ~/.ssh/oracle_vps YOUR_VPS_USER@YOUR_VPS_HOST \
  'sudo journalctl -u oracle-ctrader -f -o cat'
```

This gives a live mirror of the trader's output. Closing the SSH window does not stop the service.

Useful commands:

```bash
ssh -i ~/.ssh/oracle_vps YOUR_VPS_USER@YOUR_VPS_HOST \
  'sudo systemctl status oracle-ctrader --no-pager'

ssh -i ~/.ssh/oracle_vps YOUR_VPS_USER@YOUR_VPS_HOST \
  'sudo journalctl -u oracle-ctrader -n 200 --no-pager'
```

## Private network access with Tailscale

Install Tailscale on the VPS and on your own computer, then use the VPS's Tailscale address instead of its public address. Keep SSH restricted to the Tailscale network when possible. Do not expose a log dashboard directly to the public internet.

The SSH workflow remains the same:

```bash
ssh -i ~/.ssh/oracle_vps YOUR_VPS_USER@TAILSCALE_VPS_NAME \
  'sudo journalctl -u oracle-ctrader -f -o cat'
```

## Interactive maintenance

For occasional shell work, connect with SSH. For a manually started diagnostic process, use `tmux` so it survives a disconnected terminal:

```bash
ssh -i ~/.ssh/oracle_vps YOUR_VPS_USER@TAILSCALE_VPS_NAME
tmux new -s oracle-diagnostics
# run a diagnostic command here
tmux detach
```

The production trader should remain managed by systemd, not by a terminal or `tmux` session.

## GitHub deployment

GitHub Actions can deploy from your repository using the repository's deployment secrets. The VPS only needs its own deploy key or read access to the repository; it does not need your friend's account. Keep `.env`, SSH private keys, logs, and runtime memory outside Git.
