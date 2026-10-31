# Telegram Group Voice Bot — Render-ready package

This project uses Telegram long polling and an always-running `aiohttp` server. It is **not a Vercel serverless function** as-is. Deploy it as a Python Web Service on a host that runs a persistent process (the included Render blueprint is configured for this).

## Deploy on Render

1. Upload these files to a GitHub repository: `bot.py`, `miniapp.html`, `requirements.txt`, and `render.yaml`.
2. In Render, choose **New → Blueprint** and connect that repository, or create a Python Web Service manually.
3. Build command: `pip install -r requirements.txt`
4. Start command: `python bot.py`
5. Set these environment variables in the service settings:
   - `BOT_TOKEN`: Telegram bot token from BotFather
   - `OWNER_IDS`: owner Telegram numeric ID(s), comma-separated
   - `WEBAPP_URL`: your deployed service's public HTTPS URL, e.g. `https://your-service.onrender.com` (set/update this after Render gives you the URL)
   - `DB_FILE`: `/var/data/data.json` only if a persistent disk is mounted at `/var/data`; otherwise set it to `data.json` and know that local data can be lost on redeploy/restart.
6. Redeploy after changing environment variables.
7. Open `https://YOUR-SERVICE.onrender.com/app` to check the Mini App page. Open the bot in Telegram and send `/start` or `/panel`.

## Notes

- Do not put the actual bot token in source files or commit it to GitHub.
- Render's free web services can spin down after inactivity, which can interrupt polling until the service wakes up. See Render's current plan limits.
- The bot uses a local JSON file for settings/statistics. For reliable persistence, use a persistent disk or migrate storage to a database.
- TTS: `TTS_PROVIDER=both` tries Cartesia, then ElevenLabs, then falls back to Edge TTS. Keys come from environment variables only.
- For Telegram Mini Apps, `WEBAPP_URL` must be a public HTTPS URL.


## Reply behavior and messages from other bots
Generated voice messages always reply to the latest text message collected from that sender. To receive messages posted by other Telegram bots, configure BotFather's bot-to-bot communication mode if available for your bot, and disable Group Privacy with `/setprivacy` where applicable. The bot must be in the group and Telegram must deliver those messages to it; code changes cannot override Telegram delivery restrictions.


## v2 additions
- **Persistence:** settings/history/admins are saved to `DB_FILE` and auto-backed-up to a pinned Telegram document in `BACKUP_CHAT_ID` (default: first `OWNER_IDS`; the owner must have pressed Start). On start the newer copy wins, so Render redeploys keep your data.
- **Admin approval:** users send `/request` (or tap the button in the panel); owners approve in the panel or with the inline buttons / `/approve <id>`.
- **Browser login:** set `PANEL_USER` + `PANEL_PASS` (owner) or let each approved admin set a username/password in Panel -> More. Open `WEBAPP_URL/app` in any browser.
- **Commands:** send `/help` in the bot for the full list.

## v3 fixes
- **Group voice replies:** group settings now store only overrides, so Global defaults (targets, voice) are inherited and old frozen snapshots are healed on start. Targets match by @username, first name or numeric ID; captions are read too. If voice generation fails, owners get a private alert.
- **Voice library:** one shared library, managed only in the Mini App (add/edit/delete/select). `/voices` lists everything for everyone; `/addvoice`, `/usevoice`, `/delvoice` now just point to the panel.
- **Always on:** the bot self-pings `/health` every 4 min (uses `RENDER_EXTERNAL_URL`/`WEBAPP_URL`). For extra safety add a free UptimeRobot monitor on `https://YOUR-SERVICE.onrender.com/health`.
- **Persistence:** backup to the pinned Telegram file runs within ~5s of any change. For zero-risk storage, mount a Render disk and set `DB_FILE=/var/data/data.json`.
- **UI:** glass/blur depth design across the whole panel.
- 
