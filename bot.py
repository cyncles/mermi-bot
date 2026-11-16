import os
import json
import time
from datetime import datetime, timezone, timedelta

import aiohttp
from aiohttp import web
import discord
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
SB_URL = (os.getenv("SUPABASE_URL") or "").strip().rstrip("/")
SB_KEY = (os.getenv("SUPABASE_KEY") or "").strip()
CLOUD = bool(SB_URL and SB_KEY)          # Supabase ayarlıysa bulut modu, değilse yerel dosya modu
PORT = os.getenv("PORT")                 # Render bunu otomatik verir

BASE = os.path.dirname(os.path.abspath(__file__))
STATS_FILE = os.path.join(BASE, "stats.json")
DATA_JS = os.path.join(BASE, "data.js")
TR = timezone(timedelta(hours=3))        # Türkiye saati

intents = discord.Intents.default()
intents.members = True                   # Developer Portal'da "Server Members Intent" açık olmalı
intents.voice_states = True
intents.guilds = True
intents.messages = True                  # mesaj içeriği okunmaz, sadece sayılır

stats = {"guild": "", "users": {}}
voice_since = {}                         # user_id -> sesli süre en son ne zaman eklendi
dirty = set()                            # Supabase'e yazılması gereken kullanıcılar
http = None                              # aiohttp oturumu (bulut modu)


# ---------------------------------------------------------------- Supabase
def sb_headers(extra=None):
    h = {"apikey": SB_KEY, "Content-Type": "application/json"}
    if SB_KEY.startswith("eyJ"):         # eski tip (JWT) anahtarlar için
        h["Authorization"] = "Bearer " + SB_KEY
    if extra:
        h.update(extra)
    return h


async def sb_load(session):
    users, offset = {}, 0
    while True:
        params = {"select": "*", "order": "id", "limit": "1000", "offset": str(offset)}
        async with session.get(f"{SB_URL}/rest/v1/members", params=params, headers=sb_headers()) as r:
            if r.status != 200:
                raise RuntimeError(f"Supabase okuma hatası {r.status}: {(await r.text())[:200]}")
            rows = await r.json()
        for row in rows:
            users[str(row["id"])] = {
                "id": str(row["id"]),
                "name": row.get("name") or "",
                "avatar": row.get("avatar") or "",
                "joined": row.get("joined"),
                "messages": int(row.get("messages") or 0),
                "voice_seconds": int(row.get("voice_seconds") or 0),
                "night_messages": int(row.get("night_messages") or 0),
                "last_seen": row.get("last_seen"),
            }
        if len(rows) < 1000:
            return users
        offset += 1000


async def sb_upsert(session, rows):
    for i in range(0, len(rows), 200):
        chunk = rows[i:i + 200]
        headers = sb_headers({"Prefer": "resolution=merge-duplicates,return=minimal"})
        async with session.post(f"{SB_URL}/rest/v1/members", params={"on_conflict": "id"},
                                json=chunk, headers=headers) as r:
            if r.status >= 300:
                raise RuntimeError(f"Supabase yazma hatası {r.status}: {(await r.text())[:200]}")


def row_for(uid):
    u = stats["users"][uid]
    return {
        "id": u["id"], "name": u["name"], "avatar": u["avatar"], "joined": u["joined"],
        "messages": u["messages"], "voice_seconds": u["voice_seconds"],
        "night_messages": u["night_messages"], "last_seen": u["last_seen"],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------- Kullanıcı verisi
def get_user(member):
    uid = str(member.id)
    u = stats["users"].setdefault(uid, {
        "id": uid, "name": "", "avatar": "", "joined": None,
        "messages": 0, "voice_seconds": 0, "night_messages": 0, "last_seen": None,
    })
    u["name"] = member.display_name
    u["avatar"] = str(member.display_avatar.with_size(128).url)
    if member.joined_at:
        u["joined"] = member.joined_at.isoformat()
    dirty.add(uid)
    return u


def save_local():
    stats["updated"] = datetime.now(TR).isoformat()
    tmp = STATS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATS_FILE)
    tmp = DATA_JS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("window.MERMI_DATA = " + json.dumps(stats, ensure_ascii=False) + ";")
    os.replace(tmp, DATA_JS)


async def flush():
    """Değişenleri kaydeder."""
    if not CLOUD:
        save_local()
        return
    if not dirty:
        return
    ids = [i for i in dirty if i in stats["users"]]
    dirty.clear()
    if not ids:
        return
    try:
        await sb_upsert(http, [row_for(i) for i in ids])
    except Exception as e:
        dirty.update(ids)                # yazılamadıysa bir sonraki turda tekrar dene
        print("Kaydetme hatası:", e)


def credit_voice(uid, remove=False):
    now = time.time()
    last = voice_since.get(uid)
    if last is None:
        return
    u = stats["users"].get(str(uid))
    if u is not None:
        u["voice_seconds"] += int(now - last)
        u["last_seen"] = datetime.now(TR).isoformat()
        dirty.add(str(uid))
    if remove:
        voice_since.pop(uid, None)
    else:
        voice_since[uid] = now


# ---------------------------------------------------------------- Bot
class Bot(discord.Client):
    async def setup_hook(self):
        global http, stats
        if CLOUD:
            http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
            stats["users"] = await sb_load(http)          # yeniden başlasa da sayaçlar sıfırlanmaz
            print(f"Supabase'ten {len(stats['users'])} üye yüklendi.")
        elif os.path.exists(STATS_FILE):
            try:
                with open(STATS_FILE, "r", encoding="utf-8") as f:
                    stats = json.load(f)
            except Exception:
                pass
        if PORT:                                          # Render'ın "ayakta mısın" kontrolü için
            app = web.Application()

            async def ok(_):
                return web.Response(text="ok")
            app.router.add_get("/", ok)
            app.router.add_get("/health", ok)
            runner = web.AppRunner(app)
            await runner.setup()
            await web.TCPSite(runner, "0.0.0.0", int(PORT)).start()
            print("Keepalive sunucusu açık, port", PORT)
        tick.start()


client = Bot(intents=intents)


@client.event
async def on_ready():
    print(f"Bot hazır: {client.user}")
    for guild in client.guilds:
        stats["guild"] = guild.name
        for m in guild.members:
            if m.bot:
                continue
            get_user(m)
            if m.voice and m.voice.channel:
                voice_since.setdefault(m.id, time.time())
    await flush()


@client.event
async def on_member_join(member):
    if not member.bot:
        get_user(member)


@client.event
async def on_message(message):
    if message.author.bot or message.guild is None:
        return
    u = get_user(message.author)
    u["messages"] += 1
    if datetime.now(TR).hour < 6:
        u["night_messages"] += 1
    u["last_seen"] = datetime.now(TR).isoformat()


@client.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return
    get_user(member)
    if after.channel and member.id not in voice_since:
        voice_since[member.id] = time.time()
    elif not after.channel and member.id in voice_since:
        credit_voice(member.id, remove=True)


@tasks.loop(seconds=30)
async def tick():
    for uid in list(voice_since.keys()):
        credit_voice(uid)
    await flush()


if not TOKEN:
    raise SystemExit("DISCORD_TOKEN bulunamadı. .env dosyasına ya da Render ortam değişkenlerine token'ı yaz.")

client.run(TOKEN)
