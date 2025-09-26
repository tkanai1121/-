# -*- coding: utf-8 -*-
import os, json, re, gc, unicodedata, asyncio
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import discord
from discord.ext import commands, tasks
from fastapi import FastAPI
from uvicorn import Config, Server

# ====== CONSTS ======
JST = timezone(timedelta(hours=9))
DATA_DIR = "data"
STORE_FILE = os.path.join(DATA_DIR, "store.json")
PRESET_FILE = "bosses_preset.json"
CHECK_SEC = 10
MERGE_WINDOW_SEC = 60  # ±60秒で通知を集約

# ====== Alias Normalize ======
KANAS = str.maketrans({
    'ア':'あ','イ':'い','ウ':'う','エ':'え','オ':'お',
    'カ':'か','キ':'き','ク':'く','ケ':'け','コ':'こ',
    'サ':'さ','シ':'し','ス':'す','セ':'せ','ソ':'そ',
    'タ':'た','チ':'ち','ツ':'つ','テ':'て','ト':'と',
    'ナ':'な','ニ':'に','ヌ':'ぬ','ネ':'ね','ノ':'の',
    'ハ':'は','ヒ':'ひ','フ':'ふ','ヘ':'へ','ホ':'ほ',
    'マ':'ま','ミ':'み','ム':'む','メ':'め','モ':'も',
    'ヤ':'や','ユ':'ゆ','ヨ':'よ',
    'ラ':'ら','リ':'り','ル':'る','レ':'れ','ロ':'ろ',
    'ワ':'わ','ヲ':'を','ン':'ん'
})
ROMA = {
    "qa":"クイーンアント", "queen":"クイーンアント",
    "orfen":"オルフェン",
    "timi":"ティミトリス", "timiniel":"ティミニエル",
    "glaaki":"グラーキ", "glaki":"グラーキ",
    "medu":"メデューサ", "katan":"カタン"
}
def normalize_name(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"[\s_\-・/\\]+", "", s)
    s = s.translate(KANAS).lower()
    return s

# ====== Data Model ======
@dataclass
class BossState:
    name: str
    respawn_min: int               # 分
    rate: int = 100                # %
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

# ====== Storage ======
class Store:
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
        intents.message_content = True   # ←これを追加
        super().__init__(command_prefix="!", intents=intents)
        self.store = Store(STORE_FILE)
        self.data: Dict[str, Dict[str, dict]] = self.store.load()   # guild -> name -> dict
        self.presets: Dict[str, Tuple[int, int]] = {}               # name -> (respawn_min, rate)
        self.alias_map: Dict[str, Dict[str, str]] = {}              # guild -> norm -> canonical
        self._load_presets()
        self._seed_alias = self._build_seed_alias()
        # NOTE: self.tick.start() は setup_hook で開始する（イベントループ起動後）

    async def setup_hook(self):
        # ここならイベントループ起動後なので安全
        self.tick.start()

    # ---- helpers: storage / ids ----
    def _gkey(self, gid: int) -> str:
        return str(gid)
    def _get(self, gid: int, name: str) -> Optional[BossState]:
        g = self.data.get(self._gkey(gid), {})
        d = g.get(name)
        return BossState(**d) if d else None
    def _set(self, gid: int, st: BossState):
        gkey = self._gkey(gid)
        if gkey not in self.data:
            self.data[gkey] = {}
        self.data[gkey][st.name] = asdict(st)
        self.store.save(self.data)
    def _all(self, gid: int) -> List[BossState]:
        return [BossState(**d) for d in self.data.get(self._gkey(gid), {}).values()]

    # ---- presets ----
    def _load_presets(self):
        try:
            with open(PRESET_FILE, "r", encoding="utf-8") as f:
                arr = json.load(f)
            self.presets = {
                x["name"]: (int(round(float(x["respawn_h"]) * 60)), int(x["rate"]))
                for x in arr
            }
        except Exception as e:
            print("preset load error:", e)
            self.presets = {}

    # ---- alias ----
    def _build_seed_alias(self) -> Dict[str, str]:
        seed: Dict[str, str] = {}
        for name in self.presets.keys():
            n = normalize_name(name)
            for L in (2, 3, 4):
                seed.setdefault(n[:L], name)
        for k, v in ROMA.items():
            seed[normalize_name(k)] = v
        return seed

    def _resolve_alias(self, guild_id: int, raw: str) -> Optional[str]:
        g_alias = self.alias_map.get(self._gkey(guild_id), {})
        norm = normalize_name(raw)
        if norm in g_alias:
            return g_alias[norm]
        for canonical in self.presets.keys():
            if normalize_name(canonical) == norm:
                return canonical
        if norm in self._seed_alias:
            return self._seed_alias[norm]
        # 前方一致がユニークなら採用
        cands = [n for n in self.presets.keys() if normalize_name(n).startswith(norm)]
        if len(cands) == 1:
            return cands[0]
        return None

    # ---- input parse ----
    def _parse_input(self, content: str) -> Optional[Tuple[str, datetime, Optional[int]]]:
        # 例: "コルーン 1120", "ティミニエル 1121 8h", "フェリス"
        parts = content.strip().split()
        if not parts:
            return None
        raw_name = parts[0]
        jst_now = datetime.now(JST)
        when = None
        respawn_min = None
        if len(parts) >= 2 and parts[1].isdigit() and 3 <= len(parts[1]) <= 4:
            p = parts[1].zfill(4)
            h, m = int(p[:2]), int(p[2:])
            base = jst_now.replace(hour=h, minute=m, second=0, microsecond=0)
            if base > jst_now:  # 未来は前日扱い
                base -= timedelta(days=1)
            when = base
        if when is None:
            when = jst_now
        if len(parts) >= 3 and parts[2].lower().endswith("h"):
            try:
                respawn_min = int(round(float(parts[2][:-1]) * 60))
            except ValueError:
                pass
        return raw_name, when, respawn_min

    # ---- notify helper ----
    async def _notify_grouped(self, ch: discord.TextChannel, title: str, items: List[str]):
        if items:
            await ch.send(f"{title} " + "\n".join(items))

    # ---- ticker ----
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
            for d in bosses.values():
                st = BossState(**d)
                if not st.channel_id or not st.next_spawn_utc:
                    continue
                center = datetime.fromtimestamp(st.next_spawn_utc, tz=timezone.utc)
                # 1分前
                if abs((now - (center - timedelta(minutes=1))).total_seconds()) <= MERGE_WINDOW_SEC:
                    pre_items.setdefault(st.channel_id, []).append(
                        f"{center.astimezone(JST).strftime('%H:%M:%S')} : {st.name} {st.label_flags()}".strip()
                    )
                # 出現
                if abs((now - center).total_seconds()) <= MERGE_WINDOW_SEC:
                    now_items.setdefault(st.channel_id, []).append(
                        f"{st.name} 出現！ [{center.astimezone(JST).strftime('%H:%M:%S')}] (skip:{st.skip}) {st.label_flags()}".strip()
                    )
                # 出現から1分経過 → 自動スライド
                if (now - center).total_seconds() >= 60:
                    st.next_spawn_utc += st.respawn_min * 60
                    st.skip += 1
                    self._set(int(gkey), st)
            # 送信
            for cid, arr in pre_items.items():
                ch = guild.get_channel(cid) or await guild.fetch_channel(cid)
                await self._notify_grouped(ch, "⏰ 1分前", sorted(arr))
            for cid, arr in now_items.items():
                ch = guild.get_channel(cid) or await guild.fetch_channel(cid)
                await self._notify_grouped(ch, "🔥", sorted(arr))

    @tick.before_loop
    async def before_tick(self):
        await self.wait_until_ready()

    # ---- events ----
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        parsed = self._parse_input(message.content.strip())
        if parsed:
            raw, when_jst, respawn_min_override = parsed
            canonical = self._resolve_alias(message.guild.id, raw)
            if not canonical:
                await message.reply(
                    f"ボス名を特定できません：`{raw}`\n`!aliasshow` で候補確認、または `!alias {raw} 正式名` で登録してください。",
                    mention_author=False
                )
                return
            gkey = self._gkey(message.guild.id)
            g = self.data.get(gkey, {})
            st = BossState(name=canonical, respawn_min=60)
            if canonical in self.presets:
                st.respawn_min, st.rate = self.presets[canonical]
            if canonical in g:
                st = BossState(**g[canonical])
            if respawn_min_override:
                st.respawn_min = respawn_min_override
            st.channel_id = st.channel_id or message.channel.id
            center = when_jst.astimezone(timezone.utc) + timedelta(
                minutes=st.respawn_min + st.initial_delay_min
            )
            st.next_spawn_utc = int(center.timestamp())
            st.skip = 0
            self._set(message.guild.id, st)
            await message.add_reaction("✅")
            return
        await self.process_commands(message)

    # ---- commands ----
    @commands.command(name="restart")
    async def restart_cmd(self, ctx: commands.Context):
        await ctx.send("♻️ Botを再起動します...")
        gc.collect()
        self.store.save(self.data)
        await self.close()
        os._exit(1)  # Render が再起動

    @commands.command(name="alias")
    async def alias(self, ctx: commands.Context, short: str, canonical: str):
        gkey = self._gkey(ctx.guild.id)
        self.alias_map.setdefault(gkey, {})[normalize_name(short)] = canonical
        await ctx.send(f"`{short}` を `{canonical}` の別名として登録しました。")

    @commands.command(name="aliasshow")
    async def aliasshow(self, ctx: commands.Context):
        g_alias = self.alias_map.get(self._gkey(ctx.guild.id), {})
        if not g_alias:
            await ctx.send("（別名は未登録です）")
            return
        lines = [f"• {k} → {v}" for k, v in sorted(g_alias.items())]
        await ctx.send("\n".join(lines))

    @commands.command(name="bt")
    async def bt(self, ctx: commands.Context):
        await self._send_bt(ctx, None)

    @commands.command(name="bt3")
    async def bt3(self, ctx: commands.Context):
        await self._send_bt(ctx, 3)

    @commands.command(name="bt6")
    async def bt6(self, ctx: commands.Context):
        await self._send_bt(ctx, 6)

    @commands.command(name="bt12")
    async def bt12(self, ctx: commands.Context):
        await self._send_bt(ctx, 12)

    @commands.command(name="bt24")
    async def bt24(self, ctx: commands.Context):
        await self._send_bt(ctx, 24)

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
        lines: List[str] = []
        current_hour = None
        for t, st in items:
            j = t.astimezone(JST)
            if current_hour is None:
                current_hour = j.hour
            if j.hour != current_hour:
                lines += ["", "", ""]  # 改行×3で段落
                current_hour = j.hour
            lines.append(f"{j.strftime('%H:%M:%S')} : {st.name} {st.label_flags()}")
        await ctx.send("\n".join(lines) if lines else "予定はありません。")

    @commands.command(name="reset")
    async def reset(self, ctx: commands.Context, hhmm: str):
        p = hhmm.zfill(4)
        h, m = int(p[:2]), int(p[2:])
        base = datetime.now(JST).replace(hour=h, minute=m, second=0, microsecond=0)
        for st in self._all(ctx.guild.id):
            if st.excluded_reset:
                continue
            center = base + timedelta(minutes=st.respawn_min + st.initial_delay_min)
            st.next_spawn_utc = int(center.astimezone(timezone.utc).timestamp())
            st.skip = 0
            self._set(ctx.guild.id, st)
        await ctx.send(f"全体を {base.strftime('%H:%M')} リセットしました。")

    @commands.command(name="rh")
    async def rh(self, ctx: commands.Context, name: str, hours: str):
        canonical = self._resolve_alias(ctx.guild.id, name) or name
        st = self._get(ctx.guild.id, canonical) or BossState(name=canonical, respawn_min=60)
        h = float(hours.rstrip('hH'))
        st.respawn_min = int(round(h * 60))
        self._set(ctx.guild.id, st)
        await ctx.send(f"{canonical} の周期を {h}h に設定しました。")

    @commands.command(name="rhshow")
    async def rhshow(self, ctx: commands.Context, kw: Optional[str] = None):
        arr = sorted(self._all(ctx.guild.id), key=lambda s: s.name)
        lines: List[str] = []
        for st in arr:
            if kw and kw not in st.name:
                continue
            lines.append(f"• {st.name} : {st.respawn_min/60:.2f}h / rate {st.rate}%")
        await ctx.send("\n".join(lines) or "登録なし")

# ====== keepalive (FastAPI) ======
app = FastAPI()
bot: Optional[BossBot] = None

@app.get("/health")
async def health():
    # 軽い健全化：GC＋ストア再読込（読み取りのみ）
    gc.collect()
    try:
        if bot is not None:
            bot.data = bot.store.load()
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True}

def run():
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN not set")
    global bot
    bot = BossBot()

    async def main_async():
        config = Config(app=app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), loop="asyncio")
        server = Server(config)
        bot_task = asyncio.create_task(bot.start(token))
        api_task = asyncio.create_task(server.serve())
        await asyncio.wait([bot_task, api_task], return_when=asyncio.FIRST_COMPLETED)

    asyncio.run(main_async())

if __name__ == "__main__":
    run()


