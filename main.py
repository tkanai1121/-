import os
import gc
import json
import asyncio
import unicodedata
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import discord
from discord.ext import commands, tasks

# ---------- keepalive API (FastAPI) ----------
try:
    from fastapi import FastAPI
    from uvicorn import Config, Server
    HAVE_API = True
except Exception:
    HAVE_API = False

# ---------- JST ----------
JST = timezone(timedelta(hours=9))

# ---------- paths ----------
DATA_DIR = "data"
STORE_FILE = os.path.join(DATA_DIR, "store.json")
PRESET_FILE = "bosses_preset.json"

# ---------- tunables ----------
CHECK_SEC = 10                      # 予約確認のポーリング間隔
MERGE_WINDOW_SEC = 60               # ±この秒数内をまとめて1メッセージ
BACKOFF_429_MIN = int(os.getenv("BACKOFF_429_MIN", "900"))   # 429/1015 時の待機分
BACKOFF_JITTER_SEC = int(os.getenv("BACKOFF_JITTER_SEC", "30"))

# ============================================================
#                       Data Models
# ============================================================
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
    last_pre_minute_utc: Optional[int] = None
    last_spawn_minute_utc: Optional[int] = None

    def label_flags(self) -> str:
        parts = []
        if self.rate == 100:
            parts.append("※確定")
        if self.skip > 0:
            parts.append(f"{self.skip}周")
        return "[" + "] [".join(parts) + "]" if parts else ""


# ============================================================
#                       Storage
# ============================================================
class Store:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        if not os.path.exists(self.path):
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({}, f, ensure_ascii=False)

    def load(self) -> Dict[str, Dict[str, dict]]:
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)

    def save(self, data: Dict[str, Dict[str, dict]]):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ============================================================
#                       Alias / Name normalize
# ============================================================
def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).strip()
    s = s.replace(" ", "").replace("　", "")
    return s.lower()

# 手書きエイリアス（足りなければ追加してOK）
MANUAL_ALIASES = {
    "qa": "クイーンアント", "queenant": "クイーンアント",
    "garesu": "ガレス", "gareth": "ガレス",
    "behemoth": "ベヒモス",
    "timi": "ティミニエル", "timiniel": "ティミニエル",
    "orfen": "オルフェン",
    "cabrio": "カブリオ",
    "coreceptor": "コアサセプタ", "coaseceptor": "コアサセプタ",
}

def build_name_index(presets: Dict[str, Tuple[int, int, int]]) -> Tuple[Dict[str, str], List[str]]:
    """
    returns: (alias_map, official_names)
      alias_map: normalized -> official
      official_names: list of official
    """
    alias = {}
    official = list(presets.keys())
    for name in official:
        alias[_norm(name)] = name
        # ひらがな⇔カタカナ簡易：カタカナをひらがなへ
        hira = "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ン" else c for c in name)
        alias[_norm(hira)] = name
    for k, v in MANUAL_ALIASES.items():
        alias[_norm(k)] = v
    return alias, official


def resolve_boss_name(raw: str, alias_map: Dict[str, str], officials: List[str]) -> Optional[str]:
    key = _norm(raw)
    if key in alias_map:
        return alias_map[key]
    # official へ startswith で一意に絞れたらOK
    cand = [n for n in officials if _norm(n).startswith(key)]
    if len(cand) == 1:
        return cand[0]
    return None


# ============================================================
#                         Bot
# ============================================================
class BossBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)

        self.store = Store(STORE_FILE)
        self.data: Dict[str, Dict[str, dict]] = self.store.load()

        # name -> (respawn_min, rate, initial_delay_min)
        self.presets: Dict[str, Tuple[int, int, int]] = {}
        self.alias_map: Dict[str, str] = {}
        self.officials: List[str] = []
        self._load_presets()

        self.tick.start()

    # -------------------- meta （通知チャンネル固定） --------------------
    def _gkey(self, gid: int) -> str:
        return str(gid)

    def _get_announce_channel(self, guild_id: int) -> Optional[int]:
        g = self.data.get(self._gkey(guild_id), {})
        meta = g.get("__meta") or {}
        cid = meta.get("announce_channel_id")
        return int(cid) if cid else None

    def _set_announce_channel(self, guild_id: int, cid: Optional[int]):
        gkey = self._gkey(guild_id)
        if gkey not in self.data:
            self.data[gkey] = {}
        meta = self.data[gkey].get("__meta", {})
        meta["announce_channel_id"] = int(cid) if cid else None
        self.data[gkey]["__meta"] = meta
        self.store.save(self.data)

    # -------------------- storage helpers --------------------
    def _get(self, guild_id: int, name: str) -> Optional[BossState]:
        g = self.data.get(self._gkey(guild_id), {})
        d = g.get(name)
        return BossState(**d) if isinstance(d, dict) and d.get("name") else None

    def _set(self, guild_id: int, st: BossState):
        gkey = self._gkey(guild_id)
        if gkey not in self.data:
            self.data[gkey] = {}
        self.data[gkey][st.name] = asdict(st)
        self.store.save(self.data)

    def _all(self, guild_id: int) -> List[BossState]:
        g = self.data.get(self._gkey(guild_id), {})
        out: List[BossState] = []
        for d in g.values():
            if isinstance(d, dict) and "name" in d and "respawn_min" in d:
                out.append(BossState(**d))
        return out

    # -------------------- preset --------------------
    def _load_presets(self):
        self.presets = {}
        try:
            with open(PRESET_FILE, "r", encoding="utf-8") as f:
                arr = json.load(f)
            for x in arr:
                name = x["name"]
                respawn_min = int(round(float(x["respawn_h"]) * 60))
                rate = int(x["rate"])
                initial_delay_min = int(round(float(x.get("initial_delay_h", 0)) * 60))
                self.presets[name] = (respawn_min, rate, initial_delay_min)
        except Exception as e:
            print("preset load error", e)
        self.alias_map, self.officials = build_name_index(self.presets)

    # -------------------- parse input --------------------
    def parse_quick_input(self, content: str) -> Optional[Tuple[str, datetime, Optional[int]]]:
        # "ボス名 1120 [8h]" / "ボス名"
        parts = content.strip().split()
        if not parts:
            return None
        name_part = parts[0]
        name = resolve_boss_name(name_part, self.alias_map, self.officials) or name_part

        jnow = datetime.now(JST)
        when = jnow
        if len(parts) >= 2 and parts[1].isdigit() and 3 <= len(parts[1]) <= 4:
            p = parts[1].zfill(4)
            try:
                h, m = int(p[:2]), int(p[2:])
                base = jnow.replace(hour=h, minute=m, second=0, microsecond=0)
                if base > jnow:
                    base -= timedelta(days=1)  # 未来は前日扱い
                when = base
            except ValueError:
                pass

        respawn_override = None
        if len(parts) >= 3 and parts[2].lower().endswith("h"):
            try:
                respawn_override = int(round(float(parts[2][:-1]) * 60))
            except ValueError:
                pass

        return name, when, respawn_override

    # -------------------- notify --------------------
    async def _notify_grouped(self, guild: discord.Guild, cid: int, title_emoji: str, items: List[str]):
        if not items:
            return
        ch = guild.get_channel(cid) or await guild.fetch_channel(cid)
        await ch.send(f"{title_emoji} " + "\n".join(items))

    # -------------------- ticker --------------------
    @tasks.loop(seconds=CHECK_SEC)
    async def tick(self):
        await self.wait_until_ready()
        now = now_utc()

        for gkey, bosses in list(self.data.items()):
            try:
                guild = self.get_guild(int(gkey))
                if not guild:
                    continue

                pre_group: Dict[int, List[str]] = {}
                now_group: Dict[int, List[str]] = {}

                for d in list(bosses.values()):
                    if not (isinstance(d, dict) and "name" in d and "respawn_min" in d):
                        continue
                    st = BossState(**d)
                    if not st.next_spawn_utc:
                        continue

                    # 送信先チャンネル（固定があれば固定優先）
                    target_cid = self._get_announce_channel(int(gkey)) or st.channel_id
                    if not target_cid:
                        continue

                    center = datetime.fromtimestamp(st.next_spawn_utc, tz=timezone.utc)
                    pre_m = int((center - timedelta(minutes=1)).timestamp()) // 60
                    spawn_m = int(center.timestamp()) // 60

                    # 1分前（重複抑止）
                    if abs((now - (center - timedelta(minutes=1))).total_seconds()) <= MERGE_WINDOW_SEC:
                        if st.last_pre_minute_utc != pre_m:
                            label = f"{center.astimezone(JST).strftime('%H:%M:%S')} : {st.name} {st.label_flags()}".strip()
                            pre_group.setdefault(target_cid, []).append(label)
                            st.last_pre_minute_utc = pre_m
                            self._set(int(gkey), st)

                    # 出現（重複抑止）
                    if abs((now - center).total_seconds()) <= MERGE_WINDOW_SEC:
                        if st.last_spawn_minute_utc != spawn_m:
                            label = f"{st.name} 出現！ [{center.astimezone(JST).strftime('%H:%M:%S')}] (skip:{st.skip}) {st.label_flags()}".strip()
                            now_group.setdefault(target_cid, []).append(label)
                            st.last_spawn_minute_utc = spawn_m
                            self._set(int(gkey), st)

                    # 出現から60秒経過で自動スライド
                    if (now - center).total_seconds() >= 60:
                        st.next_spawn_utc += st.respawn_min * 60
                        st.skip += 1
                        self._set(int(gkey), st)

                # 送信
                for cid, items in pre_group.items():
                    await self._notify_grouped(guild, cid, "⏰ 1分前", sorted(items))
                for cid, items in now_group.items():
                    await self._notify_grouped(guild, cid, "🔥", sorted(items))

            except Exception as e:
                print("tick error", e)

        # GC + ちょい軽量化
        if gc.isenabled():
            gc.collect()

    @tick.before_loop
    async def before_tick(self):
        await self.wait_until_ready()

    # -------------------- commands / message handler --------------------
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        text = message.content.strip()

        # ====== hereon / hereoff （!無し/!付き両対応）======
        if text.lower() in ("hereon", "!hereon"):
            self._set_announce_channel(message.guild.id, message.channel.id)
            await message.channel.send("📌 以後の通知はこのチャンネルに固定します。")
            return
        if text.lower() in ("hereoff", "!hereoff"):
            self._set_announce_channel(message.guild.id, None)
            await message.channel.send("📌 通知チャンネルの固定を解除しました。")
            return

        # ====== bt系（!無し/!付き両対応）======
        if text.lower() in ("bt", "!bt"):
            await self._send_bt(message.channel, message.guild.id, None)
            return
        if text.lower() in ("bt3", "!bt3"):
            await self._send_bt(message.channel, message.guild.id, 3)
            return
        if text.lower() in ("bt6", "!bt6"):
            await self._send_bt(message.channel, message.guild.id, 6)
            return
        if text.lower() in ("bt12", "!bt12"):
            await self._send_bt(message.channel, message.guild.id, 12)
            return
        if text.lower() in ("bt24", "!bt24"):
            await self._send_bt(message.channel, message.guild.id, 24)
            return

        # ====== rh / preset / help（!無し/!付き両対応の簡易版）======
        if text.lower().startswith(("rh ", "!rh ")):
            _, name, hours = text.replace("!rh", "rh", 1).split(maxsplit=2)
            off = resolve_boss_name(name, self.alias_map, self.officials) or name
            st = self._get(message.guild.id, off) or BossState(name=off, respawn_min=60)
            st.respawn_min = int(round(float(hours.rstrip("hH")) * 60))
            self._set(message.guild.id, st)
            await message.channel.send(f"{off} の周期を {st.respawn_min/60:.2f}h に設定しました。")
            return

        if text.lower() in ("preset", "!preset"):
            self._load_presets()
            for st in self._all(message.guild.id):
                if st.name in self.presets:
                    rmin, rate, delay = self.presets[st.name]
                    st.respawn_min, st.rate, st.initial_delay_min = rmin, rate, delay
                    self._set(message.guild.id, st)
            await message.channel.send("プリセットを再読込しました。")
            return

        if text.lower() in ("help", "!help"):
            await message.channel.send(
                "使い方：\n"
                "・`ボス名 HHMM [周期h]` 例: `メデューサ 2208` / `ティミニエル 1121 8h`\n"
                "・`bt / bt3 / bt6 / bt12 / bt24` … 直近一覧（!無しでOK）\n"
                "・`hereon` / `hereoff` … 通知チャンネルの固定/解除\n"
                "・`rh ボス名 8h` … 既定周期の変更\n"
                "・`preset` … プリセット再読込\n"
            )
            return

        # ====== 討伐入力（ボス名 …）======
        parsed = self.parse_quick_input(text)
        if parsed:
            name_in, when_jst, respawn_override = parsed
            off = resolve_boss_name(name_in, self.alias_map, self.officials) or name_in

            st = self._get(message.guild.id, off) or BossState(name=off, respawn_min=60)
            if off in self.presets:
                rmin, rate, delay = self.presets[off]
                if st.respawn_min == 60 and respawn_override is None:
                    st.respawn_min = rmin
                st.rate = rate
                st.initial_delay_min = delay

            if respawn_override is not None:
                st.respawn_min = respawn_override

            st.channel_id = st.channel_id or message.channel.id

            center = when_jst.astimezone(timezone.utc) + timedelta(
                minutes=st.respawn_min + st.initial_delay_min
            )
            st.next_spawn_utc = int(center.timestamp())
            st.skip = 0
            st.last_pre_minute_utc = None
            st.last_spawn_minute_utc = None

            self._set(message.guild.id, st)
            try:
                await message.add_reaction("✅")
            except Exception:
                pass
            return

    # -------------------- list render --------------------
    async def _send_bt(self, channel: discord.abc.Messageable, gid: int, horizon_h: Optional[int]):
        arr = self._all(gid)
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
            await channel.send("予定はありません。")
            return

        # 時台が変わるたびに改行1つ（ユーザー要望）
        lines = []
        current_hour = None
        for t, st in items:
            j = t.astimezone(JST)
            if current_hour is None:
                current_hour = j.hour
            if j.hour != current_hour:
                lines.append("")  # 改行1つ
                current_hour = j.hour
            lines.append(f"{j.strftime('%H:%M:%S')} : {st.name} {st.label_flags()}")

        await channel.send("\n".join(lines))


# ============================================================
#                   keepalive (FastAPI)
# ============================================================
if HAVE_API:
    app = FastAPI()

    @app.get("/health")
    async def health(silent: Optional[int] = 0):
        # silent=1 のときは短文（UptimeRobot/BetterStack向け）
        return {"ok": True} if silent else {"ok": True, "service": "l2m-boss-bot"}

# ============================================================
#                          run
# ============================================================
async def run_async():
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN not set")

    bot = BossBot()

    if HAVE_API:
        config = Config(app=app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), loop="asyncio")
        server = Server(config)

    async def start_bot_with_backoff():
        while True:
            try:
                await bot.start(token)
            except discord.HTTPException as e:
                # 429 / Cloudflare 1015 相当
                msg = str(e)
                if "429" in msg or "Too Many Requests" in msg or "rate limited" in msg or "1015" in msg:
                    wait = BACKOFF_429_MIN * 60 + BACKOFF_JITTER_SEC
                    print(f"[BOT] 429/RateLimited を検出。{wait}s 待機して再試行します。")
                    await asyncio.sleep(wait)
                    continue
                raise
            except Exception as e:
                print("bot start error:", e)
                await asyncio.sleep(10)
                continue
            break

    if HAVE_API:
        await asyncio.gather(server.serve(), start_bot_with_backoff())
    else:
        await start_bot_with_backoff()


def run():
    asyncio.run(run_async())


if __name__ == "__main__":
    run()
