# -*- coding: utf-8 -*-
"""
Discord BossBot (Render/Glitch対応)
- 入力: 「ボス名 HHMM [周期h]」…HHMMは24h表記、周期未指定ならプリセット/既存値
- プリセット: !preset jp（出現率つき/日本語名）
- 一覧: bt / bt3 / bt6 / bt12 / bt24
  - 表示は「HH:MM:SS : ボス名 [n周]/※確定 (skip:x)」
  - 時（HH）が変わるたび空行3つで段落化
- 通知:
  - 出現1分前のみ（±30秒補正）
  - ±1分以内に同時湧きは1通にまとめて送信
  - 出現率100%のみ「※確定」マーク
  - スキップ時の通知は出さない（内部skipカウントのみ進行）
- 設定:
  !setchannel（通知チャンネル指定）
  !reset HHMM（全ボスを指定時刻基準に再設定）
  !rh ボス名 h（周期だけ変更）
  !rhshow [kw]（周期＋出現率の一覧）
- /health: 204 No Content（軽量ヘルスチェック）

環境変数:
  DISCORD_TOKEN  … Discord Bot Token
"""

import os, re, json, threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from flask import Flask, Response
import discord
from discord.ext import commands, tasks

# ===== タイムゾーン & 時刻ユーティリティ =====
JST = ZoneInfo("Asia/Tokyo")
def now() -> datetime:
    return datetime.now(tz=JST)

def fmt_hms(dt: datetime) -> str:
    return dt.astimezone(JST).strftime("%H:%M:%S")  # 表示は秒付き

def fmt_ymdhm(dt: datetime) -> str:
    return dt.astimezone(JST).strftime("%Y-%m-%d %H:%M")  # 永続は分まで

def parse_hhmm_today(hhmm: str) -> datetime:
    h = int(hhmm[:2]); m = int(hhmm[2:])
    return datetime(now().year, now().month, now().day, h, m, tzinfo=JST)

def parse_ymdhm(s: str):
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=JST)
    except Exception:
        return None

# ===== 表示幅（全角=2）ユーティリティ =====
try:
    from wcwidth import wcwidth
except Exception:
    def wcwidth(ch: str) -> int:
        code = ord(ch)
        return 2 if (
            0x1100 <= code <= 0x115F or
            0x2E80 <= code <= 0xA4CF or
            0xAC00 <= code <= 0xD7A3 or
            0xF900 <= code <= 0xFAFF or
            0xFE10 <= code <= 0xFE19 or
            0xFE30 <= code <= 0xFE6F or
            0xFF00 <= code <= 0xFF60 or
            0xFFE0 <= code <= 0xFFE6
        ) else 1

def visual_len(s: str) -> int:
    return sum(wcwidth(ch) for ch in s)

def pad_to(s: str, width: int) -> str:
    cur = visual_len(s)
    return s if cur >= width else s + " " * (width - cur)

# ===== 永続化 =====
SAVE_PATH = "data.json"
state = {
    "notify_channel_id": None,
    "bosses": {
        # "ボス": {
        #   "respawn_h": float,        # 周期
        #   "rate": int,               # 出現率（%）
        #   "last_kill": "YYYY-MM-DD HH:MM",
        #   "next_spawn": "YYYY-MM-DD HH:MM",
        #   "skip_count": int,
        #   "rem1": bool               # 1分前通知済フラグ
        # }
    }
}

def load():
    global state
    try:
        with open(SAVE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        save()

def save():
    with open(SAVE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

# ===== JPプリセット（間隔h, 出現率%） =====
JP_PRESET: dict[str, tuple[float, int]] = {
    "フェリス": (2.0, 50), "バシラ": (2.5, 50), "パンナロード": (3.0, 50),
    "エンクラ": (3.5, 50), "テンペスト": (3.5, 50), "マトゥラ": (4.0, 50),
    "チェトゥルゥバ": (3.0, 50), "ブレカ": (4.0, 50), "クイーンアント": (6.0, 33),
    "ヒシルローメ": (6.0, 50), "レピロ": (5.0, 33), "トロンバ": (4.5, 50),
    "スタン": (4.0, 100), "ミュータントクルマ": (8.0, 100),
    "ティミトリス": (5.0, 100), "汚染したクルマ": (8.0, 100),
    "タルキン": (5.0, 50), "ティミニエル": (8.0, 100), "グラーキ": (8.0, 100),
    "忘却の鏡": (12.0, 100), "ガレス": (6.0, 50), "ベヒモス": (6.0, 100),
    "ランドール": (8.0, 100), "ケルソス": (6.0, 50), "タラキン": (7.0, 100),
    "メデューサ": (7.0, 100), "サルカ": (7.0, 100), "カタン": (8.0, 100),
    "コアサセプタ": (12.0, 33), "ブラックリリー": (12.0, 100),
    "パンドライド": (8.0, 100), "サヴァン": (12.0, 100),
    "ドラゴンビースト": (12.0, 50), "バルポ": (8.0, 50), "セル": (7.5, 33),
    "コルーン": (10.0, 100), "オルフェン": (24.0, 33), "サミュエル": (12.0, 100),
    "アンドラス": (12.0, 50), "カブリオ": (12.0, 50), "ハーフ": (24.0, 33),
    "フリント": (8.0, 33),
}
DEFAULT_RH = 8.0

def get_rh(name: str) -> float:
    b = state["bosses"].get(name)
    if b and "respawn_h" in b:
        return float(b["respawn_h"])
    if name in JP_PRESET:
        return JP_PRESET[name][0]
    return DEFAULT_RH

def get_rate(name: str) -> int | None:
    b = state["bosses"].get(name)
    if b and "rate" in b:
        return int(b["rate"])
    if name in JP_PRESET:
        return JP_PRESET[name][1]
    return None

def set_rh(name: str, h: float):
    info = state["bosses"].setdefault(name, {})
    info["respawn_h"] = float(h)

# ===== Discord Bot =====
TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print("✅ Logged in as", bot.user)
    load()
    if not ticker.is_running():
        ticker.start()

async def notify(text: str):
    ch_id = state.get("notify_channel_id")
    if not ch_id:
        return
    try:
        ch = await bot.fetch_channel(int(ch_id))
        await ch.send(text)
    except Exception:
        pass

def set_next_spawn(name: str, base: datetime, rh: float):
    info = state["bosses"].setdefault(name, {})
    info["next_spawn"] = fmt_ymdhm(base + timedelta(hours=rh))
    info["rem1"] = False
    info["skip_count"] = int(info.get("skip_count") or 0)

TIME_RE = re.compile(r"^(.+?)\s+(\d{3,4})(?:\s+(\d+(?:\.\d+)?))?$")

# ---- コマンド ----
@bot.command()
async def setchannel(ctx):
    state["notify_channel_id"] = str(ctx.channel.id)
    save()
    await ctx.reply("✅ 通知チャンネルを設定しました。")

@bot.command()
async def reset(ctx, hhmm: str = None):
    if not hhmm or not re.fullmatch(r"\d{4}", hhmm):
        return await ctx.reply("使い方: `!reset HHMM`")
    base = parse_hhmm_today(hhmm)
    target = base if base > now() else base + timedelta(days=1)
    for info in state["bosses"].values():
        info["next_spawn"] = fmt_ymdhm(target)
        info["rem1"] = False
        info["skip_count"] = 0
    save()
    await ctx.reply(f"♻️ 全ボスを **{target.strftime('%m/%d %H:%M')}** に再設定しました。")

@bot.command()
async def rh(ctx, name: str = None, hours: str = None):
    if not name or not hours:
        return await ctx.reply("使い方: `!rh ボス名 時間h`")
    try:
        h = float(hours)
    except ValueError:
        return await ctx.reply("時間h は数値で指定してください。")
    set_rh(name, h)
    save()
    await ctx.reply(f"🔧 {name} の周期を {h}h に設定しました。")

@bot.command()
async def rhshow(ctx, kw: str = None):
    names = set(JP_PRESET.keys()) | set(state["bosses"].keys())
    rows = []
    for n in sorted(names):
        if kw and kw not in n:
            continue
        rh = get_rh(n)
        rate = get_rate(n)
        rows.append(f"{n} : {rh}h / 出現率 {rate}%" if rate else f"{n} : {rh}h")
    if not rows:
        return await ctx.reply("（該当なし）")
    await ctx.send("```\n" + "\n".join(rows) + "\n```")

@bot.command()
async def preset(ctx, which: str = None):
    if not which or which.lower() != "jp":
        return await ctx.reply("対応プリセット: jp")
    now_dt = now()
    for n, (h, rate) in JP_PRESET.items():
        info = state["bosses"].setdefault(n, {})
        info["respawn_h"] = float(h)
        info["rate"] = int(rate)
        # 既に次湧きが無ければ、とりあえず今＋周期で初期化
        if not info.get("next_spawn"):
            info["next_spawn"] = fmt_ymdhm(now_dt + timedelta(hours=h))
        info.setdefault("skip_count", 0)
        info["rem1"] = False
    save()
    await ctx.reply("✅ JPプリセットを読み込みました。")

# ---- メッセージ（ショート入力 & 一覧） ----
@bot.event
async def on_message(message: discord.Message):
    await bot.process_commands(message)
    if message.author.bot:
        return

    content = message.content.strip()
    low = content.lower()

    # 一覧
    if low in {"bt3", "bt 3"}:
        return await send_list(message.channel, 3)
    if low in {"bt6", "bt 6"}:
        return await send_list(message.channel, 6)
    if low in {"bt12", "bt 12"}:
        return await send_list(message.channel, 12)
    if low in {"bt24", "bt 24"}:
        return await send_list(message.channel, 24)
    if low in {"bt", "btall", "bl"}:
        return await send_list(message.channel, None)

    # ショート入力: 「ボス名 HHMM [周期h]」
    m = TIME_RE.match(content)
    if m:
        name, hhmm, opt = m.groups()
        if len(hhmm) == 3:
            hhmm = "0" + hhmm
        if not re.fullmatch(r"\d{4}", hhmm):
            return await message.reply("⛔ 時刻はHHMMで指定してください。")

        rh = get_rh(name)
        if opt:
            try:
                rh = float(opt)
                set_rh(name, rh)
            except ValueError:
                pass

        kill = parse_hhmm_today(hhmm)
        if kill > now():  # 未来は前日討伐扱い
            kill -= timedelta(days=1)

        info = state["bosses"].setdefault(name, {})
        info["last_kill"] = fmt_ymdhm(kill)
        info["skip_count"] = 0
        info["rem1"] = False
        set_next_spawn(name, kill, rh)
        save()

        ns = parse_ymdhm(info["next_spawn"])
        if ns:
            await message.channel.send(
                f"{name}\n次回出現{ns.strftime('%m月%d日%H時%M分%S秒')}※確定出現"
            )

async def send_list(channel: discord.abc.Messageable, hours: int | None):
    """
    一覧を '時刻 : ボス名 [n周]/※確定 (skip:x)' で整形表示。
    時（HH）が変わるたびに空行3つで段落化。hours=None は次湧きのみ。
    """
    now_dt = now()
    rows: list[tuple[datetime, str, int, int | None, int]] = []

    if hours is not None:
        end = now_dt + timedelta(hours=hours)

    for name, info in state["bosses"].items():
        ns = parse_ymdhm(info.get("next_spawn", ""))
        if not ns:
            continue
        rh   = get_rh(name)
        rate = get_rate(name)
        skip = int(info.get("skip_count") or 0)

        if hours is None:
            rows.append((ns, name, 0, rate, skip))
        else:
            t = ns
            rounds = 0
            while t <= end:
                if t > now_dt:
                    rows.append((t, name, rounds, rate, skip))
                rounds += 1
                t = ns + timedelta(hours=rh * rounds)

    rows.sort(key=lambda x: x[0])

    if not rows:
        return await channel.send(f"（該当なし / {hours or 'next'}）")

    header = f"----- In {hours}hours Boss Time-----" if hours else "----- Next Boss Time -----"
    lines = [header]

    NAME_COL = 18
    prev_key = None  # 段落化（年月日＋時）
    for (t, n, r, rate, skip) in rows:
        hour_key = t.strftime("%Y-%m-%d %H")
        if prev_key is not None and hour_key != prev_key:
            lines.extend(["", "", ""])  # 空行3つ
        prev_key = hour_key

        time_str = t.strftime("%H:%M:%S")
        name_str = pad_to(n, NAME_COL)
        tail = "※確定" if (rate == 100) else (f"[{r}周]" if r > 0 else "")
        lines.append(f"{time_str} : {name_str}{tail} (skip:{skip})")

    await channel.send("```\n" + "\n".join(lines) + "\n```")

# ===== 通知ループ（1分前まとめ、スキップ通知なし）=====
@tasks.loop(seconds=30)
async def ticker():
    """
    30秒ごとにチェック。
    - 出現1分前通知（±30秒補正、未送信のみ）
    - 同時刻±1分に湧くボスを1通にまとめて通知
    - 出現時は通知せず、次湧き更新＋skip+1、rem1解除
    """
    now_dt = now()
    changed = False
    pre_lines: list[tuple[datetime, str, int | None]] = []  # (spawn_time, name, rate)

    for name, info in state["bosses"].items():
        ns = parse_ymdhm(info.get("next_spawn", ""))
        if not ns:
            continue

        rh   = get_rh(name)
        rate = get_rate(name)
        skip = int(info.get("skip_count") or 0)

        # 1分前（±30秒）
        pre_at = ns - timedelta(minutes=1)
        if abs((pre_at - now_dt).total_seconds()) <= 30:
            if not info.get("rem1", False):
                pre_lines.append((ns, name, rate))  # 全ボス通知対象
                info["rem1"] = True
                changed = True

        # 出現時（±30秒）：通知なしで次周へ
        if abs((ns - now_dt).total_seconds()) <= 30:
            ns_next = ns + timedelta(hours=rh)
            info["next_spawn"] = fmt_ymdhm(ns_next)
            info["rem1"] = False
            info["skip_count"] = skip + 1
            changed = True

    # ±1分以内をまとめて1通ずつ送信
    if pre_lines:
        pre_lines.sort(key=lambda x: x[0])
        groups = []
        cur = []
        for item in pre_lines:
            if not cur:
                cur.append(item); continue
            anchor = cur[0][0]
            if abs((item[0] - anchor).total_seconds()) <= 60:
                cur.append(item)
            else:
                groups.append(cur)
                cur = [item]
        if cur:
            groups.append(cur)

        NAME_COL = 18
        for group in groups:
            group.sort(key=lambda x: x[0])
            lines = ["----- 1min Before Spawn -----"]
            for (t, n, rate) in group:
                time_str = t.strftime("%H:%M:%S")
                name_str = pad_to(n, NAME_COL)
                tail = "※確定" if (rate == 100) else ""
                lines.append(f"{time_str} : {name_str}{tail}")
            await notify("```\n" + "\n".join(lines) + "\n```")

    if changed:
        save()

# ===== Flask (Health) =====
app = Flask(__name__)

@app.route("/")
def root():
    return "OK"

@app.route("/health", methods=["GET", "HEAD"])
def health():
    return Response(status=204)

def run_http():
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

# ===== エントリポイント =====
if __name__ == "__main__":
    TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
    if not TOKEN:
        print("❌ 環境変数 DISCORD_TOKEN が未設定です。RenderのEnvironmentに追加してください。")
    else:
        threading.Thread(target=run_http, daemon=True).start()
        bot.run(TOKEN)


