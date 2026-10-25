# Marstek Grid Bot

Telegram bot + Mini App for monitoring **Marstek** home batteries (Venus E and others).
Tells you whether mains power is on or off, shows live power flows, and warns you before things go wrong.

> Unofficial project, not affiliated with Marstek / Hame. It talks to the same cloud and MQTT channel as the official app. Use at your own risk.

## Features

- **Grid status**: "power is on / off" (with duration), detected from the battery working state (EPS/backup = no grid)
- **Live data over MQTT**: SoC, battery / grid / EPS power, Wi-Fi signal, counters, ~2.5 s refresh
- **Alerts**: low charge (custom thresholds), fully charged, overload, lost connection
- **Runtime forecast** while on battery
- **Outage statistics**: 24 h / 7 d / 30 d, uptime %
- **Anomaly detector** (weak Wi-Fi, idle drain, SoC leakage / jumps, mode flapping)
- **Telegram Mini App** (served by the bot, exposed through a Cloudflare quick tunnel)
- **Multi-user**: admin approval, device sharing (viewer role), per-device settings
- Times are shown in `Europe/Kyiv`

Full feature list (Russian): [docs/FEATURES.ru.txt](docs/FEATURES.ru.txt).
Reverse-engineered API notes: [docs/ENDPOINTS.md](docs/ENDPOINTS.md).

## Requirements

- Python 3.11+
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- A Marstek account (the one used in the official app)
- For the Mini App: `cloudflared` (optional, see below)

## Setup

```bash
git clone <your-fork-url> marstek-grid-bot
cd marstek-grid-bot
python -m venv .venv
.venv/bin/pip install -r requirements.txt        # Windows: .venv\Scripts\pip
cp .env.example .env                              # then edit BOT_TOKEN
```

**MQTT certificates.** Live data needs client certificates. They are *not* included in this repo.
The helper downloads them from the public `ghcr.io/tomquist/hame-relay` image into `mqtt_certs/`:

```bash
python tools/pull_certs.py
```

**Mini App tunnel (optional).** Download [`cloudflared`](https://github.com/cloudflare/cloudflared/releases)
and put the binary at `tools/cloudflared.exe` (Windows). The bot starts a quick tunnel automatically and sets
the menu button. The URL changes on every start. Set `WEBAPP=0` in `.env` to disable the Mini App.

**Run.**

```bash
python bot.py
```

On Windows `start_bot.bat` creates the virtualenv, installs dependencies and starts the bot.

Send `/start` to the bot. If `ADMIN_ID` is `0`, the first user becomes admin. Then add your Marstek account in the bot's menu.

## Configuration (`.env`)

| Variable | Default | Description |
|---|---|---|
| `BOT_TOKEN` | – | Telegram bot token (required) |
| `ADMIN_ID` | `0` | Telegram user ID of the admin; `0` = first `/start` wins |
| `POLL_SEC` | `180` | Marstek cloud polling interval, seconds (min 10) |
| `WEBAPP` | `1` | `1` enables the Mini App, `0` disables it |
| `WEBAPP_PORT` | `8765` | Local port of the Mini App server (bound to 127.0.0.1) |

## Security notes

- `.env` and `data.db` contain secrets (bot token, Marstek credentials as MD5 hash, tokens). **Never commit or share them.**
- Mini App auth uses Telegram's signed `initData` (HMAC with the bot token, 2-day expiry); the server listens on `127.0.0.1` only.
- Marstek's API requires an MD5 hash of the account password. The bot deletes the chat message containing it.

## Project layout

```
bot.py          Telegram bot: screens, alerts, LIVE mode, settings, tunnel
webapp.py       Mini App HTTP server (aiohttp)
webapp/         Mini App frontend
db.py           SQLite storage (users, accounts, devices, outages, history, sharing)
marstek.py      Marstek cloud client + grid state detection
mqtt_live.py    Live MQTT data, frame decoding
hame_mqtt.py    MQTT topic ID derivation
anomaly.py      Anomaly detector (pure functions)
tools/          pull_certs.py, mqtt_probe.py
docs/           API notes, feature list
```

## Credits

MQTT field meanings and relay certificates come from the community project
[tomquist/hame-relay](https://github.com/tomquist/hame-relay) and
[tomquist/hm2mqtt](https://github.com/tomquist/hm2mqtt).

## License

[MIT](LICENSE)
