import os
import json
import math
import asyncio
import random
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import discord
from discord.ext import tasks
from fastapi import FastAPI
from uvicorn import Config, Server

# ====== JST/Storage ======
JST = timezone(timedelta(hours=9))
DATA_DIR = "data"
STORE_FILE = os.path.join(DATA_DIR, "store.json")
PRESET_FILE = "bosses_preset.json"

CHECK_SEC = 10                 # ポーリング間隔
MERGE_WINDOW_SEC = 60          # 通知集約ウィンドウ（±）
DEFAULT_RESPAWN_MIN = 60       # プリセットにない場合のデフォルト
BACKOFF_429_MIN = int(os.getenv("BACKOFF_429_MIN", "900") or "900")
BACKOFF_JITTER_SEC = int(os.getenv("BACKOFF_JITTER_SEC", "0") or "0")

# ====== Name aliases (表記揺れ) ======
# 片仮名・平仮名・一部一致・英略称などを正規化 -> 正式名
ALIAS_MAP: Dict[str, str] = {
    "ふぇりす": "フェリス",
    "ばしら": "バシラ",
    "ぱんなろーど": "パンナロード",
    "えんくら": "エンクラ",
    "てんぺすと": "テンペスト",
    "まとぅら": "マトゥラ",
    "ちぇるとぅば": "チェルトゥバ",  # 正式：チェルトゥバ（旧チェトゥバ等を吸収）
    "ちぇとぅば": "チェルトゥバ",
    "ぶれか": "ブレカ",
    "くいーんあんと": "クイーンアント",
    "qa": "クイーンアント",
    "ｑａ": "クイーンアント",
    "ひしるろーめ": "ヒシルローメ",
    "れぴろ": "レピロ",
    "とろんば": "トロンバ",
    "すたん": "スタン",
    "みゅーたんとくるま": "ミュータントクルマ",
    "てぃみとりす": "ティミトリス",
    "おせんしたくるま": "汚染したクルマ",
    "たるきん": "タルキン",
    "てぃみにえる": "ティミニエル",
    "ぐらーき": "グラーキ",
    "わすれのかがみ": "忘却の鏡",
    "がれす": "ガレス",
    "べひもす": "ベヒモス",
    "らんどーる": "ランドール",
    "けるそす": "ケルソス",
    "たらきん": "タラキン",
    "めでゅーさ": "メデューサ",
    "さるか": "サルカ",
    "かたん": "カタン",
    "こあさせぷた": "コアサセプタ",
    "ぶらっくりりー": "ブラックリリー",
    "ぱんどらいど": "パンドライド",
    "さゔぁん": "サヴァン",
    "どらごんびーすと": "ドラゴンビースト",
    "ばるぽ": "バルポ",
    "せる": "セル",
    "こるーん": "コルーン",
    "おるふぇん": "オルフェン",
    "さみゅえる": "サミュエル",
    "あんどらす": "アンドラス",
    "かぶりお": "カブリオ",
    "はーふ": "ハーフ",
    "ふりんと": "フリント",
    "たなとす": "タナトス",
    "らーは": "ラーハ",
    "おるくす": "オルクス",

    # ローマ字・短縮例（必要に応じて追加）
    "qa/queenant": "クイーンアント",
}

# 部分一致候補（先頭一致など）→ 正式名
PARTIALS: List[Tuple[str, str]] = [
    ("フェリ", "フェリス"),
    ("バシラ", "バシラ"),
    ("パンナ", "パンナロード"),
    ("エンク", "エンクラ"),
    ("テンペ", "テンペスト"),
    ("ヒシル", "ヒシルローメ"),
    ("クイーン", "クイーンアント"),
    ("レピロ", "レピロ"),
    ("トロン", "トロンバ"),
    ("ティミニ", "ティミニエル"),
    ("ミュータ", "ミュータントクルマ"),
    ("汚染", "汚染したクルマ"),
    ("ガレス", "ガレス"),
    ("ベヒ", "ベヒモス"),
    ("ケルソ", "ケルソス"),
    ("メデュ", "メデューサ"),
    ("サルカ", "サルカ"),
    ("カタン", "カタン"),
    ("コアサ", "コアサセプタ"),
    ("ブラック", "ブラックリリー"),
    ("パンド", "パンドライド"),
    ("サヴァ", "サヴァン"),
    ("ドラゴ", "ドラゴンビースト"),
    ("バルポ", "バルポ"),
    ("セル", "セル"),
    ("コルー", "コルーン"),
    ("オルフ", "オルフェン"),
    ("サミュ", "サミュエル"),
    ("アンドラ", "アンドラス"),
    ("カブリ", "カブリオ"),
    ("ハーフ", "ハーフ"),
    ("フリント", "フリント"),
    ("タナト", "タナトス"),
    ("ラーハ", "ラーハ"),
    ("オルク", "オルクス"),
    ("チェル", "チェルトゥバ"),
    ("チェト", "チェルトゥバ"),
]

def kana_lower(s: str) -> str:
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ン" else c for c in s).lower()

def canonical_name(raw: str) -> Optional[str]:
    t = raw.strip()
    if not t:
        return None
    k = kana_lower(t)
    if k in ALIAS_MAP:
        return ALIAS_MAP[k]
    # 完全一致（カタカナ正式名直接）
    return_next = None
    # 先頭・部分一致
    for head, out in PARTIALS:
        if kana_lower(t).startswith(kana_lower(head)):
            return out
    # そのまま返す（カタナや正式名を入れたケース）
    return t

# ====== Models / Store ======
@dataclass
class BossState:
    name: str
    respawn_min: int
    rate: int = 100
    next_spawn_utc: Optional[int] = None
    channel_id: Optional[int] = None
    skip: int = 0
    excluded_reset: bool = False
    initial_delay_min: int = 0
    # 重複通知防止（前回通知済みの分）
    last_pre_minute_utc: Optional[int] = None
    last_spawn_minute_utc: Optional[int] = None

    def label_flags(self) -> str:
        parts = []
        if self.rate == 100:
            parts.append("※確定")
        if self.skip > 0:
            parts.append(f"{self.skip}周")
        return "[" + "] [".join(parts) + "]" if parts else ""

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

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def hm_to_min(hm: str) -> int:
    # "H:MM" / "HH:MM" 形式を分に
    try:
        h, m = hm.strip().split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return 0

def parse_initial_delay(obj: dict) -> int:
    if "initial_delay_min" in obj:
        try:
            return int(obj["initial_delay_min"])
        except Exception:
            pass
    if "initial_delay_hm" in obj:
        return hm_to_min(obj["initial_delay_hm"])
    if "初回出現遅延" in obj:
        # "H:MM" 期待
        return hm_to_min(str(obj["初回出現遅延"]))
    return 0

# ====== Bot ======
class BossBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # ←これが超重要
        intents.guilds = True

        super().__init__(intents=intents)

        self.store = Store(STORE_FILE)
        self.data: Dict[str, Dict[str, dict]] = self.store.load()    # guild -> name -> dict
        self.presets: Dict[str, Tuple[int,int,int]] = {}             # name -> (respawn_min, rate, initial_delay_min)
        self._load_presets()

        self.tick_task = self._tick.start()

    # --- presets ---
    def _load_presets(self):
        try:
            with open(PRESET_FILE, "r", encoding="utf-8") as f:
                arr = json.load(f)
        except Exception as e:
            print("[PRESET] load error:", e)
            self.presets = {}
            return
        dic = {}
        for x in arr:
            name = x.get("name") or x.get("名称")
            if not name:
                continue
            rate = int(x.get("rate") or x.get("出現率") or 100)
            respawn_h = x.get("respawn_h") or x.get("間隔") or x.get("respawnH")
            # "7:30" みたいな表記は分解、数字は時間とみなす
            respawn_min = None
            if isinstance(respawn_h, (int, float, str)):
                s = str(respawn_h)
                if ":" in s:
                    respawn_min = hm_to_min(s)
                else:
                    respawn_min = int(round(float(s) * 60))
            if respawn_min is None:
                respawn_min = DEFAULT_RESPAWN_MIN
            initial_delay = parse_initial_delay(x)
            dic[name] = (respawn_min, rate, initial_delay)
        # チェルトゥバ 正式名の吸収（過去表記が違っても正しいキーへ）
        if "チェトゥバ" in dic and "チェルトゥバ" not in dic:
            dic["チェルトゥバ"] = dic["チェトゥバ"]
        self.presets = dic
        print(f"[PRESET] loaded {len(self.presets)} bosses")

    # --- store helpers ---
    def _gkey(self, guild_id: int) -> str: return str(guild_id)

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
        return [BossState(**d) for d in g.values()]

    # --- tick / notifications ---
    @tasks.loop(seconds=CHECK_SEC)
    async def _tick(self):
        await self.wait_until_ready()
        now = now_utc()
        for gkey, bosses in list(self.data.items()):
            guild = self.get_guild(int(gkey))
            if not guild:
                continue

            pre_group: Dict[int, List[str]] = {}
            now_group: Dict[int, List[str]] = {}

            updated_any = False

            for d in list(bosses.values()):
                st = BossState(**d)
                if not st.channel_id or not st.next_spawn_utc:
                    continue
                ch = guild.get_channel(st.channel_id) or await guild.fetch_channel(st.channel_id)
                center = datetime.fromtimestamp(st.next_spawn_utc, tz=timezone.utc)

                # 1分前通知（重複防止：その分のminuteを記録）
                pre_m = int(((center - timedelta(minutes=1)).timestamp()) // 60)
                if abs((now - (center - timedelta(minutes=1))).total_seconds()) <= MERGE_WINDOW_SEC:
                    if st.last_pre_minute_utc != pre_m:
                        label = f"{center.astimezone(JST).strftime('%H:%M:%S')} : {st.name} {st.label_flags()}".strip()
                        pre_group.setdefault(st.channel_id, []).append(label)
                        st.last_pre_minute_utc = pre_m
                        self._set(int(gkey), st)

                # 出現通知（重複防止）
                spawn_m = int((center.timestamp()) // 60)
                if abs((now - center).total_seconds()) <= MERGE_WINDOW_SEC:
                    if st.last_spawn_minute_utc != spawn_m:
                        label = f"{st.name} 出現！ [{center.astimezone(JST).strftime('%H:%M:%S')}] (skip:{st.skip}) {st.label_flags()}".strip()
                        now_group.setdefault(st.channel_id, []).append(label)
                        st.last_spawn_minute_utc = spawn_m
                        self._set(int(gkey), st)

                # 自動スキップ：出現時刻から60秒過ぎたら次周
                if (now - center).total_seconds() >= 60:
                    st.next_spawn_utc += st.respawn_min * 60
                    st.skip += 1
                    st.last_pre_minute_utc = None
                    st.last_spawn_minute_utc = None
                    self._set(int(gkey), st)
                    updated_any = True

            # 送信（チャンネルごとにまとめる）
            for cid, arr in pre_group.items():
                ch = guild.get_channel(cid) or await guild.fetch_channel(cid)
                await ch.send("⏰ 1分前\n" + "\n".join(sorted(arr)))
            for cid, arr in now_group.items():
                ch = guild.get_channel(cid) or await guild.fetch_channel(cid)
                await ch.send("🔥\n" + "\n".join(sorted(arr)))

    @_tick.before_loop
    async def _before_tick(self):
        await self.wait_until_ready()

    # --- util ---
    def _parse_input(self, content: str) -> Optional[Tuple[str, datetime, Optional[int]]]:
        # 例: "コルーン 1120" / "ティミニエル 1121 8h" / "フェリス"
        parts = content.strip().split()
        if len(parts) == 0:
            return None
        name_raw = parts[0]
        name = canonical_name(name_raw) or name_raw
        jst_now = datetime.now(JST)
        hhmm_dt = None
        respawn_min = None

        if len(parts) >= 2 and parts[1].isdigit() and 3 <= len(parts[1]) <= 4:
            p = parts[1].zfill(4)
            try:
                h, m = int(p[:2]), int(p[2:])
                base = jst_now.replace(hour=h, minute=m, second=0, microsecond=0)
                if base > jst_now:
                    base -= timedelta(days=1)  # 未来は前日討伐扱い
                hhmm_dt = base
            except Exception:
                hhmm_dt = None
        if hhmm_dt is None:
            hhmm_dt = jst_now

        if len(parts) >= 3 and parts[2].lower().endswith("h"):
            try:
                respawn_min = int(round(float(parts[2][:-1]) * 60))
            except Exception:
                pass

        return name, hhmm_dt, respawn_min

    async def _send_bt(self, message: discord.Message, horizon_h: Optional[int]):
        arr = self._all(message.guild.id)
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
            await message.channel.send("予定はありません。")
            return

        lines = []
        current_hour = None
        for t, st in items:
            j = t.astimezone(JST)
            if current_hour is None:
                current_hour = j.hour
            if j.hour != current_hour:
                lines.append("")  # ← 改行1つ（段落）
                current_hour = j.hour
            lines.append(f"{j.strftime('%H:%M:%S')} : {st.name} {st.label_flags()}".strip())

        await message.channel.send("\n".join(lines))

    # --- message entrypoint (prefix無し/有り両対応) ---
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        raw = message.content.strip()
        raw_l = raw.lower()

        # 1) まず "!～" を剥がして同じルーティングへ
        if raw_l.startswith("!"):
            cmdline = raw[1:].strip()
        else:
            cmdline = raw

        # 2) 管理系 / ヘルプ
        if cmdline in ("help", "h", "？", "ヘルプ", "!help"):
            await message.channel.send(
                "使い方：\n"
                "• 討伐入力：`ボス名 HHMM [周期h]` 例 `メデューサ 2208` / `ティミニエル 1121 8h`\n"
                "  （時刻省略は現在時刻。未来HHMMは前日扱い）\n"
                "• 一覧：`bt` / `bt3` / `bt6` / `bt12` / `bt24`\n"
                "• 周期変更：`rh ボス名 時間h` 例 `rh コルーン 10h`\n"
                "• 周期一覧：`rhshow [kw]`\n"
                "• プリセット再読込：`preset`\n"
                "• 全体リセット：`reset HHMM`\n"
                "（! を付けても同じ動作：例 `!bt6`）"
            )
            return

        # 3) 一覧ショート（!なしOK）
        if cmdline in ("bt", "bt3", "bt6", "bt12", "bt24", "!bt", "!bt3", "!bt6", "!bt12", "!bt24"):
            key = cmdline.lstrip("!")
            horizon = None
            if key != "bt":
                horizon = int(key.replace("bt", ""))
            await self._send_bt(message, horizon)
            return

        # 4) reset / rh / rhshow / preset / restart / gc
        if cmdline.startswith(("reset ", "!reset ")):
            p = cmdline.split(maxsplit=1)[1].zfill(4)
            try:
                h, m = int(p[:2]), int(p[2:])
            except Exception:
                await message.channel.send("`reset HHMM` の形式で入力してください。")
                return
            base = datetime.now(JST).replace(hour=h, minute=m, second=0, microsecond=0)
            arr = self._all(message.guild.id)
            for st in arr:
                if st.excluded_reset:
                    continue
                # 100%湧き/初回遅延あり → reset + 初回遅延
                # 100%湧き/初回遅延なし → メンテ後一斉湧き（手動入力運用推奨）→ここでは通常周期で回す
                add_min = (st.initial_delay_min or 0)
                center = base + timedelta(minutes=st.respawn_min + add_min)
                st.next_spawn_utc = int(center.astimezone(timezone.utc).timestamp())
                st.skip = 0
                st.last_pre_minute_utc = None
                st.last_spawn_minute_utc = None
                self._set(message.guild.id, st)
            await message.channel.send(f"全体を {base.strftime('%H:%M')} 基準でリセットしました。")
            return

        if cmdline.startswith(("rh ", "!rh ")):
            try:
                _, n, htxt = cmdline.split(maxsplit=2)
                name = canonical_name(n) or n
                h = float(htxt.rstrip("hH"))
                st = self._get(message.guild.id, name) or BossState(name=name, respawn_min=DEFAULT_RESPAWN_MIN)
                st.respawn_min = int(round(h * 60))
                self._set(message.guild.id, st)
                await message.channel.send(f"{name} の周期を {h:.2f}h に設定しました。")
            except Exception:
                await message.channel.send("`rh ボス名 時間h` 例 `rh コルーン 10h`")
            return

        if cmdline.startswith(("rhshow", "!rhshow")):
            kw = None
            sp = cmdline.split(maxsplit=1)
            if len(sp) == 2:
                kw = sp[1]
            arr = sorted(self._all(message.guild.id), key=lambda s: s.name)
            lines = []
            for st in arr:
                if kw and kw not in st.name:
                    continue
                lines.append(f"• {st.name} : {st.respawn_min/60:.2f}h / rate {st.rate}% / 初回遅延{st.initial_delay_min}m")
            await message.channel.send("\n".join(lines) if lines else "登録なし")
            return

        if cmdline in ("preset", "!preset"):
            self._load_presets()
            # 既存反映（周期/出現率/初回遅延）
            arr = self._all(message.guild.id)
            for st in arr:
                if st.name in self.presets:
                    rmin, rate, ide = self.presets[st.name]
                    st.respawn_min = rmin
                    st.rate = rate
                    st.initial_delay_min = ide
                    self._set(message.guild.id, st)
            await message.channel.send("プリセットを再読込しました。")
            return

        if cmdline in ("gc", "!gc"):
            import gc
            gc.collect()
            await message.channel.send("GC Done.")
            return

        if cmdline in ("restart", "!restart"):
            await message.channel.send("再起動します…")
            await self.close()
            return

        # 5) 討伐ショート入力（ボス名…）
        parsed = self._parse_input(cmdline)
        if parsed:
            name, when_jst, respawn_min_override = parsed
            gkey = self._gkey(message.guild.id)
            g = self.data.get(gkey, {})
            st = BossState(name=name, respawn_min=DEFAULT_RESPAWN_MIN, channel_id=message.channel.id)
            if name in g:
                st = BossState(**g[name])
            # プリセット反映（未登録時・不足時）
            if st.name in self.presets:
                p_rmin, p_rate, p_ide = self.presets[st.name]
                st.respawn_min = st.respawn_min or p_rmin
                st.rate = p_rate
                st.initial_delay_min = p_ide
            # 上書き周期
            if respawn_min_override:
                st.respawn_min = respawn_min_override
            # 次湧き = 討伐時刻 + 周期 + 初回遅延（初回のみ）
            add_min = st.initial_delay_min or 0
            center = when_jst.astimezone(timezone.utc) + timedelta(minutes=st.respawn_min + add_min)
            st.next_spawn_utc = int(center.timestamp())
            st.channel_id = st.channel_id or message.channel.id
            st.skip = 0
            st.last_pre_minute_utc = None
            st.last_spawn_minute_utc = None
            self._set(message.guild.id, st)
            try:
                await message.add_reaction("✅")
            except Exception:
                pass
            return

        # 6) ここまで何も該当しなければ無視
        return

    async def on_ready(self):
        print(f"[BOT] ONLINE as {self.user} / guilds={len(self.guilds)}")


# ====== keepalive (FastAPI) ======
app = FastAPI()

@app.get("/health")
async def health(silent: int = 0):
    return {"ok": True, "ts": int(datetime.now(timezone.utc).timestamp())}

# ====== runner with 429 backoff ======
async def run_main():
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN not set")

    bot = BossBot()
    port = int(os.environ.get("PORT", "10000"))

    async def run_bot():
        while True:
            try:
                await bot.start(token)
            except discord.errors.HTTPException as e:
                # Too Many Requests / Cloudflare 1015 を検知してバックオフ
                msg = str(e)
                if "429" in msg or "Too Many Requests" in msg or "1015" in msg or "rate limited" in msg.lower():
                    wait_sec = BACKOFF_429_MIN * 60 + (random.randint(0, BACKOFF_JITTER_SEC) if BACKOFF_JITTER_SEC > 0 else 0)
                    print(f"[BOT] 429/RateLimited を検出。{wait_sec}s 待機して再試行します。")
                    await asyncio.sleep(wait_sec)
                    continue
                else:
                    print("[BOT] HTTPException:", e)
                    await asyncio.sleep(10)
                    continue
            except Exception as e:
                print("[BOT] Exception:", e)
                await asyncio.sleep(10)
                continue
            finally:
                try:
                    await bot.close()
                except Exception:
                    pass
            break

    async def run_api():
        config = Config(app=app, host="0.0.0.0", port=port, loop="asyncio", log_level="info")
        server = Server(config)
        await server.serve()

    await asyncio.gather(run_bot(), run_api())

def run():
    asyncio.run(run_main())

if __name__ == "__main__":
    run()
