# main.py
# Lineage2M Boss Bot (JST fixed) + FastAPI keepalive + Render friendly
# - Env: DISCORD_TOKEN (required)
# - Optional: BACKOFF_429_MIN (default 900), BACKOFF_JITTER_SEC (default 30), PORT (default 10000)

import os
import json
import asyncio
import logging
import random
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import discord
from discord.ext import commands, tasks

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from uvicorn import Config, Server

# -------------------- constants --------------------
JST = timezone(timedelta(hours=9))
DATA_DIR = "data"
STORE_FILE = os.path.join(DATA_DIR, "store.json")
PRESET_FILE = "bosses_preset.json"

CHECK_SEC = 10
MERGE_WINDOW_SEC = 60  # +- seconds to merge notifications
BACKOFF_429_MIN = int(os.getenv("BACKOFF_429_MIN", "900"))
BACKOFF_JITTER = int(os.getenv("BACKOFF_JITTER_SEC", "30"))

# -------------------- logging ----------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("bossbot")

# -------------------- models & storage --------------
@dataclass
class BossState:
    name: str
    respawn_min: int
    rate: int = 100
    next_spawn_utc: Optional[int] = None
    channel_id: Optional[int] = None
    skip: int = 0
    excluded_reset: bool = False
    initial_delay_min: int = 0

    def label_flags(self) -> str:
        parts = []
        if self.rate == 100:
            parts.append("※確定")
        if self.skip > 0:
            parts.append(f"{self.skip}周")
        return "[" + "] [".join(parts) + "]" if parts else ""

class Store:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        if not os.path.exists(self.path):
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({}, f, ensure_ascii=False)

    def load(self) -> Dict[str, Dict[str, dict]]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def save(self, data: Dict[str, Dict[str, dict]]):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

# -------------------- alias table (short names) ----
ALIASES: Dict[str, str] = {
    # examples
    "qa": "クイーンアント",
    "QA": "クイーンアント",
    "がれす": "ガレス",
    "べひ": "ベヒモス",
    "てぃみに": "ティミニエル",
    "ころーん": "コルーン",
    "めでゅ": "メデューサ",
    # add freely...
}

def norm_boss_name(text: str) -> str:
    # alias hit
    if text in ALIASES:
        return ALIASES[text]
    return text

# -------------------- bot --------------------------
class BossBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # we parse messages
        super().__init__(command_prefix="!", intents=intents)

        self.store = Store(STORE_FILE)
        self.data: Dict[str, Dict[str, dict]] = self.store.load()  # guild -> name -> dict
        self.presets: Dict[str, Tuple[int, int, int]] = {}  # name -> (respawn_min, rate, initial_delay_min)

        self._load_presets()

        # background ticker
        self.tick.start()

    # -------- storage utils --------
    def _gkey(self, guild_id: int) -> str:
        return str(guild_id)

    def _get(self, guild_id: int, name: str) -> Optional[BossState]:
        g = self.data.get(self._gkey(guild_id), {})
        d = g.get(name)
        return BossState(**d) if d else None

    def _set(self, guild_id: int, st: BossState):
        gkey = self._gkey(guild_id)
        if gkey not in self.data:
            self.data[gkey] = {}
        self.data[gkey][st.name] = asdict(st)
        self.store.save(self.data)

    def _all(self, guild_id: int) -> List[BossState]:
        g = self.data.get(self._gkey(guild_id), {})
        return [BossState(**d) for d in g.values()]

    def _load_presets(self):
        # Format: [{"name": "...", "rate": 100, "respawn_h": 8, "initial_delay_h": 0}, ...]
        try:
            with open(PRESET_FILE, "r", encoding="utf-8") as f:
                arr = json.load(f)
            for x in arr:
                name = x["name"]
                respawn_min = int(round(float(x.get("respawn_h", 1)) * 60))
                rate = int(x.get("rate", 100))
                init_delay_min = int(round(float(x.get("initial_delay_h", 0)) * 60))
                self.presets[name] = (respawn_min, rate, init_delay_min)
        except Exception as e:
            log.warning("preset load error: %s", e)
            self.presets = {}

    # -------- input parser --------
    def _parse_input(self, content: str) -> Optional[Tuple[str, datetime, Optional[int]]]:
        # "ボス名 HHMM [xh]" or "ボス名"
        parts = content.strip().split()
        if not parts:
            return None
        name = norm_boss_name(parts[0])

        jst_now = datetime.now(JST)
        when = None
        override_min = None

        if len(parts) >= 2 and parts[1].isdigit() and 3 <= len(parts[1]) <= 4:
            p = parts[1].zfill(4)
            h, m = int(p[:2]), int(p[2:])
            base = jst_now.replace(hour=h, minute=m, second=0, microsecond=0)
            if base > jst_now:
                base = base - timedelta(days=1)  # future -> previous day
            when = base
        if when is None:
            when = jst_now

        if len(parts) >= 3 and parts[2].lower().endswith("h"):
            try:
                override_min = int(round(float(parts[2][:-1]) * 60))
            except Exception:
                pass

        return name, when, override_min

    # -------- background ticker --------
    @tasks.loop(seconds=CHECK_SEC)
    async def tick(self):
        await self.wait_until_ready()
        now = now_utc()

        for gkey, bosses in list(self.data.items()):
            guild = self.get_guild(int(gkey))
            if not guild:
                continue

            pre_items: Dict[int, List[str]] = {}
            now_items: Dict[int, List[str]] = {}

            for d in list(bosses.values()):
                st = BossState(**d)
                if not st.channel_id or not st.next_spawn_utc:
                    continue

                center = datetime.fromtimestamp(st.next_spawn_utc, tz=timezone.utc)

                # 1 min before
                if abs((now - (center - timedelta(minutes=1))).total_seconds()) <= MERGE_WINDOW_SEC:
                    label = f"{center.astimezone(JST).strftime('%H:%M:%S')} : {st.name} {st.label_flags()}".strip()
                    pre_items.setdefault(st.channel_id, []).append(label)

                # on spawn
                if abs((now - center).total_seconds()) <= MERGE_WINDOW_SEC:
                    label = f"{st.name} 出現！ [{center.astimezone(JST).strftime('%H:%M:%S')}] (skip:{st.skip}) {st.label_flags()}".strip()
                    now_items.setdefault(st.channel_id, []).append(label)

                # auto skip after 60s
                if (now - center).total_seconds() >= 60:
                    st.next_spawn_utc += st.respawn_min * 60
                    st.skip += 1
                    self._set(int(gkey), st)

            # send grouped messages
            for cid, arr in pre_items.items():
                ch = guild.get_channel(cid) or await guild.fetch_channel(cid)
                if arr:
                    await ch.send("⏰ 1分前\n" + "\n".join(sorted(set(arr))))
            for cid, arr in now_items.items():
                ch = guild.get_channel(cid) or await guild.fetch_channel(cid)
                if arr:
                    await ch.send("🔥\n" + "\n".join(sorted(set(arr))))

    @tick.before_loop
    async def before_tick(self):
        await self.wait_until_ready()

    # -------- events --------
    async def on_ready(self):
        log.info("LOGGED IN as %s (id=%s)", self.user, self.user.id)

    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        content = message.content.strip()

        # Allow "bt" aliases without "!" (message command)
        if content.lower() in {"bt", "bt3", "bt6", "bt12", "bt24"}:
            ctx = await self.get_context(message)
            horizon = None
            if content.lower() != "bt":
                horizon = int(content.lower().replace("bt", ""))
            await self._send_bt(ctx, horizon_h=horizon)
            return

        # parse "name HHMM [xh]"
        parsed = self._parse_input(content)
        if parsed:
            name, when_jst, respawn_min_override = parsed
            gkey = self._gkey(message.guild.id)
            g = self.data.get(gkey, {})

            st = BossState(name=name, respawn_min=60)
            if name in g:
                st = BossState(**g[name])
            elif name in self.presets:
                resp, rate, init_delay = self.presets[name]
                st.respawn_min, st.rate, st.initial_delay_min = resp, rate, init_delay

            if respawn_min_override:
                st.respawn_min = respawn_min_override

            st.channel_id = st.channel_id or message.channel.id
            center = when_jst.astimezone(timezone.utc) + timedelta(minutes=st.respawn_min + st.initial_delay_min)
            st.next_spawn_utc = int(center.timestamp())
            st.skip = 0
            self._set(message.guild.id, st)

            await message.add_reaction("✅")
            return

        await self.process_commands(message)

    # -------- commands --------
    @commands.command(name="bt")
    async def cmd_bt(self, ctx: commands.Context):
        await self._send_bt(ctx, horizon_h=None)

    @commands.command(name="bt3")
    async def cmd_bt3(self, ctx: commands.Context):
        await self._send_bt(ctx, horizon_h=3)

    @commands.command(name="bt6")
    async def cmd_bt6(self, ctx: commands.Context):
        await self._send_bt(ctx, horizon_h=6)

    @commands.command(name="bt12")
    async def cmd_bt12(self, ctx: commands.Context):
        await self._send_bt(ctx, horizon_h=12)

    @commands.command(name="bt24")
    async def cmd_bt24(self, ctx: commands.Context):
        await self._send_bt(ctx, horizon_h=24)

    async def _send_bt(self, ctx: commands.Context, horizon_h: Optional[int]):
        arr = self._all(ctx.guild.id)
        now = now_utc()
        items: List[Tuple[datetime, BossState]] = []
        for st in arr:
            if not st.next_spawn_utc:
                continue
            t = datetime.fromtimestamp(st.next_spawn_utc, tz=timezone.utc)
            if horizon_h is not None and (t - now).total_seconds() > horizon_h * 3600:
                continue
            items.append((t, st))
        items.sort(key=lambda x: x[0])

        if not items:
            await ctx.send("予定はありません。")
            return

        lines: List[str] = []
        current_hour: Optional[int] = None
        for t, st in items:
            j = t.astimezone(JST)
            if current_hour is None:
                current_hour = j.hour
            if j.hour != current_hour:
                lines.append("")  # only one blank line between hours
                current_hour = j.hour
            lines.append(f"{j.strftime('%H:%M:%S')} : {st.name} {st.label_flags()}")

        await ctx.send("\n".join(lines))

# -------------------- FastAPI keepalive -------------
app = FastAPI()

@app.get("/health")
async def health(silent: Optional[int] = None):
    return {"ok": True}

@app.get("/")
async def root():
    # Optional: simple top page (UptimeRobot uses /health)
    return JSONResponse({"ok": True, "hint": "use /health for uptime monitor"})

# -------------------- run helpers -------------------
async def _sleep_backoff():
    jitter = random.randint(-BACKOFF_JITTER, BACKOFF_JITTER) if BACKOFF_JITTER else 0
    wait = max(60, BACKOFF_429_MIN + jitter)
    log.warning("[BOT] 429/RateLimited detected. sleep %ss and retry.", wait)
    await asyncio.sleep(wait)

async def run_bot_and_api():
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN not set")

    bot = BossBot()

    # run FastAPI
    port = int(os.getenv("PORT", "10000"))
    server = Server(Config(app=app, host="0.0.0.0", port=port, loop="asyncio", access_log=False))

    async def serve_api():
        await server.serve()

    async def run_bot_loop():
        # resilient start with backoff on rate limit, and clear message on login failure
        while True:
            try:
                log.info("[BOT] starting login...")
                await bot.start(token)
            except discord.errors.LoginFailure:
                log.error("[BOT] LoginFailure: DISCORD_TOKEN invalid or expired. Reset token and redeploy.")
                await asyncio.sleep(3600)  # wait 1h to avoid tight loop
            except discord.errors.HTTPException as e:
                status = getattr(e, "status", None)
                if status == 429:
                    await _sleep_backoff()
                else:
                    log.exception("[BOT] HTTPException (status=%s)", status)
                    await asyncio.sleep(30)
            except Exception:
                log.exception("[BOT] unexpected error")
                await asyncio.sleep(30)
            finally:
                try:
                    await bot.close()
                except Exception:
                    pass

    await asyncio.gather(serve_api(), run_bot_loop())

def run():
    asyncio.run(run_bot_and_api())

if __name__ == "__main__":
    run()
