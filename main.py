"""
めるる – Lineage2M Boss Bot (Discord/Python)
--------------------------------
Features implemented per request:
- 24/7 loop-friendly design (use any always-on host; see README at bottom of file)
- Free to run (works on Fly.io/Render/Railway free tiers or a spare PC/Raspberry Pi)
- Boss defeat input updates next spawn automatically
- "BossName HHMM" sets next spawn to HH:MM (today or next day if past)
- "reset HHMM" sets ALL bosses' next spawn to HH:MM
- "bt" shows all upcoming spawns; "bt3"/"bt6" filter within N hours (also "!bt 3")
- If no defeat input arrives by spawn time, auto-skip and roll to the next cycle, with counter like BossName【スキップn回】
- Timezone fixed to Asia/Tokyo
- Persistent storage in JSON (bosses.json)

Commands (prefix "!")
!addboss <Name> <hours>         # Add a boss with respawn interval (hours, can be decimal)
!delboss <Name>                  # Remove a boss
!interval <Name> <hours>         # Change respawn interval
!BossName                        # Mark boss defeated now -> next spawn = now + interval
!BossName HHMM                   # Set boss next spawn to HH:MM (today/tomorrow)
!reset HHMM                      # Set ALL bosses next spawn to HH:MM (today/tomorrow)
!bt [N]                          # List upcoming spawns; if N provided show within N hours
!bt3 / !bt6                      # Shorthand filters (3 or 6 hours)
!bosses                          # List all bosses + interval
!help                            # Show help

Environment variables:
DISCORD_TOKEN   = your Discord bot token
ANNOUNCE_CHANNEL_ID = channel ID for spawn announcements (optional; if omitted, bot uses first text channel it can send to)
TZ              = Asia/Tokyo (optional; we force Asia/Tokyo in code regardless)

Requirements (requirements.txt):
discord.py==2.4.0
python-dateutil==2.9.0.post0
aiohttp==3.9.5
"""

import asyncio
import json
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, time
from typing import Dict, Optional, List

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
# Models & Storage
# =========================
@dataclass
class Boss:
    name: str
    interval_minutes: int
    next_spawn_iso: Optional[str] = None
    skip_count: int = 0
    last_announced_iso: Optional[str] = None  # prevent duplicate announce within same minute

    def next_spawn_dt(self) -> Optional[datetime]:
        return (
            datetime.fromisoformat(self.next_spawn_iso).astimezone(JST)
            if self.next_spawn_iso
            else None
        )

    def set_next_spawn(self, dt: datetime):
        self.next_spawn_iso = dt.astimezone(JST).isoformat()

    def due(self, now: datetime) -> bool:
        ns = self.next_spawn_dt()
        return bool(ns and ns <= now)


def load_store() -> Dict[str, Boss]:
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        raw = json.load(f)
    bosses: Dict[str, Boss] = {}
    for k, v in raw.items():
        bosses[k.lower()] = Boss(**v)
    return bosses


def save_store(store: Dict[str, Boss]):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump({k: asdict(v) for k, v in store.items()}, f, ensure_ascii=False, indent=2)


# =========================
# Utilities
# =========================

def hhmm_to_dt(hhmm: str, base: Optional[datetime] = None) -> Optional[datetime]:
    """Parse "HHMM" into a JST datetime today (or tomorrow if past)."""
    if base is None:
        base = datetime.now(JST)
    try:
        hh = int(hhmm[:2])
        mm = int(hhmm[2:4])
        target = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target < base:
            target += timedelta(days=1)
        return target
    except Exception:
        return None


def fmt_dt(dt: Optional[datetime]) -> str:
    return dt.astimezone(JST).strftime("%m/%d %H:%M") if dt else "—"


def find_announce_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    if ANNOUNCE_CHANNEL_ID:
        ch = guild.get_channel(ANNOUNCE_CHANNEL_ID)
        if isinstance(ch, discord.TextChannel):
            return ch
    # fallback: first text channel we can talk in
    for ch in guild.text_channels:
        if ch.permissions_for(guild.me).send_messages:
            return ch
    return None


# =========================
# Bot Setup
# =========================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)

store: Dict[str, Boss] = {}


@bot.event
async def on_ready():
    global store
    store = load_store()
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("Guilds:", [g.name for g in bot.guilds])
    if not ticker.is_running():
        ticker.start()


# =========================
# Core Commands
# =========================

@bot.command(name="help")
async def _help(ctx: commands.Context):
    msg = (
        "**めるる コマンド**\n"
        f"プレフィックス: `{PREFIX}`\n\n"
        "**登録/設定**\n"
        f"`{PREFIX}addboss <Name> <hours>` 例: `{PREFIX}addboss アナキム 8`\n"
        f"`{PREFIX}delboss <Name>`\n"
        f"`{PREFIX}interval <Name> <hours>`\n"
        f"`{PREFIX}bosses` 登録済みボス一覧\n\n"
        "**更新/リセット**\n"
        f"`{PREFIX}<BossName>` 討伐入力(今) → 次回=今+interval\n"
        f"`{PREFIX}<BossName> HHMM` 例: `{PREFIX}アナキム 2130`\n"
        f"`{PREFIX}reset HHMM` 全ボスをその時刻へ\n\n"
        "**表示**\n"
        f"`{PREFIX}bt [N]` 例: `{PREFIX}bt`, `{PREFIX}bt 3`\n"
        f"`{PREFIX}bt3` / `{PREFIX}bt6`\n\n"
        "**自動スキップ**\n"
        "スポーン時刻までに討伐入力が無い場合、1サイクル自動スキップして再告知。`【スキップn回】`をタイトルに付与します。\n"
    )
    await ctx.send(msg)


@bot.command(name="addboss")
async def addboss(ctx: commands.Context, name: str, hours: str):
    try:
        interval_h = float(hours)
    except ValueError:
        return await ctx.send("時間は数値で指定してください (例: 8 または 1.5)")
    key = name.lower()
    store[key] = Boss(name=name, interval_minutes=int(round(interval_h * 60)))
    save_store(store)
    await ctx.send(f"✅ 追加: {name} (リスポーン {interval_h}h)")


@bot.command(name="delboss")
async def delboss(ctx: commands.Context, name: str):
    key = name.lower()
    if key in store:
        del store[key]
        save_store(store)
        await ctx.send(f"🗑️ 削除: {name}")
    else:
        await ctx.send("未登録のボスです。")


@bot.command(name="interval")
async def set_interval(ctx: commands.Context, name: str, hours: str):
    key = name.lower()
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
    target = hhmm_to_dt(hhmm)
    if not target:
        return await ctx.send("時刻は HHMM で入力してください。例: 0930")
    for b in store.values():
        b.set_next_spawn(target)
        b.skip_count = 0
        b.last_announced_iso = None
    save_store(store)
    await ctx.send(f"♻️ 全ボスの次回を {fmt_dt(target)} にリセットしました。")


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
# Message Hook for "!<Boss>" and "!<Boss> HHMM"
# =========================

@bot.listen("on_message")
async def boss_quick_update(message: discord.Message):
    if message.author.bot:
        return
    if not message.content.startswith(PREFIX):
        return
    # ignore if it's a known command name
    parts = message.content[len(PREFIX):].strip().split()
    if not parts:
        return
    cmd = parts[0].lower()
    known = {"addboss","delboss","interval","reset","bt","bt3","bt6","bosses","help"}
    if cmd in known:
        return
    # treat as BossName [HHMM]
    key = cmd
    if key not in store:
        return  # silently ignore unknown boss aliases so normal chat isn't polluted

    now = datetime.now(JST)
    boss = store[key]

    # If HHMM provided -> set next spawn to that specific time
    if len(parts) >= 2:
        hhmm = parts[1]
        target = hhmm_to_dt(hhmm, base=now)
        if not target:
            await message.channel.send("時刻は HHMM で入力してください。例: 0930")
            return
        boss.set_next_spawn(target)
        boss.skip_count = 0
        boss.last_announced_iso = None
        save_store(store)
        await message.channel.send(f"🕘 {boss.name} 次回スポーンを {fmt_dt(target)} に設定しました。")
        return

    # Otherwise: defeat now -> next = now + interval
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
            # Dedup announce within the same minute
            if b.last_announced_iso:
                last = datetime.fromisoformat(b.last_announced_iso).astimezone(JST)
                if last >= now - timedelta(minutes=1):
                    continue
            # If due or overdue
            if ns <= now:
                # No defeat input -> auto-skip 1 cycle and re-announce
                b.skip_count += 1
                next_dt = ns + timedelta(minutes=b.interval_minutes)
                b.set_next_spawn(next_dt)
                b.last_announced_iso = now.isoformat()
                save_store(store)
                try:
                    await channel.send(
                        f"⏰ **{b.name}【スキップ{b.skip_count}回】** → 次 {fmt_dt(next_dt)}\n"
                        f"(討伐入力が無かったため自動スキップしました。`!{b.name} HHMM` または `!{b.name}` で更新可)"
                    )
                except Exception as e:
                    print("announce error:", e)


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
    """Start a tiny HTTP server so external pingers (UptimeRobot) can hit /health.
    Render の無料 Web サービスは 15分無通信でスリープするため、
    5分おきに外部から叩いてもらう想定です。
    """
    if web is None:
        return  # aiohttp 未インストールでもBOT自体は動かす
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
    # Run Discord bot and HTTP server concurrently
    await asyncio.gather(
        bot.start(token),
        start_http_server(),
    )

if __name__ == "__main__":
    asyncio.run(amain())


# =========================
# README (quick start)
# =========================
"""
1) Discord 側の準備
- https://discord.com/developers/applications で新規アプリ作成 → Bot を追加 → Token をコピー
- PRIVILEGED INTENTS: "MESSAGE CONTENT INTENT" を ON
- OAuth2 → URL Generator → scopes: bot, permissions: Send Messages, Read Messages → 生成URLでサーバーに招待

2) ローカル実行 (Windows/Mac/Linux)
python -m venv .venv
. .venv/bin/activate  (Windows: .venv\\Scripts\\activate)
pip install -r requirements.txt   # 下記参照
set DISCORD_TOKEN=xxxx   (PowerShell: $env:DISCORD_TOKEN="xxxx")
# 任意: アナウンス用チャンネルを固定したい場合
set ANNOUNCE_CHANNEL_ID=123456789012345678
python main.py

requirements.txt:
--------------------------------
discord.py==2.4.0
python-dateutil==2.9.0.post0
--------------------------------

3) 初期セットアップ (Discord内)
!addboss アナキム 8
!addboss リリス 8
!addboss コア 12
!bosses
!reset 0900     # 全ボスの次回スポーンを 09:00 に仮置き

4) 使い方例
- 討伐したら → `!アナキム` (今+8h)
- 固定の時刻に変えたい → `!アナキム 2130`
- 全部同じ時刻にしたい → `!reset 0000`
- 一覧 → `!bt` / 3時間以内 → `!bt3` / 6時間以内 → `!bt 6`

5) 24時間無料運用のコツ
- 家の常時起動PC/古いノート/Raspberry Piで動かすのが確実に無料。
- クラウド無料枠 (時期で変動): Render/Railway/Fly.io など。無料枠はスリープや時間制限の場合あり。
  → "スリープしない" が必要なら自前の常時起動マシンが最安定。
- **Render無料プランを使う場合**: このファイルは HTTP サーバーを内蔵しています（/health）。UptimeRobot 等から5分おきにアクセスすればスリープしにくくなります（各サービスのポリシーに従ってご利用ください）。

6) Render にデプロイ（無料枠想定）
- リポジトリ直下に `render.yaml` を置くとワンクリックデプロイが楽です。

render.yaml（例）
--------------------------------
services:
  - type: web            # webにしてHTTPを公開（/health 用）
    name: meruru-boss-bot
    env: python
    plan: free
    region: singapore    # 近いリージョンに変更可（東京はProのみの場合あり）
    buildCommand: "pip install -r requirements.txt"
    startCommand: "python main.py"
    autoDeploy: true
    envVars:
      - key: DISCORD_TOKEN
        sync: false        # 手動でダッシュボードに設定
      - key: ANNOUNCE_CHANNEL_ID
        sync: false
--------------------------------

7) UptimeRobot の設定（5分おきに起こす）
- 監視タイプ: HTTP(s)
- URL: `https://<Renderのホスト名>/health`
- チェック間隔: 5分
- 注意: 無料プランの制約や Render のポリシー変更により動作が変わることがあります。

8) よくあるカスタム
- ボスごとに時間窓(例: 8h ± 30m)を持たせる → interval_minutes と別に window_minutes を追加し、告知文を調整
- 役職メンションを付けたい → announce時に <@&ROLE_ID> を文中に追加
- 複数ギルドで別データにしたい → guild.id ごとに json を分ける
- ボスごとに時間窓(例: 8h ± 30m)を持たせる → interval_minutes と別に window_minutes を追加し、告知文を調整
- 役職メンションを付けたい → announce時に <@&ROLE_ID> を文中に追加
- 複数ギルドで別データにしたい → guild.id ごとに json を分ける

困ったらこのファイルを貼ったまま「この仕様を追加して」と言ってください。"""
