# main.py
import os
import json
import asyncio
import unicodedata
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import discord
from discord.ext import commands, tasks
from fastapi import FastAPI
from uvicorn import Config, Server

# -------------------- 基本定数 -------------------- #
JST = timezone(timedelta(hours=9))

DATA_DIR = "data"
STORE_FILE = os.path.join(DATA_DIR, "store.json")
PRESET_FILE = "bosses_preset.json"

# ポーリング周期 / 通知の集約窓 / 重複送信のTTL
CHECK_SEC = 10
MERGE_WINDOW_SEC = 10
NOTIFY_DEDUP_TTL_SEC = 120

# 429対策（Cloudflare/Discordレートリミット）
BACKOFF_429_MIN = int(os.environ.get("BACKOFF_429_MIN", "900"))
BACKOFF_JITTER_SEC = int(os.environ.get("BACKOFF_JITTER_SEC", "30"))

# -------------------- 便利関数 -------------------- #
def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def dt_to_ts(dt: datetime) -> int:
    return int(dt.timestamp())

def ts_to_jst_str(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(JST).strftime("%H:%M:%S")

def jst_now() -> datetime:
    return datetime.now(JST)

def zfill_hhmm(s: str) -> Tuple[int, int]:
    p = s.zfill(4)
    return int(p[:2]), int(p[2:])

def normalize_for_match(s: str) -> str:
    # 全角→半角、記号除去、大小無視
    s = unicodedata.normalize("NFKC", s)
    s = s.lower()
    return "".join(ch for ch in s if ch.isalnum())

# -------------------- ストレージ -------------------- #
class Store:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        if not os.path.exists(self.path):
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({}, f, ensure_ascii=False)

    def load(self) -> Dict[str, dict]:
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)

    def save(self, data: Dict[str, dict]):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

# -------------------- モデル -------------------- #
@dataclass
class BossState:
    name: str
    respawn_min: int
    rate: int = 100
    first_delay_min: int = 0    # 初回遅延
    next_spawn_utc: Optional[int] = None
    channel_id: Optional[int] = None
    skip: int = 0

    def flags(self) -> str:
        parts = []
        if self.rate == 100:
            parts.append("※確定")
        if self.skip > 0:
            parts.append(f"{self.skip}周")
        return "[" + "] [".join(parts) + "]" if parts else ""

# -------------------- 本体 -------------------- #
class BossBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

        self.store = Store(STORE_FILE)
        raw = self.store.load()
        self.data: Dict[str, dict] = raw  # {guild_id: {bosses:{name:BossState...}, channels:[ids]}}

        self.presets: Dict[str, Tuple[int, int, int]] = {}  # name -> (respawn_min, rate, first_delay_min)
        self.alias_map: Dict[str, str] = {}  # normalize(alias) -> official_name

        # 通知の重複抑止用（ギルドごとに送信済みキーと期限）
        self._sent_keys: Dict[str, Dict[str, int]] = {}

    # ---- discord.py v2 の正しい起動箇所 ---- #
    async def setup_hook(self):
        self._load_presets()
        # ここで tasks.loop を開始する（__init__ や on_ready ではなく）
        self.tick.start()

    # ----------------- プリセット/別名 ----------------- #
    def _load_presets(self):
        try:
            with open(PRESET_FILE, "r", encoding="utf-8") as f:
                arr = json.load(f)
            # 例: {"name":"スタン","rate":100,"respawn_h":4,"first_delay_h":"0:00"}
            m = {}
            alias = {}
            for row in arr:
                name = row["name"]
                rate = int(row.get("rate", 100))
                respawn_h = row.get("respawn_h", 0)
                respawn_min = int(round(float(respawn_h) * 60))
                first_delay_min = 0
                # "初回出現遅延" を "H:MM" / floatH のどちらでも受ける
                if "first_delay_h" in row:
                    fd = str(row["first_delay_h"])
                    if ":" in fd:
                        h, mm = fd.split(":")
                        first_delay_min = int(h) * 60 + int(mm)
                    else:
                        first_delay_min = int(round(float(fd) * 60))
                m[name] = (respawn_min, rate, first_delay_min)

                # シンプルな別名（ローマ字略など）: 必要ならここで増やす
                nkey = normalize_for_match(name)
                alias[nkey] = name
                if name == "クイーンアント":
                    alias[normalize_for_match("qa")] = name
                    alias[normalize_for_match("queenant")] = name

            self.presets = m
            self.alias_map = alias
        except Exception as e:
            print("preset load error:", e)
            self.presets = {}
            self.alias_map = {}

    # ----------------- ギルドデータ操作 ----------------- #
    def _gkey(self, guild_id: int) -> str:
        return str(guild_id)

    def _ensure_guild(self, guild_id: int):
        gkey = self._gkey(guild_id)
        if gkey not in self.data:
            self.data[gkey] = {"bosses": {}, "channels": []}
            self.store.save(self.data)

    def _get_boss(self, guild_id: int, name: str) -> Optional[BossState]:
        g = self.data.get(self._gkey(guild_id), {})
        b = g.get("bosses", {}).get(name)
        return BossState(**b) if b else None

    def _set_boss(self, guild_id: int, st: BossState):
        self._ensure_guild(guild_id)
        g = self.data[self._gkey(guild_id)]
        g["bosses"][st.name] = asdict(st)
        self.store.save(self.data)

    def _all_bosses(self, guild_id: int) -> List[BossState]:
        self._ensure_guild(guild_id)
        g = self.data[self._gkey(guild_id)]
        return [BossState(**d) for d in g.get("bosses", {}).values()]

    def _channels(self, guild_id: int) -> List[int]:
        self._ensure_guild(guild_id)
        return self.data[self._gkey(guild_id)].get("channels", [])

    def _set_channels(self, guild_id: int, ids: List[int]):
        self._ensure_guild(guild_id)
        self.data[self._gkey(guild_id)]["channels"] = ids
        self.store.save(self.data)

    # ----------------- 入力パース ----------------- #
    def _resolve_boss_name(self, user_text: str) -> Optional[str]:
        # 正式名一致
        if user_text in self.presets:
            return user_text
        # 別名 / 部分一致
        key = normalize_for_match(user_text)
        if key in self.alias_map:
            return self.alias_map[key]
        # 先頭一致・部分一致（normalize）
        for off in self.presets.keys():
            if normalize_for_match(off).startswith(key) or key in normalize_for_match(off):
                return off
        return None

    def _parse_kill_input(self, content: str) -> Optional[Tuple[str, datetime, Optional[int]]]:
        # 例: 「スタン 1120」 / 「スタン 1120 4h」 / 「フェリス」
        parts = content.strip().split()
        if len(parts) == 0:
            return None
        name_txt = parts[0]
        off_name = self._resolve_boss_name(name_txt)
        if not off_name:
            return None

        jnow = jst_now()
        kill_dt = jnow
        respawn_min = None

        if len(parts) >= 2 and parts[1].isdigit() and 3 <= len(parts[1]) <= 4:
            h, m = zfill_hhmm(parts[1])
            base = jnow.replace(hour=h, minute=m, second=0, microsecond=0)
            if base > jnow:
                base -= timedelta(days=1)
            kill_dt = base

        if len(parts) >= 3 and parts[2].lower().endswith("h"):
            try:
                respawn_min = int(round(float(parts[2][:-1]) * 60))
            except Exception:
                respawn_min = None

        return off_name, kill_dt, respawn_min

    # ----------------- 通知送信の重複抑止 ----------------- #
    def _sent_bucket(self, guild_id: int) -> Dict[str, int]:
        gkey = self._gkey(guild_id)
        if gkey not in self._sent_keys:
            self._sent_keys[gkey] = {}
        return self._sent_keys[gkey]

    def _mark_sent(self, guild_id: int, key: str):
        self._sent_bucket(guild_id)[key] = dt_to_ts(now_utc()) + NOTIFY_DEDUP_TTL_SEC

    def _already_sent(self, guild_id: int, key: str) -> bool:
        b = self._sent_bucket(guild_id)
        ts = b.get(key)
        return ts is not None and ts >= dt_to_ts(now_utc())

    def _cleanup_sent(self):
        nowts = dt_to_ts(now_utc())
        for gkey, b in list(self._sent_keys.items()):
            for k, ttl in list(b.items()):
                if ttl < nowts:
                    b.pop(k, None)

    # ----------------- ループ（通知） ----------------- #
    @tasks.loop(seconds=CHECK_SEC)
    async def tick(self):
        await self.wait_until_ready()
        self._cleanup_sent()
        n = now_utc()

        for g in list(self.data.keys()):
            guild = self.get_guild(int(g))
            if not guild:
                continue

            # チャンネルごとの集約
            pre_labels: Dict[int, List[str]] = {}
            now_labels: Dict[int, List[str]] = {}

            for st in self._all_bosses(guild.id):
                if not st.next_spawn_utc or not st.channel_id:
                    continue
                ch: discord.TextChannel = guild.get_channel(st.channel_id) or await guild.fetch_channel(st.channel_id)
                center = datetime.fromtimestamp(st.next_spawn_utc, tz=timezone.utc)

                # 1分前
                pre_key = f"pre|{st.channel_id}|{st.next_spawn_utc}|{st.name}"
                if abs((n - (center - timedelta(minutes=1))).total_seconds()) <= MERGE_WINDOW_SEC:
                    if not self._already_sent(guild.id, pre_key):
                        pre_labels.setdefault(st.channel_id, []).append(
                            f"{ts_to_jst_str(st.next_spawn_utc)} : {st.name} {st.flags()}".strip()
                        )
                        self._mark_sent(guild.id, pre_key)

                # 出現
                now_key = f"now|{st.channel_id}|{st.next_spawn_utc}|{st.name}"
                if abs((n - center).total_seconds()) <= MERGE_WINDOW_SEC:
                    if not self._already_sent(guild.id, now_key):
                        now_labels.setdefault(st.channel_id, []).append(
                            f"{st.name} 出現！ [{ts_to_jst_str(st.next_spawn_utc)}] (skip:{st.skip}) {st.flags()}".strip()
                        )
                        self._mark_sent(guild.id, now_key)

                # 自動スライド（出現＋60秒）
                if (n - center).total_seconds() >= 60:
                    st.next_spawn_utc += st.respawn_min * 60
                    st.skip += 1
                    self._set_boss(guild.id, st)

            # 集約送信
            for cid, arr in pre_labels.items():
                ch = guild.get_channel(cid) or await guild.fetch_channel(cid)
                if arr:
                    await ch.send("⏰ 1分前\n" + "\n".join(sorted(arr)))
            for cid, arr in now_labels.items():
                ch = guild.get_channel(cid) or await guild.fetch_channel(cid)
                if arr:
                    await ch.send("🔥\n" + "\n".join(sorted(arr)))

    # ----------------- メッセージ監視（!省略でもOK） ----------------- #
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        content = message.content.strip()
        # まず「管理系コマンド」（!省略OK）
        if await self._maybe_handle_text_command(message, content):
            return

        # 監視チャンネル以外では無視
        if message.channel.id not in self._channels(message.guild.id):
            return

        # 討伐入力（「ボス名 HHMM [x h]」形式）
        parsed = self._parse_kill_input(content)
        if parsed:
            name, when_jst, respawn_override = parsed
            st = self._get_boss(message.guild.id, name) or BossState(
                name=name, respawn_min=self.presets.get(name, (60, 100, 0))[0],
                rate=self.presets.get(name, (60, 100, 0))[1],
                first_delay_min=self.presets.get(name, (60, 100, 0))[2],
            )
            if respawn_override:
                st.respawn_min = respawn_override
            st.channel_id = st.channel_id or message.channel.id
            center = when_jst.astimezone(timezone.utc) + timedelta(minutes=st.respawn_min)
            st.next_spawn_utc = dt_to_ts(center)
            st.skip = 0
            self._set_boss(message.guild.id, st)
            await message.add_reaction("✅")
            return

        await self.process_commands(message)

    # ----------------- テキストコマンド群（!省略対応） ----------------- #
    async def _maybe_handle_text_command(self, message: discord.Message, content: str) -> bool:
        raw = content
        if raw.startswith("!"):
            raw = raw[1:].strip()

        low = raw.lower()

        # hereon/hereoff
        if low in ("hereon", "here on"):
            ids = self._channels(message.guild.id)
            if message.channel.id not in ids:
                ids.append(message.channel.id)
                self._set_channels(message.guild.id, ids)
            await message.channel.send("このチャンネルを**監視対象ON**にしました。")
            return True

        if low in ("hereoff", "here off"):
            ids = self._channels(message.guild.id)
            if message.channel.id in ids:
                ids.remove(message.channel.id)
                self._set_channels(message.guild.id, ids)
            await message.channel.send("このチャンネルを**監視対象OFF**にしました。")
            return True

        # bt / btx
        if low in ("bt", "bt3", "bt6", "bt12", "bt24"):
            horizon = None
            if low != "bt":
                horizon = int(low[2:])
            await self._send_bt(message.channel, message.guild.id, horizon)
            return True

        if low in ("bosses", "list", "bname", "bnames"):
            lines = []
            for name, (rm, rate, fd) in sorted(self.presets.items(), key=lambda x: x[0]):
                lines.append(f"• {name} : {rm/60:.2f}h / rate {rate}% / 初回遅延 {fd}分")
            await message.channel.send("\n".join(lines) or "プリセット無し")
            return True

        if low.startswith("rh "):  # rh ボス名 8h
            parts = raw.split()
            if len(parts) >= 3:
                name = self._resolve_boss_name(parts[1]) or parts[1]
                try:
                    h = float(parts[2].rstrip("hH"))
                    st = self._get_boss(message.guild.id, name) or BossState(
                        name=name,
                        respawn_min=self.presets.get(name, (60, 100, 0))[0],
                        rate=self.presets.get(name, (60, 100, 0))[1],
                        first_delay_min=self.presets.get(name, (60, 100, 0))[2],
                    )
                    st.respawn_min = int(round(h * 60))
                    self._set_boss(message.guild.id, st)
                    await message.channel.send(f"{name} の周期を {h}h に設定しました。")
                except Exception:
                    await message.channel.send("`rh ボス名 時間h` の形式で。")
            else:
                await message.channel.send("`rh ボス名 時間h` の形式で。")
            return True

        if low.startswith("reset "):  # reset HHMM
            p = raw.split()
            if len(p) == 2 and p[1].isdigit():
                h, m = zfill_hhmm(p[1])
                base = jst_now().replace(hour=h, minute=m, second=0, microsecond=0)
                await self._reset_all(message.guild.id, base)
                await message.channel.send(f"全体を {base.strftime('%H:%M')} リセットしました。")
            else:
                await message.channel.send("`reset HHMM` の形式で。")
            return True

        if low == "restart":
            await message.channel.send("再起動します。保存済みデータは引き継ぎます…")
            await asyncio.sleep(1)
            os._exit(0)

        if low in ("help", "commands"):
            await message.channel.send(self._help_text())
            return True

        return False

    def _help_text(self) -> str:
        return (
            "【使い方】\n"
            "- 討伐入力：`ボス名 HHMM [周期h]` 例:`スタン 1120` / `ティミニエル 0930 8h`\n"
            "- 一覧：`bt` / `bt3` / `bt6` / `bt12` / `bt24`（!省略OK）\n"
            "- 監視ON/OFF：`hereon` / `hereoff`\n"
            "- 周期変更：`rh ボス名 8h`\n"
            "- 一覧(プリセット)：`bosses`\n"
            "- 全体リセット：`reset HHMM`\n"
            "- 再起動：`restart`（Renderが自動再起動）\n"
        )

    async def _reset_all(self, guild_id: int, base_jst: datetime):
        # 仕様：
        # 100% & 初回遅延0 → 手動入力（next未設定）
        # 100% & 初回遅延あり → (reset + 初回遅延)
        # 50%/33% & 初回遅延0 → (reset + 通常周期)
        # 50%/33% & 初回遅延あり → (reset + 初回遅延)
        for st in self._all_bosses(guild_id):
            preset = self.presets.get(st.name, (st.respawn_min, st.rate, st.first_delay_min))
            st.respawn_min, st.rate, st.first_delay_min = preset
            if st.rate == 100 and st.first_delay_min == 0:
                st.next_spawn_utc = None
                st.skip = 0
            elif st.rate == 100 and st.first_delay_min > 0:
                center = base_jst.astimezone(timezone.utc) + timedelta(minutes=st.first_delay_min)
                st.next_spawn_utc = dt_to_ts(center)
                st.skip = 0
            elif st.rate in (50, 33) and st.first_delay_min == 0:
                center = base_jst.astimezone(timezone.utc) + timedelta(minutes=st.respawn_min)
                st.next_spawn_utc = dt_to_ts(center)
                st.skip = 0
            else:  # 50/33 & 初回遅延あり
                center = base_jst.astimezone(timezone.utc) + timedelta(minutes=st.first_delay_min)
                st.next_spawn_utc = dt_to_ts(center)
                st.skip = 0
            self._set_boss(guild_id, st)

    async def _send_bt(self, channel: discord.TextChannel, guild_id: int, horizon_h: Optional[int]):
        # 時刻順、時台切替で改行1つ
        items = []
        now = now_utc()
        for st in self._all_bosses(guild_id):
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

        lines = []
        cur_h = None
        for t, st in items:
            j = t.astimezone(JST)
            if cur_h is None:
                cur_h = j.hour
            if j.hour != cur_h:
                lines.append("")  # 改行1つに変更（ユーザー要望）
                cur_h = j.hour
            lines.append(f"{j.strftime('%H:%M:%S')} : {st.name} {st.flags()}")

        await channel.send("\n".join(lines))

# -------------------- Keepalive API -------------------- #
app = FastAPI()

@app.get("/health")
async def health():
    return {"ok": True}

# -------------------- 起動 -------------------- #
def run():
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN not set")

    bot = BossBot()

    async def main_async():
        # Discord と FastAPI を同一イベントループで並列起動
        config = Config(app=app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), loop="asyncio")
        server = Server(config)

        async def run_bot():
            try:
                await bot.start(token)
            except discord.errors.HTTPException as e:
                if e.status == 429:
                    # Cloudflare RateLimited → バックオフ
                    wait = BACKOFF_429_MIN * 60 + BACKOFF_JITTER_SEC
                    chs = [cid for g in bot.data.values() for cid in g.get("channels", [])]
                    for gid, g in bot.data.items():
                        guild = bot.get_guild(int(gid))
                        if guild:
                            for cid in g.get("channels", []):
                                ch = guild.get_channel(cid)
                                if ch:
                                    try:
                                        await ch.send(f"[BOT] 429/RateLimited を検出。{wait}s 待機して再試行します。")
                                    except Exception:
                                        pass
                    await asyncio.sleep(wait)
                    await bot.start(token)
                else:
                    raise

        bot_task = asyncio.create_task(run_bot())
        api_task = asyncio.create_task(server.serve())
        await asyncio.wait([bot_task, api_task], return_when=asyncio.FIRST_COMPLETED)

    asyncio.run(main_async())

if __name__ == "__main__":
    run()
