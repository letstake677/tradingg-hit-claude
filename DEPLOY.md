# Deploying Kehlo Trading

This runs two processes that need to stay up 24/7, plus a database file
they share. Pick ONE path below.

- **Option A — VPS + Docker Compose**: full control, bot and API run as
  independent containers (one can restart without the other), you manage
  the server yourself.
- **Option B — Railway**: no server to manage, HTTPS is automatic, but
  Railway doesn't support sharing a volume between two services, so bot
  and API run **combined in one service** instead (see why below).

---

## Option A: VPS + Docker Compose

### 1. Get a server

Any small VPS works — this bot is light on CPU/RAM. Reasonable options:

- **DigitalOcean** or **Vultr** — $4-6/month droplet, very well documented,
  good for a first deployment
- **Hetzner Cloud** — cheaper for similar specs, excellent performance
- **Contabo** — cheapest, less polished control panel

Pick **Ubuntu 22.04 or 24.04**. If you can choose a region, Singapore/Tokyo/
Hong Kong are physically closer to most exchange infrastructure — a nice-to-
have for this strategy's 15m-4H timeframes, not something that will make or
break it.

### 2. Point a domain at it

Caddy (below) gets you free, automatic HTTPS, but only if a real domain
points at the server — Let's Encrypt won't issue a certificate for a bare
IP address. A cheap domain (or a subdomain of one you already own) is
enough:

1. Buy/use a domain, e.g. from Namecheap or Cloudflare Registrar.
2. Add an **A record**: `bot.yourdomain.com` → your server's IP.
3. Wait a few minutes for DNS to propagate (check with `dig bot.yourdomain.com`).

No domain yet and just want to test? See the commented-out block in
`Caddyfile` for a plain-HTTP fallback — but don't attach real credentials
or go live over it; the Connect tab sends your API secret and PIN in the
clear over unencrypted HTTP.

### 3. Install Docker on the server

```bash
ssh root@your-server-ip
curl -fsSL https://get.docker.com | sh
```

### 4. Get the code onto the server

```bash
# Option A — a git repo (recommended, makes updates a `git pull`)
git clone https://github.com/you/kehlo-trading.git
cd kehlo-trading

# Option B — copy the folder directly from your machine
# (run this from your OWN machine, not the server)
scp -r ./kehlo-trading root@your-server-ip:/root/
```

### 5. Configure

```bash
cp .env.example .env
nano .env
```

At minimum, set:
- `CREDENTIAL_ENCRYPTION_KEY` — generate one:
  `docker run --rm python:3.11-slim python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
- `LIVE_MODE_PIN` — pick something real, not `0011`

Bitget keys can go in `.env` too, or be left blank and attached later
through the dashboard's Connect tab.

Then edit `Caddyfile` and replace `your-domain.com` with your actual domain.

### 6. Basic firewall (recommended)

```bash
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw enable
```

### 7. Launch

```bash
docker compose up -d --build
docker compose logs -f
```

### 8. Verify

- `https://bot.yourdomain.com/api/status` in a browser should return JSON.
- Open `frontend/dashboard.jsx` (as a Claude artifact, or your own static
  build) and point the address field at `https://bot.yourdomain.com`.
- Connect tab → attach demo keys → status should flip to "Attached".
- Start the bot in demo and watch the Logs tab.

### Updating later

```bash
git pull
docker compose up -d --build
```

The database lives in a named Docker volume (`kehlo_data`), so it survives
rebuilds/restarts. `docker compose down -v` would delete it — don't run
that unless you actually want to wipe trade history.

### Once HTTPS is confirmed working

Remove the `ports: - "8000:8000"` line under the `api` service in
`docker-compose.yml` and re-run `docker compose up -d` — that closes the
unencrypted direct-to-API path now that Caddy is fronting it properly.

### Useful commands

```bash
docker compose ps                 # what's running
docker compose logs -f bot        # just the bot's logs
docker compose logs -f api        # just the API's logs
docker compose restart bot        # restart one service
docker compose down               # stop everything (keeps the data volume)
```

---

## Option B: Railway

### Why this path looks different

`docker-compose.yml` defines three services (bot, api, caddy) sharing one
volume. Railway **does not run docker-compose.yml directly** — each
service in a Compose file has to become its own separate Railway service —
and, more importantly, **Railway does not support attaching one volume to
two services** (confirmed in their own docs and community answers; their
official recommendation for sharing state between services is a real
database, not a raw volume).

Since bot.py and api.py need to read/write the *same* SQLite file, the
practical fix is `start_combined.py` — it runs bot.py's loop in a
background thread and the API in the main thread, **one process, one
service, one volume**. The tradeoff: they restart together if either
crashes, which is a reasonable trade for a single-operator bot that
already shares one database. (Splitting them into two real Railway
services later is possible, but means migrating off SQLite to Railway's
Postgres add-on — ask if you want that path instead.)

### Steps

1. Push this project to a GitHub repo (Railway deploys from GitHub).
2. Railway dashboard → **New Project → Deploy from GitHub repo** → pick it.
3. In the service's **Settings**:
   - **Root Directory**: `backend`
   - **Build**: Railway should auto-detect the Dockerfile there. If not,
     set the builder to "Dockerfile" explicitly.
   - **Custom Start Command**: `python3 start_combined.py`
   - **Restart Policy**: Always
4. Add a **Volume** (Settings → Volumes): mount path `/data`.
5. **Variables** tab — set:
   - `KEHLO_DB_PATH=/data/kehlo_trading.db`
   - `CREDENTIAL_ENCRYPTION_KEY` (generate the same way as above)
   - `LIVE_MODE_PIN` (something real)
   - `BITGET_PRODUCT_TYPE=USDT-FUTURES`
   - Bitget keys too, if you'd rather not use the Connect tab for the first setup
   - **Don't set `PORT`** — Railway injects it automatically and
     `start_combined.py` already reads it.
6. **Settings → Networking → Generate Domain** — Railway gives you a free
   `https://your-app.up.railway.app` URL with HTTPS already handled. No
   Caddy needed on this path.
7. Deploy. Check the deploy logs for both `"Bot process started"` and
   uvicorn's `"Uvicorn running on http://0.0.0.0:$PORT"`.
8. Point the dashboard's address field at your Railway URL and verify the
   same way as Option A, step 8.

### Updating later

Push to the connected GitHub branch — Railway redeploys automatically.

