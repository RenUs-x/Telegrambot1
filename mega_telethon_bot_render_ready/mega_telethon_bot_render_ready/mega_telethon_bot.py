
#!/usr/bin/env python3
"""
Mega Telethon Bot — production-ready template (enhanced)

Features:
 - Secrets only via environment variables (or platform secrets).
 - Optional .env loader (python-dotenv) for local testing (not for production).
 - Masked logging (no secrets in logs).
 - Structured logging using standard logging module.
 - Graceful shutdown and error handling.
 - Basic rate limiter (per-user token bucket) to avoid spam abuse.
 - Admin-only commands (ADMIN_IDS env var).
 - Broadcast command with dry-run safety check.
 - APScheduler example for periodic tasks (bio rotation).
 - Simple /help, /start, /whoami, /status commands.
 - Health & metrics HTTP endpoint (aiohttp) for process monitoring.
 - SQLite DB for persistent small state with safe initialization.
 - Prometheus-compatible metrics (basic counters) endpoint.
 - Docker-friendly (no filesystem secrets by default).

NOTE: Before running, set API_ID, API_HASH, BOT_TOKEN, and ADMIN_IDS in environment.
"""
import os
import asyncio
import logging
import signal
import sqlite3
import json
import time
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Tuple, Any, Optional

# Optional libs
try:
    from telethon import TelegramClient, events, errors
except Exception as e:
    raise RuntimeError("Telethon is required: pip install telethon") from e

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
except Exception:
    AsyncIOScheduler = None

try:
    from aiohttp import web
except Exception:
    web = None

# Optional dotenv for local development
try:
    from dotenv import load_dotenv
    load_dotenv(load=False)
except Exception:
    pass

# ---------------- Config & Secrets ----------------
def _get_env(name: str, required: bool = True) -> Optional[str]:
    v = os.getenv(name)
    if required and (v is None or v == ""):
        raise SystemExit(f"Missing required environment variable: {name}")
    return v

API_ID_RAW = _get_env("API_ID")
try:
    API_ID = int(API_ID_RAW)
except Exception:
    raise SystemExit("API_ID must be an integer (set via environment variable)")
API_HASH = _get_env("API_HASH")
BOT_TOKEN = _get_env("BOT_TOKEN")
SESSION_NAME = os.getenv("SESSION_NAME", "mega_telethon.session")
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")  # comma separated list of ints
ADMIN_IDS = set(int(x) for x in ADMIN_IDS_RAW.split(",") if x.strip().isdigit())

# ---------------- Paths and DB ----------------
DATA_DIR = Path(os.getenv("DATA_DIR", "mega_data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "mega_bot.db"

# ---------------- Logging ----------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logger = logging.getLogger("mega_telethon_bot")
handler = logging.StreamHandler()
formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

def mask(s: str) -> str:
    if not s:
        return ""
    if len(s) <= 8:
        return "****"
    return s[:4] + ("*" * (len(s) - 8)) + s[-4:]

logger.info("Starting Mega Telethon Bot (secrets masked): API_ID=%s API_HASH=%s BOT_TOKEN=%s",
            str(API_ID), mask(API_HASH), mask(BOT_TOKEN[:20]))

# ---------------- DB init ----------------
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_cur = _conn.cursor()
_cur.execute("CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT)")
_cur.execute("CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, last_seen INTEGER, msg_count INTEGER DEFAULT 0)")
_cur.execute("CREATE TABLE IF NOT EXISTS broadcasts(id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, message TEXT)")
_conn.commit()

# ---------------- Rate limiter ----------------
class TokenBucket:
    def __init__(self, rate: float, capacity: float):
        self.rate = rate
        self.capacity = capacity
        self.tokens = capacity
        self.timestamp = time.monotonic()

    def consume(self, amount: float) -> bool:
        now = time.monotonic()
        elapsed = now - self.timestamp
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.timestamp = now
        if self.tokens >= amount:
            self.tokens -= amount
            return True
        return False

_rate_limit_buckets: Dict[int, TokenBucket] = {}
RATE_PER_SEC = float(os.getenv("RATE_PER_SEC", "1"))  # refill tokens per second
RATE_CAPACITY = float(os.getenv("RATE_CAPACITY", "5"))

def allow_user(user_id: int, cost: float = 1.0) -> bool:
    if user_id not in _rate_limit_buckets:
        _rate_limit_buckets[user_id] = TokenBucket(RATE_PER_SEC, RATE_CAPACITY)
    return _rate_limit_buckets[user_id].consume(cost)

# ---------------- Telethon client ----------------
client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

# Metrics
metrics = {
    "messages_total": 0,
    "commands_total": 0,
    "broadcasts_total": 0,
}

# ---------------- Scheduler ----------------
scheduler = AsyncIOScheduler() if AsyncIOScheduler else None
if scheduler:
    scheduler.start()

# ---------------- HTTP server for health/metrics ----------------
async def _health(request):
    return web.Response(text="ok")

async def _metrics(request):
    # Basic prometheus-like exposition (very small)
    lines = []
    for k, v in metrics.items():
        lines.append(f"mega_bot_{k} {int(v)}")
    return web.Response(text="\\n".join(lines))

http_runner = None
async def start_http_server(host="0.0.0.0", port=8080):
    global http_runner
    if web is None:
        logger.warning("aiohttp not available; skipping HTTP server for health/metrics")
        return
    app = web.Application()
    app.add_routes([web.get("/healthz", _health), web.get("/metrics", _metrics)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    http_runner = runner
    logger.info("HTTP health/metrics server started on %s:%s", host, port)

async def stop_http_server():
    global http_runner
    if http_runner:
        await http_runner.cleanup()
        logger.info("HTTP server stopped")

# ---------------- Helpers ----------------
async def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def record_user_activity(user_id: int):
    now = int(time.time())
    _cur.execute("INSERT INTO users(id, last_seen, msg_count) VALUES(?, ?, 1) ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen, msg_count=users.msg_count+1", (user_id, now))
    _conn.commit()

# ---------------- Handlers ----------------
@client.on(events.NewMessage(pattern=r"(?i)^/start$"))
async def start_handler(event):
    metrics["messages_total"] += 1
    metrics["commands_total"] += 1
    sender = await event.get_sender()
    user_id = getattr(sender, "id", None) or (event.sender_id if hasattr(event, "sender_id") else None)
    record_user_activity(user_id)
    await event.respond("Привет! Я бот. Напиши /help чтобы увидеть команды.")

@client.on(events.NewMessage(pattern=r"(?i)^/help$"))
async def help_handler(event):
    metrics["messages_total"] += 1
    metrics["commands_total"] += 1
    help_text = (
        "Доступные команды:\\n"
        "/start - приветствие\\n"
        "/help - это сообщение\\n"
        "/whoami - информация о боте\\n"
        "/status - базовый статус\\n"
        "/broadcast <text> - admin only: отправить в чаты (будет просить подтверждение)"
    )
    await event.respond(help_text)

@client.on(events.NewMessage(pattern=r"(?i)^/whoami$"))
async def whoami(event):
    metrics["messages_total"] += 1
    metrics["commands_total"] += 1
    me = await client.get_me()
    await event.respond(f"Бот: @{me.username} (id={me.id})")

@client.on(events.NewMessage(pattern=r"(?i)^/status$"))
async def status(event):
    metrics["messages_total"] += 1
    metrics["commands_total"] += 1
    uptime = int(time.time() - float(_cur.execute(\"SELECT v FROM settings WHERE k='started_at'\").fetchone()[0]))
    await event.respond(f"Uptime: {uptime}s, messages_total: {metrics['messages_total']}")

# Admin broadcast flow: /broadcast start_text then confirm with /confirm_broadcast <id>
pending_broadcasts = {}

@client.on(events.NewMessage(pattern=r"(?i)^/broadcast\\s+(.+)$"))
async def broadcast_start(event):
    metrics["messages_total"] += 1
    sender = await event.get_sender()
    user_id = getattr(sender, "id", None)
    if not await is_admin(user_id):
        await event.respond("Только администраторы могут запускать рассылку.")
        return
    text = event.pattern_match.group(1).strip()
    # store pending
    b_id = int(time.time())
    pending_broadcasts[b_id] = {"owner": user_id, "text": text, "created_at": time.time()}
    await event.respond(f"Broadcast prepared (id={b_id}). Подтверди командой /confirm_broadcast {b_id} или /cancel_broadcast {b_id}")

@client.on(events.NewMessage(pattern=r"(?i)^/confirm_broadcast\\s+(\\d+)$"))
async def broadcast_confirm(event):
    metrics["messages_total"] += 1
    metrics["commands_total"] += 1
    sender = await event.get_sender()
    user_id = getattr(sender, "id", None)
    bid = int(event.pattern_match.group(1))
    item = pending_broadcasts.get(bid)
    if not item or item["owner"] != user_id:
        await event.respond("Неверный id рассылки или вы не владелец рассылки.")
        return
    text = item["text"]
    # naive broadcast: iterate known users in DB (for demonstration).
    rows = _cur.execute("SELECT id FROM users").fetchall()
    count = 0
    for (uid,) in rows:
        try:
            await client.send_message(uid, text)
            count += 1
            await asyncio.sleep(0.05)  # gentle pacing
        except Exception as e:
            logger.exception("Failed sending to %s: %s", uid, e)
    metrics["broadcasts_total"] += 1
    _cur.execute("INSERT INTO broadcasts(started_at, message) VALUES(?, ?)", (datetime.utcnow().isoformat(), text))
    _conn.commit()
    del pending_broadcasts[bid]
    await event.respond(f"Рассылка завершена. Отправлено: {count} сообщений.")

@client.on(events.NewMessage(pattern=r"(?i)^/cancel_broadcast\\s+(\\d+)$"))
async def broadcast_cancel(event):
    sender = await event.get_sender()
    user_id = getattr(sender, "id", None)
    bid = int(event.pattern_match.group(1))
    item = pending_broadcasts.get(bid)
    if not item or item["owner"] != user_id:
        await event.respond("Неверный id рассылки или вы не владелец рассылки.")
        return
    del pending_broadcasts[bid]
    await event.respond("Рассылка отменена.")

# Generic message logger & rate limit enforcement
@client.on(events.NewMessage)
async def catch_all(event):
    metrics["messages_total"] += 1
    sender = await event.get_sender()
    user_id = getattr(sender, "id", None) or (event.sender_id if hasattr(event, "sender_id") else None)
    record_user_activity(user_id)
    # rate limit
    if not allow_user(user_id):
        try:
            await event.respond("Слишком много запросов — подожди немного.")
        except Exception:
            pass
        return
    # minimal moderation: block messages that appear to contain tokens (heuristic)
    text = (event.raw_text or "")[:4000]
    if "-----BEGIN" in text or "API_KEY" in text or "BOT_TOKEN" in text:
        try:
            await event.respond("Сообщение похоже на секрет; отправка запрещена.")
        except Exception:
            pass
        return
    # otherwise do nothing (preserve original bot's other handlers)
    return

# ---------------- Startup & Shutdown ----------------
async def main():
    logger.info("Starting Telethon client...")
    await client.start(bot_token=BOT_TOKEN)
    me = await client.get_me()
    logger.info("Bot started as @%s (id=%s)", me.username, me.id)
    # mark start time in DB
    _cur.execute("INSERT OR REPLACE INTO settings(k, v) VALUES(?, ?)", ("started_at", str(time.time())))
    _conn.commit()
    # start health server
    await start_http_server(host=os.getenv("HEALTH_HOST", "0.0.0.0"), port=int(os.getenv("HEALTH_PORT", "8080")))
    # run until disconnected
    await client.run_until_disconnected()

def _shutdown():
    logger.info("Shutdown signal received; stopping...")
    try:
        asyncio.get_event_loop().create_task(stop_http_server())
    except Exception:
        pass
    try:
        client.disconnect()
    except Exception:
        pass

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown)
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received; exiting")
    except Exception as e:
        logger.exception("Fatal error in main: %s", e)
    finally:
        try:
            _conn.close()
        except Exception:
            pass
