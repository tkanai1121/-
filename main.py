# －－中略なし・このファイルまるごとコピペしてください－－
# めるる – L2M Boss Bot（エイリアス・一括登録・出現率・/ping 204 対応）

import asyncio, json, os, re
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from typing import Dict, Optional, List, Tuple

import discord
from discord.ext import commands, tasks
from dateutil.tz import gettz

PREFIX = "!"
JST = gettz("Asia/Tokyo")
DATA_FILE = "bosses.json"
ALIAS_FILE = "aliases.json"
ANNOUNCE_CHANNEL_ID = int(os.getenv("ANNOUNCE_CHANNEL_ID", "0"))

# ── プリセット（名称, 時間[h], 出現率%）──
def h(m):  # "H:MM" -> float hour
    if isinstance(m, (int, float)): return float(m)
    m = str(m)
    mm = re.fullmatch(r"(\d+):(\d{2})", m)
    if mm: return int(mm.group(1)) + int(mm.group(2))/60.0
    return float(m)

PRESET_BOSSES: Dict[str, List[Tuple[str, float, int]]] = {
    "jp": [
        ("フェリス", h("2:00"), 50),
        ("バシラ", h("2:30"), 50),
        ("パンナロード", h("3:00"), 50),
        ("エンクラ", h("3:30"), 50),
        ("テンペスト", h("3:30"), 50),
        ("マトゥラ", h("4:00"), 50),
        ("チェルトゥバ", h("3:00"), 50),
        ("ブレカ", h("4:00"), 50),
        ("クイーンアント", h("6:00"), 33),
        ("ヒシルローメ", h("6:00"), 50),
        ("レピロ", h("5:00"), 33),
        ("トロンバ", h("4:30"), 50),
        ("スタン", h("4:00"), 100),
        ("ミュータントクルマ", h("8:00"), 100),
        ("ティミトリス", h("5:00"), 100),   # 旧表記: ディミトリス
        ("汚染したクルマ", h("8:00"), 100),
        ("タルキン", h("5:00"), 50),
        ("ティミニエル", h("8:00"), 100),
        ("グラーキ", h("8:00"), 100),
        ("忘却の鏡", h("12:00"), 100),
        ("ガレス", h("6:00"), 50),
        ("ベヒモス", h("6:00"), 100),
        ("ランドール", h("8:00"), 100),
        ("ケルソス", h("6:00"), 50),
        ("タラキン", h("7:00"), 100),
        ("メデューサ", h("7:00"), 100),
        ("サルカ", h("7:00"), 100),
        ("カタン", h("8:00"), 100),
        ("コアサセプタ", h("12:00"), 33),
        ("ブラックリリー", h("12:00"), 100),
        ("パンドライド", h("8:00"), 100),
        ("サヴァン", h("12:00"), 100),
        ("ドラゴンビースト", h("12:00"), 50),
        ("バルボ", h("8:00"), 50),           # ご指定の「バルポ」を正規名に統一
        ("セル", h("7:30"), 33),
        ("コルーン", h("10:00"), 100),
        ("オルフェン", h("24:00"), 33),
        ("サミュエル", h("12:00"), 100),
        ("アンドラス", h("12:00"), 50),
        ("カプリオ", h("12:00"), 50),
        ("ハーフ", h("24:00"), 33),
        ("フリント", h("8:00"), 33),
    ]
}

# 代表エイリアス（略称→正規名）
PRESET_ALIASES: Dict[str, Dict[str, str]] = {
    "jp": {
        "鏡": "忘却の鏡",
        "汚染": "汚染したクルマ", "おせん": "汚染したクルマ",
        "ニエル": "ティミニエル", "ﾆｴﾙ": "ティミニエル",
        "ディミトリス": "ティミトリス",
        "チェトゥルゥバ": "チェルトゥバ",
        "コア": "コアサセプタ",
        "アント": "クイーンアント",
        "メデュ": "メデューサ",
        "ランド": "ランドール",
        "ベヒ": "ベヒモス",
        "パンナ": "パンナロード",
        "バルポ": "バルボ",
    }
}

@dataclass
class Boss:
    name: str
    interval_minutes: int
    rate: Optional[int] = None   # 0-100
    next_spawn_iso: Optional[str] = None
    skip_count: int = 0
    last_announced_iso: Optional[str] = None
    def next_spawn_dt(self) -> Optional[datetime]:
        return datetime.fromisoformat(self.next_spawn_iso).astimezone(JST) if self.next_spawn_iso else None
    def set_next_spawn(self, dt: datetime):
        self.next_spawn_iso = dt.astimezone(JST).isoformat()

def load_store() -> Dict[str, Boss]:
    if not os.path.exists(DATA_FILE): return {}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        raw = json.load(f)
    # 旧データに rate が無くてもOK
    return {k.lower(): Boss(**v) for k, v in raw.items()}

def save_store(store: Dict[str, Boss]):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump({k: asdict(v) for k, v in store.items()}, f, ensure_ascii=False, indent=2)

def load_aliases() -> Dict[str, str]:
    if not os.path.exists(ALIAS_FILE): return {}
    with open(ALIAS_FILE, "r", encoding="utf-8") as f: return json.load(f)

def save_aliases(aliases: Dict[str, str]):
    with open(ALIAS_FILE, "w", encoding="utf-8") as f:
        json.dump(aliases, f, ensure_ascii=False, indent=2)

def normalize(t: str) -> str:
    s = t.strip().lower()
    for ch in (" ", "　", "・", "/", "／"): s = s.replace(ch, "")
    return s

def parse_hours(token: str) -> Optional[float]:
    t = token.strip().lower()
    m = re.fullmatch(r"(\d+):(\d{2})", t)
    if m: return int(m.group(1)) + int(m.group(2))/60.0
    m = re.search(r"\d+(?:\.\d+)?", t)
    if not m: return None
    val = float(m.group())
    if "m" in t or "分" in t: return val/60.0
    return val

def parse_percent(token: str) -> Optional[int]:
    m = re.search(r"(\d{1,3})\s*[%％]", token)
    return max(0, min(100, int(m.group(1)))) if m else None

def hhmm_to_dt(hhmm: str, base: Optional[datetime] = None) -> Optional[datetime]:
    base = base or datetime.now(JST)
    try:
        hh = int(hhmm[:2]); mm = int(hhmm[2:4])
        return base.replace(hour=hh, minute=mm, second=0, microsecond=0)
    except Exception:
        return None

def fmt_dt(dt: Optional[datetime]) -> str:
    return dt.astimezone(JST).strftime("%m/%d %H:%M") if dt else "—"

def find_announce_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    if ANNOUNCE_CHANNEL_ID:
        ch = guild.get_channel(ANNOUNCE_CHANNEL_ID)
        if isinstance(ch, discord.TextChannel): return ch
    for ch in guild.text_channels:
        me = guild.me or guild.get_member(guild.owner_id)
        if me and ch.permissions_for(me).send_messages: return ch
    return None

def resolve_boss_key(token: str, store: Dict[str, Boss], aliases: Dict[str, str]):
    q = normalize(token)
    if not q: return None, []
    if q in aliases: return aliases[q], []
    if q in store: return q, []
    starts = [k for k in store if normalize(store[k].name).startswith(q)]
    if len(starts) == 1: return starts[0], []
    subs = [k for k in store if q in normalize(store[k].name)]
    if len(subs) == 1: return subs[0], []
    cand = list(dict.fromkeys(starts + subs))[:6]
    return None, [store[k].name for k in cand]

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)

store: Dict[str, Boss] = {}
aliases: Dict[str, str] = {}

@bot.event
async def on_ready():
    global store, aliases
    store = load_store()
    aliases = load_aliases()
    print(f"Logged in as {bot.user}")
    if not ticker.is_running(): ticker.start()

@bot.command(name="help")
async def _help(ctx):
    msg = (
        "**めるる コマンド**\n"
        f"`{PREFIX}preset jp` プリセット一括登録（出現率込み）\n"
        f"`{PREFIX}bulkadd` まとめ登録（<名前> <率%?> <時間> / <名前> <時間> <率%?>）\n"
        f"`{PREFIX}addboss <名> <時間> [率%]` / `{PREFIX}interval <名> <時間>` / `{PREFIX}bosses`\n"
        f"`{PREFIX}alias <別名> <正規名>` / `{PREFIX}aliases`\n"
        f"`{PREFIX}<ボス名>` or `<ボス名> HHMM`（`!`省略OK・エイリアス/部分一致対応）\n"
        f"`{PREFIX}reset HHMM` 全ボスの最終討伐を統一\n"
        f"`{PREFIX}bt` / `{PREFIX}bt3` / `{PREFIX}bt6`\n"
    )
    await ctx.send(msg)

@bot.command(name="addboss")
async def addboss(ctx, name: str, time: str, rate_text: Optional[str] = None):
    hours = parse_hours(time)
    if hours is None: return await ctx.send("時間は `H:MM` / `H` / `Hh` / `Mm` で指定してください。")
    rate = parse_percent(rate_text) if rate_text else None
    k = normalize(name)
    store[k] = Boss(name=name, interval_minutes=int(round(hours*60)), rate=rate)
    save_store(store)
    await ctx.send(f"✅ 追加: {name} (every {hours:.2f}h{'' if rate is None else f', {rate}%'} )")

@bot.command(name="delboss")
async def delboss(ctx, name: str):
    k = normalize(name)
    if k in store: del store[k]; save_store(store); return await ctx.send(f"🗑️ 削除: {name}")
    await ctx.send("未登録のボスです。")

@bot.command(name="interval")
async def set_interval(ctx, name: str, time: str):
    k = normalize(name)
    if k not in store: return await ctx.send("未登録のボスです。")
    hours = parse_hours(time)
    if hours is None: return await ctx.send("時間は `H:MM` / `H` / `Hh` / `Mm` で指定してください。")
    store[k].interval_minutes = int(round(hours*60)); save_store(store)
    await ctx.send(f"⏱️ {store[k].name} interval -> {hours:.2f}h")

@bot.command(name="bosses")
async def bosses(ctx):
    if not store: return await ctx.send("まだボスが登録されていません。")
    lines = ["**登録ボス**"]
    for b in store.values():
        rate = f" ({b.rate}%)" if b.rate is not None else ""
        lines.append(f"・{b.name}{rate} / every {b.interval_minutes/60:.2f}h / next {fmt_dt(b.next_spawn_dt())} / skip {b.skip_count}")
    await ctx.send("\n".join(lines))

@bot.command(name="reset")
async def reset_all(ctx, hhmm: str):
    base = datetime.now(JST)
    kill_time = hhmm_to_dt(hhmm, base=base)
    if not kill_time: return await ctx.send("時刻は HHMM で入力してください。例: 0930")
    for b in store.values():
        next_dt = kill_time + timedelta(minutes=b.interval_minutes)
        b.set_next_spawn(next_dt); b.skip_count = 0; b.last_announced_iso = None
    save_store(store)
    await ctx.send(f"♻️ 全ボスの**最終討伐**を {kill_time.strftime('%H:%M')} に設定 → 各 interval で更新しました。")

@bot.command(name="bt")
async def bt(ctx, hours: Optional[str] = None):
    within = float(hours) if hours else None
    await send_board(ctx.channel, within_hours=within)

@bot.command(name="bt3")
async def bt3(ctx): await send_board(ctx.channel, within_hours=3)

@bot.command(name="bt6")
async def bt6(ctx): await send_board(ctx.channel, within_hours=6)

@bot.command(name="preset")
async def preset(ctx, key: str):
    key = key.lower()
    if key not in PRESET_BOSSES: return await ctx.send("使い方: `!preset jp`")
    added = 0
    for name, hval, rate in PRESET_BOSSES[key]:
        k = normalize(name)
        store[k] = Boss(name=name, interval_minutes=int(round(hval*60)), rate=rate); added += 1
    save_store(store)
    # エイリアスも一括投入
    if key in PRESET_ALIASES:
        for a, target in PRESET_ALIASES[key].items():
            aliases[normalize(a)] = normalize(target)
        save_aliases(aliases)
    await ctx.send(f"📦 プリセット `{key}` を登録: {added}件 追加/更新 & エイリアス設定完了。`!bosses` で確認してね。")

@bot.command(name="bulkadd")
async def bulkadd(ctx, *, body: str = ""):
    content = ctx.message.content
    i = content.lower().find("!bulkadd")
    if i >= 0: body = content[i+len("!bulkadd"):].strip()
    m = re.search(r"```(.*?)```", body, flags=re.DOTALL)
    if m: body = m.group(1).strip()
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    if not lines: return await ctx.send("使い方：`!bulkadd` の次行から `<名前> <率%?> <時間>` で改行列挙。")
    added, failed = 0, []
    for ln in lines:
        parts = ln.split()
        if len(parts) < 2: failed.append(ln); continue
        # トークンから時間と率を見つける
        hour_idx = next((i for i,t in enumerate(parts) if parse_hours(t) is not None), None)
        rate_idx = next((i for i,t in enumerate(parts) if parse_percent(t) is not None), None)
        if hour_idx is None: failed.append(ln); continue
        hours = parse_hours(parts[hour_idx]); rate = parse_percent(parts[rate_idx]) if rate_idx is not None else None
        name = " ".join(p for idx,p in enumerate(parts) if idx not in {hour_idx} | ({rate_idx} if rate_idx is not None else set()))
        if not name: failed.append(ln); continue
        k = normalize(name)
        store[k] = Boss(name=name, interval_minutes=int(round(hours*60)), rate=rate); added += 1
    save_store(store)
    msg = f"✅ 一括登録: {added}件 追加/更新。"
    if failed: msg += f"\n⚠️ 失敗 {len(failed)}行 → 例 `{failed[0]}`（形式: `<名前> <率%?> <時間>`）"
    await ctx.send(msg)

async def send_board(channel: discord.TextChannel, within_hours: Optional[float] = None):
    now = datetime.now(JST)
    rows = [b for b in store.values() if b.next_spawn_dt()]
    rows.sort(key=lambda b: b.next_spawn_dt())
    if within_hours is not None:
        limit = now + timedelta(hours=within_hours)
        rows = [b for b in rows if b.next_spawn_dt() and b.next_spawn_dt() <= limit]
    lines = ["**📜 Boss Timers**" if within_hours is None else f"**📜 Boss Timers (<= {within_hours}h)**"]
    if not rows: lines.append("(該当なし)")
    else:
        for b in rows:
            ns = b.next_spawn_dt()
            remain = ns - now if ns else None
            rem = f"[{int(remain.total_seconds()//3600)}h{int((remain.total_seconds()%3600)//60)}m]" if remain else ""
            rate = f" ({b.rate}%)" if b.rate is not None else ""
            skip = f"【スキップ{b.skip_count}回】" if b.skip_count else ""
            lines.append(f"・{b.name}{rate}{skip} → {fmt_dt(ns)} {rem}")
    await channel.send("\n".join(lines))

@bot.listen("on_message")
async def boss_quick_update(message: discord.Message):
    if message.author.bot or not store: return
    s = message.content.strip()
    if not s: return
    s = s[1:].lstrip() if s.startswith(PREFIX) else s
    parts = s.split()
    if not parts: return
    if normalize(parts[0]) in {"addboss","delboss","interval","reset","bt","bt3","bt6","bosses","help","preset","bulkadd","alias","unalias","aliases"}: return
    key, cand = resolve_boss_key(parts[0], store, aliases)
    if not key:
        if cand: await message.channel.send("🤔 どれですか？ " + " / ".join(cand))
        return
    boss = store[key]; now = datetime.now(JST)
    if len(parts) >= 2 and re.fullmatch(r"\d{4}", parts[1]):
        kill = hhmm_to_dt(parts[1], base=now)
        if not kill: return await message.channel.send("時刻は HHMM で入力してください。例: 0930")
        next_dt = kill + timedelta(minutes=boss.interval_minutes)
        boss.set_next_spawn(next_dt); boss.skip_count=0; boss.last_announced_iso=None; save_store(store)
        return await message.channel.send(f"⚔️ {boss.name} {kill.strftime('%H:%M')} に討伐 → 次回 {fmt_dt(next_dt)}")
    next_dt = now + timedelta(minutes=boss.interval_minutes)
    boss.set_next_spawn(next_dt); boss.skip_count=0; boss.last_announced_iso=None; save_store(store)
    await message.channel.send(f"⚔️ {boss.name} 討伐! 次回 {fmt_dt(next_dt)}")

@tasks.loop(seconds=60.0)
async def ticker():
    now = datetime.now(JST)
    for guild in bot.guilds:
        ch = find_announce_channel(guild)
        if not ch: continue
        for b in list(store.values()):
            ns = b.next_spawn_dt()
            if not ns: continue
            if b.last_announced_iso:
                last = datetime.fromisoformat(b.last_announced_iso).astimezone(JST)
                if last >= now - timedelta(minutes=1): continue
            if ns <= now:
                b.skip_count += 1
                next_dt = ns + timedelta(minutes=b.interval_minutes)
                b.set_next_spawn(next_dt); b.last_announced_iso = now.isoformat(); save_store(store)
                await ch.send(f"⏰ **{b.name}【スキップ{b.skip_count}回】** → 次 {fmt_dt(next_dt)}\n(討伐入力が無かったため自動スキップ。`{PREFIX}{b.name} HHMM` で更新可)")

@ticker.before_loop
async def before_ticker(): await bot.wait_until_ready()

# ── /health と /ping ──
try:
    from aiohttp import web
except Exception:
    web = None

async def start_http_server():
    if web is None: return
    app = web.Application()
    async def health(_): return web.Response(text="ok")   # 小さな本文
    async def ping(_):   return web.Response(status=204)  # 本文なし → cron-job向け
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_get("/ping", ping)  # HEADでもOK（aiohttpが自動処理）
    runner = web.AppRunner(app); await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port); await site.start()
    print(f"HTTP server started on :{port}")

async def amain():
    token = os.getenv("DISCORD_TOKEN")
    if not token: raise SystemExit("Please set DISCORD_TOKEN")
    await asyncio.gather(bot.start(token), start_http_server())

if __name__ == "__main__":
    asyncio.run(amain())
