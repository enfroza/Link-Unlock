# Link Unlock — Telegram Bot + FastAPI

A lightweight, single-file application that combines a **Telegram bot** and a **FastAPI REST API**.

Users create gated unlock links via the bot. Visitors open a frontend page, complete a short verification step, then receive the final content link.

---

## Features

- **Telegram Bot** (button-driven)
  - Create new unlock links in 2 simple steps
  - View your last 10 links
  - Cancel anytime
- **REST API**
  - Serves link configuration to the frontend
  - CORS enabled for all origins
- **SQLite** (via `aiosqlite`) — zero external database setup
- Runs bot + API concurrently in one process
- Low memory footprint

---

## Architecture

```
Telegram User
     │
     ▼
┌─────────────────┐         ┌──────────────────┐
│  Telegram Bot   │────────▶│   SQLite (links) │
└─────────────────┘         └──────────────────┘
                                      │
                                      ▼
┌─────────────────┐         ┌──────────────────┐
│  Frontend Page  │◀────────│   FastAPI API    │
│  (Netlify)      │  GET    │   /api/config/id │
└─────────────────┘         └──────────────────┘
```

1. User creates a link in Telegram → stored in SQLite
2. Bot returns a shareable URL: `https://hornyunlock.netlify.app/?id=123`
3. Visitor opens the page → frontend fetches config from your API
4. After the timer, the unlock link is revealed

---

## Requirements

- Python 3.10+
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

---

## Quick Start

```bash
# 1. Clone the repo
git clone https://github.com/YOUR_USERNAME/link-unlock.git
cd link-unlock

# 2. Install dependencies
pip install -r requirements.txt

# 3. Set your bot token
export BOT_TOKEN="123456789:ABC-DEF..."

# 4. Run
python main.py
```

The API starts on `http://0.0.0.0:8000` and the bot begins polling.

---

## Environment Variables

| Variable            | Required | Default                              | Description                          |
|---------------------|----------|--------------------------------------|--------------------------------------|
| `BOT_TOKEN`         | **Yes**  | —                                    | Telegram bot token from @BotFather   |
| `DB_PATH`           | No       | `links.db`                           | Path to the SQLite database file     |
| `FRONTEND_BASE_URL` | No       | `https://hornyunlock.netlify.app`    | Base URL used when generating links  |
| `API_HOST`          | No       | `0.0.0.0`                            | Host the API binds to                |
| `API_PORT`          | No       | `8000`                               | Port the API listens on              |

Example:

```bash
export BOT_TOKEN="123456:ABC..."
export API_PORT=8080
export FRONTEND_BASE_URL="https://your-frontend.com"
python main.py
```

---

## Telegram Bot Usage

| Action            | Description                                              |
|-------------------|----------------------------------------------------------|
| `/start`          | Show main menu                                           |
| 🆕 Create New Link | Step 1 → send Open URL<br>Step 2 → send Unlock URL       |
| 📋 My Links       | List your last 10 created links                          |
| ❌ Cancel         | Abort the current creation flow                          |

After creating a link you receive a ready-to-share URL:

```
https://hornyunlock.netlify.app/?id=42
```

---

## API Endpoints

### `GET /health`

```json
{ "status": "ok" }
```

### `GET /api/config/{config_id}`

Returns the configuration the frontend needs.

**Success (200)**

```json
{
  "openLinkUrl": "https://example.com/offer",
  "unlockUrl": "https://example.com/content",
  "branding": {
    "pageTitle": "Link Unlock — Premium Content Gate",
    "cardTitle": "UNLOCK <span>LINK</span>",
    "cardSubtitle": "Complete the actions below to unlock the content"
  },
  "steps": {
    "step1Label": "Step 1 — Visit Link",
    "step2Label": "Step 2 — Unlock Content",
    "openButtonText": "OPEN LINK",
    "unlockButtonText": "Complete Steps to Unlock"
  },
  "timer": { "duration": 30 },
  "messages": {
    "successTitle": "Link Unlocked!",
    "successSub": "Your content has been unlocked successfully.<br/>The link has been opened in a new tab."
  }
}
```

**Not found (404)**

```json
{ "detail": "Link configuration not found" }
```

---

## Database Schema

```sql
CREATE TABLE links (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id  INTEGER NOT NULL,
    open_url     TEXT NOT NULL,
    unlock_url   TEXT NOT NULL,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

The database file (`links.db`) is created automatically on first run.

---

## Frontend Integration

Point your frontend's `API_BASE` to the public URL of this server:

```js
const API_BASE = "https://your-vps-or-domain.com";
```

Then the page loads config with:

```
GET {API_BASE}/api/config/{id}
```

Make sure the API is publicly reachable (and ideally behind HTTPS).

---

## Production Tips

- Run behind a reverse proxy (Nginx / Caddy) with HTTPS
- Use a process manager (`systemd`, `pm2`, or `docker`)
- Set a strong, unique `BOT_TOKEN` and never commit it
- Optionally move `links.db` to a persistent volume

Example systemd service:

```ini
[Unit]
Description=Link Unlock Bot + API
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/link-unlock
Environment=BOT_TOKEN=your-token-here
Environment=API_PORT=8000
ExecStart=/usr/bin/python3 main.py
Restart=always

[Install]
WantedBy=multi-user.target
```

---

## License

MIT
```
