# app.py
import os
import re
import json
import asyncio
from datetime import datetime, timedelta, timezone
from collections import defaultdict

import discord
from discord.ext import commands, tasks
from flask import Flask, jsonify

from storage import load_json, save_json, ensure_data_dir
from presets import JP_PRESET

# ---- 基本設定 ----
TOKEN = os.getenv("DISCORD_TOKEN")  # RenderのEnvironmentに設定
TZ = timezone(timedelta(hours=9))   # JST
DATA_DIR = "data"
STATE_FILE = os.path.join(DATA_DIR, "state.json")
PERIODS_FILE = os.path.join(DATA_DIR, "periods.json")
DEFAULT_NOTIFY_WINDOW_MIN = 3       # bt3 の既定
GROUP_WINDOW_SEC = 60               # 同時沸きの定義（±1分）
PRE_NOTIFY_SEC = 60                 # 出現 1分前 通知

# ---- Flask keep-alive ----
flask_app = Flask(__name__)

@flask_app.get("/health")
def health():
    state = load_json(STATE_FILE, default={})
    return jsonify({"ok": True, "bosses": len(state.get("bosses", {}))})

def run_flask():
    port = int(os.getenv("PORT", "10000"))
    flask_app.run(host="0.0.0.0", port=port)

# ---- Discord ----
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# ---- 状態管理 ----
def now():
    return datetime.now(TZ)

def parse_hhmm(s: str) -> datetime | None:
    m = re.fullmatch(r"(\d{2})(\d{2})", s)
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    today = now().date()
    dt = datetime(today.year, today.month, today.day, hh, mm, tzinfo=TZ)
    # 未来入力は「前日討伐扱い」
    if dt > now():
        dt -= timedelta(days=1)
    return dt

def hours_to_timedelta(hstr: str) -> timedelta | None:
    try:
        return timedelta(hours=float(hstr))
    except Exception:
        return None

# state.json の構造
# {
#   "channel_id": 1234567890 or null,
#   "bosses": {
#       "エンクラ": {"next_at": "2025-09-25T19:35:00+09:00", "period_h": 3.5, "skip": 0},
#       ...
#   }
# }
def load_state():
    return load_json(STATE_FILE, default={"channel_id": None, "bosses": {}})

def save_state(state):
    save_json(STATE_FILE, state)

def load_periods():
    # periods.json: {"エンクラ": {"period_h": 3.5, "chance": 50}, ...}
    return load_json(PERIODS_FILE, default={})

def save_periods(periods):
    save_json(PERIODS_FILE, periods)

def chance_mark(name: str) -> str:
    periods = load_periods()
    c = periods.get(name, {}).get("chance")
    if c is None:
        return ""
    mark = "※確定" if c >= 100 else f"({c}%)"
    return mark

def ensure_boss(state, name):
    if name not in state["bosses"]:
        state["bosses"][name] = {"next_at": None, "period_h": None, "skip": 0}

def set_next_from_kill(state, name, killed_at: datetime, period_h: float | None):
    ensure_boss(state, name)
    periods = load_periods()
    if period_h is None:
        # プリセットがあれば既定値
        p = periods.get(name, {}).get("period_h")
    else:
        p = period_h
        # ユーザが明示したら periods にも反映（学習）
        periods[name] = periods.get(name, {})
        periods[name]["period_h"] = float(period_h)
        if "chance" not in periods[name]:
            periods[name]["chance"] = 100 if float(period_h) == int(period_h) else 50
        save_periods(periods)

    if p is None:
        return False
    nxt = killed_at + timedelta(hours=float(p))
    state["bosses"][name]["next_at"] = nxt.isoformat()
    state["bosses"][name]["period_h"] = float(p)
    state["bosses"][name]["skip"] = 0
    save_state(state)
    return True

def bump_skip_if_passed(state):
    """予定時刻を過ぎたボスは、スキップ（通知はしない）"""
    changed = False
    nowt = now()
    for name, b in state["bosses"].items():
        na = b.get("next_at")
        per = b.get("period_h")
        if not na or not per:
            continue
        nat = datetime.fromisoformat(na)
        while nat <= nowt:
            nat += timedelta(hours=float(per))
            b["skip"] = int(b.get("skip", 0)) + 1
            changed = True
        if nat.isoformat() != b["next_at"]:
            b["next_at"] = nat.isoformat()
            changed = True
    if changed:
        save_state(state)

def hour_bucket(dt: datetime) -> str:
    return dt.strftime("%H")

def build_list_message(state, hours=DEFAULT_NOTIFY_WINDOW_MIN):
    bosses = state["bosses"]
    nowt = now()
    lim = nowt + timedelta(hours=hours)

    # 期間内のボスを抽出
    items = []
    for name, b in bosses.items():
        na = b.get("next_at")
        if not na:
            continue
        nat = datetime.fromisoformat(na)
        if nowt <= nat <= lim:
            items.append((name, nat, int(b.get("skip", 0))))

    if not items:
        return "該当なし"

    # 時間帯で段落化（時:）
    items.sort(key=lambda x: x[1])
    lines = ["```", f"----- In {hours}hours Boss Time -----"]
    current_hour = None
    for name, nat, skip in items:
        hh = hour_bucket(nat)
        if current_hour is None:
            current_hour = hh
            lines.append("")
            lines.append(f"{hh}:00台")
        elif hh != current_hour:
            # 段落区切り（空行3つ）
            lines.extend(["", "", ""])
            current_hour = hh
            lines.append(f"{hh}:00台")

        mark = chance_mark(name)
        tstr = nat.strftime("%H:%M:%S")
        sk = f"【スキップ{skip}回】" if skip else ""
        # 例: 17:35:13 : エンクラ(50%)【スキップ3回】
        lines.append(f"{tstr} : {name}{mark}{sk}")
    lines.append("```")
    return "\n".join(lines)

async def safe_send(channel: discord.TextChannel, content: str):
    try:
        await channel.send(content)
    except Exception:
        pass

# ---- コマンド ----
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    ensure_data_dir()
    # 初回起動で periods が無ければ JP を焼く
    if not load_periods():
        save_periods(JP_PRESET)
    notifier.start()

@bot.command()
@commands.has_permissions(manage_channels=True)
async def setchannel(ctx: commands.Context):
    """現在のチャンネルを通知先に設定（管理者のみ）"""
    state = load_state()
    state["channel_id"] = ctx.channel.id
    save_state(state)
    await ctx.reply("通知チャンネルを登録しました。")

@bot.command()
async def preset(ctx: commands.Context, name: str = "jp"):
    """!preset jp を適用"""
    if name.lower() != "jp":
        await ctx.reply("利用可能: jp")
        return
    save_periods(JP_PRESET)
    await ctx.reply("JPプリセットを適用しました。")

@bot.command(aliases=["bt"])
async def bt_hours(ctx: commands.Context, hours: str = "3"):
    """bt / bt3 / bt6 / bt12 / bt24"""
    if hours.startswith("bt"):
        hours = hours[2:]
    try:
        h = int(hours)
    except Exception:
        h = DEFAULT_NOTIFY_WINDOW_MIN
    msg = build_list_message(load_state(), hours=h)
    await ctx.reply(msg)

# ショート入力: 「ボス名 HHMM [周期h]」
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    content = message.content.strip()
    # 例: 「エンクラ 1935 4.5」 / 「トロンバ 1120」
    m = re.fullmatch(r"(.+?)\s+(\d{4})(?:\s+([0-9]*\.?[0-9]+)h?)?$", content)
    if m:
        name = m.group(1).strip()
        hhmm = m.group(2)
        per = m.group(3)

        killed = parse_hhmm(hhmm)
        if not killed:
            await message.reply("時刻はHHMMで入力してください（例: 1935）")
            return

        period_td = None
        per_h = None
        if per:
            td = hours_to_timedelta(per)
            if not td:
                await message.reply("周期は 4 / 4.5 / 8 などで指定してください")
                return
            period_td = td
            per_h = float(per)

        st = load_state()
        ok = set_next_from_kill(st, name, killed, per_h)
        if not ok:
            await message.reply("周期が不明です。`!preset jp` を入れるか、末尾に 周期h を付けてください。")
            return

        nat = datetime.fromisoformat(st["bosses"][name]["next_at"])
        mark = chance_mark(name)
        await message.reply(f"✅ {name} を登録 → 次 {nat.strftime('%m/%d %H:%M:%S')} {mark}")
        return

    await bot.process_commands(message)

# ---- 通知（1分前をまとめて）----
@tasks.loop(seconds=10)
async def notifier():
    state = load_state()
    bump_skip_if_passed(state)

    ch_id = state.get("channel_id")
    if not ch_id:
        return
    channel = bot.get_channel(ch_id)
    if not isinstance(channel, discord.TextChannel):
        return

    nowt = now()
    # 直近 1分以内に「1分前」になるボスを拾う
    due = []
    for name, b in state["bosses"].items():
        na = b.get("next_at")
        if not na:
            continue
        nat = datetime.fromisoformat(na)
        # 1分前の時刻
        pre_at = nat - timedelta(seconds=PRE_NOTIFY_SEC)
        if 0 <= (nowt - pre_at).total_seconds() < 10:  # ループ10秒で検出
            due.append((name, nat))

    if not due:
        return

    # ±1分でグルーピング（同時沸き）
    due.sort(key=lambda x: x[1])
    groups = []
    current = [due[0]]
    for item in due[1:]:
        if abs((item[1] - current[-1][1]).total_seconds()) <= GROUP_WINDOW_SEC:
            current.append(item)
        else:
            groups.append(current)
            current = [item]
    groups.append(current)

    # まとめて1通ずつ送る
    for group in groups:
        lines = ["⏰ **1分前通知**"]
        for name, nat in group:
            mark = chance_mark(name)
            lines.append(f"・{name} {mark} → {nat.strftime('%m/%d %H:%M:%S')}")
        await safe_send(channel, "\n".join(lines))

# ---- エントリ ----
if __name__ == "__main__":
    ensure_data_dir()
    # Flask を別スレッドで
    import threading
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()

    if not TOKEN:
        raise RuntimeError("環境変数 DISCORD_TOKEN を設定してください")
    bot.run(TOKEN)

