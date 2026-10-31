"""
Telegram Group -> Voice Bot + Mini App Control Panel  (v2)

Env (Render -> Environment): BOT_TOKEN, OWNER_IDS, WEBAPP_URL, DB_FILE,
  TTS_PROVIDER=both|cartesia|elevenlabs|edge, CARTESIA_API_KEY, ELEVENLABS_API_KEY,
  (optional) CARTESIA_VOICE_BN/EN, ELEVENLABS_VOICE_BN/EN, CARTESIA_MODEL, ELEVENLABS_MODEL,
  BACKUP_CHAT_ID (default: first OWNER_ID), PANEL_USER + PANEL_PASS (owner browser login), SESSION_SECRET
API keys are read from environment only and are never sent to the panel.
"""
import asyncio, base64, hashlib, hmac, json, logging, os, re, secrets, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl

import edge_tts
import aiohttp
from aiohttp import web
from telegram import (BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, InputFile,
                      InputMediaDocument, MenuButtonWebApp, ReplyParameters, Update, WebAppInfo)
from telegram.ext import (ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not TOKEN or TOKEN == "PASTE_YOUR_TOKEN_HERE":
    raise RuntimeError("BOT_TOKEN environment variable is missing. Set it in your hosting dashboard.")
WEBAPP_URL = os.getenv("WEBAPP_URL", "").rstrip("/")
PORT = int(os.getenv("PORT", "8080"))
OWNERS = {int(x) for x in os.getenv("OWNER_IDS", "").split(",") if x.strip()}
DB_FILE = Path(os.getenv("DB_FILE", "data.json"))
CARTESIA_API_KEY = os.getenv("CARTESIA_API_KEY", "").strip()
CARTESIA_VOICE_BN = os.getenv("CARTESIA_VOICE_BN", "").strip()
CARTESIA_VOICE_EN = os.getenv("CARTESIA_VOICE_EN", "").strip()
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "").strip()
PROVIDERS = ("both", "cartesia", "elevenlabs", "edge")
TTS_PROVIDER = os.getenv("TTS_PROVIDER", "both").strip().lower()
if TTS_PROVIDER not in PROVIDERS:
    TTS_PROVIDER = "both"
BACKUP_CHAT = os.getenv("BACKUP_CHAT_ID", "").strip() or (str(min(OWNERS)) if OWNERS else "")
KEEPALIVE_URL = (os.getenv("RENDER_EXTERNAL_URL", "") or os.getenv("WEBAPP_URL", "")).rstrip("/")
PANEL_USER = os.getenv("PANEL_USER", "").strip().lower()
PANEL_PASS = os.getenv("PANEL_PASS", "")
SESSION_SECRET = (os.getenv("SESSION_SECRET") or hashlib.sha256(("session:" + TOKEN).encode()).hexdigest()).encode()

VOICES = {
    "bn": ["bn-BD-NabanitaNeural", "bn-BD-PradeepNeural", "bn-IN-TanishaaNeural", "bn-IN-BashkarNeural"],
    "en": ["en-US-AriaNeural", "en-US-GuyNeural", "en-US-JennyNeural", "en-GB-SoniaNeural", "en-IN-NeerjaNeural"],
}
# preset -> (speed delta %, pitch delta Hz, edge voice gender). Added on top of the manual sliders.
PRESETS = {"normal": (0, 0, None), "deep": (-8, -30, "m"), "robot": (-5, -12, "m"),
           "child": (10, 45, "f"), "deep_robot": (-15, -45, "m")}
MALE = {"bn": "bn-BD-PradeepNeural", "en": "en-US-GuyNeural"}
FEMALE = {"bn": "bn-BD-NabanitaNeural", "en": "en-US-JennyNeural"}
DEFAULTS = {
    "enabled": True, "default_timer": 30, "timers": {}, "only_listed": True, "ignore": [],
    "read_names": False, "lang": "auto", "voice_bn": VOICES["bn"][0], "voice_en": VOICES["en"][0],
    "rate": 0, "pitch": 0, "volume": 0, "preset": "normal", "preset_edge": True,
    "provider": TTS_PROVIDER, "cartesia_voice_bn": "", "cartesia_voice_en": "",
    "elevenlabs_voice_bn": "", "elevenlabs_voice_en": "",
    "strip_links": True, "strip_emoji": True, "min_chars": 1, "max_chars": 3000, "caption": False, "reply": True,
}
ENUMS = {"provider": set(PROVIDERS), "lang": {"auto", "bn", "en"}, "preset": set(PRESETS),
         "voice_bn": set(VOICES["bn"]), "voice_en": set(VOICES["en"])}
CLAMP = {"default_timer": (1, 600), "rate": (-50, 100), "pitch": (-50, 50), "volume": (-50, 50),
         "min_chars": (1, 500), "max_chars": (200, 20000)}
ID_RE = re.compile(r"^[A-Za-z0-9_\-]{6,80}$")
CHUNK = 800
EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("voicebot")

db: dict = {"chats": {}}
buffers: dict[tuple, dict[int, str]] = {}
last_mid: dict[tuple, int] = {}
timers: dict[tuple, asyncio.Task] = {}
locks: dict[tuple, asyncio.Lock] = {}
admin_cache: dict[tuple, tuple] = {}
login_hits: dict[str, list] = {}
last_tts: dict[int, float] = {}
backup = {"dirty": False, "mid": None}


# ---------- storage (local JSON + Telegram-pinned backup so Render redeploys keep data) ----------
def load():
    if DB_FILE.exists():
        try:
            db.update(json.loads(DB_FILE.read_text("utf-8")))
            db.setdefault("chats", {})
        except (OSError, json.JSONDecodeError):
            log.exception("Could not read database file %s", DB_FILE)

def save():
    try:
        db["_saved_at"] = time.time()
        DB_FILE.parent.mkdir(parents=True, exist_ok=True)
        # Atomic replace prevents half-written JSON if Render restarts mid-save.
        tmp = DB_FILE.with_suffix(DB_FILE.suffix + ".tmp")
        tmp.write_text(json.dumps(db, ensure_ascii=False, indent=1), "utf-8")
        os.replace(tmp, DB_FILE)
    except OSError:
        log.exception("Could not save database file %s", DB_FILE)
    backup["dirty"] = True

async def restore(bot):
    """On start: if the pinned backup in BACKUP_CHAT is newer than the local file, use it."""
    if not BACKUP_CHAT:
        return
    try:
        pm = (await bot.get_chat(BACKUP_CHAT)).pinned_message
        if not (pm and pm.document and pm.document.file_name == "voicebot-backup.json"):
            log.info("No pinned backup found in %s", BACKUP_CHAT); backup["dirty"] = True; return
        backup["mid"] = pm.message_id
        f = await bot.get_file(pm.document.file_id)
        data = json.loads(bytes(await f.download_as_bytearray()))
        if data.get("_saved_at", 0) > db.get("_saved_at", 0):
            db.clear(); db.update(data); db.setdefault("chats", {})
            migrate(); save(); backup["dirty"] = False
            log.info("Database restored from Telegram backup")
    except Exception:
        log.exception("Backup restore failed")

async def do_backup(bot):
    if not BACKUP_CHAT:
        return
    doc = InputFile(json.dumps(db, ensure_ascii=False).encode(), filename="voicebot-backup.json")
    if backup["mid"]:
        try:
            await bot.edit_message_media(chat_id=BACKUP_CHAT, message_id=backup["mid"], media=InputMediaDocument(doc))
            return
        except Exception:
            backup["mid"] = None
    m = await bot.send_document(BACKUP_CHAT, doc, caption="Auto backup (pinned) - do not delete", disable_notification=True)
    await bot.pin_chat_message(BACKUP_CHAT, m.message_id, disable_notification=True)
    backup["mid"] = m.message_id

async def backup_loop(bot):
    while True:
        await asyncio.sleep(5)
        if backup["dirty"]:
            backup["dirty"] = False
            try: await do_backup(bot)
            except Exception:
                backup["dirty"] = True; log.exception("Backup failed")

def chat(cid):
    return db["chats"].setdefault(str(cid), {"title": "", "s": {}, "stats": {"voices": 0, "chars": 0}})

def base_cfg() -> dict:
    return {**DEFAULTS, **db.get("global_settings", {})}

def cfg(cid) -> dict:
    # Global defaults are inherited by every group; a group only stores the values it overrides.
    if str(cid) == "__global__":
        return base_cfg()
    return {**base_cfg(), **chat(cid)["s"]}

def lib() -> list:
    """The ONE voice library shared by every group, /voices and the panel."""
    return db.setdefault("voice_lib", [])

def scopes():
    return [db.setdefault("global_settings", {})] + [c.setdefault("s", {}) for c in db["chats"].values()]

def migrate():
    L = clean_lib(db.get("voice_lib", []))
    for sc in scopes():
        for v in clean_lib(sc.pop("voice_lib", [])):
            if not any((x["id"], x["prov"], x["lang"]) == (v["id"], v["prov"], v["lang"]) for x in L):
                L.append(v)
    db["voice_lib"] = L[:100]
    db["chats"].pop("__global__", None)
    if db.get("_v", 0) < 3:  # drop old full-settings snapshots that froze groups away from global defaults
        for c in db["chats"].values():
            sc = c.setdefault("s", {})
            for k in [k for k, v in sc.items() if k in DEFAULTS and v == DEFAULTS[k]]:
                sc.pop(k)
        db["_v"] = 3

def command_scope(update):
    # Admin commands in private chat edit global defaults; in groups they edit that group.
    return "__global__" if update.effective_chat.type == "private" else update.effective_chat.id

def clean_lib(v):
    out, seen = [], set()
    for x in v if isinstance(v, list) else []:
        try:
            n, p, l, i = str(x["name"]).strip()[:30], str(x["prov"]).lower(), str(x["lang"]).lower(), str(x["id"]).strip()
            k = str(x.get("k") or "")
        except Exception:
            continue
        if not re.fullmatch(r"[0-9a-f]{8}", k) or k in seen:
            k = secrets.token_hex(4)
        if n and p in ("cartesia", "elevenlabs") and l in ("bn", "en") and ID_RE.match(i):
            seen.add(k); out.append({"k": k, "name": n, "prov": p, "lang": l, "id": i})
    return out[:100]

def sanitize(s: dict) -> dict:
    out = {}
    norm = lambda x: str(x).lower().lstrip("@").strip()
    for k, d in DEFAULTS.items():
        if k not in s:
            continue
        v = s[k]
        try:
            if isinstance(d, bool): out[k] = bool(v)
            elif isinstance(d, int):
                lo, hi = CLAMP.get(k, (-10**6, 10**6)); out[k] = max(lo, min(hi, int(v)))
            elif isinstance(d, list): out[k] = [norm(x) for x in v if norm(x)]
            elif isinstance(d, dict): out[k] = {norm(a): max(1, min(600, int(b))) for a, b in v.items() if norm(a)}
            else: out[k] = str(v).strip()[:80]
        except Exception:
            continue
        if k in ENUMS and out.get(k) not in ENUMS[k]:
            out.pop(k, None)
        if k.endswith(("voice_bn", "voice_en")) and k[:3] in ("car", "ele") and out.get(k) and not ID_RE.match(out[k]):
            out.pop(k, None)
    return out

def put(cid, **kw):
    clean = sanitize(kw)
    if str(cid) == "__global__":
        db.setdefault("global_settings", {}).update(clean)
    else:
        chat(cid)["s"].update(clean)
    save()

def today(): return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def record(cid, uid, text, prov, lang, preset, fid, target=""):
    """History + usage statistics for every generated voice."""
    h = db.setdefault("history", [])
    hid = db["_hid"] = db.get("_hid", 0) + 1
    h.append({"id": hid, "cid": cid, "uid": uid, "ts": int(time.time()), "text": text[:300], "prov": prov,
              "lang": lang, "preset": preset, "fid": fid, "fav": False, "chars": len(text), "target": target})
    if len(h) > 300:
        favs = [x for x in h if x["fav"]][-100:]
        db["history"] = sorted(favs + [x for x in h if not x["fav"]][-200:], key=lambda x: x["id"])
    if str(cid) in db["chats"]:
        st = chat(cid)["stats"]
        st["voices"] += 1; st["chars"] += len(text)
        pv = st.setdefault("prov", {}); pv[prov] = pv.get(prov, 0) + 1
        dl = st.setdefault("daily", {}); d = dl.setdefault(today(), [0, 0]); d[0] += 1; d[1] += len(text)
        for old in sorted(dl)[:-30]: dl.pop(old)
    save()


# ---------- text / tts ----------
def is_bangla(t):
    bn = len(re.findall(r"[\u0980-\u09FF]", t))
    return bn / (len(re.findall(r"\w", t)) or 1) > 0.3

def pick_lang(text, s):
    return s["lang"] if s.get("lang", "auto") != "auto" else ("bn" if is_bangla(text) else "en")

def split_text(text, limit=CHUNK):
    chunks, cur = [], ""
    for p in re.split(r"(?<=[.!?।\n])\s+", text):
        if len(cur) + len(p) + 1 <= limit:
            cur = f"{cur} {p}".strip()
        else:
            if cur: chunks.append(cur)
            while len(p) > limit:
                chunks.append(p[:limit]); p = p[limit:]
            cur = p
    if cur: chunks.append(cur)
    return chunks

def voice_settings(s):
    """Presets add to the manual sliders (they never delete them) and pick a matching Edge voice."""
    x = dict(s)
    dr, dp, g = PRESETS.get(x.get("preset", "normal"), PRESETS["normal"])
    x["rate"] = int(x.get("rate", 0)) + dr
    x["pitch"] = int(x.get("pitch", 0)) + dp
    if g:
        v = MALE if g == "m" else FEMALE
        x["voice_bn"], x["voice_en"] = v["bn"], v["en"]
    return x

# ---------- provider errors: tell the owner exactly what is wrong ----------
PNAME = {"cartesia": "Cartesia", "elevenlabs": "ElevenLabs", "edge": "Edge TTS"}
KIND_TEXT = {
    "quota": "credits / monthly quota used up",
    "auth": "API key is invalid or not allowed",
    "rate": "too many requests (rate limit)",
    "voice": "voice ID not found or not valid for this language",
    "config": "API key or voice ID is not set",
    "server": "service is down or unreachable",
    "other": "request was rejected",
}
KIND_FIX = {
    "quota": "Add credits or upgrade the plan, or switch provider in Panel > Voice > Provider.",
    "auth": "Check the API key in your hosting environment variables.",
    "rate": "This usually clears by itself. If it repeats, reduce simultaneous voices.",
    "voice": "Check the voice ID in Panel > Voice > Voice library.",
    "config": "Set the API key and voice ID.",
    "server": "The provider seems to be down. It should recover by itself.",
    "other": "Check the details below.",
}
QUOTA_WORDS = ("quota", "credit", "insufficient", "billing", "payment required", "out of characters",
               "character limit", "usage limit")
pstat = {p: {"ok": None, "kind": "", "msg": "", "ts": 0} for p in ("cartesia", "elevenlabs")}
notified: dict = {}
app_ref: dict = {}


class TTSError(RuntimeError):
    def __init__(self, provider, kind, detail=""):
        self.provider, self.kind, self.detail = provider, kind, detail
        super().__init__(f"{PNAME.get(provider, provider)}: {KIND_TEXT.get(kind, kind)}")


class TTSFailure(RuntimeError):
    """Every selected provider failed. Owners were already notified."""


def classify(status, body):
    b = (body or "").lower()
    if status == 402:
        return "quota"
    if status == 429:
        return "quota" if any(w in b for w in ("quota", "credit", "character")) else "rate"
    if any(w in b for w in QUOTA_WORDS):
        return "quota"
    if status in (401, 403):
        return "auth"
    if status == 404 or (status in (400, 422) and "voice" in b):
        return "voice"
    if status >= 500:
        return "server"
    return "other"


async def notify_owners(key, text, cooldown=3600):
    if time.time() - notified.get(key, 0) < cooldown or not app_ref.get("bot"):
        return
    notified[key] = time.time()
    for o in OWNERS:
        try: await app_ref["bot"].send_message(o, text[:800])
        except Exception: pass


def note_ok(p):
    st = pstat.get(p)
    if st is None:
        return
    was_bad = st["ok"] is False
    st.update(ok=True, kind="", msg="", ts=time.time())
    if was_bad:
        asyncio.create_task(notify_owners((p, "recovered"), f"✅ {PNAME[p]} is working again.", 0))


def note_failure(p, e):
    st = pstat.get(p)
    if st is not None:
        st.update(ok=False, kind=e.kind, msg=KIND_TEXT.get(e.kind, ""), ts=time.time())
    text = f"⚠️ {PNAME.get(p, p)} problem: {KIND_TEXT.get(e.kind, e.kind)}\nFix: {KIND_FIX.get(e.kind, '')}"
    if e.detail:
        text += f"\nDetails: {e.detail[:200]}"
    asyncio.create_task(notify_owners((p, e.kind), text, 900 if e.kind in ("server", "rate") else 3600))


def user_error(exc, uid):
    if isinstance(exc, TTSFailure):
        if is_staff(uid):
            return f"⚠️ Voice could not be created.\n{exc}\nOpen the panel (Home) for the fix."
        return "⚠️ The voice service is temporarily unavailable. The owner has been notified."
    return "⚠️ Voice could not be created. Please try again in a moment."


async def post_audio(provider, url, headers, payload):
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as ses:
            async with ses.post(url, headers=headers, json=payload) as r:
                if r.status >= 400:
                    body = (await r.text())[:300]
                    raise TTSError(provider, classify(r.status, body), f"HTTP {r.status}: {body}")
                return await r.read()
    except TTSError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise TTSError(provider, "server", type(exc).__name__) from exc


async def cartesia_tts(text, s, lang):
    voice_id = (s.get("cartesia_voice_bn") or CARTESIA_VOICE_BN) if lang == "bn" else (s.get("cartesia_voice_en") or CARTESIA_VOICE_EN)
    if not CARTESIA_API_KEY or not voice_id:
        raise TTSError("cartesia", "config", "API key or voice ID is not set")
    rate = max(0.6, min(1.5, 1.0 + float(s.get("rate", 0)) / 200))
    volume = max(0.5, min(2.0, 1.0 + float(s.get("volume", 0)) / 50))
    payload = {
        "model_id": os.getenv("CARTESIA_MODEL", "sonic-3.6"),
        "transcript": text,
        "voice": {"mode": "id", "id": voice_id},
        "language": "bn" if lang == "bn" else "en",
        "output_format": {"container": "mp3", "encoding": "mp3", "sample_rate": 44100},
        "generation_config": {"speed": rate, "volume": volume},
    }
    headers = {"Authorization": f"Bearer {CARTESIA_API_KEY}", "Cartesia-Version": "2025-04-16", "Content-Type": "application/json"}
    return await post_audio("cartesia", "https://api.cartesia.ai/tts/bytes", headers, payload)

async def elevenlabs_tts(text, s, lang):
    voice_id = (s.get("elevenlabs_voice_bn") or os.getenv("ELEVENLABS_VOICE_BN", "").strip()) if lang == "bn" else (s.get("elevenlabs_voice_en") or os.getenv("ELEVENLABS_VOICE_EN", "").strip())
    if not ELEVENLABS_API_KEY or not voice_id:
        raise TTSError("elevenlabs", "config", "API key or voice ID is not set")
    speed = max(0.7, min(1.2, 1.0 + float(s.get("rate", 0)) / 250))
    payload = {"text": text, "model_id": os.getenv("ELEVENLABS_MODEL", "eleven_multilingual_v2"),
               "voice_settings": {"stability": 0.5, "similarity_boost": 0.75, "speed": speed}}
    headers = {"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json", "Accept": "audio/mpeg"}
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=mp3_44100_128"
    return await post_audio("elevenlabs", url, headers, payload)

async def tts_full(text, s, lang=None):
    """Returns (mp3 bytes, provider_used). both = Cartesia -> ElevenLabs -> Edge fallback."""
    s = voice_settings(s)
    lang = lang or pick_lang(text, s)
    provider = s.get("provider", TTS_PROVIDER)
    attempts = []
    if provider in ("both", "cartesia"): attempts.append(("cartesia", cartesia_tts))
    if provider in ("both", "elevenlabs"): attempts.append(("elevenlabs", elevenlabs_tts))
    errors = []
    for label, engine in attempts:
        try:
            data = await engine(text, s, lang)
            if data:
                note_ok(label)
                return data, label
        except Exception as exc:
            err = exc if isinstance(exc, TTSError) else TTSError(label, "other", str(exc))
            log.warning("%s failed: %s (%s)", label, err, err.detail)
            if not (err.kind == "config" and provider == "both"):  # unused provider in auto mode: stay quiet
                note_failure(label, err)
            errors.append(err)
            if provider != "both":
                raise TTSFailure(str(err)) from exc
    if provider != "edge":
        raise TTSFailure("; ".join(str(e) for e in errors) or "No voice provider is configured")
    voice = s["voice_bn"] if lang == "bn" else s["voice_en"]
    c = edge_tts.Communicate(text, voice, rate=f"{max(-50, min(100, s['rate'])):+d}%",
                             volume=f"{max(-50, min(50, s['volume'])):+d}%",
                             pitch=f"{max(-80, min(80, s['pitch'])):+d}Hz")
    data = b""
    async for part in c.stream():
        if part["type"] == "audio": data += part["data"]
    if not data: raise RuntimeError("Edge TTS returned no audio")
    return data, "edge"

async def tts(text, s, lang=None) -> bytes:
    return (await tts_full(text, s, lang))[0]

async def synth(text, s):
    """Chunked synthesis -> (audio, 'prov1+prov2')."""
    parts, provs = [], set()
    for c in split_text(text):
        a, p = await tts_full(c, s); parts.append(a); provs.add(p)
    return b"".join(parts), "+".join(sorted(provs))


# ---------- core: collect + flush ----------
last_alert = {"t": 0.0}

async def alert(bot, text):
    """Tell owners (max once / 10 min) when voice generation silently fails."""
    if time.time() - last_alert["t"] < 600: return
    last_alert["t"] = time.time()
    for o in OWNERS:
        try: await bot.send_message(o, text[:500])
        except Exception: pass

async def flush(key, bot):
    cid = key[0]
    async with locks.setdefault(key, asyncio.Lock()):
        msgs = buffers.pop(key, {})
        mid = last_mid.get(key)
        if not msgs:
            return
        s = cfg(cid)
        text = "\n".join(msgs.values())[: s["max_chars"]]
        try:
            audio, prov = await synth(text, s)
            if audio:
                sent = await bot.send_voice(
                    chat_id=cid, voice=InputFile(audio, filename="voice.mp3"),
                    caption=text[:1000] if s["caption"] else None,
                    # Reply to the latest message in this sender's buffered batch.
                    reply_parameters=ReplyParameters(message_id=mid, allow_sending_without_reply=True)
                    if (mid and s["reply"]) else None)
                record(cid, 0, text, prov, pick_lang(text, s), s["preset"], sent.voice.file_id if sent.voice else "", key[1])
        except Exception as exc:
            log.exception("Voice error")
            if not isinstance(exc, TTSFailure):
                await alert(bot, f"⚠️ Voice failed in {chat(cid).get('title') or cid}: {exc}")

async def wait_and_flush(key, bot, delay):
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return
    timers.pop(key, None)
    await flush(key, bot)

async def collect(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message
    raw = (m.text or m.caption) if m else None
    if not raw or not m.from_user or m.from_user.id == ctx.bot.id:
        return
    c = chat(m.chat_id)
    if c["title"] != m.chat.title:
        c["title"] = m.chat.title or str(m.chat_id); save()
    s = cfg(m.chat_id)
    fu = m.from_user
    ids = [x.lower().lstrip("@").strip() for x in (fu.username, fu.first_name, str(fu.id)) if x]
    if not s["enabled"] or any(n in s["ignore"] for n in ids):
        return
    u = next((n for n in ids if n in s["timers"]), None)   # match by @username, first name or numeric ID
    if u is None:
        if s["only_listed"]: return
        u = ids[0]
    t = raw
    if s["strip_links"]: t = re.sub(r"https?://\S+", "link", t)
    if s["strip_emoji"]: t = EMOJI.sub("", t)
    t = t.strip()
    if len(t) < s["min_chars"]:
        return
    if s["read_names"]: t = f"{fu.first_name}: {t}"
    key = (m.chat_id, u)
    buffers.setdefault(key, {})[m.message_id] = t
    last_mid[key] = m.message_id
    if key in timers: timers[key].cancel()
    timers[key] = asyncio.create_task(wait_and_flush(key, ctx.bot, s["timers"].get(u, s["default_timer"])))

def keys_of(cid): return [k for k in list(buffers) if k[0] == cid]

async def run_action(cid, action, bot):
    for k in keys_of(cid):
        if k in timers: timers.pop(k).cancel()
        if action == "send": await flush(k, bot)
        else: buffers.pop(k, None)


# ---------- access control ----------
def role_of(uid):
    if uid in OWNERS: return "owner"
    if str(uid) in db.get("admins", {}): return "admin"
    if str(uid) in db.get("requests", {}): return "pending"
    return "none"

def is_staff(uid): return role_of(uid) in ("owner", "admin")

async def can_manage(bot, uid, cid):
    if uid in OWNERS: return True
    if not is_staff(uid): return False
    t = admin_cache.get((uid, cid))
    if t and time.time() - t[0] < 60: return t[1]
    try: ok = (await bot.get_chat_member(cid, uid)).status in ("administrator", "creator")
    except Exception: ok = False
    admin_cache[(uid, cid)] = (time.time(), ok)
    return ok

async def make_request(bot, uid, name, username):
    if role_of(uid) in ("owner", "admin"): return "admin"
    db.setdefault("requests", {})[str(uid)] = {"name": name, "username": username, "ts": int(time.time())}
    save()
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Approve", callback_data=f"adm:ok:{uid}"),
                                InlineKeyboardButton("❌ Deny", callback_data=f"adm:no:{uid}")]])
    for o in OWNERS:
        try: await bot.send_message(o, f"🔔 Admin access request\n{name} (@{username or '-'})\nID: {uid}", reply_markup=kb)
        except Exception: log.warning("Could not notify owner %s", o)
    return "pending"

async def decide(bot, uid, ok):
    r = db.get("requests", {}).pop(str(uid), None)
    if ok:
        db.setdefault("admins", {})[str(uid)] = {"name": (r or {}).get("name", ""), "username": (r or {}).get("username", ""), "ts": int(time.time())}
    save()
    try: await bot.send_message(uid, "✅ Access approved. Send /panel to open the control panel." if ok else "❌ Access request denied.")
    except Exception: pass


# ---------- Mini App API ----------
def verify_user(init):
    if not init: return None
    d = dict(parse_qsl(init, keep_blank_values=True))
    h = d.pop("hash", "")
    chk = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    sec = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    if not h or not hmac.compare_digest(hmac.new(sec, chk.encode(), hashlib.sha256).hexdigest(), h):
        return None
    if time.time() - int(d.get("auth_date", 0)) > 86400:
        return None
    try: return json.loads(d["user"])
    except Exception: return None

def make_token(uid):
    p = base64.urlsafe_b64encode(json.dumps({"u": uid, "e": int(time.time()) + 7 * 86400}).encode()).decode()
    return p + "." + hmac.new(SESSION_SECRET, p.encode(), hashlib.sha256).hexdigest()[:32]

def check_token(t):
    try:
        p, sig = t.split(".")
        if not hmac.compare_digest(sig, hmac.new(SESSION_SECRET, p.encode(), hashlib.sha256).hexdigest()[:32]): return None
        d = json.loads(base64.urlsafe_b64decode(p.encode()))
        return d["u"] if d["e"] > time.time() else None
    except Exception:
        return None

def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(8)
    return salt, hashlib.scrypt(pw.encode(), salt=salt.encode(), n=2 ** 14, r=8, p=1).hex()

def auth(req):
    uid = check_token(req.headers.get("X-Token", ""))  # a signed login wins, so staff can sign in anywhere
    if uid: return uid, {"id": uid}
    u = verify_user(req.headers.get("X-Init", ""))
    return (u["id"], u) if u else (None, None)

async def need(req, owner=False):
    uid, _ = auth(req)
    if uid is None: raise web.HTTPUnauthorized(text="login required")
    r = role_of(uid)
    if r not in ("owner", "admin") or (owner and r != "owner"):
        raise web.HTTPForbidden(text="not approved")
    return uid

async def can_see(bot, uid, h):
    return uid in OWNERS or h.get("uid") == uid or (h["cid"] < 0 and await can_manage(bot, uid, h["cid"]))

def stats_view(c):
    st = c["stats"]; dl = st.get("daily", {})
    days = [(d, *(dl.get(d, [0, 0]))) for d in sorted({*dl})[-7:]]
    return {"voices": st["voices"], "chars": st["chars"], "prov": st.get("prov", {}), "daily": days}

async def api_state(req):
    uid, _ = auth(req)
    if uid is None: raise web.HTTPUnauthorized(text="login required")
    r = role_of(uid)
    out = {"role": r, "uid": uid, "in_tg": bool(req.headers.get("X-Init"))}
    if r not in ("owner", "admin"): return web.json_response(out)
    out["keys"] = {"cartesia": bool(CARTESIA_API_KEY), "elevenlabs": bool(ELEVENLABS_API_KEY)}
    out["status"] = {p: {"ok": v["ok"], "kind": v["kind"]} for p, v in pstat.items()}
    chats = [{"id": "__global__", "title": "Global defaults (all groups)",
              "s": base_cfg(),
              "stats": stats_view({"stats": {"voices": 0, "chars": 0}}), "pending": 0}]
    for cid, c in db["chats"].items():
        if cid != "__global__" and await can_manage(req.app["bot"], uid, int(cid)):
            chats.append({"id": cid, "title": c["title"], "s": cfg(cid), "stats": stats_view(c),
                          "pending": sum(len(v) for k, v in buffers.items() if str(k[0]) == cid)})
    out.update(chats=chats, voice_lib=lib(), voices=VOICES, providers=list(PROVIDERS), presets=list(PRESETS),
               login=next((n for n, v in db.get("creds", {}).items() if v["uid"] == uid), ""),
               backup=bool(BACKUP_CHAT))
    if r == "owner":
        out["admins"] = [{"uid": k, **v} for k, v in db.get("admins", {}).items()]
        out["requests"] = [{"uid": k, **v} for k, v in db.get("requests", {}).items()]
    return web.json_response(out)

async def api_save(req):
    uid, b = await need(req), await req.json()
    raw = str(b["chat_id"])
    patch = sanitize(b.get("patch", {}))
    if raw == "__global__":
        if not is_staff(uid): raise web.HTTPForbidden()
        if b.get("reset"): db["global_settings"] = {}
        else: db.setdefault("global_settings", {}).update(patch)
    else:
        cid = int(raw)
        if not await can_manage(req.app["bot"], uid, cid): raise web.HTTPForbidden()
        sc, base = chat(cid)["s"], base_cfg()
        if b.get("reset"): sc.clear()
        for k, v in patch.items():
            if v == base.get(k): sc.pop(k, None)
            else: sc[k] = v
    save()
    return web.json_response({"ok": True})

async def api_voice(req):
    """Voice library: add / edit / delete. Panel only, approved admins only."""
    uid, b = await need(req), await req.json()
    a, L = b.get("action"), lib()
    cur = next((x for x in L if x["k"] == b.get("k")), None)
    if a == "delete":
        if not cur: raise web.HTTPNotFound()
        L.remove(cur)
        fld = f"{cur['prov']}_voice_{cur['lang']}"
        for sc in scopes():
            if sc.get(fld) == cur["id"]: sc.pop(fld)
    elif a in ("add", "edit"):
        item = clean_lib([{**(b.get("voice") or {}), "k": cur["k"] if cur else ""}])
        if not item: raise web.HTTPBadRequest(text="Name, provider, language and a valid voice ID are required")
        item = item[0]
        if any(x["name"].lower() == item["name"].lower() and x is not cur for x in L):
            raise web.HTTPConflict(text="A voice with this name already exists")
        if a == "edit":
            if not cur: raise web.HTTPNotFound()
            old = f"{cur['prov']}_voice_{cur['lang']}"; new = f"{item['prov']}_voice_{item['lang']}"
            for sc in scopes():
                if sc.get(old) == cur["id"]:
                    sc.pop(old); sc[new] = item["id"]
            L[L.index(cur)] = item
        else:
            if len(L) >= 100: raise web.HTTPBadRequest(text="Library is full (100)")
            L.append(item)
    else:
        raise web.HTTPBadRequest()
    save()
    return web.json_response({"voice_lib": L})

async def api_action(req):
    uid, b = await need(req), await req.json()
    raw_cid = str(b["chat_id"])
    if raw_cid == "__global__": raise web.HTTPBadRequest(text="Queue actions require a group.")
    cid = int(raw_cid)
    if not await can_manage(req.app["bot"], uid, cid): raise web.HTTPForbidden()
    await run_action(cid, b["action"], req.app["bot"])
    return web.json_response({"ok": True})

async def api_preview(req):
    await need(req)
    b = await req.json()
    s = {**DEFAULTS, **sanitize(b.get("s", {}))}
    try:
        audio, prov = await tts_full(str(b.get("text", "Hello"))[:300], s, b.get("lang") or None)
    except TTSFailure as exc:
        raise web.HTTPBadGateway(text=str(exc)[:200])
    except Exception:
        log.exception("Preview failed")
        raise web.HTTPBadGateway(text="Could not generate the preview.")
    return web.Response(body=audio, content_type="audio/mpeg", headers={"X-TTS-Provider": prov})

async def api_request(req):
    u = verify_user(req.headers.get("X-Init", ""))
    if not u: raise web.HTTPUnauthorized(text="open inside Telegram")
    r = await make_request(req.app["bot"], u["id"], (u.get("first_name") or "")[:40], u.get("username", ""))
    return web.json_response({"role": r})

async def api_admin(req):
    await need(req, owner=True)
    b = await req.json(); uid = int(b["uid"]); a = b["action"]
    if a in ("approve", "deny"): await decide(req.app["bot"], uid, a == "approve")
    elif a == "remove":
        db.get("admins", {}).pop(str(uid), None)
        for n in [n for n, v in db.get("creds", {}).items() if v["uid"] == uid]: db["creds"].pop(n)
        save()
    return web.json_response({"ok": True})

async def api_login(req):
    ip = (req.headers.get("X-Forwarded-For", "") or req.remote or "?").split(",")[0].strip()
    hits = [t for t in login_hits.get(ip, []) if time.time() - t < 300]
    if len(hits) >= 8: raise web.HTTPTooManyRequests(text="Too many attempts, wait 5 minutes")
    login_hits[ip] = hits + [time.time()]
    b = await req.json()
    u, p, uid = str(b.get("username", "")).strip().lower(), str(b.get("password", "")), None
    if len(login_hits) > 5000: login_hits.clear()
    uh = [t for t in login_hits.get("u:" + u, []) if time.time() - t < 300]
    if len(uh) >= 10: raise web.HTTPTooManyRequests(text="Too many attempts, wait 5 minutes")
    login_hits["u:" + u] = uh + [time.time()]
    if PANEL_USER and PANEL_PASS and OWNERS and hmac.compare_digest(u.encode(), PANEL_USER.encode()) \
            and hmac.compare_digest(p.encode(), PANEL_PASS.encode()):
        uid = min(OWNERS)
    else:
        rec = db.get("creds", {}).get(u)
        if rec and hmac.compare_digest(hash_pw(p, rec["salt"])[1], rec["hash"]): uid = rec["uid"]
    if uid is None or not is_staff(uid): raise web.HTTPUnauthorized(text="Invalid credentials")
    return web.json_response({"token": make_token(uid)})

async def api_creds(req):
    uid, b = await need(req), await req.json()
    u, p = str(b.get("username", "")).strip().lower(), str(b.get("password", ""))
    if not re.fullmatch(r"[a-z0-9_.\-]{3,32}", u) or len(p) < 8:
        raise web.HTTPBadRequest(text="Username 3-32 chars (a-z 0-9 _ . -), password min 8 chars")
    cr = db.setdefault("creds", {})
    if u in cr and cr[u]["uid"] != uid or u == PANEL_USER: raise web.HTTPConflict(text="Username taken")
    for n in [n for n, v in cr.items() if v["uid"] == uid]: cr.pop(n)
    salt, h = hash_pw(p); cr[u] = {"uid": uid, "salt": salt, "hash": h}; save()
    return web.json_response({"ok": True})

async def api_history(req):
    uid, bot = await need(req), req.app["bot"]
    cid, favs, out = req.query.get("chat_id"), req.query.get("fav") == "1", []
    if cid == "__global__": cid = None
    for h in reversed(db.get("history", [])):
        if (cid and str(h["cid"]) != cid) or (favs and not h["fav"]): continue
        if await can_see(bot, uid, h):
            out.append({k: h[k] for k in ("id", "ts", "text", "prov", "lang", "preset", "fav", "chars", "target")})
        if len(out) >= 60: break
    return web.json_response({"items": out})

async def api_hist(req):
    uid, b, bot = await need(req), await req.json(), req.app["bot"]
    h = next((x for x in db.get("history", []) if x["id"] == int(b["id"])), None)
    if not h or not await can_see(bot, uid, h): raise web.HTTPNotFound()
    a = b["action"]
    if a == "fav": h["fav"] = bool(b.get("fav"))
    elif a == "del": db["history"].remove(h)
    elif a == "send":
        if str(b["chat_id"]) == "__global__": raise web.HTTPBadRequest(text="Select a group to send to.")
        cid = int(b["chat_id"])
        if not await can_manage(bot, uid, cid) or not h["fid"]: raise web.HTTPForbidden()
        await bot.send_voice(cid, h["fid"])
    save()
    return web.json_response({"ok": True})

async def api_histaudio(req):
    uid, bot = await need(req), req.app["bot"]
    h = next((x for x in db.get("history", []) if x["id"] == int(req.query.get("id", 0))), None)
    if not h or not h["fid"] or not await can_see(bot, uid, h): raise web.HTTPNotFound()
    data = bytes(await (await bot.get_file(h["fid"])).download_as_bytearray())
    return web.Response(body=data, content_type="audio/mpeg")

async def page(_):
    return web.FileResponse(Path(__file__).with_name("miniapp.html"), headers={"Cache-Control": "no-cache"})

async def keepalive_loop():
    """Self-ping so Render's free tier never idles the service to sleep."""
    if not KEEPALIVE_URL:
        log.warning("No RENDER_EXTERNAL_URL/WEBAPP_URL: keep-alive disabled (use an external pinger on /health)"); return
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as ses:
        while True:
            await asyncio.sleep(240)
            try:
                async with ses.get(KEEPALIVE_URL + "/health") as r: await r.read()
            except Exception: log.warning("Keep-alive ping failed")

async def on_error(update, ctx):
    log.error("Handler error", exc_info=ctx.error)

async def health(_):
    return web.Response(text="Voice bot is running", content_type="text/plain")


# ---------- chat commands ----------
HELP = """SAKIB Lite — Voice Assistant

/tts <text> — Turn text into a voice message
      (or reply to any message with /tts)
/voicepreset <name> — Voice style: normal, deep, robot, child, deep_robot
/voices — See the available voices
/replay — Replay your last voice (or /replay <id>)"""

async def say(update, text): await update.effective_message.reply_text(text)
def grp(update): return update.effective_chat.type != "private"

async def cmd_ok(update, ctx):
    u, c = update.effective_user, update.effective_chat
    if not u: return False
    return is_staff(u.id) if c.type == "private" else await can_manage(ctx.bot, u.id, c.id)

async def group_admin(update, ctx):
    """Approved admins can manage global defaults in private, or a group they administer."""
    if not await cmd_ok(update, ctx):
        await say(update, "⛔ Approved admin access required. Use /request.")
        return False
    return True

async def help_cmd(update, ctx): await say(update, HELP)

async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Welcome to SAKIB Lite! 🎙\n\n"
        "/tts <text> — Turn text into a voice message\n"
        "/voicepreset <name> — Change the voice style\n"
        "/voices — See the available voices\n"
        "/replay — Replay your last voice"
    )

async def about_cmd(update, ctx):
    await say(update, "SAKIB Lite is a voice assistant bot created by @sakibXXL. It converts text into voice messages with selectable voice models and styles.")

async def panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return await update.message.reply_text("Open my private chat and send /panel to access the control panel.")
    if not WEBAPP_URL:
        return await update.message.reply_text("The panel is not set up yet. Ask the owner to set WEBAPP_URL (an HTTPS link).")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ Control Panel", web_app=WebAppInfo(f"{WEBAPP_URL}/app"))]])
    await update.message.reply_text("Open the control panel 👇", reply_markup=kb)

async def voice_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await run_action(update.effective_chat.id, "send", ctx.bot)

async def clear_cmd(update, ctx):
    if await group_admin(update, ctx):
        await run_action(command_scope(update), "clear", ctx.bot); await say(update, "🗑 Queue cleared.")

async def set_enabled(update, ctx, val):
    if not await group_admin(update, ctx): return
    cid = command_scope(update)
    put(cid, enabled=val)
    if not val: await run_action(cid, "clear", ctx.bot)
    await say(update, "✅ Voice replies are ON." if val else "🔴 Voice replies are OFF. Use /on to turn them back on.")

async def on_cmd(update, ctx): await set_enabled(update, ctx, True)
async def off_cmd(update, ctx): await set_enabled(update, ctx, False)

async def status_cmd(update, ctx):
    if not await group_admin(update, ctx): return
    s = cfg(command_scope(update))
    t = ", ".join(f"@{n}({v}s)" for n, v in s["timers"].items()) or "-"
    await say(update, f"Bot: {'ON' if s['enabled'] else 'OFF'}\nProvider: {s['provider']} | Lang: {s['lang']} | Preset: {s['preset']}\n"
                      f"Speed {s['rate']}% | Pitch {s['pitch']}Hz | Volume {s['volume']}%\nOnly listed: {s['only_listed']}\nTargets: {t}")

async def users_cmd(update, ctx):
    if not await group_admin(update, ctx): return
    s = cfg(command_scope(update))
    await say(update, "🎯 Target usernames:\n" + ("\n".join(f"@{n} - {v}s" for n, v in s["timers"].items()) or "(none)") +
                      f"\nDefault wait: {s['default_timer']}s | Only listed: {s['only_listed']}")

async def adduser_cmd(update, ctx):
    if not await group_admin(update, ctx): return
    if not ctx.args: return await say(update, "Use: /adduser @username [seconds]")
    cid, n = command_scope(update), ctx.args[0].lstrip("@").lower()
    sec = int(ctx.args[1]) if len(ctx.args) > 1 and ctx.args[1].isdigit() else cfg(cid)["default_timer"]
    t = dict(cfg(cid)["timers"]); t[n] = sec
    put(cid, timers=t)
    await say(update, f"✅ @{n} added ({max(1, min(600, sec))}s wait). I will now reply to their messages with voice.")

async def deluser_cmd(update, ctx):
    if not await group_admin(update, ctx): return
    if not ctx.args: return await say(update, "Use: /deluser @username")
    cid, n = command_scope(update), ctx.args[0].lstrip("@").lower()
    t = dict(cfg(cid)["timers"])
    if t.pop(n, None) is None: return await say(update, "That username is not in the list.")
    put(cid, timers=t); await say(update, f"🗑 @{n} removed.")

async def onlylisted_cmd(update, ctx):
    if not await group_admin(update, ctx): return
    v = (ctx.args[0].lower() if ctx.args else "")
    if v not in ("on", "off"): return await say(update, "Use: /onlylisted on|off")
    put(command_scope(update), only_listed=v == "on")
    await say(update, "✅ Only listed: " + v)

async def apply_setting(update, ctx, **kw):
    """Group: chat settings (approved admin). Private: personal prefs used by /tts."""
    c, u = update.effective_chat, update.effective_user
    clean = sanitize(kw)
    if not clean: return await say(update, "❌ Invalid value.")
    if c.type == "private":
        if is_staff(u.id):
            db.setdefault("global_settings", {}).update(clean); save()
            return await say(update, "Saved as the global default for all groups.")
        db.setdefault("users", {}).setdefault(str(u.id), {}).update(clean); save()
        return await say(update, "Saved for your personal /tts commands.")
    if not await cmd_ok(update, ctx): return await say(update, "⛔ Only approved admins can change this group's settings.")
    put(c.id, **clean); await say(update, "✅ Saved for this group.")

def numcmd(key, lo, hi):
    async def h(update, ctx):
        try: v = max(lo, min(hi, int(float(ctx.args[0]))))
        except Exception: return await say(update, f"Use: /{key if key != 'rate' else 'speed'} <{lo}..{hi}>")
        await apply_setting(update, ctx, **{key: v})
    return h

def enumcmd(key, opts):
    async def h(update, ctx):
        v = ctx.args[0].lower() if ctx.args else ""
        if v not in opts: return await say(update, f"Use: {' | '.join(opts)}")
        await apply_setting(update, ctx, **{key: v})
    return h

def lib_text(s):
    L = lib()
    if not L: return "🎙 Voice library is empty. Admins can add voices from /panel."
    return f"🎙 Voice library ({len(L)}):\n" + "\n".join(
        f"{'✅' if s.get(x['prov'] + '_voice_' + x['lang']) == x['id'] else '▫️'} {x['name']} — {x['prov']}/{x['lang']}" for x in L)

async def voices_cmd(update, ctx):
    await say(update, lib_text(cfg(command_scope(update))))

async def panel_only_cmd(update, ctx):
    await say(update, "🔒 Voices can only be added, edited, deleted or selected from the Mini App panel (/panel).")

def hist_scope(update):
    c, u = update.effective_chat, update.effective_user
    return [h for h in db.get("history", []) if (h["cid"] == c.id if c.type != "private" else h.get("uid") == u.id)]

def fmt_h(h): return f"#{h['id']} {'⭐' if h['fav'] else ''} {datetime.fromtimestamp(h['ts'], timezone.utc):%d/%m %H:%M} [{h['prov']}] {h['text'][:45]}"

async def history_cmd(update, ctx):
    hs = hist_scope(update)[-10:][::-1]
    await say(update, "🕘 History:\n" + "\n".join(map(fmt_h, hs)) if hs else "No history yet.")

async def favs_cmd(update, ctx):
    hs = [h for h in hist_scope(update) if h["fav"]][-15:][::-1]
    await say(update, "⭐ Favorites:\n" + "\n".join(map(fmt_h, hs)) if hs else "No favourites yet. Use /fav <id>")

async def fav_cmd(update, ctx):
    h = next((x for x in hist_scope(update) if ctx.args and ctx.args[0].isdigit() and x["id"] == int(ctx.args[0])), None)
    if not h: return await say(update, "Use: /fav <id> (get the id from /history)")
    h["fav"] = not h["fav"]; save(); await say(update, "⭐ Favorite" if h["fav"] else "Removed from favourites")

async def replay_cmd(update, ctx):
    hs = hist_scope(update)
    if ctx.args and ctx.args[0].isdigit():
        h = next((x for x in hs if x["id"] == int(ctx.args[0])), None)
    else:
        h = next((x for x in reversed(hs) if x["fid"]), None)
    if not h or not h["fid"]:
        return await say(update, "No voice to replay yet. Create one with /tts <text>.")
    await ctx.bot.send_voice(update.effective_chat.id, h["fid"])

async def stats_cmd(update, ctx):
    if not await group_admin(update, ctx): return
    v = stats_view(chat(update.effective_chat.id))
    await say(update, f"📊 Voices: {v['voices']} | Characters: {v['chars']}\nProviders: " + (", ".join(f"{k}={n}" for k, n in v["prov"].items()) or "-") +
              "\nLast days: " + (", ".join(f"{d[5:]}:{n}" for d, n, _ in v["daily"]) or "-"))

async def request_cmd(update, ctx):
    u = update.effective_user
    r = await make_request(ctx.bot, u.id, u.first_name or "", u.username or "")
    await say(update, "You are already an approved admin." if r == "admin" else "📨 Request sent to the owner. You will be notified once approved.")

async def approve_cmd(update, ctx, ok=True):
    if update.effective_user.id not in OWNERS: return await say(update, "⛔ Owner only.")
    if not ctx.args or not ctx.args[0].isdigit(): return await say(update, "Use: /approve <user_id>")
    await decide(ctx.bot, int(ctx.args[0]), ok); await say(update, "✅ Approved." if ok else "❌ Denied.")

async def deny_cmd(update, ctx): await approve_cmd(update, ctx, False)

async def admins_cmd(update, ctx):
    if update.effective_user.id not in OWNERS: return await say(update, "⛔ Owner only.")
    a, r = db.get("admins", {}), db.get("requests", {})
    await say(update, "👮 Admins:\n" + ("\n".join(f"{k} {v['name']} @{v['username']}" for k, v in a.items()) or "-") +
              "\n\n🔔 Pending:\n" + ("\n".join(f"{k} {v['name']} @{v['username']}" for k, v in r.items()) or "-"))

async def removeadmin_cmd(update, ctx):
    if update.effective_user.id not in OWNERS: return await say(update, "⛔ Owner only.")
    if not ctx.args or not ctx.args[0].isdigit(): return await say(update, "Use: /removeadmin <user_id>")
    db.get("admins", {}).pop(ctx.args[0], None); save(); await say(update, "🗑 Removed.")

async def cb_admin(update, ctx):
    q = update.callback_query
    if q.from_user.id not in OWNERS: return await q.answer("Owner only", show_alert=True)
    _, a, uid = q.data.split(":")
    await decide(ctx.bot, int(uid), a == "ok")
    await q.answer("Done"); await q.edit_message_text(q.message.text + ("\n\n✅ Approved" if a == "ok" else "\n\n❌ Denied"))

async def tts_cmd(update, ctx):
    m = update.effective_message
    text = " ".join(ctx.args) if ctx.args else (m.reply_to_message.text if m.reply_to_message and m.reply_to_message.text else "")
    if not text:
        return await m.reply_text("Usage: /tts your text (or reply to a message with /tts)\nVoice style: /voicepreset deep|robot|child|deep_robot|normal")
    uid, c = update.effective_user.id, update.effective_chat
    if time.time() - last_tts.get(uid, 0) < 4: return await m.reply_text("⏳ Please wait a few seconds before the next voice.")
    last_tts[uid] = time.time()
    s = cfg(c.id) if c.type != "private" else {**DEFAULTS, **db.get("global_settings", {})}
    s.update(db.get("users", {}).get(str(uid), {}))
    text = text[:3000]
    try:
        audio, prov = await synth(text, s)
        tgt = m.reply_to_message.message_id if m.reply_to_message else m.message_id
        sent = await ctx.bot.send_voice(c.id, InputFile(audio, filename="tts.mp3"),
                                        reply_parameters=ReplyParameters(message_id=tgt, allow_sending_without_reply=True))
        record(c.id, uid, text, prov, pick_lang(text, s), s["preset"], sent.voice.file_id if sent.voice else "")
    except Exception as exc:
        log.exception("Chat TTS failed")
        await m.reply_text(user_error(exc, uid))

async def voicepreset_cmd(update, ctx):
    v = ctx.args[0].lower() if ctx.args else ""
    if v not in PRESETS: return await say(update, "Preset: " + ", ".join(PRESETS))
    await apply_setting(update, ctx, preset=v)


# ---------- lifecycle ----------
COMMANDS = [
    ("tts", "Turn text into a voice message"),
    ("voicepreset", "Change the voice style"),
    ("voices", "See the available voices"),
    ("replay", "Replay your last voice"),
]

async def post_init(app):
    load()
    app_ref["bot"] = app.bot
    await restore(app.bot)
    migrate(); save()
    w = web.Application()
    w["bot"] = app.bot
    w.add_routes([web.get("/", health), web.get("/health", health), web.get("/app", page), web.get("/api/state", api_state),
                  web.post("/api/save", api_save), web.post("/api/voice", api_voice), web.post("/api/action", api_action),
                  web.post("/api/preview", api_preview), web.post("/api/request", api_request),
                  web.post("/api/login", api_login), web.post("/api/creds", api_creds),
                  web.post("/api/admin", api_admin), web.get("/api/history", api_history),
                  web.post("/api/hist", api_hist), web.get("/api/histaudio", api_histaudio)])
    runner = web.AppRunner(w); await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    app.bot_data["runner"] = runner
    app.bot_data["bg"] = asyncio.create_task(backup_loop(app.bot))
    app.bot_data["ka"] = asyncio.create_task(keepalive_loop())
    try: await app.bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS])
    except Exception: log.exception("set_my_commands failed")
    if WEBAPP_URL:
        await app.bot.set_chat_menu_button(menu_button=MenuButtonWebApp("⚙️ Panel", WebAppInfo(f"{WEBAPP_URL}/app")))
    log.info("Web server running on port %s", PORT)

async def post_stop(app):
    app.bot_data["bg"].cancel(); app.bot_data["ka"].cancel()
    try: await do_backup(app.bot)  # final snapshot before a Render redeploy/restart
    except Exception: log.exception("Final backup failed")

async def post_shutdown(app):
    await app.bot_data["runner"].cleanup()

def main():
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).post_stop(post_stop).post_shutdown(post_shutdown).build()
    H = app.add_handler
    H(CommandHandler("start", start_cmd)); H(CommandHandler("panel", panel)); H(CommandHandler("about", about_cmd))
    H(CommandHandler("help", help_cmd))
    H(CommandHandler("voice", voice_cmd)); H(CommandHandler("clear", clear_cmd))
    H(CommandHandler("on", on_cmd)); H(CommandHandler("off", off_cmd)); H(CommandHandler("status", status_cmd))
    H(CommandHandler("users", users_cmd)); H(CommandHandler("adduser", adduser_cmd))
    H(CommandHandler("deluser", deluser_cmd)); H(CommandHandler("onlylisted", onlylisted_cmd))
    H(CommandHandler("speed", numcmd("rate", -50, 100))); H(CommandHandler("pitch", numcmd("pitch", -50, 50)))
    H(CommandHandler("volume", numcmd("volume", -50, 50)))
    H(CommandHandler("lang", enumcmd("lang", ("auto", "bn", "en"))))
    H(CommandHandler("provider", enumcmd("provider", PROVIDERS)))
    H(CommandHandler(["voicepreset", "preset"], voicepreset_cmd))
    H(CommandHandler("voices", voices_cmd))
    H(CommandHandler(["addvoice", "usevoice", "delvoice"], panel_only_cmd))
    H(CommandHandler("history", history_cmd)); H(CommandHandler("favs", favs_cmd))
    H(CommandHandler("fav", fav_cmd)); H(CommandHandler("replay", replay_cmd)); H(CommandHandler("stats", stats_cmd))
    H(CommandHandler("request", request_cmd)); H(CommandHandler("approve", approve_cmd))
    H(CommandHandler("deny", deny_cmd)); H(CommandHandler("admins", admins_cmd))
    H(CommandHandler("removeadmin", removeadmin_cmd)); H(CommandHandler("tts", tts_cmd))
    H(CallbackQueryHandler(cb_admin, pattern=r"^adm:(ok|no):\d+$"))
    H(MessageHandler(filters.ChatType.GROUPS & (filters.TEXT | filters.CAPTION) & ~filters.COMMAND, collect))
    app.add_error_handler(on_error)
    app.run_polling(allowed_updates=Update.ALL_TYPES, bootstrap_retries=-1)

if __name__ == "__main__":
    main()
