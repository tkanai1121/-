"""
めるる – Lineage2M Boss Bot (Discord/Python)
--------------------------------
機能ハイライト:
- ボス名だけのクイック入力は `!` 省略OK（例: `グラーキ 1159` / `鏡 0930`）
- `ボス名 HHMM` は「HH:MM に討伐」解釈（次回 = 討伐時刻 + interval）
- `!reset HHMM` は「全ボスの**最終討伐時間**を HH:MM に統一」（次回は各 interval で更新）
- 一括登録:
    - プリセット: `!preset jp`（日本向けフィールドボスまとめ登録＋代表エイリアス）
    - 任意リスト: `!bulkadd` の下に複数行で `<名前> <時間>` を貼る
- **エイリアス（別名）**:
    - 追加: `!alias <別名> <正式名>` 例: `!alias 鏡 忘却の鏡`
    - 解除: `!unalias <別名>`
    - 一覧: `!aliases`
- クイック入力は **エイリアス優先 → 前方一致 → 部分一致** の順で解決（曖昧なら候補提示）
- /health で軽量HTTPサーバー（Render等の監視用）

Python 3.11.x 推奨（Render の Environment に PYTHON_VERSION=3.11.9）。
"""

import asyncio
import json
import os
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from typing import Dict, Optional, List, Tuple

import discord
from discord.ext import commands, tasks
from dateutil.tz import gettz

# =========================
# Config
# =========================
PREFIX = "!"
JST = gettz("Asia/Tokyo")
DATA_FILE = "bosses.json"
ALIAS_FILE = "aliases.json"
ANNOUNCE_CHANNEL_ID = int(os.getenv("ANNOUNCE_CHANNEL_ID", "0"))

# =========================
# Presets (JP field bosses + default aliases)
# =========================
PRESET_BOSSES: Dict[str, List[Tuple[str, float]]] = {
    "jp": [
        # グルーディオ
        ("チェルトゥバ", 6), ("バシラ", 4), ("ケルソス", 10), ("サヴァン", 12), ("クイーンアント", 6), ("トロンバ", 7),
        # ディオン
        ("フェリス", 3), ("タラキン", 10), ("エンクラ", 6), ("パンドライド", 12), ("ミュータントクルマ", 8),
        ("テンペスト", 6), ("汚染したクルマ", 8), ("カタン", 10), ("コアサセプタ", 10),
        ("サルカ", 10), ("ディミトリス", 12), ("スタン", 7), ("ガレス", 9),
        # ギラン
        ("メデューサ", 10), ("ブラックリリー", 12), ("マトゥラ", 6), ("ブレカ", 6), ("パンナロード", 5), ("ベヒモス", 9),
        ("ドラゴンビースト", 12),
        # オーレン（フライン系はランダムのため除外）
        ("タルキン", 8), ("セル", 12), ("バルボ", 12), ("ティミニエル", 8), ("レピロ", 7), ("オルフェン", 24),
        ("コルーン", 12), ("サミュエル", 12),
        # アデン
        ("忘却の鏡", 11), ("ヒシルローメ", 6), ("ランドール", 9), ("グラーキ", 8), ("オルクス", 24), ("カプリオ", 12),
        ("フリント", 5), ("ハーフ", 20), ("アンドラス", 15), ("タナトス", 25), ("ラーハ", 33), ("フェニックス", 24),
    ]
}

# 代表的なエイリアス（※必要に応じて自分で追加/編集OK）
PRESET_ALIASES: Dict[str, Dict[str, str]] = {
    "jp": {
        "鏡": "忘却の鏡",
        "汚染": "汚染したクルマ", "おせん": "汚染したクルマ",
        "ﾆｴﾙ": "ティミニエル", "ニエル": "ティミニエル",
        "コア": "コアサセプタ",
        "アント": "クイーンアント",
        "メデュ": "メデューサ",
        "ランド": "ランドール",
        "ベヒ": "ベヒモス",
        "パンナ": "パンナロード",
        "クイーンアント": "QA",
    }
}

# =========================
# Models & Storage
# =========================
@dataclass
class Boss:
    name: str
    interval_minutes: int
    next_spawn_iso: Optional[str] = None
    skip_count: int = 0
    last_announced_iso: Optional[str] = None

    def next_spawn_dt(self) -> Optional[datetime]:
        return datetime.fromisoformat(self.next_spawn_iso).astimezone(JST) if self.next_spawn_iso else None

    def set_next_spawn(self, dt: datetime):
        self.next_spawn_iso = dt.astimezone(JST).isoformat()


def load_store() -> Dict[str, Boss]:
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return {k.lower(): Boss(**v) for k, v in raw.items()}


def save_store(store: Dict[str, Boss]):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump({k: asdict(v) for k, v in store.items()}, f, ensure_ascii=False, indent=2)

# --- aliases ---
def load_aliases() -> Dict[str, str]:
    if not os.path.exists(ALIAS_FILE):
        return {}
    with open(ALIAS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_aliases(aliases: Dict[str, str]):
    with open(ALIAS_FILE, "w", encoding="utf-8") as f:
        json.dump(aliases, f, ensure_ascii=False, indent=2)

# =========================
# Utilities
# =========================
def normalize(text: str) -> str:
    t = text.strip().lower()
    for ch in (" ", "　", "・", "/", "／"):
        t = t.replace(ch, "")
    return t

def hhmm_to_dt(hhmm: str, base: Optional[datetime] = None) -> Optional[datetime]:
    if base is None:
        base = datetime.now(JST)
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
        if isinstance(ch, discord.TextChannel):
            return ch
    for ch in guild.text_channels:
        me = guild.me or guild.get_member(guild.owner_id)
        if me and ch.permissions_for(me).send_messages:
            return ch
    return None

def parse_hours(token: str) -> Optional[float]:
    m = re.search(r"\d+(?:\.\d+)?", token)
    return float(m.group()) if m else None

def resolve_boss_key(token: str, store: Dict[str, Boss], aliases: Dict[str, str]) -> Tuple[Optional[str], List[str]]:
    """
    入力トークンを「エイリアス→前方一致→部分一致」で解決。
    返り値: (決定キー or None, 曖昧候補リスト)
    """
    q = normalize(token)
    if not q:
        return None, []
    # 1) alias
    if q in aliases:
        return aliases[q], []
    # 2) exact key
    if q in store:
        return q, []
    # 3) 前方一致
    starts = [k for k in store if normalize(store[k].name).startswith(q)]
    if len(starts) == 1:
        return starts[0], []
    # 4) 部分一致
    subs = [k for k in store if q in normalize(store[k].name)]
    if len(subs) == 1:
        return subs[0], []
    # 曖昧
    # 候補を（前方一致 > 部分一致）の順で提示
    cand = list(dict.fromkeys(starts + subs))[:6]
    return None, [store[k].name for k in cand]

# =========================
# Bot Setup
# =========================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)

store: Dict[str, Boss] = {}
aliases: Dict[str, str] = {}

# =========================
# Events
# =========================
@bot.event
async def on_ready():
    global store, aliases
    store = load_store()
    aliases = load_aliases()
    print(f"Logged in as {bot.user}")
    if not ticker.is_running():
        ticker.start()

# =========================
# Commands (prefix必須)
# =========================
@bot.command(name="help")
async def _help(ctx: commands.Context):
    msg = (
        "**めるる コマンド**\n"
        f"プレフィックス: `{PREFIX}`（※**ボス名だけは `!` 省略OK** / エイリアス & 部分一致対応）\n\n"
        "**登録/設定**\n"
        f"`{PREFIX}addboss <Name> <hours>` 例: `{PREFIX}addboss グラーキ 8`\n"
        f"`{PREFIX}delboss <Name>`\n"
        f"`{PREFIX}interval <Name> <hours>`\n"
        f"`{PREFIX}bosses` 登録済みボス一覧\n"
        f"`{PREFIX}preset jp` プリセット一括登録（＋代表エイリアス）\n"
        f"`{PREFIX}bulkadd <複数行>` まとめて登録\n\n"
        "**更新/リセット**\n"
        f"`{PREFIX}<BossName>` 討伐(今) → 次回=今+interval  ※`!`省略可\n"
        f"`{PREFIX}<BossName> HHMM` 例: `{PREFIX}鏡 1159` = **11:59に討伐 → 次回=+interval**\n"
        f"`{PREFIX}reset HHMM` **全ボスの最終討伐時間**を HH:MM に統一\n\n"
        "**表示**\n"
        f"`{PREFIX}bt [N]` / `{PREFIX}bt3` / `{PREFIX}bt6`\n\n"
        "**エイリアス**\n"
        f"`{PREFIX}alias <別名> <正式名>` / `{PREFIX}unalias <別名>` / `{PREFIX}aliases`\n"
    )
    await ctx.send(msg)

@bot.command(name="addboss")
async def addboss(ctx: commands.Context, name: str, hours: str):
    try:
        interval_h = float(hours)
    except ValueError:
        return await ctx.send("時間は数値で指定してください (例: 8 または 1.5)")
    key = normalize(name)
    store[key] = Boss(name=name, interval_minutes=int(round(interval_h * 60)))
    save_store(store)
    await ctx.send(f"✅ 追加: {name} (リスポーン {interval_h}h)")

@bot.command(name="delboss")
async def delboss(ctx: commands.Context, name: str):
    key = normalize(name)
    if key in store:
        del store[key]
        save_store(store)
        await ctx.send(f"🗑️ 削除: {name}")
    else:
        await ctx.send("未登録のボスです。")

@bot.command(name="interval")
async def set_interval(ctx: commands.Context, name: str, hours: str):
    key = normalize(name)
    if key not in store:
        return await ctx.send("未登録のボスです。まず `!addboss` してください。")
    try:
        interval_h = float(hours)
    except ValueError:
        return await ctx.send("時間は数値で指定してください (例: 8 または 1.5)")
    store[key].interval_minutes = int(round(interval_h * 60))
    save_store(store)
    await ctx.send(f"⏱️ {store[key].name} interval -> {interval_h}h")

@bot.command(name="bosses")
async def bosses(ctx: commands.Context):
    if not store:
        return await ctx.send("まだボスが登録されていません。`!addboss` で追加してください。")
    lines = ["**登録ボス**"]
    for b in store.values():
        lines.append(f"・{b.name}  / every {b.interval_minutes/60:.2f}h  / next {fmt_dt(b.next_spawn_dt())}  / skip {b.skip_count}")
    await ctx.send("\n".join(lines))

@bot.command(name="reset")
async def reset_all(ctx: commands.Context, hhmm: str):
    base = datetime.now(JST)
    kill_time = hhmm_to_dt(hhmm, base=base)
    if not kill_time:
        return await ctx.send("時刻は HHMM で入力してください。例: 0930")
    for b in store.values():
        next_dt = kill_time + timedelta(minutes=b.interval_minutes)
        b.set_next_spawn(next_dt)
        b.skip_count = 0
        b.last_announced_iso = None
    save_store(store)
    await ctx.send(f"♻️ 全ボスの**最終討伐**を {kill_time.strftime('%H:%M')} に設定 → 次回は各 interval で更新しました。")

@bot.command(name="bt")
async def bt(ctx: commands.Context, hours: Optional[str] = None):
    within: Optional[float] = None
    if hours:
        try:
            within = float(hours)
        except ValueError:
            return await ctx.send("使い方: `!bt` または `!bt 3` (3時間以内)")
    await send_board(ctx.channel, within_hours=within)

@bot.command(name="bt3")
async def bt3(ctx: commands.Context):
    await send_board(ctx.channel, within_hours=3)

@bot.command(name="bt6")
async def bt6(ctx: commands.Context):
    await send_board(ctx.channel, within_hours=6)

@bot.command(name="preset")
async def preset(ctx: commands.Context, key: str):
    key = key.lower()
    if key not in PRESET_BOSSES:
        return await ctx.send("使い方: `!preset jp`")
    added = 0
    for name, h in PRESET_BOSSES[key]:
        k = normalize(name)
        store[k] = Boss(name=name, interval_minutes=int(round(h * 60)))
        added += 1
    save_store(store)
    # 代表エイリアスも一括登録
    if key in PRESET_ALIASES:
        for a, tgt in PRESET_ALIASES[key].items():
            ak = normalize(a)
            tk, _ = resolve_boss_key(tgt, store, aliases)
            if tk:
                aliases[ak] = tk
        save_aliases(aliases)
    await ctx.send(f"📦 プリセット `{key}` を登録: {added}件 追加/更新しました。エイリアスも設定済みです。(`!aliases` で確認)")

@bot.command(name="bulkadd")
async def bulkadd(ctx: commands.Context, *, body: str = ""):
    """
    改行区切りで <名前> <時間> をまとめて登録。例:
    !bulkadd
    グラーキ 8
    エンクラ 6
    """
    content = ctx.message.content
    idx = content.lower().find("!bulkadd")
    if idx >= 0:
        body = content[idx + len("!bulkadd"):].strip()
    m = re.search(r"```(.*?)```", body, flags=re.DOTALL)
    if m:
        body = m.group(1).strip()
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    if not lines:
        return await ctx.send("使い方：`!bulkadd` の次の行から「<名前> <時間>」を改行で並べて送ってください。")
    added, failed = 0, []
    for ln in lines:
        parts = ln.split()
        if len(parts) < 2:
            failed.append(ln); continue
        hours_val = parse_hours(parts[-1])
        name = " ".join(parts[:-1])
        if hours_val is None or not name:
            failed.append(ln); continue
        key = normalize(name)
        store[key] = Boss(name=name, interval_minutes=int(round(hours_val * 60)))
        added += 1
    save_store(store)
    msg = f"✅ 一括登録: {added}件 追加/更新しました。"
    if failed:
        msg += f"\n⚠️ 失敗: {len(failed)}行 → `{failed[0]}` など（形式: `<名前> <時間>`）"
    await ctx.send(msg)

# --- aliases commands ---
@bot.command(name="alias")
async def alias_add(ctx: commands.Context, alias: str, *, target: str):
    tk, cand = resolve_boss_key(target, store, aliases)
    if not tk:
        if cand:
            return await ctx.send("曖昧です。候補: " + " / ".join(cand))
        return await ctx.send("そのボスが見つかりません。`!bosses` で確認してください。")
    aliases[normalize(alias)] = tk
    save_aliases(aliases)
    await ctx.send(f"🔗 エイリアス登録: **{alias}** → **{store[tk].name}**")

@bot.command(name="unalias")
async def alias_del(ctx: commands.Context, alias: str):
    ak = normalize(alias)
    if ak in aliases:
        del aliases[ak]
        save_aliases(aliases)
        return await ctx.send(f"🗑️ エイリアス削除: {alias}")
    await ctx.send("そのエイリアスは登録されていません。")

@bot.command(name="aliases")
async def alias_list(ctx: commands.Context):
    if not aliases:
        return await ctx.send("エイリアスはまだ登録されていません。`!alias 別名 正式名` で追加できます。")
    items = [f"・{a} → {store[k].name}" for a, k in aliases.items() if k in store]
    await ctx.send("**エイリアス一覧**\n" + "\n".join(sorted(items)))

# =========================
# 出力系
# =========================
async def send_board(channel: discord.TextChannel, within_hours: Optional[float] = None):
    now = datetime.now(JST)
    rows: List[Boss] = [b for b in store.values() if b.next_spawn_dt()]
    rows.sort(key=lambda b: b.next_spawn_dt())
    lines = []
    header = "**📜 Boss Timers**" if within_hours is None else f"**📜 Boss Timers (<= {within_hours}h)**"
    lines.append(header)
    if within_hours is not None:
        limit = now + timedelta(hours=within_hours)
        rows = [b for b in rows if b.next_spawn_dt() and b.next_spawn_dt() <= limit]
    if not rows:
        lines.append("(該当なし)")
    else:
        for b in rows:
            ns = b.next_spawn_dt()
            remain = ns - now if ns else None
            rem_s = f"[{int(remain.total_seconds()//3600)}h{int((remain.total_seconds()%3600)//60)}m]" if remain else ""
            skip = f"【スキップ{b.skip_count}回】" if b.skip_count else ""
            lines.append(f"・{b.name}{skip} → {fmt_dt(ns)} {rem_s}")
    await channel.send("\n".join(lines))

# =========================
# Quick input (ボス名だけは `!` 省略OK)
# =========================
@bot.listen("on_message")
async def boss_quick_update(message: discord.Message):
    if message.author.bot:
        return
    if not store:
        return
    # 先頭トークンを解析
    content = message.content.strip()
    if not content:
        return
    # `!` 先頭なら剥がす（通常コマンドは別ハンドラに任せる）
    s = content[1:].lstrip() if content.startswith(PREFIX) else content
    parts = s.split()
    if not parts:
        return
    name_token = parts[0]
    # 通常コマンド名ならスキップ
    if normalize(name_token) in {"addboss","delboss","interval","reset","bt","bt3","bt6","bosses","help","preset","bulkadd","alias","unalias","aliases"}:
        return
    key, cand = resolve_boss_key(name_token, store, aliases)
    if not key:
        if cand:
            await message.channel.send("🤔 どれですか？ " + " / ".join(cand))
        return
    boss = store[key]
    now = datetime.now(JST)
    if len(parts) >= 2 and re.fullmatch(r"\d{4}", parts[1]):
        kill_time = hhmm_to_dt(parts[1], base=now)
        if not kill_time:
            return await message.channel.send("時刻は HHMM で入力してください。例: 0930")
        next_dt = kill_time + timedelta(minutes=boss.interval_minutes)
        boss.set_next_spawn(next_dt)
        boss.skip_count = 0
        boss.last_announced_iso = None
        save_store(store)
        return await message.channel.send(f"⚔️ {boss.name} {kill_time.strftime('%H:%M')} に討伐 → 次回 {fmt_dt(next_dt)}")
    # 時刻なしは今討伐
    next_dt = now + timedelta(minutes=boss.interval_minutes)
    boss.set_next_spawn(next_dt)
    boss.skip_count = 0
    boss.last_announced_iso = None
    save_store(store)
    await message.channel.send(f"⚔️ {boss.name} 討伐! 次回 {fmt_dt(next_dt)}")

# =========================
# Background ticker – checks every 60s
# =========================
@tasks.loop(seconds=60.0)
async def ticker():
    now = datetime.now(JST)
    for guild in bot.guilds:
        channel = find_announce_channel(guild)
        if not channel:
            continue
        for b in list(store.values()):
            ns = b.next_spawn_dt()
            if not ns:
                continue
            if b.last_announced_iso:
                last = datetime.fromisoformat(b.last_announced_iso).astimezone(JST)
                if last >= now - timedelta(minutes=1):
                    continue
            if ns <= now:
                b.skip_count += 1
                next_dt = ns + timedelta(minutes=b.interval_minutes)
                b.set_next_spawn(next_dt)
                b.last_announced_iso = now.isoformat()
                save_store(store)
                await channel.send(
                    f"⏰ **{b.name}【スキップ{b.skip_count}回】** → 次 {fmt_dt(next_dt)}\n"
                    f"(討伐入力が無かったため自動スキップしました。`{PREFIX}{b.name} HHMM` または `{PREFIX}{b.name}` で更新可)"
                )

@ticker.before_loop
async def before_ticker():
    await bot.wait_until_ready()

# =========================
# HTTP keep-alive server (for Render/UptimeRobot)
# =========================
try:
    from aiohttp import web
except Exception:
    web = None

async def start_http_server():
    if web is None:
        return
    app = web.Application()

    async def health(_):
        return web.Response(text="ok")

    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"HTTP server started on :{port}")

# =========================
# Bootstrap
# =========================
async def amain():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Please set DISCORD_TOKEN environment variable")
    await asyncio.gather(
        bot.start(token),
        start_http_server(),
    )

if __name__ == "__main__":
    asyncio.run(amain())
