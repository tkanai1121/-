"""
めるる – Lineage2M Boss Bot (Discord/Python)
--------------------------------
機能:
- ボス名だけのクイック入力は `!` 省略OK（例: `グラーキ 1159` / `グラーキ`）
- `ボス名 HHMM` は「HH:MM に討伐」解釈（次回 = 討伐時刻 + interval）
- `!reset HHMM` は「全ボスの**最終討伐時間**を HH:MM に統一」（次回は各 interval で更新）
- 一括登録:
    - プリセット: `!preset jp`（日本向けフィールドボスまとめ登録）
    - 任意リスト: `!bulkadd` の下に複数行で `<名前> <時間>` を貼る
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
ANNOUNCE_CHANNEL_ID = int(os.getenv("ANNOUNCE_CHANNEL_ID", "0"))

# =========================
# Presets (JP field bosses)
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
        # オーレン（ランダムのフライン系は除外）
        ("タルキン", 8), ("セル", 12), ("バルボ", 12), ("ティミニエル", 8), ("レピロ", 7), ("オルフェン", 24),
        ("コルーン", 12), ("サミュエル", 12),
        # アデン
        ("忘却の鏡", 11), ("ヒシルローメ", 6), ("ランドール", 9), ("グラーキ", 8), ("オルクス", 24), ("カプリオ", 12),
        ("フリント", 5), ("ハーフ", 20), ("アンドラス", 15), ("タナトス", 25), ("ラーハ", 33), ("フェニックス", 24),
    ]
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

# =========================
# Utilities
# =========================

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


def normalize(text: str) -> str:
    return text.strip().lower()


def parse_boss_quick(content: str, boss_keys: List[str]) -> Optional[Tuple[str, Optional[str]]]:
    """`グラーキ 1159` / `!グラーキ 1159` / `グラーキ` / `!グラーキ` を検出。
    戻り値: (boss_key, hhmm or None)
    """
    s = content.strip()
    if not s:
        return None
    # `!` はあってもなくてもよい（先頭1個のみ許可）
    if s.startswith(PREFIX):
        s = s[len(PREFIX):].lstrip()
    # 先頭トークンがボス名か？
    parts = s.split()
    if not parts:
        return None
    key = normalize(parts[0])
    if key not in boss_keys:
        return None
    hhmm = None
    if len(parts) >= 2:
        m = re.fullmatch(r"(\d{4})", parts[1])
        if m:
            hhmm = m.group(1)
    return key, hhmm


def parse_hours(token: str) -> Optional[float]:
    """'8' '8h' '8時間' '1.5' を数値として解釈"""
    m = re.search(r"\d+(?:\.\d+)?", token)
    return float(m.group()) if m else None

# =========================
# Bot Setup
# =========================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)

store: Dict[str, Boss] = {}

# =========================
# Events
# =========================
@bot.event
async def on_ready():
    global store
    store = load_store()
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
        f"プレフィックス: `{PREFIX}`（※**ボス名だけは `!` 省略OK**）\n\n"
        "**登録/設定**\n"
        f"`{PREFIX}addboss <Name> <hours>` 例: `{PREFIX}addboss グラーキ 8`\n"
        f"`{PREFIX}delboss <Name>`\n"
        f"`{PREFIX}interval <Name> <hours>`\n"
        f"`{PREFIX}bosses` 登録済みボス一覧\n"
        f"`{PREFIX}preset jp` 定義済みの日本向けフィールドボスを**一括登録**\n"
        f"`{PREFIX}bulkadd <複数行>` まとめて登録（下の使い方を参照）\n\n"
        "**更新/リセット**\n"
        f"`{PREFIX}<BossName>` 討伐(今) → 次回=今+interval  ※`!`省略可\n"
        f"`{PREFIX}<BossName> HHMM` 例: `{PREFIX}グラーキ 1159` = **11:59に討伐 → 次回=+interval**  ※`!`省略可\n"
        f"`{PREFIX}reset HHMM` **全ボスの最終討伐時間**を HH:MM に統一（= 各ボスの次回は *討伐時刻 + interval*）。こちらは `!` 必須。\n\n"
        "**表示**\n"
        f"`{PREFIX}bt [N]` 例: `{PREFIX}bt`, `{PREFIX}bt 3`\n"
        f"`{PREFIX}bt3` / `{PREFIX}bt6`\n\n"
        "**bulkadd の使い方**\n"
        "1メッセージで改行区切りで貼り付け:\n"
        "```\n"
        "!bulkadd\n"
        "グラーキ 8\n"
        "エンクラ 6\n"
        "チェルトゥバ 6\n"
        "```\n"
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
    # 全ボスの「最終討伐」を指定時刻に統一 → 次回 = 討伐時刻 + interval
    for b in store.values():
        next_dt = kill_time + timedelta(minutes=b.interval_minutes)
        b.set_next_spawn(next_dt)
        b.skip_count = 0
        b.last_announced_iso = None
    save_store(store)
    await ctx.send(f"♻️ 全ボスの**最終討伐**を {kill_time.strftime('%H:%M')} に設定 → 次回は各ボスの interval で更新しました。")


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
    await ctx.send(f"📦 プリセット `{key}` を登録: {added}件 追加/更新しました。(`!bosses` で確認)")


@bot.command(name="bulkadd")
async def bulkadd(ctx: commands.Context, *, body: str = ""):
    """
    改行区切りで <名前> <時間> をまとめて登録。例:
    !bulkadd
    グラーキ 8
    エンクラ 6
    """
    # コマンド全文から手動で切り出す（複数行対応）
    content = ctx.message.content
    # 先頭の "!bulkadd" を除去
    idx = content.lower().find("!bulkadd")
    if idx >= 0:
        body = content[idx + len("!bulkadd"):].strip()
    # コードブロックがあれば中身だけ抽出
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
    parsed = parse_boss_quick(message.content, list(store.keys()))
    if not parsed:
        return
    key, hhmm = parsed
    now = datetime.now(JST)
    boss = store[key]

    if hhmm:  # HHMM で討伐時刻を指定 → 次回 = 討伐時刻 + interval
        kill_time = hhmm_to_dt(hhmm, base=now)
        if not kill_time:
            await message.channel.send("時刻は HHMM で入力してください。例: 0930")
            return
        next_dt = kill_time + timedelta(minutes=boss.interval_minutes)
        boss.set_next_spawn(next_dt)
        boss.skip_count = 0
        boss.last_announced_iso = None
        save_store(store)
        await message.channel.send(f"⚔️ {boss.name} {kill_time.strftime('%H:%M')} に討伐 → 次回 {fmt_dt(next_dt)}")
        return

    # 時刻なしは「今討伐」扱い
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
