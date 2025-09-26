# -*- coding: utf-8 -*-
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
import re
from collections import defaultdict

import discord
from discord.ext import commands, tasks

# ====== 基本設定 ======
TOKEN = os.getenv("DISCORD_TOKEN", "YOUR_DISCORD_BOT_TOKEN")  # ←必要なら直書きに差し替え
STATE_PATH = os.path.join("data", "state.json")
PRESET_PATH = "preset_jp.json"

# 1分前通知の“同時湧き”判定（±1分）
GROUP_SEC = 60
# 通知ループの間隔（秒）
LOOP_SEC = 30

JST = timezone(timedelta(hours=9))

# ====== ユーティリティ ======
def now_jst():
    return datetime.now(JST)

def fmt_dt(dt: datetime) -> str:
    """秒まで出す表示（JST）"""
    return dt.astimezone(JST).strftime("%m/%d %H:%M:%S")

def parse_hhmm(hhmm: str) -> datetime:
    """HHMM を “今日のその時刻”の JST datetime に。未来なら昨日に戻す。"""
    m = re.fullmatch(r"(\d{2})(\d{2})", hhmm)
    if not m:
        raise ValueError("HHMM 形式（例：1120）で入力してください。")
    h, mnt = int(m.group(1)), int(m.group(2))
    base = now_jst().replace(hour=h, minute=mnt, second=0, microsecond=0)
    if base > now_jst():
        base = base - timedelta(days=1)  # 未来なら前日扱い
    return base

def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def minutes_to_td(mins: int) -> timedelta:
    return timedelta(minutes=int(mins))

def hours_to_minutes(hours_f) -> int:
    # "4" / "4.5" / 4 / 4.5 など → 分
    v = float(hours_f)
    return int(round(v * 60))

# ====== データ管理 ======
class BossDB:
    """
    JSON 永続化（/data/state.json）
    構造：
    {
      "notify_channel_id": 1234567890 or null,
      "bosses": {
        "エンクラ": {
           "minutes": 210,            # 再出現間隔（分）
           "prob": 50,                # 出現率（%）
           "next_spawn": "2025-09-24T19:35:00+09:00",
           "skip_count": 3,
           "last_pre_notice_key": ""  # 1分前通知済み判定キー
        },
        ...
      }
    }
    """
    def __init__(self, state_path=STATE_PATH, preset_path=PRESET_PATH):
        self.path = state_path
        self.state = load_json(self.path, {"notify_channel_id": None, "bosses": {}})
        self.preset = load_json(preset_path, {"bosses": {}})

    def save(self):
        save_json(self.path, self.state)

    # --- boss ops ---
    def ensure_boss(self, name: str, minutes: int = None, prob: int = None):
        b = self.state["bosses"].get(name)
        if not b:
            # 新規
            b = {
                "minutes": int(minutes) if minutes is not None else 60,
                "prob": int(prob) if prob is not None else 100,
                "next_spawn": (now_jst() + timedelta(hours=1)).isoformat(),
                "skip_count": 0,
                "last_pre_notice_key": ""
            }
            self.state["bosses"][name] = b
        else:
            if minutes is not None:
                b["minutes"] = int(minutes)
            if prob is not None:
                b["prob"] = int(prob)
        return b

    def set_cycle(self, name: str, minutes: int):
        self.ensure_boss(name, minutes=minutes)
        self.save()

    def set_next_by_kill(self, name: str, kill_time: datetime):
        b = self.ensure_boss(name)
        b["next_spawn"] = (kill_time + minutes_to_td(b["minutes"])).isoformat()
        b["last_pre_notice_key"] = ""
        self.save()

    def advance_one_cycle(self, name: str):
        """自動スキップ：出現時に次周へ回す（通知は出すがスキップ通知は出さない）"""
        b = self.state["bosses"].get(name)
        if not b:
            return
        next_dt = datetime.fromisoformat(b["next_spawn"])
        next_dt = next_dt + minutes_to_td(b["minutes"])
        b["next_spawn"] = next_dt.isoformat()
        b["skip_count"] = int(b.get("skip_count", 0)) + 1
        b["last_pre_notice_key"] = ""
        self.save()

    def set_all_next_from_time(self, hhmm: str):
        base = parse_hhmm(hhmm)
        for name, b in self.state["bosses"].items():
            b["next_spawn"] = base.isoformat()
            b["last_pre_notice_key"] = ""
        self.save()

    def all_bosses(self):
        return self.state["bosses"]

    def bosses_within_hours(self, hours: int):
        limit = now_jst() + timedelta(hours=hours)
        out = []
        for name, b in self.state["bosses"].items():
            dt = datetime.fromisoformat(b["next_spawn"])
            if dt <= limit:
                out.append((name, b))
        out.sort(key=lambda x: x[1]["next_spawn"])
        return out

    def load_preset_jp(self, overwrite_cycle=False):
        """
        preset_jp.json を読んで “minutes/prob” を登録。
        overwrite_cycle=True の時は周期上書き／False は未登録のみ更新。
        """
        count = 0
        for name, meta in self.preset.get("bosses", {}).items():
            minutes = int(meta["minutes"])
            prob = int(meta["prob"])
            if name not in self.state["bosses"]:
                self.ensure_boss(name, minutes=minutes, prob=prob)
                count += 1
            else:
                if overwrite_cycle:
                    self.ensure_boss(name, minutes=minutes, prob=prob)
                    count += 1
        self.save()
        return count

db = BossDB()

# ====== Discord ======
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ====== 表示整形 ======
def build_list_embed(boss_list, title="Boss Timers", hours=None):
    """
    時間帯で段落を分け、確率とスキップ数を表示
    """
    if hours is None:
        subtitle = "（全件）"
    else:
        subtitle = f"（≦ {hours}h）"
    title = f"📜 {title} {subtitle}"

    lines = []
    prev_hour = None
    now_ = now_jst()

    for name, b in boss_list:
        dt = datetime.fromisoformat(b["next_spawn"]).astimezone(JST)
        hour = dt.hour
        # 時間帯が変わったら段落空行（3 行相当）
        if prev_hour is not None and hour != prev_hour:
            lines.append("")  # 1
            lines.append("")  # 2
            lines.append("")  # 3
        prev_hour = hour

        remain = dt - now_
        remain_txt = f"+{int(remain.total_seconds()//60)}m" if remain.total_seconds() >= 0 else f"{int(remain.total_seconds()//60)}m"

        prob = b.get("prob", 100)
        skipc = b.get("skip_count", 0)
        lines.append(f"・{name}（{prob}%）【スキップ{skipc}回】→ {fmt_dt(dt)} [{remain_txt}]")

    if not lines:
        lines = ["対象なし"]

    embed = discord.Embed(description="\n".join(lines), color=0x2B90D9)
    embed.set_author(name=title)
    return embed

# ====== コマンド ======
@bot.command(name="bt")
async def cmd_bt(ctx):
    bosses = sorted(db.all_bosses().items(), key=lambda x: x[1]["next_spawn"])
    await ctx.send(embed=build_list_embed(bosses, hours="ALL"))

@bot.command(name="bt3")
async def cmd_bt3(ctx):
    bosses = db.bosses_within_hours(3)
    await ctx.send(embed=build_list_embed(bosses, hours=3))

@bot.command(name="bt6")
async def cmd_bt6(ctx):
    bosses = db.bosses_within_hours(6)
    await ctx.send(embed=build_list_embed(bosses, hours=6))

@bot.command(name="bt12")
async def cmd_bt12(ctx):
    bosses = db.bosses_within_hours(12)
    await ctx.send(embed=build_list_embed(bosses, hours=12))

@bot.command(name="bt24")
async def cmd_bt24(ctx):
    bosses = db.bosses_within_hours(24)
    await ctx.send(embed=build_list_embed(bosses, hours=24))

@bot.command(name="reset")
async def cmd_reset(ctx, hhmm: str):
    """!reset HHMM  全ボスの次湧き時刻を一括再設定（過ぎていれば翌日の同時刻へはしない：仕様どおり）"""
    db.set_all_next_from_time(hhmm)
    await ctx.send(f"⏱ 全ボスの次湧きを `{hhmm}` 基準に再設定しました。")

@bot.command(name="rh")
async def cmd_rh(ctx, name: str, hours_: str):
    """!rh ボス名 時間h   周期だけ変更（h は 4 / 4.5 など）"""
    mins = hours_to_minutes(hours_)
    db.set_cycle(name, mins)
    await ctx.send(f"🔧 周期変更：{name} → {hours_}h（{mins}分）")

@bot.command(name="rhshow")
async def cmd_rhshow(ctx, keyword: str = None):
    """!rhshow [kw]  周期一覧（絞り込み可）"""
    lines = []
    for name, b in sorted(db.all_bosses().items()):
        if keyword and keyword not in name:
            continue
        mins = b["minutes"]
        prob = b.get("prob", 100)
        lines.append(f"・{name} : {mins/60:.1f}h（{mins}分, {prob}%）")
    await ctx.send("```\n" + "\n".join(lines) + "\n```" if lines else "（なし）")

@bot.command(name="preset")
async def cmd_preset(ctx, which: str = "jp"):
    if which.lower() != "jp":
        await ctx.send("プリセットは `jp` のみ対応しています。")
        return
    n = db.load_preset_jp(overwrite_cycle=False)
    await ctx.send(f"📦 プリセット `jp` を読み込みました（更新 {n} 件）")

@bot.command(name="setchannel")
@commands.has_permissions(manage_guild=True)
async def cmd_setchannel(ctx):
    db.state["notify_channel_id"] = ctx.channel.id
    db.save()
    await ctx.send(f"🔔 このチャンネルを通知先に設定しました。")

# ====== ショート入力: 「ボス名 HHMM [周期h]」 ======
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    await bot.process_commands(message)  # 先に通常コマンドを通す

    # 例：「エンクラ 1120」「コルーン 1120 6」
    text = message.content.strip()
    m = re.fullmatch(r"(.+?)\s+(\d{2}\d{2})(?:\s+([0-9]+(?:\.[0-9]+)?))?", text)
    if not m:
        return

    name = m.group(1).strip()
    hhmm = m.group(2)
    hours_ = m.group(3)

    try:
        kill_dt = parse_hhmm(hhmm)
    except Exception as e:
        await message.channel.send(f"⚠ 入力エラー：{e}")
        return

    if hours_:
        db.set_cycle(name, hours_to_minutes(hours_))

    db.set_next_by_kill(name, kill_dt)
    b = db.all_bosses()[name]
    await message.channel.send(
        f"✅ `{name}` 登録：討伐 {fmt_dt(kill_dt)} → 次 {fmt_dt(datetime.fromisoformat(b['next_spawn']))} "
        + (f"（周期 {b['minutes']/60:.1f}h）" if b else "")
    )

# ====== 通知ループ ======
def groups_for_pre_notice(bosses):
    """
    1分前通知対象を ±GROUP_SEC でまとめる
    return: [ [ (name, boss_dict), ...], ... ]
    """
    # 1分前の時刻に到達していて、まだそのスポーンに対して通知していないもの
    target = []
    now_ = now_jst()
    for name, b in bosses:
        next_dt = datetime.fromisoformat(b["next_spawn"])
        pre_dt = next_dt - timedelta(seconds=GROUP_SEC)
        key = next_dt.isoformat()  # スポーン時刻をキーに1回だけ通知
        if pre_dt <= now_ < next_dt and b.get("last_pre_notice_key", "") != key:
            target.append((name, b, next_dt, key))

    # 近いものをグルーピング
    target.sort(key=lambda x: x[2])
    groups = []
    cur = []
    for item in target:
        if not cur:
            cur = [item]
            continue
        if abs((item[2] - cur[-1][2]).total_seconds()) <= GROUP_SEC:
            cur.append(item)
        else:
            groups.append(cur)
            cur = [item]
    if cur:
        groups.append(cur)
    return groups

def groups_for_spawn(bosses):
    """
    出現時（時刻 >= next_spawn）を ±GROUP_SEC でまとめる
    """
    target = []
    now_ = now_jst()
    for name, b in bosses:
        next_dt = datetime.fromisoformat(b["next_spawn"])
        if now_ >= next_dt:
            target.append((name, b, next_dt))
    target.sort(key=lambda x: x[2])
    groups = []
    cur = []
    for item in target:
        if not cur:
            cur = [item]
            continue
        if abs((item[2] - cur[-1][2]).total_seconds()) <= GROUP_SEC:
            cur.append(item)
        else:
            groups.append(cur)
            cur = [item]
    if cur:
        groups.append(cur)
    return groups

@tasks.loop(seconds=LOOP_SEC)
async def notifier_loop():
    chan_id = db.state.get("notify_channel_id")
    if not chan_id:
        return
    channel = bot.get_channel(chan_id)
    if not channel:
        return

    bosses_sorted = sorted(db.all_bosses().items(), key=lambda x: x[1]["next_spawn"])

    # --- 1分前通知（確定のみ「※確定」マーク）
    for g in groups_for_pre_notice(bosses_sorted):
        parts = []
        for name, b, next_dt, key in g:
            prob = int(b.get("prob", 100))
            mark = " ※確定" if prob == 100 else ""
            parts.append(f"・{name}{mark}  [{fmt_dt(next_dt)}]")
            b["last_pre_notice_key"] = key  # 同一スポーンに対して1回だけ
        db.save()
        txt = "⏰ **1分前**\n" + "\n".join(parts)
        await channel.send(txt)

    # --- 出現時通知（まとめて一通）＋ 自動スキップで次周へ
    for g in groups_for_spawn(bosses_sorted):
        parts = []
        for name, b, next_dt in g:
            prob = int(b.get("prob", 100))
            mark = " ※確定" if prob == 100 else ""
            parts.append(f"・{name}{mark}  [{fmt_dt(next_dt)}]")
        await channel.send("🔥 **出現！**\n" + "\n".join(parts))
        # 出現したものは次周へ（スキップ数+1、通知は出さない）
        for name, b, _ in g:
            db.advance_one_cycle(name)

@notifier_loop.before_loop
async def before_loop():
    await bot.wait_until_ready()

# ====== 起動 ======
def main():
    os.makedirs("data", exist_ok=True)
    # 初回：state.jsonが無ければプリセットを読み込み
    if not os.path.exists(STATE_PATH):
        db.load_preset_jp(overwrite_cycle=False)
        db.save()
    notifier_loop.start()
    bot.run(TOKEN)

if __name__ == "__main__":
    main()
