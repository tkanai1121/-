# -*- coding: utf-8 -*-
"""
Render 用：Discord ボス通知 BOT
 - /health は aiohttp で内蔵（FastAPI/uvicorn 不要）
 - タスク開始は setup_hook で実行し、"no running event loop" を回避
 - 1分前/出現 通知はチャンネル単位で厳密に重複抑止（TTL）
 - 管理コマンドは「!」省略でも動作（hereon / hereoff / bt / bt3… / bosses / rh / reset / restart）
 - エイリアス（ユーザー定義）: alias / unalias / aliasshow
"""

import os
import json
import asyncio
import unicodedata
import random
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import discord
from discord.ext import commands, tasks
from aiohttp import web

# -------------------- 基本定数 -------------------- #
JST = timezone(timedelta(hours=9))

DATA_DIR = "data"
STORE_FILE = os.path.join(DATA_DIR, "store.json")
PRESET_FILE = "bosses_preset.json"

# 通知ループ周期 / 集約窓 / 重複抑止TTL
CHECK_SEC = 10
MERGE_WINDOW_SEC = 10
NOTIFY_DEDUP_TTL_SEC = 120  # 同じイベントは 120s 以内は再送しない

# 429（Cloudflare/Discord）対策
BACKOFF_429_MIN = int(os.environ.get("BACKOFF_429_MIN", "900"))  # 15分
BACKOFF_JITTER_SEC = int(os.environ.get("BACKOFF_JITTER_SEC", "30"))

# -------------------- 便利関数 -------------------- #
def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def dt_to_ts(dt: datetime) -> int:
    return int(dt.timestamp())

def ts_to_jst_str(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(JST).strftime("%H:%M:%S")

def jst_now() -> datetime:
    return datetime.now(JST)

def zfill_hhmm(s: str) -> Tuple[int, int]:
    p = s.zfill(4)
    return int(p[:2]), int(p[2:])

def normalize_for_match(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).lower()
    return "".join(ch for ch in s if ch.isalnum())

# -------------------- ストレージ -------------------- #
class Store:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        if not os.path.exists(self.path):
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({}, f, ensure_ascii=False)

    def load(self) -> Dict[str, dict]:
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)

    def save(self, data: Dict[str, dict]):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

# -------------------- モデル -------------------- #
@dataclass
class BossState:
    name: str
    respawn_min: int
    rate: int = 100
    first_delay_min: int = 0
    next_spawn_utc: Optional[int] = None
    channel_id: Optional[int] = None
    skip: int = 0

    def flags(self) -> str:
        parts = []
        if self.rate == 100:
            parts.append("※確定")
        if self.skip > 0:
            parts.append(f"{self.skip}周")
        return "[" + "] [".join(parts) + "]" if parts else ""

# -------------------- BOT 本体 -------------------- #
class BossBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

        self.store = Store(STORE_FILE)
        raw = self.store.load()
        # {guild_id: {bosses:{name:BossState...}, channels:[ids], aliases:{normalized->official}}}
        self.data: Dict[str, dict] = raw

        # プリセット name -> (respawn_min, rate, first_delay_min)
        self.presets: Dict[str, Tuple[int, int, int]] = {}
        # グローバル（プリセット）別名 normalize(alias) -> official_name
        self.preset_alias: Dict[str, str] = {}

        # 送信済みイベント（ギルド別）
        self._sent_keys: Dict[str, Dict[str, int]] = {}

        # aiohttp ヘルスサーバ
        self._health_runner: Optional[web.AppRunner] = None
        self._health_port: int = int(os.environ.get("PORT", 10000))

    # ---- discord.py v2 正式な初期化フック ---- #
    async def setup_hook(self):
        self._load_presets()
        self.tick.start()              # ここでループ開始
        await self._start_health_app() # 同じイベントループで /health を起動

    async def close(self):
        # 終了時にヘルスサーバも止める
        try:
            if self._health_runner:
                await self._health_runner.cleanup()
        finally:
            await super().close()

    # ----------------- プリセット/別名 ----------------- #
    def _load_presets(self):
        try:
            with open(PRESET_FILE, "r", encoding="utf-8") as f:
                arr = json.load(f)

            m: Dict[str, Tuple[int, int, int]] = {}
            alias: Dict[str, str] = {}
            for row in arr:
                name = row["name"]
                rate = int(row.get("rate", 100))
                respawn_h = row.get("interval_h") or row.get("respawn_h") or row.get("間隔") or 0
                respawn_min = int(round(float(respawn_h) * 60))

                first_delay_min = 0
                fd = row.get("first_delay_h") or row.get("初回出現遅延") or 0
                if isinstance(fd, str):
                    if ":" in fd:
                        h, mm = fd.split(":")
                        first_delay_min = int(h) * 60 + int(mm)
                    else:
                        first_delay_min = int(round(float(fd) * 60))
                else:
                    first_delay_min = int(round(float(fd) * 60))

                m[name] = (respawn_min, rate, first_delay_min)

                # 代表的な別名（必要に応じて増やせます）
                nkey = normalize_for_match(name)
                alias[nkey] = name
                if name == "クイーンアント":
                    alias[normalize_for_match("qa")] = name
                    alias[normalize_for_match("queenant")] = name

            self.presets = m
            self.preset_alias = alias
            print(f"INFO: bosses preset loaded: {len(self.presets)} bosses")
        except Exception as e:
            print("WARN: preset load error:", e)
            self.presets = {}
            self.preset_alias = {}

    # ----------------- ギルドデータ操作 ----------------- #
    def _gkey(self, guild_id: int) -> str:
        return str(guild_id)

    def _ensure_guild(self, guild_id: int):
        gkey = self._gkey(guild_id)
        if gkey not in self.data:
            self.data[gkey] = {"bosses": {}, "channels": [], "aliases": {}}
            self.store.save(self.data)
        else:
            # 既存データにaliasesキーが無い古い形式を救う
            if "aliases" not in self.data[gkey]:
                self.data[gkey]["aliases"] = {}
                self.store.save(self.data)

    def _get_boss(self, guild_id: int, name: str) -> Optional[BossState]:
        g = self.data.get(self._gkey(guild_id), {})
        b = g.get("bosses", {}).get(name)
        return BossState(**b) if b else None

    def _set_boss(self, guild_id: int, st: BossState):
        self._ensure_guild(guild_id)
        g = self.data[self._gkey(guild_id)]
        g["bosses"][st.name] = asdict(st)
        self.store.save(self.data)

    def _all_bosses(self, guild_id: int) -> List[BossState]:
        self._ensure_guild(guild_id)
        g = self.data[self._gkey(guild_id)]
        return [BossState(**d) for d in g.get("bosses", {}).values()]

    def _channels(self, guild_id: int) -> List[int]:
        self._ensure_guild(guild_id)
        return self.data[self._gkey(guild_id)].get("channels", [])

    def _set_channels(self, guild_id: int, ids: List[int]):
        self._ensure_guild(guild_id)
        self.data[self._gkey(guild_id)]["channels"] = ids
        self.store.save(self.data)

    def _aliases(self, guild_id: int) -> Dict[str, str]:
        self._ensure_guild(guild_id)
        return self.data[self._gkey(guild_id)]["aliases"]

    # ----------------- 入力パース ----------------- #
    def _resolve_boss_name(self, user_text: str, guild_id: int) -> Optional[str]:
        # 正式名一致
        if user_text in self.presets:
            return user_text

        key = normalize_for_match(user_text)

        # 1) ユーザー定義 alias（ギルドごと）
        user_alias = self._aliases(guild_id)
        if key in user_alias:
            return user_alias[key]

        # 2) プリセット alias
        if key in self.preset_alias:
            return self.preset_alias[key]

        # 3) 先頭一致・部分一致（プリセット名）
        for off in self.presets.keys():
            n = normalize_for_match(off)
            if n.startswith(key) or key in n:
                return off
        return None

    def _parse_kill_input(self, content: str, guild_id: int) -> Optional[Tuple[str, datetime, Optional[int]]]:
        # 例: 「スタン 1120」 / 「スタン 1120 4h」 / 「フェリス」
        parts = content.strip().split()
        if len(parts) == 0:
            return None
        name_txt = parts[0]
        off_name = self._resolve_boss_name(name_txt, guild_id)
        if not off_name:
            return None

        jnow = jst_now()
        kill_dt = jnow
        respawn_min = None

        if len(parts) >= 2 and parts[1].isdigit() and 3 <= len(parts[1]) <= 4:
            h, m = zfill_hhmm(parts[1])
            base = jnow.replace(hour=h, minute=m, second=0, microsecond=0)
            if base > jnow:
                base -= timedelta(days=1)
            kill_dt = base

        if len(parts) >= 3 and parts[2].lower().endswith("h"):
            try:
                respawn_min = int(round(float(parts[2][:-1]) * 60))
            except Exception:
                respawn_min = None

        return off_name, kill_dt, respawn_min

    # ----------------- 通知の重複抑止 ----------------- #
    def _sent_bucket(self, guild_id: int) -> Dict[str, int]:
        gkey = self._gkey(guild_id)
        if gkey not in self._sent_keys:
            self._sent_keys[gkey] = {}
        return self._sent_keys[gkey]

    def _mark_sent(self, guild_id: int, key: str):
        self._sent_bucket(guild_id)[key] = dt_to_ts(now_utc()) + NOTIFY_DEDUP_TTL_SEC

    def _already_sent(self, guild_id: int, key: str) -> bool:
        b = self._sent_bucket(guild_id)
        ts = b.get(key)
        return ts is not None and ts >= dt_to_ts(now_utc())

    def _cleanup_sent(self):
        nowts = dt_to_ts(now_utc())
        for gkey, b in list(self._sent_keys.items()):
            for k, ttl in list(b.items()):
                if ttl < nowts:
                    b.pop(k, None)

    # ----------------- 通知ループ ----------------- #
    @tasks.loop(seconds=CHECK_SEC)
    async def tick(self):
        """1分前 & 出現 の通知を1回だけ送る。処理はチャンネルごとに集約。"""
        if not self.is_ready():
            return

        self._cleanup_sent()
        n = now_utc()

        for gkey in list(self.data.keys()):
            guild = self.get_guild(int(gkey))
            if not guild:
                continue

            pre_labels: Dict[int, List[str]] = {}
            now_labels: Dict[int, List[str]] = {}

            for st in self._all_bosses(guild.id):
                if not st.next_spawn_utc or not st.channel_id:
                    continue

                center = datetime.fromtimestamp(st.next_spawn_utc, tz=timezone.utc)

                # 1分前
                pre_key = f"pre|{st.channel_id}|{st.next_spawn_utc}|{st.name}"
                if abs((n - (center - timedelta(minutes=1))).total_seconds()) <= MERGE_WINDOW_SEC:
                    if not self._already_sent(guild.id, pre_key):
                        pre_labels.setdefault(st.channel_id, []).append(
                            f"{ts_to_jst_str(st.next_spawn_utc)} : {st.name} {st.flags()}".strip()
                        )
                        self._mark_sent(guild.id, pre_key)

                # 出現
                now_key = f"now|{st.channel_id}|{st.next_spawn_utc}|{st.name}"
                if abs((n - center).total_seconds()) <= MERGE_WINDOW_SEC:
                    if not self._already_sent(guild.id, now_key):
                        now_labels.setdefault(st.channel_id, []).append(
                            f"{st.name} 出現！ [{ts_to_jst_str(st.next_spawn_utc)}] (skip:{st.skip}) {st.flags()}".strip()
                        )
                        self._mark_sent(guild.id, now_key)

                # 自動スライド（出現＋60秒）
                if (n - center).total_seconds() >= 60:
                    st.next_spawn_utc += st.respawn_min * 60
                    st.skip += 1
                    self._set_boss(guild.id, st)

            # 集約送信（1チャンネル1メッセージ）
            for cid, arr in pre_labels.items():
                try:
                    ch = guild.get_channel(cid) or await guild.fetch_channel(cid)
                    if arr:
                        await ch.send("⏰ 1分前\n" + "\n".join(sorted(arr)))
                except Exception:
                    pass

            for cid, arr in now_labels.items():
                try:
                    ch = guild.get_channel(cid) or await guild.fetch_channel(cid)
                    if arr:
                        await ch.send("🔥\n" + "\n".join(sorted(arr)))
                except Exception:
                    pass

    # ----------------- メッセージ監視 ----------------- #
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        content = message.content.strip()

        # まずは「管理コマンド」（!省略可）
        if await self._maybe_handle_text_command(message, content):
            return

        # 監視対象チャンネル以外は無視
        if message.channel.id not in self._channels(message.guild.id):
            return

        # 討伐入力（「ボス名 HHMM [x h]」）
        parsed = self._parse_kill_input(content, message.guild.id)
        if parsed:
            name, when_jst, respawn_override = parsed
            st = self._get_boss(message.guild.id, name) or BossState(
                name=name,
                respawn_min=self.presets.get(name, (60, 100, 0))[0],
                rate=self.presets.get(name, (60, 100, 0))[1],
                first_delay_min=self.presets.get(name, (60, 100, 0))[2],
            )
            if respawn_override:
                st.respawn_min = respawn_override
            st.channel_id = st.channel_id or message.channel.id
            center = when_jst.astimezone(timezone.utc) + timedelta(minutes=st.respawn_min)
            st.next_spawn_utc = dt_to_ts(center)
            st.skip = 0
            self._set_boss(message.guild.id, st)
            try:
                await message.add_reaction("✅")
            except Exception:
                pass
            return

        await self.process_commands(message)

    # ----------------- テキストコマンド群（!省略OK） ----------------- #
    async def _maybe_handle_text_command(self, message: discord.Message, content: str) -> bool:
        raw = content
        if raw.startswith("!"):
            raw = raw[1:].strip()
        low = raw.lower()

        # hereon/hereoff
        if low in ("hereon", "here on"):
            ids = self._channels(message.guild.id)
            if message.channel.id not in ids:
                ids.append(message.channel.id)
                self._set_channels(message.guild.id, ids)
            await message.channel.send("このチャンネルを**監視対象ON**にしました。")
            return True

        if low in ("hereoff", "here off"):
            ids = self._channels(message.guild.id)
            if message.channel.id in ids:
                ids.remove(message.channel.id)
                self._set_channels(message.guild.id, ids)
            await message.channel.send("このチャンネルを**監視対象OFF**にしました。")
            return True

        # bt / bt3 / bt6 / bt12 / bt24
        if low in ("bt", "bt3", "bt6", "bt12", "bt24"):
            horizon = None if low == "bt" else int(low[2:])
            await self._send_bt(message.channel, message.guild.id, horizon)
            return True

        # プリセット一覧
        if low in ("bosses", "list", "bname", "bnames"):
            lines = []
            for name, (rm, rate, fd) in sorted(self.presets.items(), key=lambda x: x[0]):
                lines.append(f"• {name} : {rm/60:.2f}h / rate {rate}% / 初回遅延 {fd}分")
            await message.channel.send("\n".join(lines) or "プリセット無し")
            return True

        # 周期変更: rh ボス名 8h
        if low.startswith("rh "):
            parts = raw.split()
            if len(parts) >= 3:
                name = self._resolve_boss_name(parts[1], message.guild.id) or parts[1]
                try:
                    h = float(parts[2].rstrip("hH"))
                    st = self._get_boss(message.guild.id, name) or BossState(
                        name=name,
                        respawn_min=self.presets.get(name, (60, 100, 0))[0],
                        rate=self.presets.get(name, (60, 100, 0))[1],
                        first_delay_min=self.presets.get(name, (60, 100, 0))[2],
                    )
                    st.respawn_min = int(round(h * 60))
                    self._set_boss(message.guild.id, st)
                    await message.channel.send(f"{name} の周期を {h}h に設定しました。")
                except Exception:
                    await message.channel.send("`rh ボス名 時間h` の形式で。")
            else:
                await message.channel.send("`rh ボス名 時間h` の形式で。")
            return True

        # 全体リセット: reset HHMM
        if low.startswith("reset "):
            p = raw.split()
            if len(p) == 2 and p[1].isdigit():
                h, m = zfill_hhmm(p[1])
                base = jst_now().replace(hour=h, minute=m, second=0, microsecond=0)
                await self._reset_all(message.guild.id, base)
                await message.channel.send(f"全体を {base.strftime('%H:%M')} リセットしました。")
            else:
                await message.channel.send("`reset HHMM` の形式で。")
            return True

        # --- エイリアス: alias / unalias / aliasshow ---
        if low.startswith("alias "):
            # alias <短縮> <正式名>
            parts = raw.split(maxsplit=2)
            if len(parts) >= 3:
                short = normalize_for_match(parts[1])
                official = self._resolve_boss_name(parts[2], message.guild.id) or parts[2]
                if official not in self.presets:
                    await message.channel.send(f"正式名が見つかりません：{parts[2]}\n`bosses` で確認してください。")
                else:
                    a = self._aliases(message.guild.id)
                    a[short] = official
                    self.store.save(self.data)
                    await message.channel.send(f"エイリアス登録： `{parts[1]}` → **{official}**")
            else:
                await message.channel.send("`alias <短縮> <正式名>` の形式で。")
            return True

        if low.startswith("unalias "):
            # unalias <短縮>
            parts = raw.split(maxsplit=1)
            if len(parts) == 2:
                short = normalize_for_match(parts[1])
                a = self._aliases(message.guild.id)
                if short in a:
                    off = a.pop(short)
                    self.store.save(self.data)
                    await message.channel.send(f"エイリアス削除： `{parts[1]}` （→ {off}）")
                else:
                    await message.channel.send("そのエイリアスは登録されていません。")
            else:
                await message.channel.send("`unalias <短縮>` の形式で。")
            return True

        if low in ("aliasshow", "aliaslist", "alias show"):
            # プリセット分＋ユーザー定義を表示
            lines = ["【ユーザー定義（このギルド）】"]
            ua = self._aliases(message.guild.id)
            if ua:
                for k, v in sorted(ua.items()):
                    lines.append(f"- {k} -> {v}")
            else:
                lines.append("- （なし）")
            lines.append("")
            lines.append("【プリセット】")
            for k, v in sorted(self.preset_alias.items()):
                lines.append(f"- {k} -> {v}")
            await message.channel.send("\n".join(lines))
            return True

        # 再起動
        if low == "restart":
            await message.channel.send("再起動します。保存済みデータは引き継ぎます…")
            await asyncio.sleep(1)
            os._exit(0)

        # ヘルプ
        if low in ("help", "commands"):
            await message.channel.send(self._help_text())
            return True

        return False

    def _help_text(self) -> str:
        return (
            "【使い方】\n"
            "- 討伐入力：`ボス名 HHMM [周期h]` 例:`スタン 1120` / `ティミニエル 0930 8h`\n"
            "- 一覧：`bt` / `bt3` / `bt6` / `bt12` / `bt24`（!省略OK）\n"
            "- 監視ON/OFF：`hereon` / `hereoff`\n"
            "- 周期変更：`rh ボス名 8h`\n"
            "- 一覧(プリセット)：`bosses`\n"
            "- 全体リセット：`reset HHMM`（未登録ならプリセットを自動登録してから計算）\n"
            "- エイリアス：`aliasshow` / `alias <短縮> <正式名>` / `unalias <短縮>`\n"
            "- 再起動：`restart`（Render が自動再起動）\n"
        )

    # ---- 置き換え版 reset: ボス未登録ならプリセット全件をシードしてから計算 ----
    async def _reset_all(self, guild_id: int, base_jst: datetime):
        """
        仕様：
          - ギルドにボス未登録なら、プリセット全件を生成してから計算
          - 100% & 初回遅延0      → next=None（手動入力待ち）
          - 100% & 初回遅延あり   → reset + 初回遅延
          - 50%/33% & 初回遅延0   → reset + 通常周期
          - 50%/33% & 初回遅延あり → reset + 初回遅延
        """
        bosses = self._all_bosses(guild_id)

        # 初回: ボスが1件も無ければプリセットから全件作成（channel_idは未設定のまま）
        if not bosses:
            for name, (rm, rate, fd) in self.presets.items():
                self._set_boss(guild_id, BossState(
                    name=name, respawn_min=rm, rate=rate, first_delay_min=fd,
                    next_spawn_utc=None, channel_id=None, skip=0
                ))
            bosses = self._all_bosses(guild_id)

        # 計算ロジック
        for st in bosses:
            # 最新プリセット値を反映（プリセット側を後から直した場合にも追従）
            rm, rate, fd = self.presets.get(st.name, (st.respawn_min, st.rate, st.first_delay_min))
            st.respawn_min, st.rate, st.first_delay_min = rm, rate, fd

            if st.rate == 100 and st.first_delay_min == 0:
                st.next_spawn_utc = None
                st.skip = 0
            elif st.rate == 100 and st.first_delay_min > 0:
                center = base_jst.astimezone(timezone.utc) + timedelta(minutes=st.first_delay_min)
                st.next_spawn_utc = dt_to_ts(center)
                st.skip = 0
            elif st.rate in (50, 33) and st.first_delay_min == 0:
                center = base_jst.astimezone(timezone.utc) + timedelta(minutes=st.respawn_min)
                st.next_spawn_utc = dt_to_ts(center)
                st.skip = 0
            else:  # 50/33 & 初回遅延あり
                center = base_jst.astimezone(timezone.utc) + timedelta(minutes=st.first_delay_min)
                st.next_spawn_utc = dt_to_ts(center)
                st.skip = 0

            self._set_boss(guild_id, st)

    async def _send_bt(self, channel: discord.TextChannel, guild_id: int, horizon_h: Optional[int]):
        """時刻順、時台切替で改行1つ"""
        items = []
        now = now_utc()
        for st in self._all_bosses(guild_id):
            if not st.next_spawn_utc:
                continue
            t = datetime.fromtimestamp(st.next_spawn_utc, tz=timezone.utc)
            if horizon_h is not None and (t - now).total_seconds() > horizon_h * 3600:
                continue
            items.append((t, st))
        items.sort(key=lambda x: x[0])

        if not items:
            await channel.send("予定はありません。")
            return

        lines = []
        cur_h = None
        for t, st in items:
            j = t.astimezone(JST)
            if cur_h is None:
                cur_h = j.hour
            if j.hour != cur_h:
                lines.append("")  # 改行1つ
                cur_h = j.hour
            lines.append(f"{j.strftime('%H:%M:%S')} : {st.name} {st.flags()}")

        await channel.send("\n".join(lines))

    # -------------------- aiohttp /health -------------------- #
    async def _start_health_app(self):
        async def health(_req):
            return web.json_response({"ok": True})

        app = web.Application()
        app.add_routes([web.get("/health", health), web.get("/", health)])

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self._health_port)
        await site.start()
        self._health_runner = runner
        print(f"INFO: health server started on 0.0.0.0:{self._health_port}")

# -------------------- 起動 -------------------- #
async def _main():
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN not set")

    bot = BossBot()

    # 429 を食らったらバックオフして再試行
    while True:
        try:
            await bot.start(token)
        except discord.errors.HTTPException as e:
            if getattr(e, "status", None) == 429:
                wait = BACKOFF_429_MIN * 60 + random.randint(0, BACKOFF_JITTER_SEC)
                print(f"WARN: 429 detected. backoff {wait}s")
                await asyncio.sleep(wait)
                continue
            raise
        finally:
            # 正常終了や例外時にもヘルスサーバを確実に閉じる
            try:
                await bot.close()
            except Exception:
                pass
        break

if __name__ == "__main__":
    asyncio.run(_main())
