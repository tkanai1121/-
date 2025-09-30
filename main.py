# -*- coding: utf-8 -*-
import os
import json
import asyncio
import unicodedata
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import discord
from discord.ext import tasks
from fastapi import FastAPI
from uvicorn import Config, Server

# ====== JST（固定） ======
JST = timezone(timedelta(hours=9))

# ====== パス ======
DATA_DIR = "data"
STORE_FILE = os.path.join(DATA_DIR, "store.json")
PRESET_FILE = "bosses_preset.json"

# ====== 通知の集約ウィンドウ ======
CHECK_SEC = 10
MERGE_WINDOW_SEC = 60  # ±60秒で1分前／出現を集約

# ---------------- Data Models ---------------- #
@dataclass
class BossState:
    name: str
    respawn_min: int               # 周期（分）
    rate: int = 100                # 出現率（%）
    next_spawn_utc: Optional[int] = None
    channel_id: Optional[int] = None
    skip: int = 0
    excluded_reset: bool = False
    initial_delay_min: int = 0
    last_pre_minute_utc: Optional[int] = None   # 最後に1分前通知した「分」のUTC epoch（重複防止）
    last_spawn_minute_utc: Optional[int] = None # 最後に出現通知した「分」のUTC epoch（重複防止）

    def label_flags(self) -> str:
        parts = []
        if self.rate == 100:
            parts.append("※確定")
        if self.skip > 0:
            parts.append(f"{self.skip}周")
        return "[" + "] [".join(parts) + "]" if parts else ""


# ---------------- Storage ---------------- #
class Store:
    """
    保存形式:
    {
      "<guild_id>": {
        "meta": {"announce_channel_id": 1234567890},
        "bosses": {
          "メデューサ": {...BossState...},
          ...
        }
      }
    }
    """
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        if not os.path.exists(self.path):
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({}, f, ensure_ascii=False, indent=2)

    def load(self) -> Dict[str, dict]:
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)

    def save(self, data: Dict[str, dict]):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ---------------- Utility: 文字正規化 / ボス名解決 ---------------- #
def kata_to_hira(s: str) -> str:
    # カタカナ→ひらがな
    res = []
    for ch in s:
        code = ord(ch)
        if 0x30A1 <= code <= 0x30F6:
            res.append(chr(code - 0x60))
        else:
            res.append(ch)
    return "".join(res)

def normalize_key(s: str) -> str:
    # 全角→半角・小文字・空白除去・カタカナ→ひらがな
    s = unicodedata.normalize("NFKC", s).lower().strip()
    s = s.replace(" ", "").replace("　", "")
    s = kata_to_hira(s)
    return s

def unique_startswith(target: str, candidates: List[str]) -> Optional[str]:
    # 正規化で startswith 一意なら返す
    key = normalize_key(target)
    hits = [c for c in candidates if normalize_key(c).startswith(key)]
    if len(hits) == 1:
        return hits[0]
    return None

# ひらがな/カタカナ/一部一致/アルファベット(例: QA) のゆるエイリアス
def build_alias_map(officials: List[str]) -> Dict[str, str]:
    m = {}
    # 例: クイーンアント → qa
    m["qa"] = "クイーンアント"
    m["queenant"] = "クイーンアント"
    m["orfen"] = "オルフェン"
    m["timini"] = "ティミニエル"
    m["timiniel"] = "ティミニエル"
    m["medusa"] = "メデューサ"
    m["gareth"] = "ガレス"
    m["behemoth"] = "ベヒモス"
    m["panarlord"] = "パンナロード"
    m["coreceptor"] = "コアサセプタ"
    m["koreceptor"] = "コアサセプタ"

    # 同音のかな名も直参照できるように
    for off in officials:
        m[normalize_key(off)] = off
    return m

def resolve_boss_name(name_in: str, alias_map: Dict[str, str], officials: List[str]) -> Optional[str]:
    if not name_in:
        return None
    k = normalize_key(name_in)
    # 直接 alias
    if k in alias_map:
        return alias_map[k]
    # 一意の前方一致
    hit = unique_startswith(name_in, officials)
    if hit:
        return hit
    # 公式名そのもの（空白/全半角の違いなど）を吸収
    for off in officials:
        if normalize_key(off) == k:
            return off
    return None


# ---------------- Bot ---------------- #
class BossBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # メッセージ内容の取得をON
        super().__init__(intents=intents)
        self.store = Store(STORE_FILE)
        self.data: Dict[str, dict] = self.store.load()  # guild -> {"meta":{}, "bosses":{}}
        self.presets: Dict[str, Tuple[int, int, int]] = {}  # name -> (respawn_min, rate, initial_delay_min)
        self.officials: List[str] = []
        self.alias_map: Dict[str, str] = {}
        self._load_presets()
        self.tick.start()

    # ---------- storage helpers ---------- #
    def _gkey(self, guild_id: int) -> str:
        return str(guild_id)

    def _ensure_guild(self, guild_id: int):
        gk = self._gkey(guild_id)
        if gk not in self.data:
            self.data[gk] = {"meta": {}, "bosses": {}}

    def _get(self, guild_id: int, name: str) -> Optional[BossState]:
        self._ensure_guild(guild_id)
        d = self.data[self._gkey(guild_id)]["bosses"].get(name)
        return BossState(**d) if d else None

    def _set(self, guild_id: int, st: BossState):
        self._ensure_guild(guild_id)
        self.data[self._gkey(guild_id)]["bosses"][st.name] = asdict(st)
        self.store.save(self.data)

    def _all(self, guild_id: int) -> List[BossState]:
        self._ensure_guild(guild_id)
        return [BossState(**d) for d in self.data[self._gkey(guild_id)]["bosses"].values()]

    def _get_announce_channel(self, guild_id: int) -> Optional[int]:
        self._ensure_guild(guild_id)
        return self.data[self._gkey(guild_id)]["meta"].get("announce_channel_id")

    def _set_announce_channel(self, guild_id: int, channel_id: Optional[int]):
        self._ensure_guild(guild_id)
        if channel_id is None:
            self.data[self._gkey(guild_id)]["meta"].pop("announce_channel_id", None)
        else:
            self.data[self._gkey(guild_id)]["meta"]["announce_channel_id"] = channel_id
        self.store.save(self.data)

    # ---------- presets ---------- #
    def _load_presets(self):
        try:
            with open(PRESET_FILE, "r", encoding="utf-8") as f:
                arr = json.load(f)
            self.presets = {}
            self.officials = []
            for x in arr:
                name = x["name"]
                rate = int(x.get("rate", x.get("出現率", 100)))
                # respawn_h は小数対応
                rh = x.get("respawn_h")
                if rh is None:
                    # 誤キーにもある程度耐性
                    rh = x.get("間隔") or x.get("respawn") or x.get("interval_h")
                respawn_min = int(round(float(rh) * 60))
                delay_h = x.get("initial_delay_h", x.get("初回出現遅延", 0))
                # H:MM で渡ってくるケースも想定してパース
                if isinstance(delay_h, str) and ":" in delay_h:
                    h, m = delay_h.split(":")
                    delay_min = int(h) * 60 + int(m)
                else:
                    delay_min = int(round(float(delay_h) * 60))
                self.presets[name] = (respawn_min, rate, delay_min)
                self.officials.append(name)
            self.alias_map = build_alias_map(self.officials)
            print(f"presets loaded: {len(self.presets)}")
        except Exception as e:
            print("preset load error:", e)
            self.presets = {}
            self.officials = []
            self.alias_map = {}

    # ---------- 入力パース（ボス名 HHMM [8h] / ボス名のみ） ---------- #
    def parse_quick_input(self, content: str) -> Optional[Tuple[str, datetime, Optional[int]]]:
        parts = content.strip().split()
        if len(parts) == 0:
            return None
        name = parts[0]
        jst_now = datetime.now(JST)
        base = jst_now
        respawn_min = None

        # HHMM
        if len(parts) >= 2 and parts[1].isdigit() and 3 <= len(parts[1]) <= 4:
            p = parts[1].zfill(4)
            try:
                h, m = int(p[:2]), int(p[2:])
                base = jst_now.replace(hour=h, minute=m, second=0, microsecond=0)
                # 未来は前日扱い
                if base > jst_now:
                    base -= timedelta(days=1)
            except ValueError:
                base = jst_now

        # 8h のような周期上書き
        if len(parts) >= 3 and parts[2].lower().endswith("h"):
            try:
                respawn_min = int(round(float(parts[2][:-1]) * 60))
            except ValueError:
                respawn_min = None

        return name, base, respawn_min

    # ---------- 表示（bt系） ---------- #
    async def _send_bt(self, channel: discord.TextChannel, guild_id: int, horizon_h: Optional[int]):
        arr = self._all(guild_id)
        now = now_utc()
        items = []
        for st in arr:
            if not st.next_spawn_utc:
                continue
            t = datetime.fromtimestamp(st.next_spawn_utc, tz=timezone.utc)
            if horizon_h is not None and (t - now).total_seconds() > horizon_h * 3600:
                continue
            items.append((t, st))
        items.sort(key=lambda x: x[0])

        lines = []
        current_hour = None
        for t, st in items:
            j = t.astimezone(JST)
            if current_hour is None:
                current_hour = j.hour
            if j.hour != current_hour:
                lines.append("")  # 改行1つ（指定どおり）
                current_hour = j.hour
            lines.append(f"{j.strftime('%H:%M:%S')} : {st.name} {st.label_flags()}")

        if not lines:
            await channel.send("予定はありません。")
        else:
            await channel.send("\n".join(lines))

    async def _notify_grouped(self, guild: discord.Guild, items_by_cid: Dict[int, List[str]], title_emoji: str):
        for cid, arr in items_by_cid.items():
            ch = guild.get_channel(cid) or await guild.fetch_channel(cid)
            if not ch:
                continue
            await ch.send(f"{title_emoji} " + "\n".join(sorted(arr)))

    # ---------- 周期チェック ---------- #
    @tasks.loop(seconds=CHECK_SEC)
    async def tick(self):
        await self.wait_until_ready()
        now = now_utc()
        for gkey, gdict in list(self.data.items()):
            guild = self.get_guild(int(gkey))
            if not guild:
                continue

            pre_items: Dict[int, List[str]] = {}
            now_items: Dict[int, List[str]] = {}

            bosses = [BossState(**d) for d in gdict.get("bosses", {}).values()]
            fixed_cid = gdict.get("meta", {}).get("announce_channel_id")

            for st in bosses:
                if not st.next_spawn_utc:
                    continue

                center = datetime.fromtimestamp(st.next_spawn_utc, tz=timezone.utc)
                # 通知先は固定チャンネルがあればそちら
                target_cid = fixed_cid or st.channel_id
                if not target_cid:
                    continue

                # 1分前（重複防止: 分で同一ならスキップ）
                pre_center = center - timedelta(minutes=1)
                if abs((now - pre_center).total_seconds()) <= MERGE_WINDOW_SEC:
                    minute_key = int(pre_center.replace(second=0, microsecond=0).timestamp())
                    if st.last_pre_minute_utc != minute_key:
                        label = f"{center.astimezone(JST).strftime('%H:%M:%S')} : {st.name} {st.label_flags()}".strip()
                        pre_items.setdefault(target_cid, []).append(label)
                        st.last_pre_minute_utc = minute_key
                        self._set(int(gkey), st)

                # 出現（重複防止）
                if abs((now - center).total_seconds()) <= MERGE_WINDOW_SEC:
                    minute_key = int(center.replace(second=0, microsecond=0).timestamp())
                    if st.last_spawn_minute_utc != minute_key:
                        label = f"{st.name} 出現！ [{center.astimezone(JST).strftime('%H:%M:%S')}] (skip:{st.skip}) {st.label_flags()}".strip()
                        now_items.setdefault(target_cid, []).append(label)
                        st.last_spawn_minute_utc = minute_key
                        self._set(int(gkey), st)

                # 自動スライド（出現から60秒過ぎたら次周へ）
                if (now - center).total_seconds() >= 60:
                    st.next_spawn_utc += st.respawn_min * 60
                    st.skip += 1
                    self._set(int(gkey), st)

            # まとめて送信
            await self._notify_grouped(guild, pre_items, "⏰ 1分前")
            await self._notify_grouped(guild, now_items, "🔥")

    @tick.before_loop
    async def before_tick(self):
        await self.wait_until_ready()

    # ---------- メッセージ処理（!無し/!付き両対応 & hereon/hereoff でチャンネル固定） ---------- #
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        text = message.content.strip()
        gid = message.guild.id
        fixed_cid = self._get_announce_channel(gid)

        # --- hereon / hereoff はどこでも受け付け ---
        if text.lower() in ("hereon", "!hereon"):
            self._set_announce_channel(gid, message.channel.id)
            await message.channel.send("📌 以後の通知はこのチャンネルに固定します。")
            return

        if text.lower() in ("hereoff", "!hereoff"):
            self._set_announce_channel(gid, None)
            await message.channel.send("📌 通知チャンネルの固定を解除しました。")
            return

        # --- 固定中は、固定チャンネル以外の入力を**完全無視** ---
        if fixed_cid and message.channel.id != fixed_cid:
            return

        # --- bt系 ---
        low = text.lower()
        if low in ("bt", "!bt"):
            await self._send_bt(message.channel, gid, None)
            return
        if low in ("bt3", "!bt3"):
            await self._send_bt(message.channel, gid, 3)
            return
        if low in ("bt6", "!bt6"):
            await self._send_bt(message.channel, gid, 6)
            return
        if low in ("bt12", "!bt12"):
            await self._send_bt(message.channel, gid, 12)
            return
        if low in ("bt24", "!bt24"):
            await self._send_bt(message.channel, gid, 24)
            return

        # --- rh / preset / help ---
        if low.startswith(("rh ", "!rh ")):
            try:
                _, name, hours = low.replace("!rh", "rh", 1).split(maxsplit=2)
            except ValueError:
                return
            off = resolve_boss_name(name, self.alias_map, self.officials)
            if not off or off not in self.presets:
                return
            st = self._get(gid, off) or BossState(name=off, respawn_min=60)
            h = float(hours.rstrip("h"))
            st.respawn_min = int(round(h * 60))
            self._set(gid, st)
            await message.channel.send(f"{off} の周期を {st.respawn_min/60:.2f}h に設定しました。")
            return

        if low in ("preset", "!preset"):
            self._load_presets()
            for st in self._all(gid):
                if st.name in self.presets:
                    rmin, rate, delay = self.presets[st.name]
                    st.respawn_min, st.rate, st.initial_delay_min = rmin, rate, delay
                    self._set(gid, st)
            await message.channel.send("プリセットを再読込しました。")
            return

        if low in ("help", "!help"):
            await message.channel.send(
                "使い方：\n"
                "・`ボス名 HHMM [周期h]` 例: `メデューサ 2208` / `ティミニエル 1121 8h`\n"
                "・`bt / bt3 / bt6 / bt12 / bt24` … 直近一覧（!無しでOK）\n"
                "・`hereon` / `hereoff` … 通知チャンネルの固定/解除\n"
                "・`rh ボス名 8h` … 既定周期変更\n"
                "・`preset` … プリセット再読込\n"
            )
            return

        # --- 討伐入力（ボス名 …） ---
        parsed = self.parse_quick_input(text)
        if not parsed:
            return
        name_in, when_jst, respawn_override = parsed

        # 正式名へ解決。プリセットにない名称は**完全無視**
        off = resolve_boss_name(name_in, self.alias_map, self.officials)
        if not off or off not in self.presets:
            return

        st = self._get(gid, off) or BossState(name=off, respawn_min=60)
        if off in self.presets:
            rmin, rate, delay = self.presets[off]
            if st.respawn_min == 60 and respawn_override is None:
                st.respawn_min = rmin
            st.rate = rate
            st.initial_delay_min = delay

        if respawn_override is not None:
            st.respawn_min = respawn_override

        # 通知先は固定があれば固定チャンネル
        st.channel_id = (self._get_announce_channel(gid) or message.channel.id)

        center = when_jst.astimezone(timezone.utc) + timedelta(
            minutes=st.respawn_min + st.initial_delay_min
        )
        st.next_spawn_utc = int(center.timestamp())
        st.skip = 0
        st.last_pre_minute_utc = None
        st.last_spawn_minute_utc = None

        self._set(gid, st)
        try:
            await message.add_reaction("✅")
        except Exception:
            pass


# ---------------- keepalive (FastAPI) ---------------- #
app = FastAPI()

@app.get("/health")
async def health():
    return {"ok": True}


def run():
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN not set")

    bot = BossBot()

    async def main_async():
        # Discord & FastAPI を同一ループ内で同時起動
        config = Config(app=app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), loop="asyncio")
        server = Server(config)
        bot_task = asyncio.create_task(bot.start(token))
        api_task = asyncio.create_task(server.serve())
        await asyncio.wait([bot_task, api_task], return_when=asyncio.FIRST_COMPLETED)

    asyncio.run(main_async())


if __name__ == "__main__":
    run()

