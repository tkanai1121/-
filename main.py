# -*- coding: utf-8 -*-
import os, json, re, gc, unicodedata, asyncio, random
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import discord
from discord.ext import tasks
from fastapi import FastAPI, Response
from uvicorn import Config, Server

# ====== CONST ======
JST = timezone(timedelta(hours=9))
DATA_DIR = "data"
STORE_FILE = os.path.join(DATA_DIR, "store.json")
PRESET_FILE = "bosses_preset.json"
CHECK_SEC = 10
MERGE_WINDOW_SEC = 60  # ±60秒で通知集約

# ====== Alias Normalize ======
# カタカナ→ひらがな（大小含む）をざっくり正規化
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
    'ワ':'わ','ヲ':'を','ン':'ん',
    'ァ':'ぁ','ィ':'ぃ','ゥ':'ぅ','ェ':'ぇ','ォ':'ぉ',
    'ッ':'っ','ャ':'ゃ','ュ':'ゅ','ョ':'ょ','ヮ':'ゎ',
    'ヴ':'ゔ'
})
# 英字略称など
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

# ====== Data ======
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
    # 通知重複防止（この湧き=next_spawn_utcに対して送信済みか）
    notified_pre_for: Optional[int] = None
    notified_spawn_for: Optional[int] = None

    def label_flags(self) -> str:
        parts = []
        if self.rate == 100:
            parts.append("※確定")
        if self.skip > 0:
            parts.append(f"{self.skip}周")
        return "[" + "] [".join(parts) + "]" if parts else ""

# ====== Store ======
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
class BossBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # DevPortal側もONに
        super().__init__(intents=intents)

        self.store = Store(STORE_FILE)
        self.data: Dict[str, Dict[str, dict]] = self.store.load()     # guild -> name -> dict / "__cfg__"
        self.presets: Dict[str, Tuple[int, int, int]] = {}            # name -> (respawn_min, rate, initial_delay_min)
        self.alias_map: Dict[str, Dict[str, str]] = {}                # guild -> norm -> canonical（任意登録）
        self._load_presets()
        self._seed_alias = self._build_seed_alias()

    async def setup_hook(self):
        """イベントループ起動後に呼ばれる。ここでタスク開始。"""
        self.tick.start()

    # ---- helpers: storage / ids ----
    def _gkey(self, gid: int) -> str:
        return str(gid)
    def _get(self, gid: int, name: str) -> Optional[BossState]:
        d = self.data.get(self._gkey(gid), {}).get(name)
        return BossState(**d) if d else None
    def _set(self, gid: int, st: BossState):
        gkey = self._gkey(gid)
        if gkey not in self.data:
            self.data[gkey] = {}
        self.data[gkey][st.name] = asdict(st)
        self.store.save(self.data)
    def _all(self, gid: int) -> List[BossState]:
        g = self.data.get(self._gkey(gid), {})
        out: List[BossState] = []
        for k, d in g.items():
            if k == "__cfg__":
                continue
            if isinstance(d, dict) and "respawn_min" in d:
                out.append(BossState(**d))
        return out

    # ---- guild config: allowed channels ----
    def _cfg(self, gid: int) -> dict:
        gkey = self._gkey(gid)
        g = self.data.setdefault(gkey, {})
        cfg = g.get("__cfg__")
        if not cfg:
            cfg = {"channels": []}  # 明示登録チャンネルのみ受け付け
            g["__cfg__"] = cfg
            self.store.save(self.data)
        return cfg
    def _is_channel_enabled(self, gid: int, channel_id: int) -> bool:
        return channel_id in self._cfg(gid).get("channels", [])
    def _enable_channel(self, gid: int, channel_id: int):
        cfg = self._cfg(gid)
        if channel_id not in cfg["channels"]:
            cfg["channels"].append(channel_id)
            self.store.save(self.data)
    def _disable_channel(self, gid: int, channel_id: int):
        cfg = self._cfg(gid)
        if channel_id in cfg["channels"]:
            cfg["channels"].remove(channel_id)
            self.store.save(self.data)

    # ---- presets / alias ----
    def _load_presets(self):
        try:
            with open(PRESET_FILE, "r", encoding="utf-8") as f:
                arr = json.load(f)
            tmp = {}
            for x in arr:
                resp_min = int(round(float(x["respawn_h"]) * 60))
                rate = int(x.get("rate", 100))
                init = int(x.get("initial_delay_min", 0))
                tmp[x["name"]] = (resp_min, rate, init)
            self.presets = tmp
        except Exception as e:
            print("preset load error:", e)
            self.presets = {}

    def _build_seed_alias(self) -> Dict[str, str]:
        seed: Dict[str, str] = {}
        for name in self.presets.keys():
            n = normalize_name(name)
            for L in (2,3,4):
                seed.setdefault(n[:L], name)
        # 旧表記の吸収（正式名：チェルトゥバ）
        seed[normalize_name("チェトゥバ")] = "チェルトゥバ"
        seed[normalize_name("チェトゥルゥバ")] = "チェルトゥバ"
        # 英字略称など
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
            return self._seed_alias(norm)
        cands = [n for n in self.presets.keys() if normalize_name(n).startswith(norm)]
        if len(cands) == 1:
            return cands[0]
        return None

    # ---- input parse ----
    def _parse_kill_input(self, content: str) -> Optional[Tuple[str, datetime, Optional[int]]]:
        parts = content.strip().split()
        if not parts:
            return None
        raw = parts[0]
        jst_now = datetime.now(JST)
        when = None
        respawn_min = None
        if len(parts) >= 2 and parts[1].isdigit() and 3 <= len(parts[1]) <= 4:
            p = parts[1].zfill(4)
            h, m = int(p[:2]), int(p[2:])
            base = jst_now.replace(hour=h, minute=m, second=0, microsecond=0)
            if base > jst_now:
                base -= timedelta(days=1)
            when = base
        if when is None:
            when = jst_now
        if len(parts) >= 3 and parts[2].lower().endswith('h'):
            try:
                respawn_min = int(round(float(parts[2][:-1]) * 60))
            except ValueError:
                pass
        return raw, when, respawn_min

    # ---- background ticker ----
    @tasks.loop(seconds=CHECK_SEC)
    async def tick(self):
        try:
            await self.wait_until_ready()
            now = now_utc()
            for gkey, bosses in list(self.data.items()):
                guild = self.get_guild(int(gkey))
                if not guild:
                    continue
                pre_items: Dict[int, List[str]] = {}
                now_items: Dict[int, List[str]] = {}

                for key, d in bosses.items():
                    if key == "__cfg__":
                        continue
                    st = BossState(**d)
                    if not st.channel_id or not st.next_spawn_utc:
                        continue

                    center = datetime.fromtimestamp(st.next_spawn_utc, tz=timezone.utc)
                    pre_time = center - timedelta(minutes=1)

                    # 1分前（この湧きで未送信なら送る）
                    if (st.notified_pre_for != st.next_spawn_utc and
                        abs((now - pre_time).total_seconds()) <= MERGE_WINDOW_SEC):
                        pre_items.setdefault(st.channel_id, []).append(
                            f"{center.astimezone(JST).strftime('%H:%M:%S')} : {st.name} {st.label_flags()}".strip()
                        )
                        st.notified_pre_for = st.next_spawn_utc
                        self._set(int(gkey), st)

                    # 出現（この湧きで未送信なら送る）
                    if (st.notified_spawn_for != st.next_spawn_utc and
                        abs((now - center).total_seconds()) <= MERGE_WINDOW_SEC):
                        now_items.setdefault(st.channel_id, []).append(
                            f"{st.name} 出現！ [{center.astimezone(JST).strftime('%H:%M:%S')}] (skip:{st.skip}) {st.label_flags()}".strip()
                        )
                        st.notified_spawn_for = st.next_spawn_utc
                        self._set(int(gkey), st)

                    # 出現から1分経過 → 次周へスライド（フラグもリセット）
                    if (now - center).total_seconds() >= 60:
                        st.next_spawn_utc += st.respawn_min * 60
                        st.skip += 1
                        st.notified_pre_for = None
                        st.notified_spawn_for = None
                        self._set(int(gkey), st)

                # 送信（チャンネルごとに1メッセージ）
                for cid, arr in pre_items.items():
                    ch = guild.get_channel(cid) or await guild.fetch_channel(cid)
                    await ch.send("⏰ 1分前 " + "\n".join(sorted(arr)))
                for cid, arr in now_items.items():
                    ch = guild.get_channel(cid) or await guild.fetch_channel(cid)
                    await ch.send("🔥 " + "\n".join(sorted(arr)))
        except Exception as e:
            print("tick error:", repr(e))

    @tick.before_loop
    async def before_tick(self):
        await self.wait_until_ready()

    # ---- helpers to send lists ----
    async def _send_bt_message(self, channel: discord.TextChannel, guild_id: int, horizon_h: Optional[int]):
        arr = self._all(guild_id)
        now = now_utc()
        items: List[Tuple[datetime, BossState]] = []
        for st in arr:
            if not st.next_spawn_utc:
                continue
            t = datetime.fromtimestamp(st.next_spawn_utc, tz=timezone.utc)
            if horizon_h is not None and (t - now).total_seconds() > horizon_h*3600:
                continue
            items.append((t, st))
        items.sort(key=lambda x: x[0])
        if not items:
            await channel.send("予定はありません。")
            return
        lines: List[str] = []
        current_hour = None
        for t, st in items:
            j = t.astimezone(JST)
            if current_hour is None:
                current_hour = j.hour
            if j.hour != current_hour:
                lines.append("")   # 改行は1つだけ
                current_hour = j.hour
            lines.append(f"{j.strftime('%H:%M:%S')} : {st.name} {st.label_flags()}")
        await channel.send("\n".join(lines))

    async def _send_rhshow(self, channel: discord.TextChannel, guild_id: int, kw: Optional[str]):
        arr = sorted(self._all(guild_id), key=lambda s: s.name)
        lines = []
        for st in arr:
            if kw and kw not in st.name:
                continue
            lines.append(f"• {st.name} : {st.respawn_min/60:.2f}h / rate {st.rate}% / delay {st.initial_delay_min}m")
        await channel.send("\n".join(lines) or "登録なし")

    # ---- event: messages ----
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        content = message.content.strip()
        lower = content.lower()

        # 0) bt系は '!' 省略対応（どのチャンネルでも可）
        if lower in ("bt", "bt3", "bt6", "bt12", "bt24"):
            try:
                if lower == "bt":
                    await self._send_bt_message(message.channel, message.guild.id, None)
                elif lower == "bt3":
                    await self._send_bt_message(message.channel, message.guild.id, 3)
                elif lower == "bt6":
                    await self._send_bt_message(message.channel, message.guild.id, 6)
                elif lower == "bt12":
                    await self._send_bt_message(message.channel, message.guild.id, 12)
                elif lower == "bt24":
                    await self._send_bt_message(message.channel, message.guild.id, 24)
            except Exception as e:
                await message.channel.send(f"エラー: {e}")
            return

        # 1) '!' から始まる通常コマンド
        if content.startswith('!'):
            parts = content[1:].split()
            if not parts:
                return
            cmd = parts[0].lower()
            args = parts[1:]
            try:
                if cmd == "bt":
                    await self._send_bt_message(message.channel, message.guild.id, None)
                elif cmd == "bt3":
                    await self._send_bt_message(message.channel, message.guild.id, 3)
                elif cmd == "bt6":
                    await self._send_bt_message(message.channel, message.guild.id, 6)
                elif cmd == "bt12":
                    await self._send_bt_message(message.channel, message.guild.id, 12)
                elif cmd == "bt24":
                    await self._send_bt_message(message.channel, message.guild.id, 24)
                elif cmd == "rhshow":
                    kw = " ".join(args) if args else None
                    await self._send_rhshow(message.channel, message.guild.id, kw)
                elif cmd == "rh" and len(args) >= 2:
                    name, hours = args[0], args[1]
                    canonical = self._resolve_alias(message.guild.id, name) or name
                    st = self._get(message.guild.id, canonical) or BossState(name=canonical, respawn_min=60)
                    h = float(hours.rstrip('hH'))
                    st.respawn_min = int(round(h*60))
                    self._set(message.guild.id, st)
                    await message.channel.send(f"{canonical} の周期を {h}h に設定しました。")
                elif cmd == "reset" and len(args) >= 1:
                    # 仕様：100%&delay0 → 未スケジュール（手動入力）
                    #       100%&delay>0 → base + delay
                    #       50/33%&delay0 → base + 周期
                    #       50/33%&delay>0 → base + delay
                    p = args[0].zfill(4)
                    h, m = int(p[:2]), int(p[2:])
                    base = datetime.now(JST).replace(hour=h, minute=m, second=0, microsecond=0)
                    n_none = n_set = 0
                    for st in self._all(message.guild.id):
                        if st.excluded_reset:
                            continue
                        rate = st.rate or 100
                        delay = int(st.initial_delay_min or 0)
                        if rate == 100 and delay == 0:
                            st.next_spawn_utc = None
                            st.skip = 0
                            st.notified_pre_for = None
                            st.notified_spawn_for = None
                            self._set(message.guild.id, st)
                            n_none += 1
                            continue
                        if rate == 100 and delay > 0:
                            center = base + timedelta(minutes=delay)
                        else:
                            center = base + timedelta(minutes=(delay if delay > 0 else st.respawn_min))
                        st.next_spawn_utc = int(center.astimezone(timezone.utc).timestamp())
                        st.skip = 0
                        st.notified_pre_for = None
                        st.notified_spawn_for = None
                        self._set(message.guild.id, st)
                        n_set += 1
                    await message.channel.send(
                        f"リセット: {base.strftime('%H:%M')} / スケジュール設定 {n_set}件・手動入力待ち {n_none}件"
                    )
                elif cmd == "delay" and len(args) >= 2:
                    # !delay ボス名 10m / 1h / 5
                    name, amount = args[0], args[1].lower()
                    canonical = self._resolve_alias(message.guild.id, name) or name
                    st = self._get(message.guild.id, canonical) or BossState(name=canonical, respawn_min=60)
                    if canonical in self.presets:
                        st.respawn_min, st.rate, _init = self.presets[canonical]
                    if amount.endswith("m"):
                        minutes = int(float(amount[:-1]))
                    elif amount.endswith("h"):
                        minutes = int(round(float(amount[:-1]) * 60))
                    else:
                        minutes = int(float(amount))
                    st.initial_delay_min = max(0, minutes)
                    self._set(message.guild.id, st)
                    await message.channel.send(f"{canonical} の初回遅延を {st.initial_delay_min} 分に設定しました。")
                elif cmd == "delayshow":
                    kw = " ".join(args) if args else None
                    arr = sorted(self._all(message.guild.id), key=lambda s: s.name)
                    lines = []
                    for st in arr:
                        if kw and kw not in st.name:
                            continue
                        lines.append(f"• {st.name} : 初回遅延 {st.initial_delay_min}m / 周期 {st.respawn_min/60:.2f}h / rate {st.rate}%")
                    await message.channel.send("\n".join(lines) or "登録なし")
                # 受け付けチャンネル制御
                elif cmd in ("hereon", "enablehere", "watchon"):
                    self._enable_channel(message.guild.id, message.channel.id)
                    await message.channel.send("✅ このチャンネルを討伐入力の受け付け対象にしました。")
                elif cmd in ("hereoff", "disablehere", "watchoff"):
                    self._disable_channel(message.guild.id, message.channel.id)
                    await message.channel.send("✅ このチャンネルを受け付け対象から外しました。")
                elif cmd in ("hereshow", "watchshow"):
                    cids = self._cfg(message.guild.id).get("channels", [])
                    if not cids:
                        await message.channel.send("（受け付けチャンネル未設定）")
                    else:
                        await message.channel.send("受け付け中: " + " ".join(f"<#{cid}>" for cid in cids))
                elif cmd == "alias" and len(args) >= 2:
                    short = args[0]; canonical = " ".join(args[1:])
                    gkey = self._gkey(message.guild.id)
                    self.alias_map.setdefault(gkey, {})[normalize_name(short)] = canonical
                    await message.channel.send(f"`{short}` を `{canonical}` の別名として登録しました。")
                elif cmd == "aliasshow":
                    g_alias = self.alias_map.get(self._gkey(message.guild.id), {})
                    if not g_alias:
                        await message.channel.send("（別名は未登録です）")
                    else:
                        lines = [f"• {k} → {v}" for k, v in sorted(g_alias.items())]
                        await message.channel.send("\n".join(lines))
                elif cmd == "restart":
                    await message.channel.send("♻️ Botを再起動します...")
                    gc.collect()
                    self.store.save(self.data)
                    await self.close()
                    os._exit(1)
            except Exception as e:
                await message.channel.send(f"エラー: {e}")
            return

        # 2) 討伐入力（許可チャンネル以外は完全無視）
        if not self._is_channel_enabled(message.guild.id, message.channel.id):
            return

        parsed = self._parse_kill_input(content)
        if parsed:
            raw, when_jst, respawn_min_override = parsed
            canonical = self._resolve_alias(message.guild.id, raw)
            if not canonical:
                return  # ボス名不明は黙って無視
            st = self._get(message.guild.id, canonical) or BossState(name=canonical, respawn_min=60)
            if canonical in self.presets:
                pr = self.presets[canonical]
                st.respawn_min, st.rate = pr[0], pr[1]
                if st.initial_delay_min == 0:
                    st.initial_delay_min = int(pr[2] or 0)
            if respawn_min_override:
                st.respawn_min = respawn_min_override
            st.channel_id = st.channel_id or message.channel.id
            center = when_jst.astimezone(timezone.utc) + timedelta(
                minutes=st.respawn_min + st.initial_delay_min
            )
            st.next_spawn_utc = int(center.timestamp())
            st.skip = 0
            st.notified_pre_for = None
            st.notified_spawn_for = None
            self._set(message.guild.id, st)
            await message.add_reaction("✅")

# ====== keepalive (FastAPI) ======
app = FastAPI()
bot: Optional[BossBot] = None

@app.get("/health")
async def health(silent: int = 0):
    # 余計な処理は一切せず即レス
    if silent:
        return Response(status_code=204)  # 本文ゼロ
    return Response(content=b"ok", status_code=200, media_type="text/plain")

@app.head("/health")
async def health_head():
    return Response(status_code=204)

def run():
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN not set")

    global bot
    bot = BossBot()

    async def serve_api_forever():
        # uvicorn が落ちても自動で再起動
        while True:
            try:
                config = Config(
                    app=app,
                    host="0.0.0.0",
                    port=int(os.environ.get("PORT", 10000)),
                    loop="asyncio",
                    access_log=False,
                    log_level="warning",
                    lifespan="off",        # 重要：lifespan無効
                    timeout_keep_alive=5
                )
                server = Server(config)
                await server.serve()
            except Exception as e:
                print("uvicorn crashed:", repr(e))
            await asyncio.sleep(1)

    async def run_bot_forever():
        # Discord 429 / Cloudflare 1015 を踏んだら長めに待機して再試行
        backoff = 30  # 一般エラーの初期待機（秒）
        while True:
            try:
                await bot.start(token)  # 正常なら戻らない
                await asyncio.sleep(5)
            except Exception as e:
                is_429 = False
                try:
                    import discord as _d
                    is_429 = isinstance(e, _d.HTTPException) and getattr(e, "status", None) == 429
                except Exception:
                    pass
                text = str(e).lower()
                if is_429 or "1015" in text or "rate limited" in text:
                    wait = random.randint(900, 1500)  # 15〜25分
                    print(f"[BOT] 429/RateLimited を検出。{wait}s 待機して再試行します。")
                    backoff = 30
                else:
                    wait = backoff
                    backoff = min(backoff * 2, 300)  # 最大5分
                    print(f"[BOT] 例外で再起動: {repr(e)} / {wait}s 後に再試行")
                await asyncio.sleep(wait)

    async def main_async():
        api_task = asyncio.create_task(serve_api_forever())
        bot_task = asyncio.create_task(run_bot_forever())
        await asyncio.gather(api_task, bot_task)

    asyncio.run(main_async())

if __name__ == "__main__":
    run()
