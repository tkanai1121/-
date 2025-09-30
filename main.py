# main.py
import os
import json
import asyncio
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple, Set

import discord
from discord.ext import commands, tasks

from fastapi import FastAPI
from uvicorn import Config, Server

# ====== 基本設定 ======
JST = timezone(timedelta(hours=9))
DATA_DIR = "data"
STORE_FILE = os.path.join(DATA_DIR, "store.json")
PRESET_FILE = "bosses_preset.json"

CHECK_SEC = 10                     # 監視間隔
MERGE_WINDOW_SEC = 10              # 出現/1分前判定の許容窓（±秒）…短くして連投の機会を減らす
NOTIFY_DEDUP_TTL_SEC = 120         # 送信済みキーの保有時間（秒）…2分で掃除

# 429 対策（Cloudflare/Discordのレート制限でBANっぽく見えるやつ）
BACKOFF_429_MIN = int(os.environ.get("BACKOFF_429_MIN", "900"))      # デフォ 15分
BACKOFF_JITTER_SEC = int(os.environ.get("BACKOFF_JITTER_SEC", "30"))  # 小さなゆらぎ


# ====== モデル ======
@dataclass
class BossState:
    name: str
    respawn_min: int                 # 既定周期（分）
    rate: int = 100                  # 出現率（%）
    next_spawn_utc: Optional[int] = None
    channel_id: Optional[int] = None # 通知先チャンネル（最初に入力された所）
    skip: int = 0
    excluded_reset: bool = False     # !reset 対象外
    initial_delay_min: int = 0       # 初回遅延（分）

    def label_flags(self) -> str:
        parts = []
        if self.rate == 100:
            parts.append("※確定")
        if self.skip > 0:
            parts.append(f"{self.skip}周")
        return "[" + "] [".join(parts) + "]" if parts else ""


# ====== ストア ======
class Store:
    """self.data の実体は guild_id(str) -> Dict[str, dict]
       - 通常のキー: ボス名 -> BossState dict
       - 予約キー: "__meta__" -> {"allowed_channels": [int, ...]}
    """
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        if not os.path.exists(self.path):
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({}, f)

    def load(self) -> Dict[str, Dict[str, dict]]:
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)

    def save(self, data: Dict[str, Dict[str, dict]]):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ====== Bot ======
class BossBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)

        self.store = Store(STORE_FILE)
        self.data: Dict[str, Dict[str, dict]] = self.store.load()  # guild -> name/meta -> dict
        self.presets: Dict[str, Tuple[int, int]] = {}              # name -> (respawn_min, rate)
        self._load_presets()

        # 連投防止: 「すでに送ったキー」を覚えておく
        # gkey(str) -> set("pre|ch|spawn_ts|name" or "now|ch|spawn_ts|name")
        self._sent_keys: Dict[str, Set[str]] = {}

        # 監視開始
        self.tick.start()

    # ---------- util ----------
    def _gkey(self, guild_id: int) -> str:
        return str(guild_id)

    # ----- meta: allowed channels -----
    def _get_meta(self, guild_id: int) -> dict:
        g = self.data.setdefault(self._gkey(guild_id), {})
        return g.setdefault("__meta__", {"allowed_channels": []})

    def _is_channel_allowed(self, guild_id: int, channel_id: int) -> bool:
        allowed = set(self._get_meta(guild_id).get("allowed_channels", []))
        # 設定が空 = どこでもOK
        return (len(allowed) == 0) or (channel_id in allowed)

    def _allow_here(self, guild_id: int, channel_id: int, allow: bool):
        meta = self._get_meta(guild_id)
        s = set(meta.get("allowed_channels", []))
        if allow:
            s.add(channel_id)
        else:
            s.discard(channel_id)
        meta["allowed_channels"] = sorted(list(s))
        self.store.save(self.data)

    # ----- bosses -----
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
        arr = []
        for k, d in g.items():
            if k == "__meta__":
                continue
            arr.append(BossState(**d))
        return arr

    def _load_presets(self):
        try:
            with open(PRESET_FILE, "r", encoding="utf-8") as f:
                arr = json.load(f)
            # respawn_h(時間) -> 分に
            self.presets = {
                x["name"]: (int(round(float(x["respawn_h"]) * 60)), int(x["rate"]))
                for x in arr
            }
        except Exception as e:
            print("preset load error", e)
            self.presets = {}

    # ----- parsing -----
    def _parse_input(self, content: str) -> Optional[Tuple[str, datetime, Optional[int]]]:
        # 例: "コルーン 1120", "ティミニエル 1121 8h", "フェリス"
        parts = content.strip().split()
        if len(parts) == 0:
            return None
        name = parts[0]
        jst_now = datetime.now(JST)
        hhmm = None
        respawn_min = None

        if len(parts) >= 2 and parts[1].isdigit() and 3 <= len(parts[1]) <= 4:
            p = parts[1].zfill(4)
            try:
                h, m = int(p[:2]), int(p[2:])
                base = jst_now.replace(hour=h, minute=m, second=0, microsecond=0)
                # 未来hhmmは前日扱い
                if base > jst_now:
                    base = base - timedelta(days=1)
                hhmm = base
            except ValueError:
                hhmm = None
        if hhmm is None:
            hhmm = jst_now

        if len(parts) >= 3 and parts[2].lower().endswith("h"):
            try:
                respawn_min = int(round(float(parts[2][:-1]) * 60))
            except ValueError:
                pass

        return name, hhmm, respawn_min

    # ----- 通知 helpers -----
    async def _notify_grouped(self, channel: discord.TextChannel, title_emoji: str, items: List[str]):
        if not items:
            return
        msg = f"{title_emoji} " + "\n".join(items)
        await channel.send(msg)

    def _cleanup_sent(self, guild_id: int, now_utc_dt: datetime):
        """送信済みキーを期限切れ掃除"""
        gkey = self._gkey(guild_id)
        s = self._sent_keys.get(gkey)
        if not s:
            return
        to_del = []
        for k in s:
            try:
                # 形式: "{pre/now}|{channel_id}|{spawn_ts}|{name}"
                _, _, ts_str, _ = k.split("|", 3)
                spawn_ts = int(ts_str)
            except Exception:
                to_del.append(k)
                continue
            if now_utc_dt.timestamp() > spawn_ts + NOTIFY_DEDUP_TTL_SEC:
                to_del.append(k)
        for k in to_del:
            s.discard(k)
        if len(s) == 0:
            self._sent_keys.pop(gkey, None)

    # ====== 監視 ======
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

            # 送信済み掃除
            self._cleanup_sent(int(gkey), now)
            to_mark: List[str] = []

            meta = bosses.get("__meta__", {"allowed_channels": []})
            allowed = set(meta.get("allowed_channels", []))

            for d in list(bosses.values()):
                if isinstance(d, dict) and d.get("name") is None:
                    # __meta__ など
                    continue

                st = BossState(**d)  # type: ignore
                if not st.channel_id or not st.next_spawn_utc:
                    continue

                # 送信先が許可外ならスキップ（ここで余計なチャンネルに出ない）
                if len(allowed) > 0 and st.channel_id not in allowed:
                    continue

                center = datetime.fromtimestamp(st.next_spawn_utc, tz=timezone.utc)
                ch_id = st.channel_id

                pre_key = f"pre|{ch_id}|{st.next_spawn_utc}|{st.name}"
                now_key = f"now|{ch_id}|{st.next_spawn_utc}|{st.name}"
                sent = self._sent_keys.setdefault(gkey, set())

                # 1分前
                if abs((now - (center - timedelta(minutes=1))).total_seconds()) <= MERGE_WINDOW_SEC:
                    if pre_key not in sent:
                        label = f"{center.astimezone(JST).strftime('%H:%M:%S')} : {st.name} {st.label_flags()}".strip()
                        pre_items.setdefault(ch_id, []).append(label)
                        to_mark.append(pre_key)

                # 出現
                if abs((now - center).total_seconds()) <= MERGE_WINDOW_SEC:
                    if now_key not in sent:
                        label = f"{st.name} 出現！ [{center.astimezone(JST).strftime('%H:%M:%S')}] (skip:{st.skip}) {st.label_flags()}".strip()
                        now_items.setdefault(ch_id, []).append(label)
                        to_mark.append(now_key)

                # 出現から60秒経過で自動的に次周
                if (now - center).total_seconds() >= 60:
                    st.next_spawn_utc += st.respawn_min * 60
                    st.skip += 1
                    self._set(int(gkey), st)

            # 送信（チャンネル単位で集約）
            for cid, arr in pre_items.items():
                ch = guild.get_channel(cid) or await guild.fetch_channel(cid)
                await self._notify_grouped(ch, "⏰ 1分前", sorted(arr))
            for cid, arr in now_items.items():
                ch = guild.get_channel(cid) or await guild.fetch_channel(cid)
                await self._notify_grouped(ch, "🔥", sorted(arr))

            # 送信済みに反映
            if to_mark:
                self._sent_keys.setdefault(gkey, set()).update(to_mark)

    @tick.before_loop
    async def before_tick(self):
        await self.wait_until_ready()

    # ====== イベント: メッセージ入力 ======
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        # 監視対象チャンネルのみ（設定が空の時は全チャンネル可）
        if not self._is_channel_allowed(message.guild.id, message.channel.id):
            await self.process_commands(message)  # コマンドは通す
            return

        content = message.content.strip()
        parsed = self._parse_input(content)
        if parsed:
            name, when_jst, respawn_min_override = parsed

            gkey = self._gkey(message.guild.id)
            g = self.data.get(gkey, {})

            st = BossState(name=name, respawn_min=60)
            preset = self.presets.get(name)
            if preset:
                st.respawn_min, st.rate = preset[0], preset[1]
            if name in g:
                # 既存
                d = g[name]
                st = BossState(**d)

            if respawn_min_override:
                st.respawn_min = respawn_min_override

            # 通知先を覚えておく（ここが実際の出力先）
            st.channel_id = st.channel_id or message.channel.id

            # 次湧き = 討伐時刻 + 周期 + 初回遅延
            center = when_jst.astimezone(timezone.utc) + timedelta(
                minutes=st.respawn_min + st.initial_delay_min
            )
            st.next_spawn_utc = int(center.timestamp())
            st.skip = 0
            self._set(message.guild.id, st)

            await message.add_reaction("✅")
            return

        await self.process_commands(message)

    # ====== コマンド ======
    def _bt_list_text(self, guild_id: int, horizon_h: Optional[int]) -> str:
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

        # HH 段落化（空行1つ）
        lines = []
        current_hour = None
        for t, st in items:
            j = t.astimezone(JST)
            if current_hour is None:
                current_hour = j.hour
            if j.hour != current_hour:
                lines.append("")  # 段落
                current_hour = j.hour
            lines.append(f"{j.strftime('%H:%M:%S')} : {st.name} {st.label_flags()}")
        return "\n".join(lines) if lines else "予定はありません。"

    @commands.command(name="bt")
    async def bt(self, ctx: commands.Context):
        if not self._is_channel_allowed(ctx.guild.id, ctx.channel.id):
            return
        await ctx.send(self._bt_list_text(ctx.guild.id, None))

    @commands.command(name="bt3")
    async def bt3(self, ctx: commands.Context):
        if not self._is_channel_allowed(ctx.guild.id, ctx.channel.id):
            return
        await ctx.send(self._bt_list_text(ctx.guild.id, 3))

    @commands.command(name="bt6")
    async def bt6(self, ctx: commands.Context):
        if not self._is_channel_allowed(ctx.guild.id, ctx.channel.id):
            return
        await ctx.send(self._bt_list_text(ctx.guild.id, 6))

    @commands.command(name="bt12")
    async def bt12(self, ctx: commands.Context):
        if not self._is_channel_allowed(ctx.guild.id, ctx.channel.id):
            return
        await ctx.send(self._bt_list_text(ctx.guild.id, 12))

    @commands.command(name="bt24")
    async def bt24(self, ctx: commands.Context):
        if not self._is_channel_allowed(ctx.guild.id, ctx.channel.id):
            return
        await ctx.send(self._bt_list_text(ctx.guild.id, 24))

    @commands.command(name="reset")
    async def reset(self, ctx: commands.Context, hhmm: str):
        if not self._is_channel_allowed(ctx.guild.id, ctx.channel.id):
            return
        p = hhmm.zfill(4)
        h, m = int(p[:2]), int(p[2:])
        base = datetime.now(JST).replace(hour=h, minute=m, second=0, microsecond=0)
        g_arr = self._all(ctx.guild.id)
        for st in g_arr:
            if st.excluded_reset:
                continue
            center = base + timedelta(minutes=st.respawn_min + st.initial_delay_min)
            st.next_spawn_utc = int(center.astimezone(timezone.utc).timestamp())
            st.skip = 0
            self._set(ctx.guild.id, st)
        await ctx.send(f"全体を {base.strftime('%H:%M')} にリセットしました。")

    @commands.command(name="rh")
    async def rh(self, ctx: commands.Context, name: str, hours: str):
        if not self._is_channel_allowed(ctx.guild.id, ctx.channel.id):
            return
        st = self._get(ctx.guild.id, name) or BossState(name=name, respawn_min=60)
        h = float(hours.rstrip('hH'))
        st.respawn_min = int(round(h * 60))
        self._set(ctx.guild.id, st)
        await ctx.send(f"{name} の周期を {h}h に設定しました。")

    @commands.command(name="rhshow")
    async def rhshow(self, ctx: commands.Context, kw: Optional[str] = None):
        if not self._is_channel_allowed(ctx.guild.id, ctx.channel.id):
            return
        arr = sorted(self._all(ctx.guild.id), key=lambda s: s.name)
        lines = []
        for st in arr:
            if kw and kw not in st.name:
                continue
            lines.append(f"• {st.name} : {st.respawn_min/60:.2f}h / rate {st.rate}%")
        await ctx.send("\n".join(lines) or "登録なし")

    @commands.command(name="preset")
    async def preset(self, ctx: commands.Context):
        if not self._is_channel_allowed(ctx.guild.id, ctx.channel.id):
            return
        self._load_presets()
        # 既存へ反映（周期・rate）
        for st in self._all(ctx.guild.id):
            if st.name in self.presets:
                st.respawn_min, st.rate = self.presets[st.name]
                self._set(ctx.guild.id, st)
        await ctx.send("プリセットを再読込しました。")

    # ---- チャンネル制御 ----
    @commands.command(name="hereon")
    async def hereon(self, ctx: commands.Context):
        self._allow_here(ctx.guild.id, ctx.channel.id, True)
        await ctx.send("このチャンネルを **対象** にしました。")

    @commands.command(name="hereoff")
    async def hereoff(self, ctx: commands.Context):
        self._allow_here(ctx.guild.id, ctx.channel.id, False)
        await ctx.send("このチャンネルを **対象外** にしました。")

    @commands.command(name="hereshow")
    async def hereshow(self, ctx: commands.Context):
        meta = self._get_meta(ctx.guild.id)
        ids = meta.get("allowed_channels", [])
        if not ids:
            await ctx.send("対象チャンネルは未設定です（= 全チャンネル対象）。")
            return
        names = []
        for cid in ids:
            ch = ctx.guild.get_channel(cid)
            names.append(f"• <#{cid}>" if ch else f"• #{cid} (未発見)")
        await ctx.send("対象チャンネル:\n" + "\n".join(names))

# ====== keepalive (FastAPI) ======
app = FastAPI()

@app.get("/health")
async def health(silent: int = 0):
    # silent=1 の時は本文短く（uptime系のHEAD/GET監視を想定）
    return ({"ok": True} if not silent else {"ok": 1})

def run():
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN not set")

    bot = BossBot()

    async def start_bot_with_backoff():
        # Discord 側が 429/1015 で弾くケースのリトライ
        while True:
            try:
                await bot.start(token)
            except discord.errors.HTTPException as e:
                # レート制限（Cloudflare 1015 等）時は待って再試行
                if e.status == 429:
                    wait = BACKOFF_429_MIN * 60 + BACKOFF_JITTER_SEC
                    print(f"[BOT] 429/RateLimited を検出。{wait}s 待機して再試行します。")
                    await asyncio.sleep(wait)
                    continue
                raise
            break

    async def main_async():
        config = Config(app=app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), loop="asyncio")
        server = Server(config)

        bot_task = asyncio.create_task(start_bot_with_backoff())
        api_task = asyncio.create_task(server.serve())

        await asyncio.wait([bot_task, api_task], return_when=asyncio.FIRST_COMPLETED)

    asyncio.run(main_async())


if __name__ == "__main__":
    run()
