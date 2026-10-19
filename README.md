# Link Unlock — private multi-user Telegram bot + FastAPI

A single-file (`main.py`) application: a **Telegram bot** where the owner and
approved users create gated unlock links, and a **FastAPI** service that feeds
link configs to the public frontend.

* Access is **invitation-only** and controlled by the owner (`ADMIN_ID`).
* Every link has a **global, never-reused numeric ID**.
* Every approved user has **their own links and personal Open URL**.
* Removing or banning a user **never deletes their data**; their public links keep working.

---

## Roles

| Role | Who | Can do |
|---|---|---|
| **Owner** | Telegram numeric ID in `ADMIN_ID` | Everything below + manage users, system stats, inspect all links. Cannot be removed/banned. |
| **Approved user** | `approved_users.status = 'approved'` | Create unlimited links; view/edit/delete **only their own**; set personal Open URL. |
| **Unauthorized** | anyone else (never approved, removed, banned) | `/start` shows *Access Restricted*; `/help` explains how to request access. |

All checks use Telegram's verified numeric user ID — never usernames.

## Bot commands

| Command | Who | Description |
|---|---|---|
| `/start` | all | Dashboard (approved) or *Access Restricted* |
| `/help` | all | Help; unauthorized users see their own Telegram ID to send to the owner |
| `/cancel` | approved | Abort the current flow |
| `/adduser <id>` | owner | Approve (or re-approve) a user |
| `/removeuser <id>` | owner | Revoke access — **data is kept** |
| `/users [approved\|removed\|banned\|all] [page]` | owner | Paginated user list |
| `/userinfo <id>` | owner | Status, timestamps, link count/ID range, saved Open URL |
| `/ban <id>` / `/unban <id>` | owner | Ban (data kept) / lift ban (user becomes *removed*) |
| `/stats` | owner | Users by status, link totals, highest ID, last-24h links |
| `/alllinks [page]` | owner | Read-only list of every link with its owner |

Dashboard buttons: **Create Link · My Links · Set Open URL · Statistics · Help**;
the owner additionally gets **Manage Users · Add User · Remove User · View All Users · System Statistics**.

### Removal, ban and re-approval

`/removeuser` sets `status='removed'`. Immediately: no link creation, no My Links / edit / delete,
any in-progress conversation is rejected, and the write itself re-checks authorization in SQL.
Links, saved Open URL and the ID sequence are untouched and the public frontend links keep working.
`/adduser` later flips the **same row** back to `approved` and the user sees everything again.
`/ban` behaves the same but also blocks `/adduser` until `/unban`. `/unban` leaves the user as
*removed* — approve again explicitly.

## Authorization and ownership design

* `is_authorized()` reads `approved_users` on **every** update (no cache) → revocation is immediate.
* Every handler, **including each step of a conversation**, is wrapped in `@require_approved`
  (or `@require_admin`). Admin buttons/callbacks are checked against `ADMIN_ID` on every press,
  so forged callback data from a non-owner does nothing.
* Writes re-authorize **inside the SQL statement** (`INSERT … SELECT … WHERE EXISTS(approved)`),
  and ownership is part of the query: `UPDATE/DELETE … WHERE id = ? AND telegram_id = ?`.
  Callback data is never trusted for authorization.
* The bot only responds in private chats. All SQL is parameterized.
* URLs must be `http(s)://` with a host, ≤ 2048 chars, no whitespace/control/`<>"` characters
  (`javascript:`, `file:`, `data:` … are rejected).

## Database

SQLite, same file as before (`DB_PATH`). On every start `init_db()` runs inside one transaction:

* `links` and `user_prefs` are left exactly as they are (`CREATE TABLE IF NOT EXISTS`); the old
  `saved_unlock_url → saved_open_url` rename is applied only if still needed.
* New table `approved_users(telegram_id PK, status approved|removed|banned, added_at, updated_at, added_by)`
  plus two indexes; `PRAGMA user_version` is set to 2.
* Nothing is dropped, rewritten or reset. IDs come from `INTEGER PRIMARY KEY AUTOINCREMENT`, so they
  are global, unique under concurrency, and never reused (the `sqlite_sequence` row is preserved).
* **Existing creators are not auto-approved.** After upgrading, approve them with `/adduser`.
  To list historic creators:
  ```bash
  sqlite3 links.db "SELECT telegram_id, COUNT(*) FROM links GROUP BY telegram_id;"
  ```
* WAL mode is enabled (better reader/writer concurrency). Back up with the service stopped, or with
  `sqlite3 links.db ".backup 'backup.db'"`, rather than copying `links.db` alone while running.

## REST API (unchanged)

`GET /health` → `{"status":"ok"}` · `GET /api/config/{id}` → same JSON as before
(`openLinkUrl`, `unlockUrl`, `branding`, `steps`, `timer`, `messages`), `404 {"detail":"Link configuration not found"}`
for unknown IDs. No owner IDs or private settings are returned, there are no admin/user-management
endpoints, and links keep resolving after their creator loses bot access.

> **Known limitation (pre-existing):** the frontend contract returns `unlockUrl` in the same response as
> `openLinkUrl`, with the 30 s timer enforced client-side, so the gate is not enforced server-side.
> Fixing that needs a frontend change (e.g. a token + server-side timer endpoint), so it was deliberately
> not changed here.

## Configuration

Read from the environment, or from a `.env` file next to `main.py` (real environment variables win).

| Variable | Required | Default | Description |
|---|---|---|---|
| `BOT_TOKEN` | **yes** | — | Token from @BotFather |
| `ADMIN_ID` | **yes** | — | Owner's numeric Telegram ID |
| `DB_PATH` | no | `links.db` | SQLite file |
| `FRONTEND_BASE_URL` | no | `https://hornyunlock.netlify.app` | Base for generated links (`…/?id=N`) |
| `API_HOST` | no | `0.0.0.0` | API bind address (`.env.example` suggests `127.0.0.1` behind a proxy) |
| `API_PORT` | no | `8000` | API port |

Startup fails with a clear message if `BOT_TOKEN`/`ADMIN_ID` are missing or invalid.
Find your numeric ID by messaging [@userinfobot](https://t.me/userinfobot).

## Local run

```bash
python3 -m venv venv && . venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # edit BOT_TOKEN and ADMIN_ID
python main.py
```

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

The suite covers roles, isolation, global IDs, removal/ban/re-approval, mid-conversation revocation,
concurrency, migration of a legacy database, URL validation and API compatibility. It uses fakes and
never contacts Telegram.

## Ubuntu 24.04 VPS — fresh install

```bash
sudo apt update && sudo apt install -y python3-venv sqlite3
sudo useradd --system --home /opt/link-unlock --shell /usr/sbin/nologin linkunlock
sudo mkdir -p /opt/link-unlock && sudo chown linkunlock:linkunlock /opt/link-unlock
sudo -u linkunlock git clone https://github.com/enfroza/Link-Unlock /opt/link-unlock
cd /opt/link-unlock
sudo -u linkunlock python3 -m venv venv
sudo -u linkunlock venv/bin/pip install -r requirements.txt
sudo -u linkunlock cp .env.example .env && sudo chmod 600 .env && sudo -u linkunlock nano .env
sudo cp link-unlock.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now link-unlock
journalctl -u link-unlock -f
```

Put Nginx/Caddy with HTTPS in front of port 8000 and point the frontend's `API_BASE` at it.

## Updating an existing VPS without losing the database

Adjust paths/service name to match your current setup.

```bash
# 1. Back up (stop first so the DB is consistent)
sudo systemctl stop link-unlock
cd /opt/link-unlock
mkdir -p backup-$(date +%F) && cp -a links.db* backup-$(date +%F)/

# 2. Replace main.py (links.db is NOT touched or recreated)
cp main.py main.py.old
# now copy the new main.py over it (git pull, scp, ...)

# 3. Add the owner ID (and keep your existing BOT_TOKEN etc.)
echo 'ADMIN_ID=123456789' >> .env     # or add Environment=ADMIN_ID=… to your unit file

# 4. Dependencies are unchanged; this is a no-op unless you use a fresh venv
venv/bin/pip install -r requirements.txt

# 5. Restart and verify
sudo systemctl start link-unlock
journalctl -u link-unlock -n 50 --no-pager     # expect: "Database ready … (schema v2)"
curl -s http://127.0.0.1:8000/health
```

Then send `/start` from the owner account, and `/adduser <id>` for each existing creator.
**Rollback:** stop the service, restore `main.py.old` and the files from `backup-<date>/`, start the service.
(The old code ignores the new `approved_users` table, so the migrated DB is also safe with it.)

If your current unit file sets `Environment=BOT_TOKEN=…` lines, those keep working; `link-unlock.service`
in this repo is an optional hardened replacement that reads `/opt/link-unlock/.env`.

## License

MIT
