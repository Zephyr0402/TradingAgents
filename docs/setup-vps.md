# Setup guide: clean Linux VPS → running web app

This is a step-by-step walkthrough that takes a **brand-new VPS** with nothing
on it (no Python, no Docker, no Ollama) all the way to a working
https://trading.yourdomain.com that you and your friends can sign in to
from a phone.

**Target host**: 1 vCPU / 2 GB RAM / 55 GB SSD, Ubuntu 22.04 LTS. Works on
any small VPS (Hetzner CX22, Vultr Cloud Compute, DigitalOcean Basic, Oracle
Cloud free tier). Commands use `sudo`; run as a non-root user you create
during step 1.

**Time estimate**: 30–45 minutes, most of it waiting for DNS to propagate
and Docker images to pull.

---

## 0. Pick a VPS

If you don't have one yet, any of these will do. The app uses ~700 MB RAM
at peak, so 2 GB is comfortable headroom.

| Provider | Plan | Monthly | Notes |
|---|---|---|---|
| Hetzner | CX22 (Intel, 2 GB) | ~€4.5 | Best price/perf in EU/US |
| Vultr | Cloud Compute (1 vCPU / 2 GB) | $5 | Global regions, hourly billing |
| DigitalOcean | Basic Droplet (1 vCPU / 2 GB) | $4 | Easiest UI |
| Oracle Cloud | Ampere A1 (4 vCPU / 24 GB) | **Free** | Always-free tier, ARM CPU |

When creating the droplet/instance:
- **OS**: Ubuntu 22.04 LTS (x86_64) — or Ubuntu 22.04 ARM64 if Oracle/ARM
- **Region**: nearest to you and your friends
- **Authentication**: SSH key (recommended) or a long random password
- **Hostname**: e.g. `trading-vps`

Note the server's public IPv4 address. Below we call it `$VPS_IP`.

---

## 1. First login + system setup

```bash
# From your laptop — replace 'bevis' with whatever username you want,
# and '203.0.113.10' with your VPS public IP.
ssh root@$VPS_IP

# Create a non-root user (Ubuntu image prompts for full name etc.)
adduser bevis
usermod -aG sudo bevis
# Copy your SSH key so you can log in as the new user
rsync --archive --chown=bevis:bevis ~/.ssh /home/bevis/
# Test the new login in a separate terminal before logging out of root
exit
```

Log back in as the new user from here on:

```bash
ssh bevis@$VPS_IP
```

Set the timezone (so log timestamps match yours):

```bash
sudo timedatectl set-timezone $(curl -s https://ipapi.co/timezone)
```

Update everything:

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y curl git ufw fail2ban unattended-upgrades
```

### Firewall

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow OpenSSH        # 22 — keep this BEFORE enabling
sudo ufw allow 80/tcp         # HTTP (Caddy → Let's Encrypt)
sudo ufw allow 443/tcp        # HTTPS
sudo ufw enable
sudo ufw status
```

If you decide later to use **Cloudflare Tunnel** instead of Caddy, come back
and `sudo ufw delete allow 80/tcp` + `sudo ufw delete allow 443/tcp` — only
SSH stays open.

### Auto security updates

```bash
sudo dpkg-reconfigure -plow unattended-upgrades   # answer "Yes"
```

---

## 2. Buy a domain and point it at the VPS

Skip if you already own one.

1. Pick a registrar (Cloudflare Registrar, Porkbun, Namecheap, Gandi). Any
   one will do.
2. Buy `yourdomain.com` (or just a subdomain if you already own one).
3. In the registrar's DNS panel, add an A record:
   - **Host**: `trading` (or whatever subdomain you want — full hostname
     becomes `trading.yourdomain.com`)
   - **Points to**: `$VPS_IP`
   - **TTL**: Auto (or 300 s)
4. Wait for DNS to propagate. Test from your laptop:

   ```bash
   dig +short trading.yourdomain.com
   # should return $VPS_IP
   ```

   Propagation usually takes 1–10 minutes but can run up to 48 hours for
   stubborn TTLs.

---

## 3. Install Docker

The official convenience script is the cleanest way on a fresh Ubuntu box.

```bash
curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
sudo sh /tmp/get-docker.sh
sudo usermod -aG docker bevis
# Activate the new group without logging out
newgrp docker
# Verify
docker --version
docker compose version
```

If `get.docker.com` is blocked in your region, see the manual install at
<https://docs.docker.com/engine/install/ubuntu/>.

---

## 4. Clone the repo

```bash
cd ~
git clone https://github.com/Zephyr0402/TradingAgents.git
cd TradingAgents
git log --oneline -1   # confirm you have the latest web-UI commits
```

You should see something like:

```
9012037 docs(readme): add fork notice, web UI section, and retarget clone URL
df8b9d0 docs(env): document TRADINGAGENTS_WEB_* vars and ignore .DS_Store
```

If you forked under a different GitHub username, replace the URL above with
yours.

---

## 5. Configure environment

```bash
cp .env.example .env
nano .env
```

In the editor, **set two things** and leave the rest commented out unless
you have a key for that provider.

### 5a. LLM provider key

The web UI is hard-coded to use a local Ollama instance that talks to a
cloud model (`minimax-m3:cloud`). You need a `MINIMAX_API_KEY` if your
provider requires it for that cloud model, or you can skip Ollama and
point at any OpenAI-compatible endpoint by editing the env vars. The
default setup uses Ollama's local proxy which only needs the placeholder
key below to satisfy the OpenAI client — adjust if your setup differs.

```bash
# Required: a non-empty value, even a placeholder, for the OpenAI client
# that TradingAgents instantiates against the Ollama endpoint. Ollama
# ignores it.
OPENAI_API_KEY=ollama
```

If you switch to a paid cloud model (OpenAI, Anthropic, etc.) instead of
Ollama, set the matching key and update `llm_provider` in
`web/server.py` (the file is well-commented — see `LOCAL_PROVIDER` and
`LOCAL_MODEL`).

### 5b. Web UI password + session secret

```bash
# Required. The server refuses to start without this. Pick something
# long and share it only with people you trust.
TRADINGAGENTS_WEB_PASSWORD=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')

# Highly recommended. Used to sign session cookies. Auto-generated and
# persisted to web/secrets.key if left empty, but explicit is better.
TRADINGAGENTS_WEB_SECRET=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
```

The `python3` calls print to the terminal; copy the output into the file.

> **Save these values somewhere safe** (1Password, Bitwarden). If you lose
> the secret, all existing sessions invalidate — friends just have to
> sign in again. If you lose the password, see "Rotating the password"
> in the troubleshooting section.

### 5c. Optional knobs

```bash
# Cap concurrent analysis jobs. 1 = serialized, 2 = up to two in
# flight. Higher = more memory pressure.
TRADINGAGENTS_WEB_MAX_CONCURRENT=1

# Log level (DEBUG/INFO/WARNING/ERROR)
TRADINGAGENTS_LOG_LEVEL=INFO
```

Save and exit (`Ctrl-O`, `Enter`, `Ctrl-X`).

---

## 6. Point Caddy at your domain

```bash
nano Caddyfile
```

Replace `tradingagents.example.com` with your real hostname. Save.

---

## 7. Pull the local model and bring up the stack

```bash
# Start ollama first so the model pull can land somewhere
docker compose up -d ollama

# Give it ~5 seconds to be ready, then pull the cloud-routed model
sleep 5
docker compose exec ollama ollama pull minimax-m3:cloud

# Verify
docker compose exec ollama ollama list
# expect:
# NAME                ID              SIZE    MODIFIED
# minimax-m3:cloud    xxxxx           -       just now
```

`minimax-m3:cloud` is a thin **remote-model** entry — ollama's local
process just forwards requests to `https://ollama.com`. It uses ~0 MB of
your disk and RAM, which is why the 2 GB VPS plan works.

Now bring up the full stack:

```bash
docker compose up -d --build
```

First build takes 3–5 minutes (pulls Python 3.12 base image, installs
~30 packages). Subsequent starts are ~2 seconds.

Watch the logs to confirm everything came up healthy:

```bash
docker compose ps               # all 3 services should be "Up"
docker compose logs -f          # Ctrl-C to exit
```

You should see:

```
tradingagents-1  | INFO:     Uvicorn running on http://0.0.0.0:8000
caddy-1          | {"level":"info","msg":"obtained certificate","identifier":"trading.yourdomain.com"}
```

Caddy automatically requests a Let's Encrypt certificate. If it sits on
"obtaining certificate" for more than 5 minutes, double-check
`dig +short trading.yourdomain.com` returns your `$VPS_IP`.

---

## 8. First sign-in (from your laptop)

Open `https://trading.yourdomain.com` in a browser. You'll get a security
warning the **first** time on a brand-new domain — that's normal; Caddy
is still issuing the cert. Wait 30 s and refresh.

Sign in with the `TRADINGAGENTS_WEB_PASSWORD` you set in step 5. Your
device fingerprint (IP/24 + user-agent hash) is auto-registered. From
now on you'll stay signed in for 30 days.

You should land on the empty task list. Tap the **+** button and submit
a test analysis (`AAPL` + today's date + the defaults). It should:

1. Show the task as **running** with a spinner within ~3 s
2. Take 2–10 minutes to complete (depends on `minimax-m3:cloud` latency
   and the number of LLM round-trips)
3. Light up a rating badge (Buy / Overweight / Hold / Underweight / Sell)
   and the **Buy**/**Hold**/**Sell** color
4. Open to a detail view with 12 tabs of agent reports, rendered as
   proper Markdown

If you see a "not financial advice" footer and the rating badge color
working, you're done.

---

## 9. Add your friends

Two options:

### Option A — share the password (easiest)

1. Send the URL + password to a friend over Signal/WhatsApp/iMessage.
2. They open the URL on their phone, type the password, and they're in.
3. Their device auto-registers.
4. Use the gear icon (⚙) → **Devices** to review the whitelist.

### Option B — tighter security

If you don't want to share a password:

1. Have your friend visit the URL once. They'll see "Wrong password" —
   that's fine, the device is now registered.
2. From your account, open ⚙ → **Devices**, confirm the new device.
3. Send the password separately.
4. They sign in; from then on, only their whitelisted device works.

To revoke later, open ⚙ → **Devices** → **Remove** on the device row.

---

## Day-2 operations

### Update to the latest code

```bash
cd ~/TradingAgents
git pull
docker compose up -d --build
```

Watch the logs for `Application startup complete.`

### Clear old tasks

```bash
docker compose exec tradingagents sqlite3 /home/appuser/.tradingagents/web/tasks.db \
  "DELETE FROM tasks WHERE created_at < datetime('now', '-30 day');"
```

### Rotate the web password

```bash
nano .env                           # change TRADINGAGENTS_WEB_PASSWORD
docker compose restart tradingagents
```

Everyone has to re-authenticate; device whitelist is preserved.

### Backups

The only state worth backing up is the SQLite DB. Optional but cheap:

```bash
# Add to crontab — daily snapshot kept for 7 days
cat <<'CRON' | sudo tee /etc/cron.daily/tradingagents-backup
#!/bin/sh
install -d -m 0700 -o bevis -g bevis /home/bevis/backups
# Copy the SQLite DB from the host-side volume bind. The path is
# `tradingagents_tradingagents_data` because the project directory
# (which prefixes volume names) is named `tradingagents`.
cp /var/lib/docker/volumes/tradingagents_tradingagents_data/_data/web/tasks.db \
   /home/bevis/backups/tasks-$(date +%F).db
find /home/bevis/backups -mtime +7 -delete
CRON
sudo chmod +x /etc/cron.daily/tradingagents-backup
```

Verify the path with `docker volume inspect
tradingagents_tradingagents_data --format '{{ .Mountpoint }}'` — the
trailing `/web/tasks.db` is the in-container location.

The path `/var/lib/docker/volumes/...` is the host-side bind of the
`tradingagents_data` volume; verify it exists on your host with
`docker volume inspect tradingagents_tradingagents_data`.

### Tear it all down

```bash
cd ~/TradingAgents
docker compose down           # stop containers, keep data volume
docker compose down -v        # also delete data — DESTRUCTIVE
```

---

## Troubleshooting

### "Connection refused" on port 80/443

```bash
sudo ufw status
docker compose ps
docker compose logs caddy
```

Most common cause: DNS not pointing at this VPS, or `Caddyfile` still has
`tradingagents.example.com`.

### "Wrong password" even though I just set it

`.env` is read at container start. After editing:

```bash
docker compose up -d tradingagents   # not 'restart' — forces re-read
```

### Submit returns "Too many tasks in flight"

`MAX_CONCURRENT=1` and someone else is running. Wait for the running
task to finish, or raise `TRADINGAGENTS_WEB_MAX_CONCURRENT` and restart.

### Task fails immediately with "model not found"

```bash
docker compose exec ollama ollama list
```

If empty: `docker compose exec ollama ollama pull minimax-m3:cloud`. If
still empty after the pull, check `docker compose logs ollama` for
network errors.

### I forgot the web password

1. SSH in
2. `nano .env` → set a new `TRADINGAGENTS_WEB_PASSWORD`
3. `docker compose up -d tradingagents`
4. Tell friends the new password; old sessions invalidate.

### Out of memory

`docker stats` shows reality. With `minimax-m3:cloud` the app should sit
under 1 GB. If it climbs, check `docker compose logs tradingagents` for
a runaway task; restart with `docker compose restart tradingagents`.

---

## Hardening checklist (do these once the app is stable)

- [ ] **Disable password SSH login** if you used a password when creating
      the VPS — `sudo nano /etc/ssh/sshd_config` → `PasswordAuthentication no`
      → `sudo systemctl restart sshd`
- [ ] **Enable fail2ban** (already installed, just needs the default jail):
      `sudo systemctl enable --now fail2ban`
- [ ] **Sign up for Hetzner/Vultr/DO backup snapshots** (~$1–2/mo, saves
      you when the disk dies)
- [ ] **Add a free UptimeRobot monitor** on `https://trading.yourdomain.com/api/me`
      (returns 200 + JSON) so you get an email if it goes down

That's it. You now have a personal, password-gated, mobile-friendly
trading analysis service for a few bucks a month.
