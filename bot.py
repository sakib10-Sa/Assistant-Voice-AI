"""
Telegram Group -> Voice Bot + Mini App Control Panel  (v2)

Env (Render -> Environment): BOT_TOKEN, OWNER_IDS, WEBAPP_URL, DB_FILE,
  TTS_PROVIDER=both|cartesia|elevenlabs|edge, CARTESIA_API_KEY, ELEVENLABS_API_KEY,
  (optional) CARTESIA_VOICE_BN/EN, ELEVENLABS_VOICE_BN/EN, CARTESIA_MODEL, ELEVENLABS_MODEL,
  BACKUP_CHAT_ID (default: first OWNER_ID), PANEL_USER + PANEL_PASS (owner browser login), SESSION_SECRET
API keys are read from environment only and are never sent to the panel.
"""
import asyncio, base64, hashlib, hmac, json, logging, os, re, secrets, time
from datetime import datetime, timedelta, timezone
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
if BACKUP_CHAT.lower() in ("off", "0", "none", "false", "no"):  # BACKUP_CHAT_ID=off disables Telegram backups
    BACKUP_CHAT = ""
KEEPALIVE_URL = (os.getenv("RENDER_EXTERNAL_URL", "") or os.getenv("WEBAPP_URL", "")).rstrip("/")
PANEL_USER = os.getenv("PANEL_USER", "").strip().lower()
PANEL_PASS = os.getenv("PANEL_PASS", "")
SESSION_SECRET = (os.getenv("SESSION_SECRET") or hashlib.sha256(("session:" + TOKEN).encode()).hexdigest()).encode()

VOICES = {
    "bn": ["bn-BD-NabanitaNeural", "bn-BD-PradeepNeural", "bn-IN-TanishaaNeural", "bn-IN-BashkarNeural"],
    "en": ["en-US-AriaNeural", "en-US-GuyNeural", "en-US-JennyNeural", "en-US-MichelleNeural", "en-GB-SoniaNeural", "en-GB-LibbyNeural", "en-IN-NeerjaNeural"],
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
    "fallback_edge": True, "el_stability": 50, "el_similarity": 75,
    "strip_links": True, "strip_emoji": True, "min_chars": 1, "max_chars": 3000, "caption": False, "reply": True,
}
ENUMS = {"provider": set(PROVIDERS), "lang": {"auto", "bn", "en"}, "preset": set(PRESETS),
         "voice_bn": set(VOICES["bn"]), "voice_en": set(VOICES["en"])}
MODELS = {"el_tts": ["eleven_multilingual_v2", "eleven_flash_v2_5", "eleven_turbo_v2_5", "eleven_v3"],
          "ca_tts": ["sonic-3.6", "sonic-3.5", "sonic-3"], "el_stt": ["scribe_v2", "scribe_v1"], "ca_stt": ["ink-whisper"]}
_em, _cm = os.getenv("ELEVENLABS_MODEL", "eleven_multilingual_v2"), os.getenv("CARTESIA_MODEL", "sonic-3.6")
if _em not in MODELS["el_tts"]: MODELS["el_tts"].insert(0, _em)
if _cm not in MODELS["ca_tts"]: MODELS["ca_tts"].insert(0, _cm)
DEFAULTS.update({"el_tts_model": _em, "ca_tts_model": _cm, "el_stt_model": "scribe_v2", "ca_stt_model": "ink-whisper"})
ENUMS.update({"el_tts_model": set(MODELS["el_tts"]), "ca_tts_model": set(MODELS["ca_tts"]), "el_stt_model": set(MODELS["el_stt"]), "ca_stt_model": set(MODELS["ca_stt"])})
CLAMP = {"default_timer": (1, 600), "rate": (-50, 100), "pitch": (-50, 50), "volume": (-50, 50),
         "el_stability": (0, 100), "el_similarity": (0, 100), "min_chars": (1, 500), "max_chars": (40, 5000)}
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

def backup_target():
    """Backups go to BACKUP_CHAT unless the owner switched them off in the panel."""
    return "" if db.get("backup_off") else BACKUP_CHAT

async def do_backup(bot):
    tgt = backup_target()
    if not tgt:
        return
    doc = InputFile(json.dumps(db, ensure_ascii=False).encode(), filename="voicebot-backup.json")
    if backup["mid"]:
        try:
            await bot.edit_message_media(chat_id=tgt, message_id=backup["mid"], media=InputMediaDocument(doc))
            return
        except Exception as exc:
            if "not modified" in str(exc).lower():
                return
            log.warning("Backup edit failed (%s); sending a new backup file", exc)
            backup["mid"] = None
    m = await bot.send_document(tgt, doc, caption="Auto backup (pinned) - do not delete", disable_notification=True)
    await bot.pin_chat_message(tgt, m.message_id, disable_notification=True)
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
            acc = "public" if str(x.get("access", "admin")) == "public" else "admin"  # access is Admins or Public; per-admin reference access lives in "allow"
            allow = [int(u) for u in x.get("allow", []) if str(u).lstrip("-").isdigit()][:50]
            seen.add(k); out.append({"k": k, "name": n, "prov": p, "lang": l, "id": i, "access": acc, "visible": bool(x.get("visible", True)),
                                     "enabled": bool(x.get("enabled", True)), "model": str(x.get("model", ""))[:60], "allow": allow})
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
    "plan": "your plan cannot use this voice through the API (Voice Library voices need a paid plan)",
    "auth": "API key is invalid or not allowed",
    "rate": "too many requests (rate limit)",
    "voice": "voice ID not found or not valid for this language",
    "config": "API key or voice ID is not set",
    "server": "service is down or unreachable",
    "other": "request was rejected",
    "blocked": "ElevenLabs blocked this server (unusual-activity protection)",
    "model": "that model is not available on your plan or does not exist",
}
KIND_FIX = {
    "quota": "Add credits or upgrade the plan, or switch provider in Panel > Voice > Provider.",
    "plan": "Use an ElevenLabs Default voice (e.g. Sarah EXAVITQu4vr4xnSDxMaL), or add $5 credits / upgrade, or use Edge TTS.",
    "auth": "Check the API key in your hosting environment variables.",
    "rate": "This usually clears by itself. If it repeats, reduce simultaneous voices.",
    "voice": "Check the voice ID in Panel > Voice > Voice library.",
    "config": "Set the API key and voice ID.",
    "server": "The provider seems to be down. It should recover by itself.",
    "other": "Check the details below.",
    "blocked": "ElevenLabs disables free-tier API use from some hosting IPs. Use a paid plan, or use Cartesia/Edge from this server.",
    "model": "Pick another model in the TTS section.",
}
QUOTA_WORDS = ("quota", "credit", "insufficient", "billing", "payment required", "out of characters",
               "character limit", "usage limit")
pstat = {p: {"ok": None, "kind": "", "msg": "", "ts": 0} for p in ("cartesia", "elevenlabs")}
notified: dict = {}
app_ref: dict = {}


class TTSError(RuntimeError):
    def __init__(self, provider, kind, detail=""):
        self.provider, self.kind, self.detail = provider, kind, detail
        extra = f" ({detail[:160]})" if detail else ""
        super().__init__(f"{PNAME.get(provider, provider)}: {KIND_TEXT.get(kind, kind)}{extra}")


class TTSFailure(RuntimeError):
    """Every selected provider failed. Owners were already notified."""


def classify(status, body):
    """Structured error code first (that is what the providers actually send); loose keywords are no longer used,
    because words like 'credit' or 'insufficient' appear in unrelated errors and caused false 'out of credits'."""
    b = (body or "").lower(); code = ""
    try:
        d = json.loads(body); d = d.get("detail", d) if isinstance(d, dict) else {}
        code = str(d.get("status") or d.get("code") or d.get("type") or "").lower() if isinstance(d, dict) else ""
    except Exception:
        pass
    if code in ("detected_unusual_activity", "unusual_activity"): return "blocked"
    if code in ("paid_plan_required", "payment_required") or "library voice" in b: return "plan"
    if code in ("quota_exceeded", "insufficient_quota", "credits_exhausted"): return "quota"
    if code in ("invalid_api_key", "needs_authorization", "missing_permissions", "invalid_token", "unauthorized"): return "auth"
    if code in ("too_many_concurrent_requests", "rate_limit_exceeded", "system_busy"): return "rate"
    if code in ("voice_not_found", "voice_not_fine_tuned"): return "voice"
    if code in ("model_not_found", "model_can_not_do_text_to_speech"): return "model"
    if status == 402: return "quota"
    if status == 429: return "quota" if ("credit" in b or "quota" in b) else "rate"
    if status in (401, 403): return "auth"
    if status in (400, 422) and "model" in b: return "model"
    if status == 404 or (status in (400, 422) and "voice" in b): return "voice"
    if status >= 500: return "server"
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
    if not CARTESIA_API_KEY:
        raise TTSError("cartesia", "config", "API key missing: set CARTESIA_API_KEY and restart the bot")
    if not voice_id:
        raise TTSError("cartesia", "config", f"no {lang} voice selected: Voice > Voice library > Use, then Save")
    rate = max(0.6, min(1.5, 1.0 + float(s.get("rate", 0)) / 200))
    volume = max(0.5, min(2.0, 1.0 + float(s.get("volume", 0)) / 50))
    mdl = next((v["model"] for v in lib() if v["id"] == voice_id and v.get("model")), "") or s.get("ca_tts_model") or "sonic-3.6"
    payload = {
        "model_id": mdl,
        "transcript": text,
        "voice": {"mode": "id", "id": voice_id},
        "language": "bn" if lang == "bn" else "en",
        "output_format": {"container": "mp3", "sample_rate": 44100, "bit_rate": 128000},
    }
    if mdl.startswith("sonic-3"): payload["generation_config"] = {"speed": rate, "volume": volume}  # only sonic-3 models accept it
    headers = {"Authorization": f"Bearer {CARTESIA_API_KEY}", "X-API-Key": CARTESIA_API_KEY, "Cartesia-Version": "2025-04-16", "Content-Type": "application/json"}
    return await post_audio("cartesia", "https://api.cartesia.ai/tts/bytes", headers, payload)

_bal = {"t": 0, "v": None}
async def el_balance(force=False):
    """Real credit balance from ElevenLabs. Needs a key with the 'User > Read' permission."""
    if not ELEVENLABS_API_KEY: return None
    if not force and time.time() - _bal["t"] < 30: return _bal["v"]
    v = {"error": "Could not reach ElevenLabs"}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as ses:
            async with ses.get("https://api.elevenlabs.io/v1/user/subscription", headers={"xi-api-key": ELEVENLABS_API_KEY}) as r:
                t = await r.text()
                if r.status == 200:
                    j = json.loads(t); used, lim = int(j.get("character_count", 0)), int(j.get("character_limit", 0))
                    v = {"used": used, "limit": lim, "left": max(0, lim - used), "tier": j.get("tier", ""), "status": j.get("status", "")}
                else:
                    v = {"error": f"HTTP {r.status} ({classify(r.status, t)}): " + ("this key lacks the User > Read permission, so the balance cannot be shown" if "missing_permissions" in t else t[:120])}
    except Exception: pass
    _bal.update(t=time.time(), v=v); return v

async def elevenlabs_tts(text, s, lang):
    voice_id = (s.get("elevenlabs_voice_bn") or os.getenv("ELEVENLABS_VOICE_BN", "").strip()) if lang == "bn" else (s.get("elevenlabs_voice_en") or os.getenv("ELEVENLABS_VOICE_EN", "").strip())
    if not ELEVENLABS_API_KEY:
        raise TTSError("elevenlabs", "config", "API key missing: set ELEVENLABS_API_KEY and restart the bot")
    if not voice_id:
        raise TTSError("elevenlabs", "config", f"no {lang} voice selected: Voice > Voice library > Use, then Save")
    speed = max(0.7, min(1.2, 1.0 + float(s.get("rate", 0)) / 250))
    mdl = next((v["model"] for v in lib() if v["id"] == voice_id and v.get("model")), "") or s.get("el_tts_model") or "eleven_multilingual_v2"
    payload = {"text": text, "model_id": mdl,
               "voice_settings": {"stability": int(s.get("el_stability", 50)) / 100, "similarity_boost": int(s.get("el_similarity", 75)) / 100, "speed": speed}}
    if mdl == "eleven_v3": payload.pop("voice_settings")  # v3 ignores speed/similarity
    headers = {"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json", "Accept": "audio/mpeg"}
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=mp3_44100_128"
    try:
        return await post_audio("elevenlabs", url, headers, payload)
    except TTSError as e:
        if e.kind == "quota":  # verify against the real balance before telling anyone they are out of credits
            bal = await el_balance(force=True)
            if bal and bal.get("left", 0) >= len(text):
                raise TTSError("elevenlabs", "other", f"API said quota, but the account shows {bal['left']} of {bal['limit']} credits left ({bal['tier']}). Raw: {e.detail[:140]}") from e
        raise

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
    if provider == "both" and not s.get("fallback_edge", True):
        raise TTSFailure("; ".join(str(e) for e in errors) or "No voice provider is configured")
    if provider not in ("edge", "both"):
        raise TTSFailure("; ".join(str(e) for e in errors) or "No voice provider is configured")
    if errors:
        log.info("All API providers failed; falling back to Edge TTS")
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

PERMS = ["elevenlabs", "cartesia", "voice_refs", "voice_edit", "voice_create", "providers"]
TIERS = {"basic": [], "advanced": ["elevenlabs", "cartesia", "voice_refs", "voice_edit", "voice_create"]}

def has_perm(uid, p):
    """Server-side permission check. Owners have everything; admins get their tier's list or their custom list."""
    if uid in OWNERS: return True
    a = db.get("admins", {}).get(str(uid))
    if a is None: return False
    t = a.get("tier") or "advanced"  # admins created before tiers existed keep their old access
    return p in (a.get("perms", []) if t == "custom" else TIERS.get(t, []))

def can_use_voice(uid, v):
    if uid in OWNERS: return True
    if not v.get("enabled", True): return False
    if uid in v.get("allow", []): return True  # explicitly authorised: reference-only admins can use only these
    a = v.get("access", "admin")
    if a == "public": return True
    if a == "admin": return has_perm(uid, v["prov"]) and has_perm(uid, "voice_refs")
    return False

def can_manage_voice(uid, v):
    return uid in OWNERS or (has_perm(uid, "voice_edit") and has_perm(uid, v["prov"]))

def lib_for(uid):
    return [x for x in lib() if can_manage_voice(uid, x) or can_use_voice(uid, x)]

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

def make_token(uid, ttl=7 * 86400):
    sid = secrets.token_hex(8)  # a new login replaces the old session, so only one device stays signed in
    db.setdefault("sess", {})[str(uid)] = {"sid": sid, "ts": int(time.time())}; save()
    p = base64.urlsafe_b64encode(json.dumps({"u": uid, "s": sid, "e": int(time.time()) + ttl}).encode()).decode()
    return p + "." + hmac.new(SESSION_SECRET, p.encode(), hashlib.sha256).hexdigest()[:32]

def check_token(t):
    try:
        p, sig = t.split(".")
        if not hmac.compare_digest(sig, hmac.new(SESSION_SECRET, p.encode(), hashlib.sha256).hexdigest()[:32]): return None
        d = json.loads(base64.urlsafe_b64decode(p.encode()))
        if d["e"] <= time.time() or db.get("sess", {}).get(str(d["u"]), {}).get("sid") != d.get("s"): return None
        return d["u"]
    except Exception:
        return None

def audit(uid, what):
    """Security log: last 60 sign-in and admin events, shown to the owner."""
    a = db.setdefault("audit", []); a.append({"t": int(time.time()), "u": uid, "w": str(what)[:80]}); del a[:-60]; save()

def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(8)
    return salt, hashlib.scrypt(pw.encode(), salt=salt.encode(), n=2 ** 14, r=8, p=1).hex()

def auth(req):
    uid = check_token(req.cookies.get("vb_s", "") or req.headers.get("X-Token", ""))  # a signed login wins, so staff can sign in anywhere
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

def agg_stats():
    """Global dashboard = real totals summed over every group the bot serves."""
    t = {"voices": 0, "chars": 0, "prov": {}, "daily": {}}
    for k, c in db.get("chats", {}).items():
        if k == "__global__": continue
        st = c.get("stats", {}); t["voices"] += st.get("voices", 0); t["chars"] += st.get("chars", 0)
        for p, n in st.get("prov", {}).items(): t["prov"][p] = t["prov"].get(p, 0) + n
        for d, v in st.get("daily", {}).items(): x = t["daily"].setdefault(d, [0, 0]); x[0] += v[0]; x[1] += v[1]
    return t

def stats_view(c, cid=None):
    """Always returns 7 consecutive days and 24 hourly buckets (zeros when idle) so the graph is never empty."""
    st = c["stats"]; dl = st.get("daily", {}); now = time.time()
    days = [(datetime.now(timezone.utc) - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(6, -1, -1)]
    hs, last = [0] * 24, 0
    for h in db.get("history", []):
        if cid is not None and str(h.get("cid")) != str(cid): continue
        last = max(last, h["ts"]); age = now - h["ts"]
        if 0 <= age < 86400: hs[23 - int(age // 3600)] += 1
    return {"voices": st.get("voices", 0), "chars": st.get("chars", 0), "prov": st.get("prov", {}),
            "daily": [(d, *(dl.get(d, [0, 0]))) for d in days], "hourly": hs, "last": last}

async def api_state(req):
    uid, _ = auth(req)
    if uid is None: raise web.HTTPUnauthorized(text="login required")
    r = role_of(uid)
    out = {"role": r, "uid": uid, "in_tg": bool(req.headers.get("X-Init")), "models": MODELS}
    out.update(perms=[p for p in PERMS if has_perm(uid, p)], prefs=db.get("prefs", {}).get(str(uid)), primary=primary(), edge=ALL_EDGE)
    if uid in OWNERS: out["cmds"] = {"list": CMD_LIST, "cfg": db.get("cmdcfg", {})}
    if r not in ("owner", "admin"): return web.json_response(out)
    out["keys"] = {"cartesia": bool(CARTESIA_API_KEY), "elevenlabs": bool(ELEVENLABS_API_KEY)}
    out["status"] = {p: {"ok": None if (v["ok"] is False and time.time() - v["ts"] > 1800) else v["ok"], "kind": v["kind"]}
                     for p, v in pstat.items()}
    chats = [{"id": "__global__", "title": "Global defaults (all groups)",
              "s": base_cfg(),
              "stats": stats_view({"stats": agg_stats()}), "pending": sum(len(v) for v in buffers.values())}]
    for cid, c in db["chats"].items():
        if cid != "__global__" and await can_manage(req.app["bot"], uid, int(cid)):
            chats.append({"id": cid, "title": c["title"], "s": cfg(cid), "stats": stats_view(c, cid),
                          "pending": sum(len(v) for k, v in buffers.items() if str(k[0]) == cid)})
    out.update(chats=chats, voice_lib=lib_for(uid), voices=VOICES, providers=list(PROVIDERS), presets=list(PRESETS),
               login=next((n for n, v in db.get("creds", {}).items() if v["uid"] == uid), ""),
               backup=bool(backup_target()), backup_avail=bool(BACKUP_CHAT))
    lg = {str(v["uid"]): n for n, v in db.get("creds", {}).items()}
    out["me"] = {"name": (db.get("admins", {}).get(str(uid), {}).get("name") or ("Owner" if r == "owner" else "Admin")),
                 "login": lg.get(str(uid), ""), "via": "browser" if req.cookies.get("vb_s") else "telegram"}
    out["mfa"] = any(v["uid"] == uid and v.get("totp") for v in db.get("creds", {}).values())
    if r == "owner":
        out["admins"] = [{"uid": k, **v, "tier": v.get("tier") or "advanced", "eperms": [p for p in PERMS if has_perm(int(k), p)], "login": lg.get(k, ""), "online": k in db.get("sess", {})} for k, v in db.get("admins", {}).items()]
        out["requests"] = [{"uid": k, **v} for k, v in db.get("requests", {}).items()]
        out["audit"] = list(reversed(db.get("audit", [])[-30:]))
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
    nprov = str((b.get("voice") or {}).get("prov", cur["prov"] if cur else "")).lower()
    if a == "add" and not (has_perm(uid, "voice_create") and has_perm(uid, nprov)): raise web.HTTPForbidden(text="You need voice-creation access for this provider")
    if a in ("edit", "delete") and not (cur and can_manage_voice(uid, cur)): raise web.HTTPForbidden(text="You do not have access to edit this voice")
    if a == "edit" and not has_perm(uid, nprov): raise web.HTTPForbidden(text="You do not have access to that provider")
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
        if uid not in OWNERS:  # access level, visibility and the allow-list are owner-only; admins keep what is stored
            item.update({k: (cur or {}).get(k, d) for k, d in (("access", "admin"), ("visible", True), ("allow", []))})
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
    return web.json_response({"voice_lib": lib_for(uid)})

async def api_action(req):
    uid, b = await need(req), await req.json()
    raw_cid = str(b["chat_id"])
    if raw_cid == "__global__": raise web.HTTPBadRequest(text="Queue actions require a group.")
    cid = int(raw_cid)
    if not await can_manage(req.app["bot"], uid, cid): raise web.HTTPForbidden()
    await run_action(cid, b["action"], req.app["bot"])
    return web.json_response({"ok": True})

dl_cache = {}
async def api_preview(req):
    uid = await need(req)
    b = await req.json()
    s = {**DEFAULTS, **sanitize(b.get("s", {}))}
    pv = s.get("provider")
    need_p = ("cartesia", "elevenlabs") if pv == "both" else (pv,)
    if pv in ("both", "cartesia", "elevenlabs") and not any(has_perm(uid, p) for p in need_p):
        s["provider"] = "edge"  # no access to API providers: Edge only
    try:
        audio, prov = await tts_full(str(b.get("text", "Hello"))[:1000], s, b.get("lang") or None)
    except TTSFailure as exc:
        raise web.HTTPBadGateway(text=str(exc)[:240])
    except Exception as exc:
        log.exception("Preview failed")
        raise web.HTTPBadGateway(text=f"Preview failed ({type(exc).__name__}): {exc}"[:240])
    tok = secrets.token_urlsafe(16); dl_cache[tok] = (audio, time.time(), prov)
    for k in sorted(dl_cache, key=lambda k: dl_cache[k][1])[:-20]: dl_cache.pop(k, None)
    return web.Response(body=audio, content_type="audio/mpeg", headers={"X-TTS-Provider": prov, "X-Audio-Token": tok})

async def api_dl(req):
    """Unguessable, 15-minute download link (works in Android / Telegram WebView where blob downloads are blocked)."""
    e = dl_cache.get(req.match_info["tok"].removesuffix(".mp3"))
    if not e or time.time() - e[1] > 900: raise web.HTTPNotFound()
    return web.Response(body=e[0], content_type="audio/mpeg", headers={"Content-Disposition": f'attachment; filename="voice-{e[2]}-{int(e[1])}.mp3"', "Cache-Control": "no-store"})

async def api_bot_toggle(req):
    """Real on/off for the bot in a group (or globally): the same flag the voice pipeline checks."""
    uid, b = await need(req), await req.json(); raw = str(b.get("chat_id")); on = bool(b.get("enabled"))
    if raw == "__global__":
        if not is_staff(uid): raise web.HTTPForbidden()
        db.setdefault("global_settings", {})["enabled"] = on
    else:
        cid = int(raw)
        if not await can_manage(req.app["bot"], uid, cid): raise web.HTTPForbidden()
        chat(cid)["s"]["enabled"] = on
    audit(uid, f"Bot turned {'on' if on else 'off'} ({'global' if raw == '__global__' else raw})"); save()
    return web.json_response({"enabled": bool(cfg(raw if raw == "__global__" else int(raw))["enabled"])})

PREF_KEYS = {"th": ("dark", "light", "system"), "ns": ("bubble", "blue"), "nl": ("float", "classic"), "sp": ("next", "right"), "bg": ("default", "aurora", "plain")}
PREF_NUM = {"gb": (0, 40), "gt": (0, 90), "gi": (0, 100), "ts": (90, 120)}
PREF_BOOL = ("fx", "sr", "ig", "gbd", "rf", "m3d", "hp")
async def api_prefs(req):
    """Per-admin appearance, saved on the server so it survives cache clears and new devices."""
    uid, b = await need(req), await req.json()
    if b.get("reset"): db.setdefault("prefs", {}).pop(str(uid), None); save(); return web.json_response({"ok": True})
    p, src = {}, b.get("prefs") or {}
    for k, o in PREF_KEYS.items():
        if src.get(k) in o: p[k] = src[k]
    for k, (lo, hi) in PREF_NUM.items():
        if isinstance(src.get(k), (int, float)): p[k] = int(max(lo, min(hi, src[k])))
    for k in PREF_BOOL:
        if isinstance(src.get(k), bool): p[k] = src[k]
    if re.fullmatch(r"\d{1,3},\d{1,3},\d{1,3}", str(src.get("ac", ""))): p["ac"] = src["ac"]
    db.setdefault("prefs", {})[str(uid)] = p; save()
    return web.json_response({"ok": True})

async def api_cmds(req):
    await need(req, owner=True)
    b = await req.json()
    if b.get("side") not in ("group", "private") or b.get("cmd") not in CMD_NAMES: raise web.HTTPBadRequest()
    db.setdefault("cmdcfg", {}).setdefault(b["side"], {})[b["cmd"]] = bool(b.get("on")); save()
    return web.json_response({"cfg": db["cmdcfg"]})

async def api_primary(req):
    await need(req, owner=True)
    ids = [v for v in (await req.json()).get("ids", []) if v in ALL_EDGE]
    if len(set(ids)) != 4: raise web.HTTPBadRequest(text="Pick exactly four different voices")
    db["primary"] = ids; save()
    return web.json_response({"primary": ids})

async def api_request(req):
    u = verify_user(req.headers.get("X-Init", ""))
    if not u: raise web.HTTPUnauthorized(text="open inside Telegram")
    r = await make_request(req.app["bot"], u["id"], (u.get("first_name") or "")[:40], u.get("username", ""))
    return web.json_response({"role": r})

async def api_admin(req):
    actor = await need(req, owner=True)
    b = await req.json(); uid = int(b["uid"]); a = b["action"]
    audit(actor, f"Admin action: {a} {uid}")
    if a in ("approve", "deny"): await decide(req.app["bot"], uid, a == "approve")
    elif a in ("revoke", "signout"):
        if a == "revoke":
            for n in [n for n, v in db.get("creds", {}).items() if v["uid"] == uid]: db["creds"].pop(n)
        db.get("sess", {}).pop(str(uid), None); save()
    elif a == "add":
        if uid <= 0: raise web.HTTPBadRequest(text="Invalid Telegram ID")
        db.setdefault("admins", {})[str(uid)] = {"name": str(b.get("name", ""))[:40], "username": "", "ts": int(time.time()),
                                                 "tier": b.get("tier") if b.get("tier") in ("basic", "advanced", "custom") else "basic"}
        db.get("requests", {}).pop(str(uid), None); save()
    elif a == "perms":
        ad = db.get("admins", {}).get(str(uid))
        if ad is None: raise web.HTTPNotFound()
        ad["tier"] = b.get("tier") if b.get("tier") in ("basic", "advanced", "custom") else "basic"
        ad["perms"] = [p for p in b.get("perms", []) if p in PERMS]; save()
    elif a == "remove":
        db.get("sess", {}).pop(str(uid), None)
        db.get("admins", {}).pop(str(uid), None)
        for n in [n for n, v in db.get("creds", {}).items() if v["uid"] == uid]: db["creds"].pop(n)
        save()
    return web.json_response({"ok": True})

import struct as _st, hashlib as _hl
login_fail = {}  # "ip|user" and "ip|" -> (failures, last_failure_time); in memory on purpose, resets on restart

def _https(req): return req.secure or req.headers.get("X-Forwarded-Proto", "") == "https"

def totp_at(secret, t):
    """RFC 6238 TOTP (SHA-1, 6 digits, 30 s) using only the standard library."""
    key = base64.b32decode(secret + "=" * (-len(secret) % 8)); h = hmac.new(key, _st.pack(">Q", int(t // 30)), _hl.sha1).digest(); o = h[-1] & 15
    return "%06d" % ((_st.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % 1000000)

def totp_ok(secret, code):
    code = re.sub(r"\D", "", str(code))
    return len(code) == 6 and any(hmac.compare_digest(totp_at(secret, time.time() + d * 30), code) for d in (-1, 0, 1))

def lock_left(key):
    """Seconds left on an exponential lockout: starts after 5 failures (30 s, 60 s, 120 s ... capped at 15 min)."""
    n, ts = login_fail.get(key, (0, 0))
    return max(0, int(ts + min(900, 30 * 2 ** (n - 5)) - time.time())) if n >= 5 else 0

def note_fail(*keys):
    for k in keys: login_fail[k] = (login_fail.get(k, (0, 0))[0] + 1, time.time())

async def api_login(req):
    """Sign-in. Generic errors, per-account and per-IP lockout, constant-time compare, optional TOTP,
    and the session lives in an HttpOnly + SameSite=Strict (+Secure on HTTPS) cookie, never in browser storage."""
    ip = (req.headers.get("X-Forwarded-For", "") or req.remote or "?").split(",")[0].strip()
    try: b = await req.json()
    except Exception: raise web.HTTPBadRequest(text="Invalid request")
    u, p, code = str(b.get("username", "")).strip().lower()[:64], str(b.get("password", ""))[:256], str(b.get("code", ""))[:12]
    ku, ki = ip + "|" + u, ip + "|"
    if len(login_fail) > 5000: login_fail.clear()
    wait = max(lock_left(ku), lock_left(ki))
    if wait: raise web.HTTPTooManyRequests(text=f"Too many attempts. Try again in {wait} seconds.", headers={"Retry-After": str(wait)})
    uid = rec = None
    if PANEL_USER and PANEL_PASS and OWNERS and hmac.compare_digest(u.encode(), PANEL_USER.encode()) \
            and hmac.compare_digest(p.encode(), PANEL_PASS.encode()):
        uid = min(OWNERS)
    else:
        rec = db.get("creds", {}).get(u)
        if rec:
            if hmac.compare_digest(hash_pw(p, rec["salt"])[1], rec["hash"]): uid = rec["uid"]
        else:
            hash_pw(p)  # spend the same time when the account does not exist, so timing never reveals it
    if uid is None or not is_staff(uid):
        note_fail(ku, ki); audit(0, "Failed sign-in for '" + re.sub(r"[^a-z0-9_.\-]", "", u)[:24] + "'")
        raise web.HTTPUnauthorized(text="Invalid username or password")
    if rec and rec.get("totp"):
        if not code: raise web.HTTPUnauthorized(text="MFA_REQUIRED")
        if not totp_ok(rec["totp"], code):
            note_fail(ku, ki); audit(uid, "Failed two-step code")
            raise web.HTTPUnauthorized(text="Invalid username or password")
    login_fail.pop(ku, None); login_fail.pop(ki, None)
    audit(uid, "Signed in (browser)")
    rem = bool(b.get("remember")); ttl = 30 * 86400 if rem else 12 * 3600  # "remember me": 30-day cookie, otherwise a session cookie
    resp = web.json_response({"ok": True})
    resp.set_cookie("vb_s", make_token(uid, ttl), max_age=ttl if rem else None, httponly=True, secure=_https(req), samesite="Strict", path="/")
    return resp

async def api_mfa(req):
    """Two-step verification (TOTP) for the signed-in admin's browser login: begin, confirm, disable."""
    uid, b = await need(req), await req.json()
    n, rec = next(((n, v) for n, v in db.get("creds", {}).items() if v["uid"] == uid), (None, None))
    if not rec: raise web.HTTPBadRequest(text="Set a browser login first")
    act = b.get("action")
    if act == "begin":
        rec["totp_pending"] = base64.b32encode(secrets.token_bytes(10)).decode(); save()
        return web.json_response({"secret": rec["totp_pending"], "uri": f"otpauth://totp/VoiceBot:{n}?secret={rec['totp_pending']}&issuer=VoiceBot"})
    if act == "confirm" and rec.get("totp_pending") and totp_ok(rec["totp_pending"], b.get("code", "")):
        rec["totp"] = rec.pop("totp_pending"); save(); audit(uid, "Two-step verification enabled"); return web.json_response({"ok": True})
    if act == "disable" and rec.get("totp") and totp_ok(rec["totp"], b.get("code", "")):
        rec.pop("totp", None); save(); audit(uid, "Two-step verification disabled"); return web.json_response({"ok": True})
    raise web.HTTPBadRequest(text="Invalid code")

async def api_creds(req):
    uid, b = await need(req), await req.json()
    caller = uid
    if b.get("uid") and uid in OWNERS:  # the owner can set a login for any approved admin
        uid = int(b["uid"])
        if not is_staff(uid): raise web.HTTPBadRequest(text="Not an approved admin")
    audit(caller, ("Login removed" if b.get("remove") else "Login set") + (f" for {uid}" if uid != caller else ""))
    if b.get("remove"):
        cr = db.setdefault("creds", {})
        for n in [n for n, v in cr.items() if v["uid"] == uid]: cr.pop(n)
        db.get("sess", {}).pop(str(uid), None)
        save()
        return web.json_response({"ok": True})
    u, p = str(b.get("username", "")).strip().lower(), str(b.get("password", ""))
    if not re.fullmatch(r"[a-z0-9_.\-]{3,32}", u) or len(p) < 6:
        raise web.HTTPBadRequest(text="Username 3-32 chars (a-z 0-9 _ . -), password at least 6 characters")
    cr = db.setdefault("creds", {})
    if u in cr and cr[u]["uid"] != uid or u == PANEL_USER: raise web.HTTPConflict(text="Username taken")
    for n in [n for n, v in cr.items() if v["uid"] == uid]: cr.pop(n)
    salt, h = hash_pw(p); cr[u] = {"uid": uid, "salt": salt, "hash": h}
    if uid != caller: db.get("sess", {}).pop(str(uid), None)  # new password signs the admin out everywhere
    save()
    return web.json_response({"ok": True})

async def api_logout(req):
    uid, _ = auth(req)
    if uid is not None: db.get("sess", {}).pop(str(uid), None); audit(uid, "Signed out")
    resp = web.json_response({"ok": True}); resp.del_cookie("vb_s", path="/")
    return resp

async def api_export(req):
    await need(req, owner=True)  # settings only: logins, sessions and the audit log are never exported
    return web.json_response({k: v for k, v in db.items() if k not in ("creds", "sess", "audit")})

async def api_backup(req):
    await need(req, owner=True)
    b = await req.json()
    db["backup_off"] = not bool(b.get("enabled"))
    save()
    return web.json_response({"ok": True, "enabled": not db["backup_off"] and bool(BACKUP_CHAT)})

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

CMD_LIST = [("start", "Start", "user"), ("help", "Help", "user"), ("about", "About", "user"), ("tts", "Text to voice", "user"), ("voices", "Choose a voice", "user"),
            ("voicepreset", "Voice style", "user"), ("replay", "Replay a voice", "user"), ("history", "History", "user"), ("favs", "Favourites", "user"), ("fav", "Favourite", "user"),
            ("stats", "Statistics", "user"), ("request", "Request access", "user"), ("panel", "Admin panel", "admin"), ("on", "Bot on", "admin"),
            ("off", "Bot off", "admin"), ("status", "Bot status", "admin"), ("provider", "Provider", "admin"), ("lang", "Language", "admin"), ("speed", "Speed", "admin"),
            ("pitch", "Pitch", "admin"), ("volume", "Volume", "admin"), ("users", "Listed users", "admin"), ("adduser", "Add listed user", "admin"), ("deluser", "Remove listed user", "admin"),
            ("onlylisted", "Only listed", "admin"), ("voice", "Voice settings", "admin")]
CMD_NAMES = {c[0] for c in CMD_LIST}

def cmd_enabled(name, ctype):
    return db.get("cmdcfg", {}).get("private" if ctype == "private" else "group", {}).get(name, True)

def guarded(name, fn):
    async def g(update, ctx):
        c, u = update.effective_chat, update.effective_user
        if c and u and u.id not in OWNERS and not cmd_enabled(name, c.type): return  # disabled for this chat type
        return await fn(update, ctx)
    return g

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

USER_CMDS = [("tts", "Turn text into a voice message"), ("voices", "Choose a voice (private chat)"), ("voicepreset", "Voice style: normal, deep, robot, child, deep_robot"),
             ("replay", "Replay your last voice"), ("request", "Ask for admin access")]
ADMIN_CMDS = [("panel", "Open the admin panel", None), ("on", "Turn the bot on here", None), ("off", "Turn the bot off here", None), ("status", "Bot status", None),
              ("provider", "Change the voice provider", "providers"), ("users", "Manage listed users", None),
              ("approve", "Approve an access request", "owner"), ("deny", "Deny an access request", "owner"), ("admins", "List admins", "owner"), ("removeadmin", "Remove an admin", "owner")]

async def help_cmd(update, ctx):
    u, c = update.effective_user, update.effective_chat
    ok = lambda n: cmd_enabled(n, c.type) or u.id in OWNERS
    lines = ["SAKIB Lite — Voice Assistant", "", "User Commands"] + [f"/{n} — {d}" for n, d in USER_CMDS if ok(n) and not (n == "voices" and c.type != "private")]
    adm = [f"/{n} — {d}" for n, d, p in ADMIN_CMDS if is_staff(u.id) and ok(n) and (p is None or (u.id in OWNERS if p == "owner" else has_perm(u.id, p)))]
    if adm: lines += ["", "Admin Commands"] + adm
    await say(update, "\n".join(lines))

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

ALL_EDGE = list(VOICES["bn"]) + list(VOICES["en"])
LBL = {"en": "ENG", "bn": "BN"}
def vlabel(v): return f"{v['name']} · {LBL.get(v['lang'], v['lang'].upper())}"  # language label comes from metadata, never from the typed name
def elabel(n): return f"{n.split('-', 2)[-1].replace('Neural', '')} · {'BN' if n.startswith('bn') else 'ENG'}"
def primary():
    ids = [v for v in db.get("primary", []) if v in ALL_EDGE]
    return ids if len(ids) == 4 else (ALL_EDGE[:2] + ALL_EDGE[len(VOICES["bn"]):len(VOICES["bn"]) + 2])

def sel_settings(uid):
    return {**DEFAULTS, **db.get("global_settings", {}), **({} if is_staff(uid) else db.get("users", {}).get(str(uid), {}))}

def voices_kb(uid, page=0):
    s = sel_settings(uid); cur = {s.get("voice_bn"), s.get("voice_en")}
    mk = lambda t, d: InlineKeyboardButton(t[:34], callback_data=d)
    if page == 0:
        items = [(("✅ " if v in cur and s.get("provider") == "edge" else "") + elabel(v), f"ve:{ALL_EDGE.index(v)}") for v in primary()]
        rows = [[mk(t, d) for t, d in items[i:i + 2]] for i in range(0, len(items), 2)]
        return InlineKeyboardMarkup(rows + [[mk("MORE+", "vm:1")]])
    items = [(elabel(v), f"ve:{ALL_EDGE.index(v)}") for v in ALL_EDGE if v not in primary()]
    items += [(f"{v['prov'].upper()} · {vlabel(v)}", f"vs:{v['k']}") for v in lib() if v.get("access") == "public" and v.get("visible", True) and v.get("enabled", True)]
    rows = [[mk(t, d) for t, d in items[i:i + 2]] for i in range(0, len(items), 2)]
    return InlineKeyboardMarkup(rows + [[mk("‹ BACK", "vm:0")]])

async def voices_cmd(update, ctx):
    if update.effective_chat.type != "private":  # never show voice lists or keyboards in groups
        return await say(update, "🎙 Voice selection is available in a private chat with me.")
    await update.effective_message.reply_text("🎙 Choose a voice:", reply_markup=voices_kb(update.effective_user.id))

async def cb_voice(update, ctx):
    q = update.callback_query; u, c = q.from_user, q.message.chat
    if c.type != "private": return await q.answer("Voice selection works in a private chat only", show_alert=True)
    p = q.data.split(":")
    try:
        if p[0] == "vm":
            await q.answer(); return await q.edit_message_reply_markup(voices_kb(u.id, int(p[1])))
        if p[0] == "vs":
            v = next(x for x in lib() if x["k"] == p[1])
            if not (can_use_voice(u.id, v) and v.get("visible", True)): return await q.answer("This voice is not available to you", show_alert=True)
            kw, name = {v["prov"] + "_voice_" + v["lang"]: v["id"], "provider": v["prov"]}, f"{v['prov'].upper()} · {vlabel(v)}"
        else:
            e = ALL_EDGE[int(p[1])]; kw, name = {"voice_" + e[:2]: e, "provider": "edge"}, elabel(e)
    except (IndexError, KeyError, ValueError, StopIteration):
        return await q.answer("The list changed. Send /voices again.", show_alert=True)
    clean = sanitize(kw)
    if is_staff(u.id): db.setdefault("global_settings", {}).update(clean)
    else: db.setdefault("users", {}).setdefault(str(u.id), {}).update(clean)
    save(); await q.answer("Saved ✓")
    try: await q.edit_message_text(f"✅ Voice set: {name}", reply_markup=None)  # removes the whole keyboard
    except Exception: pass

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

STT_MAX = 40 * 1024 * 1024
async def api_credits(req):
    uid = await need(req)
    if not (has_perm(uid, "providers") or has_perm(uid, "elevenlabs")): raise web.HTTPForbidden(text="ElevenLabs access required")
    return web.json_response({"elevenlabs": await el_balance(force=True) if ELEVENLABS_API_KEY else {"error": "ELEVENLABS_API_KEY is not set"}})

async def api_stt(req):
    """Speech-to-text for audio or video files (approved admins): ElevenLabs Scribe or Cartesia ink-whisper."""
    uid = await need(req)
    prov = model = lang = ""; data = b""; fname = "audio"; ctype = "application/octet-stream"
    async for part in await req.multipart():
        if part.name == "file":
            fname = part.filename or "audio"; ctype = part.headers.get("Content-Type", ctype); buf = bytearray()
            while True:
                ch = await part.read_chunk(1 << 20)
                if not ch: break
                buf += ch
                if len(buf) > STT_MAX: raise web.HTTPRequestEntityTooLarge(max_size=STT_MAX, actual_size=len(buf))
            data = bytes(buf)
        elif part.name in ("provider", "model", "language"):
            v = (await part.text())[:40]
            if part.name == "provider": prov = v
            elif part.name == "model": model = v
            else: lang = v
    if not data: raise web.HTTPBadRequest(text="No file received")
    if prov in ("elevenlabs", "cartesia") and not has_perm(uid, prov): raise web.HTTPForbidden(text="You do not have access to this provider")
    lang = lang if re.fullmatch(r"[a-z]{2,3}", lang or "") else ""
    fd = aiohttp.FormData(); fd.add_field("file", data, filename=fname[:80], content_type=ctype)
    if prov == "elevenlabs":
        if not ELEVENLABS_API_KEY: raise web.HTTPBadRequest(text="ElevenLabs API key is not set")
        model = model if model in MODELS["el_stt"] else "scribe_v2"; fd.add_field("model_id", model)
        if lang: fd.add_field("language_code", lang)
        url, hdr = "https://api.elevenlabs.io/v1/speech-to-text", {"xi-api-key": ELEVENLABS_API_KEY}
    elif prov == "cartesia":
        if not CARTESIA_API_KEY: raise web.HTTPBadRequest(text="Cartesia API key is not set")
        model = "ink-whisper"; fd.add_field("model", model); fd.add_field("language", lang or "en")
        url, hdr = "https://api.cartesia.ai/stt", {"Authorization": f"Bearer {CARTESIA_API_KEY}", "X-API-Key": CARTESIA_API_KEY, "Cartesia-Version": "2026-03-01"}
    else:
        raise web.HTTPBadRequest(text="Unknown provider")
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as ses:
            async with ses.post(url, headers=hdr, data=fd) as r:
                body = await r.text()
                if r.status >= 400:
                    k = classify(r.status, body)
                    raise web.HTTPBadGateway(text=f"{PNAME.get(prov, prov)}: {KIND_TEXT.get(k, k)} - {body[:160]}")
                j = json.loads(body)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        raise web.HTTPBadGateway(text="The speech service did not answer correctly")
    audit(uid, f"Speech-to-text {prov}/{model} ({len(data) // 1024} KB)")
    return web.json_response({"text": str(j.get("text", "")).strip(), "provider": prov, "model": model, "language": j.get("language_code") or j.get("language") or lang})

async def post_init(app):
    load()
    app_ref["bot"] = app.bot
    await restore(app.bot)
    migrate(); save()
    w = web.Application(client_max_size=STT_MAX + (2 << 20))
    w["bot"] = app.bot
    w.add_routes([web.get("/", health), web.get("/health", health), web.get("/app", page), web.get("/api/state", api_state),
                  web.post("/api/save", api_save), web.post("/api/voice", api_voice), web.post("/api/action", api_action),
                  web.post("/api/preview", api_preview), web.post("/api/request", api_request),
                  web.post("/api/login", api_login), web.post("/api/creds", api_creds), web.post("/api/backup", api_backup), web.post("/api/logout", api_logout), web.post("/api/mfa", api_mfa), web.get("/api/export", api_export), web.get("/api/credits", api_credits), web.get("/dl/{tok}", api_dl), web.post("/api/bot_toggle", api_bot_toggle), web.post("/api/prefs", api_prefs), web.post("/api/cmds", api_cmds), web.post("/api/primary", api_primary), web.post("/api/stt", api_stt),
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
    def H(h):
        if isinstance(h, CommandHandler): h.callback = guarded(sorted(h.commands)[0], h.callback)
        app.add_handler(h)
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
    H(CallbackQueryHandler(cb_voice, pattern=r"^v[mse]:"))
    H(MessageHandler(filters.ChatType.GROUPS & (filters.TEXT | filters.CAPTION) & ~filters.COMMAND, collect))
    app.add_error_handler(on_error)
    app.run_polling(allowed_updates=Update.ALL_TYPES, bootstrap_retries=-1)

if __name__ == "__main__":
    main()
