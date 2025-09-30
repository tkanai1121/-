# main.py
# Lineage2M Boss Bot (JST固定 / Render運用)
# - 討伐入力: 「ボス名 HHMM [8h]」 or 「ボス名」（入力時刻）
# - 一覧: bt / bt3 / bt6 / bt12 / bt24 （"!" なしでも可）
# - 通知: 1分前(⏰)＆出現(🔥)を±60秒で集約、skip自動加算
# - チャンネル固定: hereon / hereoff
# - 429/1015を検出したらバックオフ → 再接続
# - Render用 FastAPI /health（GET/HEAD対応）

import os
import json
import math
import time
import random
import asyncio
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import discord
from discord.ext import commands, tasks

# ----------- ログ -----------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bossbot")

# ----------- 時刻・定数 -----------
JST = timezone(timedelta(hours=9))
DATA_DIR = "data"
STORE_FILE = os.path.join(DATA_DIR, "store.json")
PRESET_FILE = "bosses_preset.json"  # 同ディレクトリに置く
CHECK_SEC = 10
MERGE_WINDOW_SEC = 60  # 通知集約±秒

# 429 バックオフ（Render の Environment Variables で上書き可）
BACKOFF_429_MIN = int(os.environ.get("BACKOFF_429_MIN", "900"))  # 15分
BACKOFF_JITTER_SEC = int(os.environ.get("BACKOFF_JITTER_SEC", "30"))

# ----------- ユーティリティ -----------
def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def to_jst(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(JST)

def hira_to_kata(s: str) -> str:
    # ひらがな→カタカナ正規化
    res = []
    for ch in s:
        code = ord(ch)
        if 0x3041 <= code <= 0x3096:
            res.append(chr(code + 0x60))
        else:
            res.append(ch)
    return "".join(res)

def normalize_name(s: str) -> str:
    s = s.strip().replace("　", " ")
    s = "".join(s.split())  # 空白除去
    s = hira_to_kata(s)
    return s

# ----------- エイリアス -----------
ALIASES = {
    "クイーンアント": ["QA", "qa", "クイアン", "クィーンアント", "くいーんあんと"],
    "チェルトゥバ": ["チェトゥバ", "チェトゥルゥバ", "チェルトゥヴァ"],
    "ティミニエル": ["ティミ", "てぃみにえる"],
    "ガレス": ["GARETH", "gareth", "がれす"],
    "ベヒモス": ["べひ", "BEHEMOTH", "behemoth"],
    "オルフェン": ["ORFEN", "orfen", "おるふぇん"],
    "コルーン": ["COLUN", "colun", "こるーん"],
    "グラーキ": ["glaaki", "GLAAKI", "ぐらーき"],
    "スタン": ["stan", "STAN", "すたん"],
}

def build_alias_map():
    m = {}
    for official, arr in ALIASES.items():
        m[normalize_name(official)] = official
        for a in arr:
            m[normalize_name(a)] = official
    return m

ALIAS_MAP = build_alias_map()

def unify_boss_name(raw: str) -> str:
    key = normalize_name(raw)
    if key in ALIAS_MAP:
        return ALIAS_MAP[key]
    return raw

# ----------- モデル -----------
@dataclass
class BossState:
    name: str
    respawn_min: int          # 周期（分）
    rate: int = 100           # 出現率（%）
    next_spawn_utc: Optional[int] = None
    skip: int = 0

    def label_flags(self) -> str:
        parts = []
        if self.rate == 100:
            parts.append("※確定")
        if self.skip > 0:
            parts.append(f"{self.skip}周")
        return "[" + "] [".join(parts) + "]" if parts else ""

# ----------- ストア -----------
class Store:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        if not os.path.exists(self.path):
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({"guilds": {}}, f, ensure_ascii=False)

    def load(self) -> dict:
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)

    def save(self, data: dict):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

# ----------- Bot 本体 -----------
class BossBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True

        # ★ デフォルトの help を無効化（自作 help と衝突しないように）
        super().__init__(command_prefix="!", intents=intents, help_command=None)

        self.store = Store(STORE_FILE)
        self.db: dict = self.store.load()  # {"guilds": {gid: {"here": cid, "bosses": {name: BossState}}}}
        self.presets = self._load_presets()  # name -> (respawn_min, rate, initial_delay_min)

        # コマンド登録
        self.add_command(self.cmd_help)
        self.add_command(self.hereon)
        self.add_command(self.hereoff)
        self.add_command(self.bt)
        self.add_command(self.bt3)
        self.add_command(self.bt6)
        self.add_command(self.bt12)
        self.add_command(self.bt24)

    async def setup_hook(self):
        # ここで tasks.loop を start（イベントループが確実に存在する）
        if not self.tick.is_running():
            self.tick.start()
        log.info("setup_hook: background loop started")

    # -------------- ストア --------------
    def _g(self, guild_id: int) -> dict:
        gid = str(guild_id)
        if "guilds" not in self.db:
            self.db["guilds"] = {}
        if gid not in self.db["guilds"]:
            self.db["guilds"][gid] = {"bosses": {}, "here": None}
        return self.db["guilds"][gid]

    def _save(self):
        self.store.save(self.db)

    def _load_presets(self) -> Dict[str, Tuple[int, int, int]]:
        """
        bosses_preset.json の形式（例）:
        [
          {"name":"フェリス","rate":50,"respawn_h":2,"initial_delay_h":0},
          ...
        ]
        """
        try:
            with open(PRESET_FILE, "r", encoding="utf-8") as f:
                arr = json.load(f)
            res = {}
            for x in arr:
                name = x["name"]
                respawn_min = int(round(float(x.get("respawn_h", 0)) * 60))
                rate = int(x.get("rate", 100))
                initial_delay_min = int(round(float(x.get("initial_delay_h", 0)) * 60))
                res[name] = (respawn_min, rate, initial_delay_min)
            log.info(f"presets loaded: {len(res)} bosses")
            return res
        except Exception as e:
            log.warning(f"presets load failed: {e}")
            return {}

    # -------------- 入力パース --------------
    def _parse_quick_input(self, content: str) -> Optional[Tuple[str, datetime, Optional[int]]]:
        parts = content.strip().split()
        if not parts:
            return None

        raw_name = unify_boss_name(parts[0])
        name = raw_name

        jst_now = datetime.now(JST)
        base = None
        resp_min = None

        # HHMM
        if len(parts) >= 2 and parts[1].isdigit() and 3 <= len(parts[1]) <= 4:
            p = parts[1].zfill(4)
            h, m = int(p[:2]), int(p[2:])
            try:
                t = jst_now.replace(hour=h, minute=m, second=0, microsecond=0)
                if t > jst_now:
                    t -= timedelta(days=1)
                base = t
            except ValueError:
                base = None

        if base is None:
            base = jst_now

        # 可変周期（8h等）
        if len(parts) >= 3 and parts[2].lower().endswith("h"):
            try:
                resp_min = int(round(float(parts[2][:-1]) * 60))
            except ValueError:
                resp_min = None

        return name, base, resp_min

    # -------------- メッセージ受信 --------------
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        content = message.content.strip()

        # "!" なし簡易コマンド（help/here系はどこでも、一覧はhereonチャンネル限定）
        if content.lower() in {"bt", "bt3", "bt6", "bt12", "bt24", "help", "hereon", "hereoff"}:
            if content.lower() == "help":
                await self._send_help(message.channel)
                return
            if content.lower() == "hereon":
                await self._cmd_hereon(message.channel)
                return
            if content.lower() == "hereoff":
                await self._cmd_hereoff(message.channel)
                return

            if not self._is_allowed_channel(message.guild.id, message.channel.id):
                return

            if content.lower() == "bt":
                await self._send_bt(message.channel, message.guild.id, None)
                return
            hori = {"bt3": 3, "bt6": 6, "bt12": 12, "bt24": 24}.get(content.lower())
            if hori:
                await self._send_bt(message.channel, message.guild.id, hori)
                return

        # 討伐入力（hereonチャンネルのみ）
        if not self._is_allowed_channel(message.guild.id, message.channel.id):
            return

        parsed = self._parse_quick_input(content)
        if parsed:
            name, when_jst, respawn_min_override = parsed
            await self._handle_kill_input(message.guild.id, name, when_jst, respawn_min_override)
            try:
                await message.add_reaction("✅")
            except Exception:
                pass
            return

        await self.process_commands(message)

    # -------------- チャンネル固定 --------------
    def _is_allowed_channel(self, guild_id: int, channel_id: int) -> bool:
        g = self._g(guild_id)
        here = g.get("here")
        return here is None or here == channel_id

    async def _cmd_hereon(self, channel: discord.TextChannel):
        g = self._g(channel.guild.id)
        g["here"] = channel.id
        self._save()
        await channel.send("このチャンネルを通知・操作の対象にしました。")

    async def _cmd_hereoff(self, channel: discord.TextChannel):
        g = self._g(channel.guild.id)
        g["here"] = None
        self._save()
        await channel.send("チャンネル固定を解除しました。（どのチャンネルでも操作可能）")

    # -------------- 討伐入力 --------------
    async def _handle_kill_input(self, guild_id: int, name: str, when_jst: datetime, respawn_min_override: Optional[int]):
        g = self._g(guild_id)
        bosses = g["bosses"]

        st = BossState(name=name, respawn_min=60, rate=100)
        if name in self.presets:
            resp_min, rate, initial_delay_min = self.presets[name]
            st.respawn_min = resp_min
            st.rate = rate
        if name in bosses:
            st = BossState(**bosses[name])

        if respawn_min_override:
            st.respawn_min = respawn_min_override

        # 初回遅延の扱い
        add_delay = 0
        if name in self.presets:
            _, _, initial_delay_min = self.presets[name]
            if initial_delay_min and initial_delay_min > 0:
                add_delay = initial_delay_min

        center_utc = when_jst.astimezone(timezone.utc) + timedelta(minutes=st.respawn_min + add_delay)
        st.next_spawn_utc = int(center_utc.timestamp())
        st.skip = 0

        bosses[name] = asdict(st)
        self._save()

    # -------------- 一覧出力 --------------
    async def _send_bt(self, channel: discord.TextChannel, guild_id: int, horizon_h: Optional[int]):
        g = self._g(guild_id)
        arr = [BossState(**d) for d in g["bosses"].values() if d.get("next_spawn_utc")]
        now = now_utc()
        items = []
        for st in arr:
            t = datetime.fromtimestamp(st.next_spawn_utc, tz=timezone.utc)
            if horizon_h is None or (t - now).total_seconds() <= horizon_h * 3600:
                items.append((t, st))
        items.sort(key=lambda x: x[0])

        if not items:
            await channel.send("予定はありません。")
            return

        lines = []
        current_hour = None
        for t, st in items:
            j = t.astimezone(JST)
            if current_hour is None:
                current_hour = j.hour
            if j.hour != current_hour:
                lines.append("")  # 改行1つ
                current_hour = j.hour
            lines.append(f"{j.strftime('%H:%M:%S')} : {st.name} {st.label_flags()}".rstrip())
        await channel.send("\n".join(lines))

    async def _send_help(self, channel: discord.TextChannel):
        txt = (
            "使い方（JST固定）\n"
            "• 討伐入力: `ボス名 HHMM [8h]` 例) `メデューサ 2208` / `ティミニエル 1121 8h`\n"
            "  時刻省略で入力時刻。未来HHMMは前日扱い。\n"
            "• 一覧: `bt` / `bt3` / `bt6` / `bt12` / `bt24`（!なしでOK）\n"
            "• チャンネル固定: `hereon` / `hereoff`\n"
            "• 出現率100%は「※確定」。出現未入力は自動次周（skip加算）。\n"
            "• エイリアス: ひらがな/カタカナ/一部一致/QA など対応。\n"
        )
        await channel.send(txt)

    # -------------- コマンド --------------
    @commands.command(name="help")
    async def cmd_help(self, ctx: commands.Context):
        await self._send_help(ctx.channel)

    @commands.command(name="hereon")
    async def hereon(self, ctx: commands.Context):
        await self._cmd_hereon(ctx.channel)

    @commands.command(name="hereoff")
    async def hereoff(self, ctx: commands.Context):
        await self._cmd_hereoff(ctx.channel)

    @commands.command(name="bt")
    async def bt(self, ctx: commands.Context):
        if not self._is_allowed_channel(ctx.guild.id, ctx.channel.id):
            return
        await self._send_bt(ctx.channel, ctx.guild.id, None)

    @commands.command(name="bt3")
    async def bt3(self, ctx: commands.Context):
        if not self._is_allowed_channel(ctx.guild.id, ctx.channel.id):
            return
        await self._send_bt(ctx.channel, ctx.guild.id, 3)

    @commands.command(name="bt6")
    async def bt6(self, ctx: commands.Context):
        if not self._is_allowed_channel(ctx.guild.id, ctx.channel.id):
            return
        await self._send_bt(ctx.channel, ctx.guild.id, 6)

    @commands.command(name="bt12")
    async def bt12(self, ctx: commands.Context):
        if not self._is_allowed_channel(ctx.guild.id, ctx.channel.id):
            return
        await self._send_bt(ctx.channel, ctx.guild.id, 12)

    @commands.command(name="bt24")
    async def bt24(self, ctx: commands.Context):
        if not self._is_allowed_channel(ctx.guild.id, ctx.channel.id):
            return
        await self._send_bt(ctx.channel, ctx.guild.id, 24)

    # -------------- 通知ループ --------------
    @tasks.loop(seconds=CHECK_SEC)
    async def tick(self):
        await self.wait_until_ready()
        now = now_utc()

        for gid, gdata in list(self.db.get("guilds", {}).items()):
            guild = self.get_guild(int(gid))
            if not guild:
                continue

            here_ch_id = gdata.get("here")
            if not here_ch_id:
                continue  # 通知は hereon 設定時のみ

            try:
                ch = guild.get_channel(here_ch_id) or await guild.fetch_channel(here_ch_id)
            except Exception:
                continue

            pre_items = []  # 1分前
            now_items = []  # 出現

            for d in list(gdata.get("bosses", {}).values()):
                st = BossState(**d)
                if not st.next_spawn_utc:
                    continue
                center = datetime.fromtimestamp(st.next_spawn_utc, tz=timezone.utc)

                # 1分前
                if abs((now - (center - timedelta(minutes=1))).total_seconds()) <= MERGE_WINDOW_SEC:
                    pre_items.append(f"{center.astimezone(JST).strftime('%H:%M:%S')} : {st.name} {st.label_flags()}".strip())

                # 出現
                if abs((now - center).total_seconds()) <= MERGE_WINDOW_SEC:
                    now_items.append(f"{st.name} 出現！ [{center.astimezone(JST).strftime('%H:%M:%S')}] (skip:{st.skip}) {st.label_flags()}".strip())

                # 自動スキップ（1分超過）
                if (now - center).total_seconds() >= 60:
                    st.next_spawn_utc += st.respawn_min * 60
                    st.skip += 1
                    gdata["bosses"][st.name] = asdict(st)

            if pre_items:
                try:
                    await ch.send("⏰ 1分前\n" + "\n".join(sorted(pre_items)))
                except Exception:
                    pass
            if now_items:
                try:
                    await ch.send("🔥\n" + "\n".join(sorted(now_items)))
                except Exception:
                    pass

        self._save()

    @tick.before_loop
    async def before_tick(self):
        await self.wait_until_ready()

# ----------- FastAPI（/health） -----------
from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from uvicorn import Config, Server

app = FastAPI()

@app.get("/health")
async def health_get(silent: Optional[int] = None):
    return {"ok": True, "ts": int(time.time())}

# ★ HEAD でも 200/204 を返す（監視系がHEADで叩いても405にしない）
@app.head("/health")
async def health_head():
    return Response(status_code=204)

# ----------- 起動（429レート制限に耐えるリトライ） -----------
async def main_async():
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN not set")

    bot = BossBot()
    api = Server(Config(app=app, host="0.0.0.0", port=int(os.environ.get("PORT", "10000")), loop="asyncio", lifespan="on"))

    async def run_bot_with_retry():
        while True:
            try:
                log.info("discord: starting")
                await bot.start(token)
            except discord.errors.HTTPException as e:
                status = getattr(e, "status", None)
                if status == 429:
                    backoff = BACKOFF_429_MIN * 60 + random.randint(0, BACKOFF_JITTER_SEC)
                    log.warning(f"[BOT] 429/RateLimited を検出。{backoff}s 待機して再試行します。")
                    await asyncio.sleep(backoff)
                    continue
                raise
            except Exception as e:
                log.exception(f"discord fatal: {e}")
                raise

    await asyncio.gather(
        api.serve(),
        run_bot_with_retry(),
    )

def run():
    asyncio.run(main_async())

if __name__ == "__main__":
    run()
