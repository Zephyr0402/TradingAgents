# Deploying the TradingAgents web UI

A self-hosted, mobile-friendly web UI for TradingAgents. Designed for personal
use with a small group of trusted friends — not a public service.

## What you get

- Password-protected single-page web app (mobile-first)
- Background job queue (SQLite, no Redis dependency)
- Shared password + device fingerprint whitelist
- Public HTTPS via Caddy + Let's Encrypt, **or** a Cloudflare Tunnel if you
  don't want to expose ports at all

## Architecture

```
                Internet
                   │
       ┌───────────┴────────────┐
       │                        │
   Caddy :443             Cloudflare edge
   (Let's Encrypt)              │
       │                        │
       └────────┬───────────────┘
                │ 127.0.0.1:8765 (or tunnel)
                ▼
       tradingagents container
       (uvicorn web.server:app)
                │
                ▼
       SQLite + ~/.tradingagents
       (memory log, checkpoints, web data)
```

## Quick start (local network only)

For testing on your own machine first:

```bash
echo "TRADINGAGENTS_WEB_PASSWORD=changeme" >> .env
echo "TRADINGAGENTS_WEB_SECRET=$(python -c 'import secrets; print(secrets.token_urlsafe(32))')" >> .env
docker compose up --build tradingagents
```

Open `http://<your-lan-ip>:8765` from your phone. The first login
auto-registers that device. Friends log in with the same password from
their devices — they're auto-registered too. Remove unwanted ones from
the gear icon (⚙) → Devices.

## Production deploy (VPS, public domain)

You need:
- A small VPS ($5–10/mo is plenty: Hetzner, Vultr, DigitalOcean, 阿里云轻量)
- A domain name you control
- 15–30 minutes

### 1. Get the code onto the VPS

```bash
ssh user@your-vps
git clone https://github.com/TauricResearch/TradingAgents.git
cd TradingAgents
```

### 2. Configure environment

```bash
cp .env.example .env

# Set the LLM API key(s) for whichever provider you'll actually use
echo "OPENAI_API_KEY=sk-..." >> .env

# Web UI: pick a strong password and a long random secret
echo "TRADINGAGENTS_WEB_PASSWORD=$(python -c 'import secrets; print(secrets.token_urlsafe(24))')" >> .env
echo "TRADINGAGENTS_WEB_SECRET=$(python -c 'import secrets; print(secrets.token_urlsafe(32))')" >> .env
```

### 3. Pull the model

The stack runs Ollama locally. One-time:

```bash
docker compose up -d ollama
# Wait ~10s for the server to be ready
docker compose exec ollama ollama pull minimax-m3:cloud
# Verify it's there
docker compose exec ollama ollama list
```

If you change `LOCAL_MODEL` in `web/server.py`, re-pull the new tag.

### 4. Point DNS at the VPS

In your DNS provider (Cloudflare, Porkbun, Route 53, whatever):

```
tradingagents.yourdomain.com  A  <vps-public-ip>
```

### 5. Start the stack

```bash
sed -i 's/tradingagents.example.com/tradingagents.yourdomain.com/' Caddyfile
docker compose up -d --build
docker compose logs -f caddy
```

Watch the Caddy logs — within a minute or two it should fetch a Let's
Encrypt cert and start serving HTTPS. If it sits on "obtaining certificate"
for more than five minutes, check that DNS has actually propagated:

```bash
dig tradingagents.yourdomain.com
```

### 6. Hand out access

1. Open the URL on your phone, sign in once → device is whitelisted.
2. Tell a friend the URL and the password. Their device is whitelisted
   on first login.
3. Review registered devices anytime from the gear icon.

## Cloudflare Tunnel option (no public ports)

Prefer to keep the VPS with zero inbound ports open? Use a Cloudflare
Tunnel — Cloudflare handles TLS and proxies traffic in.

```bash
# One-time, on the VPS
curl -L --output cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
sudo dpkg -i cloudflared.deb

cloudflared login        # opens a browser to authorize your domain
cloudflared tunnel create tradingagents
cloudflared tunnel route dns tradingagents tradingagents.yourdomain.com
```

Then disable the `caddy` service in `docker-compose.yml` and run
`cloudflared tunnel run tradingagents` (or as a systemd service). Update
the firewall to drop all inbound traffic to 80/443.

See https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/ for the
full guide.

## What lives on disk

Everything is under `~/.tradingagents/`:

```
~/.tradingagents/
├── web/
│   ├── tasks.db       # task list, status, results
│   ├── devices.yaml   # device whitelist
│   └── secrets.key    # session signing key (auto-generated)
├── memory/
│   └── trading_memory.md   # decision log
├── cache/
│   └── checkpoints/  # per-ticker SQLite (only if --checkpoint used)
└── logs/              # raw propagate() state dumps
```

Back this directory up if you care about history. The decision log and
the web DB are the only things that would actually hurt to lose.

## Operational notes

### Restarting

`docker compose restart tradingagents` is safe — running tasks will be
marked `failed` mid-flight, but pending ones are short. The next manual
submission kicks off fresh runs.

### Clearing old tasks

```bash
docker compose exec tradingagents sqlite3 /home/appuser/.tradingagents/web/tasks.db \
  "DELETE FROM tasks WHERE created_at < datetime('now', '-30 day');"
```

### Revoking a friend

Two options:
1. **Remove the device** (gear icon → Devices → Remove). They stay logged
   in until the cookie expires (30 days), but new logins from that device
   are rejected. Cheapest.
2. **Rotate the password** by editing `.env` and `docker compose restart`.
   Everyone has to re-authenticate; new device entries stay, password
   changes.

### Cost control

The biggest variable is LLM cost. A single run with default models
(GPT-5.5 + GPT-5.4-mini) is roughly $0.30–$1.00. Some knobs:

- `TRADINGAGENTS_WEB_MAX_CONCURRENT=1` if you want hard serialization
- Lower `max_debate_rounds` in the UI to 1
- Switch to cheaper models in the new-task form (`gpt-4.1-mini`, etc.)
- Use Ollama locally — add `ollama` profile and pick the ollama provider
  in the UI; cost drops to $0

### What this is NOT

- Not a public service. No rate limiting beyond `MAX_CONCURRENT * 2`.
  Don't put it on a public link with a guessable password.
- Not multi-user in the auth sense. One shared password, one set of
  tasks, one decision log.
- Not financially safe. TradingAgents is a research tool — outputs are
  LLM-generated and may be wrong. The disclaimer is in the footer; that
  is the only "compliance" you get.
