# -*- coding: utf-8 -*-
import os
import json
import math
import asyncio
import logging
import random
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple, DefaultDict
from collections import defaultdict

import discord
from discord.ext import commands, tasks
from fastapi import FastAPI
from uvicorn import Config, Server

# -------------------- 基本設定 -------------------- #
JST = timezone(timedelta(hours=9))
CHECK_SEC = 10
MERGE_WINDOW_SEC = 60     # 通知集約の±秒
AUTOSKIP_AFTER_SEC = 60   # 出現からこの秒数たったら自動で次周へ
BLANK_LINES_BETWEEN_HOURS = 1  # !bt の時間帯の段落は空行1行に（要望対応）

DATA_DIR = "data"
STORE_FILE = os.path.join(DATA_DIR, "store.json")
PRESET_FILE = "bosses_preset.json"

TOKEN = os.environ.get("DISCORD_TOKEN", "")
BACKOFF_429_MIN = int(os.getenv("BACKOFF_429_MIN", "15"))  # 429検知時の待機（分）
BACKOFF_JITTER_SEC = int(os.getenv("BACKOFF_JITTER_SEC", "30"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bossbot")


# -------------------- ユーティリティ -------------------- #
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_ts(dt: datetime) -> int:
    return int(dt.timestamp())


def jst_from_ts(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=JST)


def jst_str(ts: int) -> str:
    return jst_from_ts(ts).strftime("%H:%M:%S")


# -------------------- データモデル -------------------- #
@dataclass
class BossState:
    name: str
    respawn_min: int                 # 既定周期（分）
    rate: int = 100                  # 出現率（%）
    initial_delay_min: int = 0       # 初回遅延（分）
    next_spawn_utc: Optional[int] = None
    channel_id: Optional[int] = None
    skip: int = 0
    excluded_reset: bool = False
    last_pre_notice_key: Optional[int] = None    # その出現center tsで1分前を通知済みか
    last_spawn_notice_key: Optional[int] = None  # その出現center tsで出現を通知済みか

    def label_flags(self) -> str:
        parts = []
        if self.rate == 100:
            parts.append("※確定")
        if self.skip > 0:
            parts.append(f"{self.skip}周")
        return "[" + "] [".join(parts) + "]" if parts else ""


# -------------------- ストレージ -------------------- #
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
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


# -------------------- ボス名エイリアス -------------------- #
# ひらがな/カタカナ/一部一致/アルファベット（例：qa）に対応する簡易マッパー
ALIASES_RAW = {
    "クイーンアント": {"qa", "QA", "queen", "queenant", "くいーん", "くいーんあんと", "クイーン", "アント"},
    "ガレス": {"gareth", "がれす"},
    "ベヒモス": {"behemoth", "べひ", "べひもす"},
    "ティミニエル": {"timiniel", "てぃみに"},
    "ティミトリス": {"timitris"},
    "ミュータントクルマ": {"mutant", "m-kuruma", "みゅーたんと"},
    "汚染したクルマ": {"contaminated", "o-kuruma"},
    "チェルトゥバ": {"cherutuba", "celtuba", "ちぇると"},
    "ヒシルローメ": {"hishilrome", "ひしる"},
    "オルフェン": {"orfen", "orfen"},
    "ドラゴンビースト": {"dragonbeast", "db"},
    "コルーン": {"korun", "colune"},
    "セル": {"cerl", "せる"},
    "カタン": {"katan"},
    # 必要に応じて追加
}

def normalize(s: str) -> str:
    return s.strip().lower().replace("　", "").replace(" ", "")

def build_alias_map() -> Dict[str, str]:
    m: Dict[str, str] = {}
    for official, keys in ALIASES_RAW.items():
        m[normalize(official)] = official
        for k in keys:
            m[normalize(k)] = official
    return m

ALIAS_MAP = build_alias_map()

def resolve_boss_name(input_name: str, candidates: List[str]) -> Optional[str]:
    """表記揺れ/省略/一部一致/英字ニックネームを考慮して公式名を返す"""
    if not input_name:
        return None
    key = normalize(input_name)
    # 1) エイリアス完全一致
    if key in ALIAS_MAP:
        return ALIAS_MAP[key]
    # 2) 公式名の一部一致（先頭一致優先→部分一致）
    # 先に正規名をnormalizeした辞書を作る
    norm_to_official = {normalize(c): c for c in candidates}
    # 先頭一致
    for nk, off in norm_to_official.items():
        if nk.startswith(key):
            return off
    # 部分一致
    for nk, off in norm_to_official.items():
        if key in nk:
            return off
    return None


# -------------------- Bot 本体 -------------------- #
class BossBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.store = Store(STORE_FILE)
        self.data: Dict[str, Dict[str, dict]] = self.store.load()  # guild -> name -> dict
        self.presets: Dict[str, Tuple[int, int, int]] = {}  # name -> (respawn_min, rate, initial_delay_min)
        self._load_presets()
        self.tick.start()

    # --- プリセット --- #
    def _load_presets(self):
        try:
            with open(PRESET_FILE, "r", encoding="utf-8") as f:
                arr = json.load(f)
            m = {}
            for x in arr:
                resp_min = int(round(float(x["respawn_h"]) * 60))
                init_min = int(round(float(x.get("initial_delay_h", 0)) * 60))
                m[x["name"]] = (resp_min, int(x["rate"]), init_min)
            self.presets = m
            log.info("presets loaded: %d", len(self.presets))
        except Exception as e:
            log.exception("preset load error: %s", e)
            self.presets = {}

    # --- ストア操作 --- #
    def _gkey(self, guild_id: int) -> str:
        return str(guild_id)

    def _all(self, guild_id: int) -> List[BossState]:
        g = self.data.get(self._gkey(guild_id), {})
        return [BossState(**d) for d in g.values()]

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

    # --- 入力パース --- #
    def _parse_input(self, content: str, known_names: List[str]) -> Optional[Tuple[str, datetime, Optional[int]]]:
        """
        例:
          "コルーン 1120" / "ティミニエル 1121 8h" / "フェリス"
        HHMMが未来なら前日扱い。周期h のみ任意上書き。
        """
        parts = content.strip().split()
        if not parts:
            return None
        # 先頭をボス名として解決
        candidate = parts[0]
        name = resolve_boss_name(candidate, known_names)
        if not name:
            return None

        jst_now = datetime.now(JST)
        hhmm = None
        respawn_override = None

        if len(parts) >= 2 and parts[1].isdigit() and 3 <= len(parts[1]) <= 4:
            p = parts[1].zfill(4)
            h, m = int(p[:2]), int(p[2:])
            base = jst_now.replace(hour=h, minute=m, second=0, microsecond=0)
            if base > jst_now:
                base -= timedelta(days=1)
            hhmm = base
        else:
            hhmm = jst_now

        if len(parts) >= 3 and parts[2].lower().endswith("h"):
            try:
                respawn_override = int(round(float(parts[2][:-1]) * 60))
            except ValueError:
                pass

        return name, hhmm, respawn_override

    # --- 通知処理 --- #
    async def _notify_grouped(self, channel: discord.TextChannel, title_emoji: str, items: List[str]):
        if not items:
            return
        msg = f"{title_emoji} " + "\n".join(items)
        await channel.send(msg)

    @tasks.loop(seconds=CHECK_SEC)
    async def tick(self):
        await self.wait_until_ready()
        now = now_utc()
        # channel_id -> labels
        pre_items: DefaultDict[int, List[str]] = defaultdict(list)
        now_items: DefaultDict[int, List[str]] = defaultdict(list)

        for gkey, bosses in list(self.data.items()):
            guild = self.get_guild(int(gkey))
            if not guild:
                continue
            for d in list(bosses.values()):
                st = BossState(**d)
                if not st.channel_id or not st.next_spawn_utc:
                    continue
                center = datetime.fromtimestamp(st.next_spawn_utc, tz=timezone.utc)

                # 1分前（重複防止：center ts をキーに）
                pre_center = center - timedelta(minutes=1)
                if abs((now - pre_center).total_seconds()) <= MERGE_WINDOW_SEC:
                    key_ts = to_ts(center)
                    if st.last_pre_notice_key != key_ts:
                        label = f"{jst_str(st.next_spawn_utc)} : {st.name} {st.label_flags()}".strip()
                        pre_items[st.channel_id].append(label)
                        st.last_pre_notice_key = key_ts
                        self._set(int(gkey), st)

                # 出現（重複防止）
                if abs((now - center).total_seconds()) <= MERGE_WINDOW_SEC:
                    key_ts = to_ts(center)
                    if st.last_spawn_notice_key != key_ts:
                        label = f"{st.name} 出現！ [{jst_str(st.next_spawn_utc)}] (skip:{st.skip}) {st.label_flags()}".strip()
                        now_items[st.channel_id].append(label)
                        st.last_spawn_notice_key = key_ts
                        self._set(int(gkey), st)

                # 自動スライド
                if (now - center).total_seconds() >= AUTOSKIP_AFTER_SEC:
                    st.next_spawn_utc += st.respawn_min * 60
                    st.skip += 1
                    # 次の出現に対しては未通知に戻す
                    st.last_pre_notice_key = None
                    st.last_spawn_notice_key = None
                    self._set(int(gkey), st)

        # 送信（チャンネルごと集約）
        for cid, arr in pre_items.items():
            try:
                ch = self.get_channel(cid) or await self.fetch_channel(cid)
                await self._notify_grouped(ch, "⏰ 1分前", sorted(arr))
            except Exception:
                log.exception("pre notify failed for ch=%s", cid)

        for cid, arr in now_items.items():
            try:
                ch = self.get_channel(cid) or await self.fetch_channel(cid)
                await self._notify_grouped(ch, "🔥", sorted(arr))
            except Exception:
                log.exception("spawn notify failed for ch=%s", cid)

    @tick.before_loop
    async def before_tick(self):
        await self.wait_until_ready()

    # --- メッセージ監視（!省略対応＋高速入力） --- #
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        raw = message.content.strip()
        # 「!」なしコマンド対応
        cmd = raw.lstrip("!")
        low = cmd.lower()

        # bt系（!省略）
        if low in {"bt", "bt3", "bt6", "bt12", "bt24"}:
            horizon = {"bt": None, "bt3": 3, "bt6": 6, "bt12": 12, "bt24": 24}[low]
            await self._send_bt(message.channel, message.guild.id, horizon)
            return

        # reset/rh/rhshow/preset/restart（!省略）
        if low.startswith("reset"):
            parts = cmd.split()
            if len(parts) >= 2 and parts[1].isdigit():
                await self._cmd_reset(message, parts[1])
                return

        if low.startswith("rhshow"):
            kw = cmd.split()[1] if len(cmd.split()) >= 2 else None
            await self._cmd_rhshow(message.channel, message.guild.id, kw)
            return

        if low.startswith("rh "):
            parts = cmd.split()
            if len(parts) >= 3:
                await self._cmd_rh(message, parts[1], parts[2])
                return

        if low == "preset":
            self._load_presets()
            # 周期＆出現率＆初回遅延を更新
            for st in self._all(message.guild.id):
                if st.name in self.presets:
                    resp, rate, init = self.presets[st.name]
                    st.respawn_min, st.rate, st.initial_delay_min = resp, rate, init
                    self._set(message.guild.id, st)
            await message.channel.send("プリセットを再読込して反映しました。")
            return

        if low in {"restart", "reboot"}:
            await message.channel.send("再起動します…")
            # 外側の run_bot_loop が再生成してくれる
            await self.close()
            return

        # 高速討伐入力（ボス名 HHMM [xh]）
        known_names = list(self.presets.keys() or [])
        parsed = self._parse_input(raw, known_names)
        if parsed:
            name, when_jst, respawn_min_override = parsed
            gkey = self._gkey(message.guild.id)
            g = self.data.get(gkey, {})

            # 既定
            if name in g:
                st = BossState(**g[name])
            else:
                # プリセットなければ適当なデフォ（60分・100%）
                resp, rate, init = self.presets.get(name, (60, 100, 0))
                st = BossState(name=name, respawn_min=resp, rate=rate, initial_delay_min=init)

            if respawn_min_override:
                st.respawn_min = respawn_min_override

            st.channel_id = message.channel.id
            center = when_jst.astimezone(timezone.utc) + timedelta(minutes=st.respawn_min + 0)
            st.next_spawn_utc = to_ts(center)
            st.skip = 0
            st.last_pre_notice_key = None
            st.last_spawn_notice_key = None

            self._set(message.guild.id, st)
            await message.add_reaction("✅")
            return

        # コマンド拡張（discord.py標準）へ
        await self.process_commands(message)

    # --- bt系描画 --- #
    async def _send_bt(self, channel: discord.TextChannel, guild_id: int, horizon_h: Optional[int]):
        arr = self._all(guild_id)
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

        lines: List[str] = []
        cur_hour: Optional[int] = None
        for t, st in items:
            j = t.astimezone(JST)
            if cur_hour is None:
                cur_hour = j.hour
            if j.hour != cur_hour:
                lines.extend([""] * BLANK_LINES_BETWEEN_HOURS)  # ← 空行1行
                cur_hour = j.hour
            lines.append(f"{j.strftime('%H:%M:%S')} : {st.name} {st.label_flags()}")

        await channel.send("\n".join(lines))

    # --- コマンド（!rh, !rhshow, !reset） --- #
    async def _cmd_rh(self, message: discord.Message, name_raw: str, hours: str):
        name = resolve_boss_name(name_raw, list(self.presets.keys()))
        if not name:
            await message.channel.send(f"ボス名を特定できません：`{name_raw}`\n`rhshow` で候補を確認してください。")
            return
        st = self._get(message.guild.id, name) or BossState(
            name=name,
            respawn_min=self.presets.get(name, (60, 100, 0))[0],
            rate=self.presets.get(name, (60, 100, 0))[1],
            initial_delay_min=self.presets.get(name, (60, 100, 0))[2],
        )
        h = float(hours.rstrip("hH"))
        st.respawn_min = int(round(h * 60))
        self._set(message.guild.id, st)
        await message.channel.send(f"{name} の周期を {h}h に設定しました。")

    async def _cmd_rhshow(self, channel: discord.TextChannel, guild_id: int, kw: Optional[str]):
        arr = sorted(self._all(guild_id), key=lambda s: s.name)
        # 未登録はプリセットだけでも出す
        if not arr:
            arr = []
            for name, (resp, rate, init) in sorted(self.presets.items()):
                arr.append(BossState(name=name, respawn_min=resp, rate=rate, initial_delay_min=init))
        lines = []
        for st in arr:
            if kw and kw not in st.name:
                continue
            lines.append(f"• {st.name} : {st.respawn_min/60:.2f}h / rate {st.rate}% / 初回遅延 {st.initial_delay_min}分")
        await channel.send("\n".join(lines) or "登録なし")

    async def _cmd_reset(self, message: discord.Message, hhmm: str):
        p = hhmm.zfill(4)
        h, m = int(p[:2]), int(p[2:])
        base_jst = datetime.now(JST).replace(hour=h, minute=m, second=0, microsecond=0)

        # 仕様（要望）：
        # 100%/初回遅延なし → 何もしない（手動入力）
        # 100%/初回遅延あり → reset+初回遅延
        # 50%/33%/初回遅延なし → reset+通常周期
        # 50%/33%/初回遅延あり → reset+初回遅延
        cnt = 0
        for st in self._all(message.guild.id):
            if st.excluded_reset:
                continue
            apply_next: Optional[datetime] = None
            if st.rate == 100 and st.initial_delay_min == 0:
                apply_next = None  # スキップ（手動入力）
            elif st.rate == 100 and st.initial_delay_min > 0:
                apply_next = base_jst + timedelta(minutes=st.initial_delay_min)
            elif st.rate in (50, 33) and st.initial_delay_min == 0:
                apply_next = base_jst + timedelta(minutes=st.respawn_min)
            else:  # 50/33 かつ 初回遅延あり
                apply_next = base_jst + timedelta(minutes=st.initial_delay_min)

            if apply_next is not None:
                st.next_spawn_utc = to_ts(apply_next.astimezone(timezone.utc))
                st.skip = 0
                st.last_pre_notice_key = None
                st.last_spawn_notice_key = None
                self._set(message.guild.id, st)
                cnt += 1

        await message.channel.send(f"リセット {base_jst.strftime('%H:%M')} を反映しました。更新 {cnt}件。")


# -------------------- keepalive (FastAPI) -------------------- #
app = FastAPI()

@app.get("/health")
async def health(silent: int = 0):
    # UptimeRobot/cron から叩かれるだけなら 200 でOK
    return {"ok": True, "ts": datetime.now(JST).isoformat()}


# -------------------- 起動ランナー（429バックオフ＋再生成） -------------------- #
async def run_bot_loop():
    while True:
        bot = BossBot()  # ★毎回新しく作る（重要：Session is closed対策）
    # ここで例外に応じて再生成
        try:
            log.info("[BOT] starting login...")
            await bot.start(TOKEN)
        except discord.errors.HTTPException as e:
            text = str(e).lower()
            if getattr(e, "status", None) == 429 or "rate" in text or "1015" in text or "cloudflare" in text:
                wait = BACKOFF_429_MIN * 60 + random.randint(0, BACKOFF_JITTER_SEC)
                log.warning(f"[BOT] 429/RateLimited detected. sleep {wait}s and retry.")
                await asyncio.sleep(wait)
            else:
                log.exception("[BOT] HTTPException (non-429). retry in 15s.")
                await asyncio.sleep(15)
        except Exception:
            log.exception("[BOT] unexpected error")
            await asyncio.sleep(15)
        finally:
            try:
                await bot.close()
            except Exception:
                pass


def run():
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN not set")

    async def main_async():
        config = Config(app=app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), loop="asyncio", log_level="info")
        server = Server(config)
        api_task = asyncio.create_task(server.serve())
        bot_task = asyncio.create_task(run_bot_loop())
        await asyncio.wait([api_task, bot_task], return_when=asyncio.FIRST_COMPLETED)

    asyncio.run(main_async())


if __name__ == "__main__":
    run()
