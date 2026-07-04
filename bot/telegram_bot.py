import base64
import csv
import fnmatch
import hashlib
import hmac as _hmac
import io
import logging
import re
import time
from datetime import datetime
from typing import Callable, Optional

from telegram import (
    BotCommand,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from .config import AppConfig
from . import db, graph

log = logging.getLogger(__name__)

_SILENT = {"disable_notification": True}


def _fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _fmt_ago(secs: int) -> str:
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def _fmt_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024


class TelegramBot:
    def __init__(self, cfg: AppConfig, reload_fn: Optional[Callable[[], AppConfig]] = None):
        self._cfg = cfg
        self._reload_fn = reload_fn
        self._bot_username: Optional[str] = None
        self.last_mqtt_fn: Optional[Callable[[], Optional[int]]] = None
        self.reset_alarm_fn: Optional[Callable[[str], None]] = None
        self._app = Application.builder().token(cfg.telegram_token).build()
        self._app.add_handler(CommandHandler("start", self._cmd_start))
        self._app.add_handler(CommandHandler("digest", self._cmd_digest))
        self._app.add_handler(CommandHandler("silent", self._cmd_silent))
        self._app.add_handler(CommandHandler("list", self._cmd_list))
        self._app.add_handler(CommandHandler("get", self._cmd_get))
        self._app.add_handler(CommandHandler("setalarm", self._cmd_setalarm))
        self._app.add_handler(CommandHandler("setAlarmLow", self._cmd_setalarmlow))
        self._app.add_handler(CommandHandler("clearAlarm", self._cmd_clearalarm))
        self._app.add_handler(CommandHandler("clearAlarmLow", self._cmd_clearalarmlow))
        self._app.add_handler(CommandHandler("getAlarm", self._cmd_getalarm))
        self._app.add_handler(CommandHandler("graph", self._cmd_graph))
        self._app.add_handler(CommandHandler("ackOff", self._cmd_ackoff))
        self._app.add_handler(CommandHandler("help", self._cmd_help))
        self._app.add_handler(CommandHandler("myid", self._cmd_myid))
        self._app.add_handler(CommandHandler("last", self._cmd_last))
        self._app.add_handler(CommandHandler("lastAlarms", self._cmd_lastalarms))
        self._app.add_handler(CommandHandler("last5Alarm", self._cmd_last5alarm))
        self._app.add_handler(CommandHandler("forgetSensor", self._cmd_forgetsensor))
        self._app.add_handler(CommandHandler("reloadConfig", self._cmd_reloadconfig))
        self._app.add_handler(CommandHandler("exprSyntax", self._cmd_exprsyntax))
        self._app.add_handler(CommandHandler("csv", self._cmd_csv))
        self._app.add_handler(CommandHandler("xlsx", self._cmd_xlsx))
        self._app.add_handler(CommandHandler("usersActivity", self._cmd_usersactivity))
        self._app.add_handler(CommandHandler("dbStats", self._cmd_dbstats))
        # captures the argument for menu commands that Telegram sends
        # immediately. Catches both replies to the ForceReply prompt (phone)
        # and a plain follow-up message (browser ignores ForceReply focus).
        self._app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_arg_reply)
        )
        # runs first on every update: record last interaction per user
        self._app.add_handler(TypeHandler(Update, self._record_activity), group=-1)

        # message_id of a pending ForceReply prompt -> command key to dispatch
        self._arg_prompts: dict[int, str] = {}
        # user_id -> (command key, prompt timestamp); fallback when the client
        # does not reply to the prompt (e.g. Telegram Web). 30s window.
        self._pending: dict[int, tuple[str, float]] = {}

    # ── token helpers ──────────────────────────────────────────────────────────

    def _make_token(self, chat_id: int) -> str:
        ts = int(time.time())
        key = self._cfg.telegram_token.encode()
        msg = f"{chat_id}:{ts}".encode()
        sig = base64.urlsafe_b64encode(
            _hmac.new(key, msg, hashlib.sha256).digest()[:16]
        ).rstrip(b"=").decode()
        return f"{chat_id}_{ts}_{sig}"

    def _verify_token(self, token: str, sender_id: int) -> bool:
        try:
            parts = token.split("_", 2)
            if len(parts) != 3:
                return False
            chat_id_str, ts_str, sig = parts
            if int(chat_id_str) != sender_id:
                return False
            if abs(int(time.time()) - int(ts_str)) > 86400:
                return False
            key = self._cfg.telegram_token.encode()
            msg = f"{chat_id_str}:{ts_str}".encode()
            expected = base64.urlsafe_b64encode(
                _hmac.new(key, msg, hashlib.sha256).digest()[:16]
            ).rstrip(b"=").decode()
            return _hmac.compare_digest(sig, expected)
        except Exception:
            return False

    # ── DM helpers ─────────────────────────────────────────────────────────────

    async def _send_registration_prompt(self, user_id: int, group_id: int):
        token = self._make_token(user_id)
        username = self._bot_username or "this_bot"
        url = f"https://t.me/{username}?start={token}"
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Avvia bot", url=url)]]
        )
        try:
            await self._app.bot.send_message(
                chat_id=group_id,
                text=(
                    "Replies and alarm notifications are sent via private message.\n"
                    "Tap the button below to open the bot chat, then press Start inside that window.\n"
                    "After that, you can use all commands directly from the private chat."
                ),
                reply_markup=keyboard,
                **_SILENT,
            )
        except Exception:
            log.exception("Failed to send registration prompt to group %s", group_id)

    async def _get_reply_chat(self, update: Update) -> Optional[int]:
        """Returns the DM chat_id to reply to, or None (sends registration prompt)."""
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        if chat_id == user_id:
            db.register_dm(user_id)
            return user_id
        if db.is_dm_registered(user_id):
            return user_id
        await self._send_registration_prompt(user_id, chat_id)
        return None

    # ── ForceReply argument prompts ─────────────────────────────────────────────

    # command key -> (handler, prompt text, input field placeholder)
    _ARG_DISPATCH: dict[str, tuple[str, str]] = {
        "graph": ("📊 /graph — send: <expr> [Nh]", "expr [Nh]"),
        "csv": ("📄 /csv — send: <expr> [Nh]", "expr [Nh]"),
        "xlsx": ("📑 /xlsx — send: <expr> [Nh]", "expr [Nh]"),
        "last5alarm": ("🔔 /last5Alarm — send: <sensor>", "sensor"),
    }

    _ARG_PENDING_WINDOW = 30  # seconds a bare command waits for its argument

    async def _prompt_args(self, reply_chat: int, cmd_key: str):
        """Ask the user for arguments via ForceReply; routed back in _on_arg_reply."""
        text, placeholder = self._ARG_DISPATCH[cmd_key]
        msg = await self._app.bot.send_message(
            chat_id=reply_chat,
            text=text,
            reply_markup=ForceReply(input_field_placeholder=placeholder),
            **_SILENT,
        )
        self._arg_prompts[msg.message_id] = cmd_key
        # reply_chat is the user's DM id; fallback for clients that ignore ForceReply
        self._pending[reply_chat] = (cmd_key, time.time(), msg.message_id)

    async def _on_arg_reply(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        msg = update.message
        if msg is None or not msg.text:
            return
        user_id = update.effective_user.id

        cmd_key = None
        prompt_id = None
        # phone: argument arrives as a reply to the ForceReply prompt
        if msg.reply_to_message is not None:
            cmd_key = self._arg_prompts.pop(msg.reply_to_message.message_id, None)
            if cmd_key is not None:
                prompt_id = msg.reply_to_message.message_id
        # browser: ForceReply focus ignored -> plain follow-up message
        if cmd_key is None:
            pending = self._pending.get(user_id)
            if pending is not None:
                key, ts, pid = pending
                self._pending.pop(user_id, None)
                if time.time() - ts <= self._ARG_PENDING_WINDOW:
                    cmd_key = key
                    prompt_id = pid
        if cmd_key is None:
            return

        self._pending.pop(user_id, None)
        # remove the ForceReply prompt so its reply box clears on every client
        if prompt_id is not None:
            self._arg_prompts.pop(prompt_id, None)
            try:
                await self._app.bot.delete_message(chat_id=user_id, message_id=prompt_id)
            except Exception:
                log.debug("could not delete arg prompt %s", prompt_id)
        handlers = {
            "graph": self._cmd_graph,
            "csv": self._cmd_csv,
            "xlsx": self._cmd_xlsx,
            "last5alarm": self._cmd_last5alarm,
        }
        ctx.args = msg.text.split()
        await handlers[cmd_key](update, ctx)

    async def _record_activity(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        u = update.effective_user
        if u is None:
            return
        try:
            db.record_activity(u.id, u.username, u.full_name)
        except Exception:
            log.exception("record_activity failed for user %s", u.id)

    async def send(self, text: str, silent: bool = False):
        await self._app.bot.send_message(
            chat_id=self._cfg.telegram_group_id,
            text=text,
            disable_notification=silent,
        )

    async def send_dm_to(
        self, chat_id: int, text: str, silent: bool = False, parse_mode: Optional[str] = None
    ):
        await self._app.bot.send_message(
            chat_id=chat_id,
            text=text,
            disable_notification=silent,
            parse_mode=parse_mode,
        )

    async def notify_sensor(self, sensor: str, text: str):
        for chat_id in self._cfg.viewers_of(sensor):
            if db.is_dm_registered(chat_id) and not db.is_muted(chat_id, sensor):
                try:
                    await self._app.bot.send_message(chat_id=chat_id, text=text)
                except Exception:
                    log.exception("DM notify failed for chat_id %s", chat_id)

    async def notify_device(self, device_key: str, text: str):
        device = self._cfg.devices.get(device_key)
        if device is None:
            return
        notified: set[int] = set()
        for sc in device.fields.values():
            for chat_id in self._cfg.admins_of(sc.name):
                if chat_id in notified or not db.is_dm_registered(chat_id):
                    continue
                if sc.name in db.get_digest_subscriptions(chat_id):
                    notified.add(chat_id)
                    try:
                        await self._app.bot.send_message(chat_id=chat_id, text=text)
                    except Exception:
                        log.exception("DM notify failed for chat_id %s", chat_id)

    # ── digest ─────────────────────────────────────────────────────────────────

    def _uptime_str(self, bot_start: float) -> str:
        uptime = int(time.time() - bot_start)
        days = uptime // 86400
        hours = (uptime % 86400) // 3600
        if days > 0 and hours > 0:
            return f"{days}d {hours}h"
        if days > 0:
            return f"{days}d"
        if hours > 0:
            return f"{hours}h"
        return "<1h"

    def build_uptime(self, bot_start: float) -> str:
        return f"🟢 live since {self._uptime_str(bot_start)}"

    def build_digest(self, bot_start: float, user_id: int) -> str:
        # Same output as /get with no args: subscribed & visible sensors,
        # rendered as the shared monospace table.
        subscribed = set(db.get_digest_subscriptions(user_id))
        visible = set(self._cfg.visible_sensors(user_id))
        names = [n for n in self._cfg.sensors if n in subscribed and n in visible]
        names = self._apply_sort(names, None)
        return self._render_sensors_text(names) or ""

    # ── formatting helpers ─────────────────────────────────────────────────────

    def _fmt_alarms(self, rows) -> str:
        if not rows:
            return "No alarms recorded."
        dot = {"ALARM": "🔴", "ALARM_LOW": "🔴", "OK": "🟢", "OK_LOW": "🟢"}
        out = []
        for r in rows:
            msg = r["message"]
            # emoji is derived from kind at display time; strip any leading
            # marker left in historical rows (ALARM/OK word or 🔴/🟢).
            first, _, rest = msg.partition(" ")
            if first in ("ALARM", "OK", "🔴", "🟢"):
                msg = rest
            out.append(f"[{_fmt_ts(r['ts'])}] {dot.get(r['kind'], '')} {msg}")
        return "\n".join(out)

    # ── sensor resolution ──────────────────────────────────────────────────────

    def _resolve_sensors(self, args: list[str], user_id: int) -> list[str]:
        visible = set(self._cfg.visible_sensors(user_id))
        ordered = [n for n in self._cfg.sensors if n in visible]
        patterns = []
        for a in args:
            patterns.extend(p.strip() for p in a.split(",") if p.strip())
        result, seen = [], set()
        for pat in patterns:
            for n in ordered:
                if n not in seen and fnmatch.fnmatch(n.lower(), pat.lower()):
                    result.append(n)
                    seen.add(n)
        return result

    def _extract_sort(self, args: list[str]) -> tuple[list[str], Optional[str]]:
        """Split out a -f/-s sort flag from /get args. Last flag wins."""
        sort_key, rest = None, []
        for a in args:
            if a in ("-f", "-s"):
                sort_key = a
            else:
                rest.append(a)
        return rest, sort_key

    def _apply_sort(self, names: list[str], sort_key: Optional[str]) -> list[str]:
        if sort_key == "-s":
            return sorted(names, key=lambda n: n.lower())
        # default (and -f): group by field (suffix after device_key)
        def key(n: str):
            sc = self._cfg.sensors.get(n)
            fk = n[len(sc.device_key) + 1:] if sc and sc.device_key else n
            return (fk.lower(), n.lower())
        return sorted(names, key=key)

    def _render_sensors_text(self, names: list[str]) -> Optional[str]:
        """Build the monospace sensor table shared by /get and the digest.
        Returns a Markdown code block, or None if no sensors resolve."""
        rows_map = {r["sensor"]: r for r in db.get_all_latest()}
        now = int(time.time())
        entries = []  # (name, value, ago)
        for name in names:
            sc = self._cfg.sensors.get(name)
            if sc is None:
                continue
            r = rows_map.get(name)
            if r:
                val = self._cfg.fmt(name, r['value'])
                mins = (now - r["ts"]) // 60
                ago = "∞" if mins > 360 else str(mins)
            else:
                val = "-"
                ago = "∞"
            entries.append((name, val, ago))
        if not entries:
            return None
        wname = max(len("Sensor"), *(len(e[0]) for e in entries))
        wval = max(len("value"), *(len(e[1]) for e in entries))
        wago = max(len("min ago"), *(len(e[2]) for e in entries))
        lines = [f"{'Sensor':<{wname}}  {'value':>{wval}}  {'min ago':>{wago}}", ""]
        for n, v, a in entries:
            lines.append(f"{n:<{wname}}  {v:>{wval}}  {a:>{wago}}")
        return "```\n" + "\n".join(lines) + "\n```"

    async def _show_sensors(self, reply_chat: int, names: list[str]):
        text = self._render_sensors_text(names) if names else None
        if text is None:
            await self._app.bot.send_message(
                chat_id=reply_chat, text="No matching sensors.", **_SILENT
            )
            return
        await self._app.bot.send_message(
            chat_id=reply_chat,
            text=text,
            parse_mode="Markdown",
            **_SILENT,
        )

    # ── commands ───────────────────────────────────────────────────────────────

    async def _cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if ctx.args:
            if self._verify_token(ctx.args[0], user_id):
                db.register_dm(user_id)
                await update.effective_chat.send_message(
                    "Registration complete. Replies and notifications will be sent here.", **_SILENT
                )
        else:
            db.register_dm(user_id)
            await update.effective_chat.send_message("Bot activated.", **_SILENT)

    async def _cmd_digest(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        reply_chat = await self._get_reply_chat(update)
        if reply_chat is None:
            return
        user_id = update.effective_user.id

        if not ctx.args:
            subs = db.get_digest_subscriptions(user_id)
            visible = set(self._cfg.visible_sensors(user_id))
            active = [s for s in subs if s in visible]
            if not active:
                text = "No digest subscriptions."
            else:
                text = "Digest subscriptions:\n" + "\n".join(f"  {s}" for s in active)
            await self._app.bot.send_message(chat_id=reply_chat, text=text, **_SILENT)
            return

        if len(ctx.args) < 2 or ctx.args[-1].lower() not in ("on", "off"):
            await self._app.bot.send_message(
                chat_id=reply_chat, text="Usage: /digest <expr> on|off", **_SILENT
            )
            return

        action = ctx.args[-1].lower()
        names = self._resolve_sensors(ctx.args[:-1], user_id)
        if not names:
            await self._app.bot.send_message(
                chat_id=reply_chat, text="No matching sensors.", **_SILENT
            )
            return

        for name in names:
            if action == "on":
                db.subscribe_digest(user_id, name)
            else:
                db.unsubscribe_digest(user_id, name)

        verb = "Subscribed to" if action == "on" else "Unsubscribed from"
        await self._app.bot.send_message(
            chat_id=reply_chat,
            text=f"{verb}: {', '.join(names)}",
            **_SILENT,
        )

    async def _cmd_silent(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        reply_chat = await self._get_reply_chat(update)
        if reply_chat is None:
            return
        user_id = update.effective_user.id

        # no args: list this user's active mutes
        if not ctx.args:
            rows = db.get_active_mutes(user_id)
            if not rows:
                text = "No active mutes."
            else:
                now = int(time.time())
                lines = [
                    f"  {r['sensor']} — {self._fmt_remaining(r['until_ts'] - now)} left"
                    for r in rows
                ]
                text = "Active mutes:\n" + "\n".join(lines)
            await self._app.bot.send_message(chat_id=reply_chat, text=text, **_SILENT)
            return

        # last arg ending in h/H is the duration -> mute; otherwise -> unmute
        last = ctx.args[-1]
        m = re.fullmatch(r"(\d+)[hH]", last)
        if m:
            hours = min(12, max(1, int(m.group(1))))
            names = self._resolve_sensors(ctx.args[:-1], user_id)
            if not names:
                await self._app.bot.send_message(
                    chat_id=reply_chat, text="No matching sensors.", **_SILENT
                )
                return
            until = int(time.time()) + hours * 3600
            for name in names:
                db.mute_sensor(user_id, name, until)
            await self._app.bot.send_message(
                chat_id=reply_chat,
                text=f"🔇 Muted {len(names)} field(s) for {hours}h",
                **_SILENT,
            )
            return

        # unmute
        names = self._resolve_sensors(ctx.args, user_id)
        if not names:
            await self._app.bot.send_message(
                chat_id=reply_chat, text="No matching sensors.", **_SILENT
            )
            return
        for name in names:
            db.unmute_sensor(user_id, name)
        await self._app.bot.send_message(
            chat_id=reply_chat,
            text=f"🔔 Unmuted {len(names)} field(s)",
            **_SILENT,
        )

    @staticmethod
    def _fmt_remaining(secs: int) -> str:
        secs = max(0, secs)
        h = secs // 3600
        m = (secs % 3600) // 60
        return f"{h}h{m:02d}m"

    async def _cmd_myid(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await self._app.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"Your Telegram ID: {update.effective_user.id}",
            **_SILENT,
        )

    async def _cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        reply_chat = update.effective_chat.id
        text = (
            "Commands:\n"
            "/get [expr] [-s|-f] — show sensors (no args = digest; sort -s name / -f field, default field)\n"
            "/exprSyntax — help for expr syntax\n"
            "/getAlarm [name] — show alarm threshold(s)\n"
            "/graph <expr> [Nh] — chart (default 8h, max 24h; 72h for admins)\n"
            "/csv <expr> [Nh] — download readings as CSV\n"
            "/xlsx <expr> [Nh] — download readings as Excel (one sheet per sensor)\n"
            "/last — last time anything arrived from MQTT\n"
            "/lastAlarms [expr] [Nh] — alarms in last N hours (default 8h, subscriptions if no expr)\n"
            "/last5Alarm <name> — last 5 alarms for a sensor\n"
            "/digest [expr on|off] — manage daily digest subscriptions\n"
            "/silent [expr [Nh]] — mute alarm DMs (no args=list, expr only=unmute, 1-12h)\n"
            "/list — list all sensors\n"
            "/myid — show your Telegram user ID"
        )
        user_id = update.effective_user.id
        if self._cfg.is_any_admin(user_id):
            text += (
                "\n\nAdmin commands:\n"
                "/setAlarm <name> <value> — set high alarm threshold (alarm if value >)\n"
                "/setAlarmLow <name> <value> — set low alarm threshold (alarm if value <)\n"
                "/clearAlarm <name> — clear high threshold\n"
                "/clearAlarmLow <name> — clear low threshold\n"
                "/ackOff <name> — acknowledge offline alarm (auto-clears when sensor reconnects)"
            )
        if self._cfg.is_superadmin(user_id):
            text += (
                "\n\nSuperadmin commands:\n"
                "/forgetSensor <name> — delete all data for a sensor\n"
                "/reloadConfig — reload sensors.d/ and credentials.yaml\n"
                "/usersActivity — last interaction time per user\n"
                "/dbStats — DB size, row counts, time span"
            )
        await self._app.bot.send_message(chat_id=reply_chat, text=text, **_SILENT)

    async def _cmd_exprsyntax(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        reply_chat = await self._get_reply_chat(update)
        if reply_chat is None:
            return
        await self._app.bot.send_message(
            chat_id=reply_chat,
            text=(
                "/get [expr] — sensor filter expression\n\n"
                "No args: digest sensors only\n"
                "* : all sensors\n"
                "NAME : exact sensor name\n"
                "PREFIX* : sensors starting with PREFIX\n"
                "*SUFFIX : sensors ending with SUFFIX\n"
                "*SUB* : sensors containing SUB\n\n"
                "Multiple patterns: space- or comma-separated\n\n"
                "Sort: default by field (group _T, _H, ...)\n"
                "-s : by sensor name\n"
                "-f : by field (default)\n\n"
                "Examples:\n"
                "  /get DEI*\n"
                "  /get *_T\n"
                "  /get DEI-P2_T UG_T\n"
                "  /get *_T,*_P\n"
                "  /get * -f"
            ),
            **_SILENT,
        )

    async def _cmd_list(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        reply_chat = await self._get_reply_chat(update)
        if reply_chat is None:
            return
        user_id = update.effective_user.id
        visible = set(self._cfg.visible_sensors(user_id))
        rows_map = {r["sensor"]: r for r in db.get_all_latest()}
        thresholds = db.get_all_thresholds()
        thresholds_low = db.get_all_thresholds_low()
        lines = []
        for dev_key, device in self._cfg.devices.items():
            parts = []
            for fk, sc in device.fields.items():
                if sc.name not in visible:
                    continue
                r = rows_map.get(sc.name)
                if r:
                    unit = sc.unit or ""
                    val_str = f"{self._cfg.fmt(sc.name, r['value'])}{unit}"
                    thr_parts = []
                    if sc.name in thresholds:
                        thr_parts.append(f"Th:{thresholds[sc.name]}{unit}")
                    if sc.name in thresholds_low:
                        thr_parts.append(f"Tl:{thresholds_low[sc.name]}{unit}")
                    thr_str = f"[{' '.join(thr_parts)}]" if thr_parts else ""
                    parts.append(f"{fk}={val_str}{thr_str}")
                else:
                    parts.append(f"{fk}=--")
            if parts:
                lines.append(f"{dev_key} {' '.join(parts)}")
        if not lines:
            await self._app.bot.send_message(chat_id=reply_chat, text="No sensors.", **_SILENT)
            return
        lines.append("")
        lines.append("Sensor name = device_field (e.g. SM2_UTA1_T)")
        lines.append("Use sensor name with /get /setAlarm /digest /graph")
        await self._app.bot.send_message(chat_id=reply_chat, text="\n".join(lines), **_SILENT)

    async def _cmd_get(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        reply_chat = await self._get_reply_chat(update)
        if reply_chat is None:
            return
        user_id = update.effective_user.id
        visible = set(self._cfg.visible_sensors(user_id))
        args, sort_key = self._extract_sort(ctx.args)
        if not args:
            subscribed = set(db.get_digest_subscriptions(user_id))
            names = [n for n in self._cfg.sensors if n in subscribed and n in visible]
        else:
            names = self._resolve_sensors(args, user_id)
        names = self._apply_sort(names, sort_key)
        await self._show_sensors(reply_chat, names)

    async def _cmd_setalarm(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        reply_chat = await self._get_reply_chat(update)
        if reply_chat is None:
            return
        user_id = update.effective_user.id

        if len(ctx.args) != 2:
            await self._app.bot.send_message(
                chat_id=reply_chat, text="Usage: /setAlarm <sensor> <value>", **_SILENT
            )
            return

        name = self._cfg.resolve_sensor(ctx.args[0])
        if not self._cfg.is_viewer(user_id, name):
            await self._app.bot.send_message(
                chat_id=reply_chat, text="Unknown sensor.", **_SILENT
            )
            return
        if not self._cfg.is_admin(user_id, name):
            await self._app.bot.send_message(
                chat_id=reply_chat, text="Not authorized.", **_SILENT
            )
            return

        try:
            value = round(float(ctx.args[1]), self._cfg.decimals_of(name))
        except ValueError:
            await self._app.bot.send_message(
                chat_id=reply_chat, text="Value must be a number.", **_SILENT
            )
            return

        db.set_threshold(name, value)
        if self.reset_alarm_fn:
            self.reset_alarm_fn(name)
        await self._app.bot.send_message(
            chat_id=reply_chat, text="Threshold updated.", **_SILENT
        )

    async def _cmd_setalarmlow(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        reply_chat = await self._get_reply_chat(update)
        if reply_chat is None:
            return
        user_id = update.effective_user.id

        if len(ctx.args) != 2:
            await self._app.bot.send_message(
                chat_id=reply_chat, text="Usage: /setAlarmLow <sensor> <value>", **_SILENT
            )
            return

        name = self._cfg.resolve_sensor(ctx.args[0])
        if not self._cfg.is_viewer(user_id, name):
            await self._app.bot.send_message(
                chat_id=reply_chat, text="Unknown sensor.", **_SILENT
            )
            return
        if not self._cfg.is_admin(user_id, name):
            await self._app.bot.send_message(
                chat_id=reply_chat, text="Not authorized.", **_SILENT
            )
            return

        try:
            value = round(float(ctx.args[1]), self._cfg.decimals_of(name))
        except ValueError:
            await self._app.bot.send_message(
                chat_id=reply_chat, text="Value must be a number.", **_SILENT
            )
            return

        db.set_threshold_low(name, value)
        if self.reset_alarm_fn:
            self.reset_alarm_fn(name)
        await self._app.bot.send_message(
            chat_id=reply_chat, text="Low threshold updated.", **_SILENT
        )

    async def _cmd_clearalarm(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        reply_chat = await self._get_reply_chat(update)
        if reply_chat is None:
            return
        user_id = update.effective_user.id
        if not ctx.args:
            await self._app.bot.send_message(
                chat_id=reply_chat, text="Usage: /clearAlarm <sensor>", **_SILENT
            )
            return
        name = self._cfg.resolve_sensor(ctx.args[0])
        if not self._cfg.is_viewer(user_id, name):
            await self._app.bot.send_message(
                chat_id=reply_chat, text="Unknown sensor.", **_SILENT
            )
            return
        if not self._cfg.is_admin(user_id, name):
            await self._app.bot.send_message(
                chat_id=reply_chat, text="Not authorized.", **_SILENT
            )
            return
        db.clear_threshold(name)
        if self.reset_alarm_fn:
            self.reset_alarm_fn(name)
        await self._app.bot.send_message(
            chat_id=reply_chat, text="High threshold cleared.", **_SILENT
        )

    async def _cmd_clearalarmlow(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        reply_chat = await self._get_reply_chat(update)
        if reply_chat is None:
            return
        user_id = update.effective_user.id
        if not ctx.args:
            await self._app.bot.send_message(
                chat_id=reply_chat, text="Usage: /clearAlarmLow <sensor>", **_SILENT
            )
            return
        name = self._cfg.resolve_sensor(ctx.args[0])
        if not self._cfg.is_viewer(user_id, name):
            await self._app.bot.send_message(
                chat_id=reply_chat, text="Unknown sensor.", **_SILENT
            )
            return
        if not self._cfg.is_admin(user_id, name):
            await self._app.bot.send_message(
                chat_id=reply_chat, text="Not authorized.", **_SILENT
            )
            return
        db.clear_threshold_low(name)
        if self.reset_alarm_fn:
            self.reset_alarm_fn(name)
        await self._app.bot.send_message(
            chat_id=reply_chat, text="Low threshold cleared.", **_SILENT
        )

    async def _cmd_getalarm(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        reply_chat = await self._get_reply_chat(update)
        if reply_chat is None:
            return
        user_id = update.effective_user.id

        def _thr_str(name: str) -> str:
            thr = db.get_threshold(name)
            low = db.get_threshold_low(name)
            ll = f"{low:g}" if low is not None else "--"
            hh = f"{thr:g}" if thr is not None else "--"
            return f"{ll}/{hh}"

        if not ctx.args:
            names = self._cfg.visible_sensors(user_id)
        else:
            name = self._cfg.resolve_sensor(ctx.args[0])
            if not self._cfg.is_viewer(user_id, name):
                await self._app.bot.send_message(
                    chat_id=reply_chat, text="Unknown sensor.", **_SILENT
                )
                return
            names = [name]

        if not names:
            await self._app.bot.send_message(
                chat_id=reply_chat, text="No sensors.", **_SILENT
            )
            return

        entries = [(n, _thr_str(n)) for n in names]
        wname = max(len("Sensor"), *(len(e[0]) for e in entries))
        wthr = max(len("Tl/Th"), *(len(e[1]) for e in entries))
        lines = [f"{'Sensor':<{wname}}  {'Tl/Th':>{wthr}}", ""]
        for n, t in entries:
            lines.append(f"{n:<{wname}}  {t:>{wthr}}")
        await self._app.bot.send_message(
            chat_id=reply_chat,
            text="```\n" + "\n".join(lines) + "\n```",
            parse_mode="Markdown",
            **_SILENT,
        )

    async def _cmd_graph(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        reply_chat = await self._get_reply_chat(update)
        if reply_chat is None:
            return

        if not ctx.args:
            await self._prompt_args(reply_chat, "graph")
            return

        user_id = update.effective_user.id
        max_hours = 72 if (
            self._cfg.is_any_admin(user_id) or self._cfg.is_superadmin(user_id)
        ) else 24
        args = list(ctx.args)
        hours = 8
        if args[-1].lower().endswith("h") and args[-1][:-1].isdigit():
            hours = max(1, min(max_hours, int(args[-1].lower()[:-1])))
            args = args[:-1]
        if not args:
            await self._app.bot.send_message(
                chat_id=reply_chat, text="Usage: /graph <expr> [Nh]", **_SILENT
            )
            return

        names = self._resolve_sensors(args, update.effective_user.id)
        if not names:
            await self._app.bot.send_message(
                chat_id=reply_chat, text="No matching sensors.", **_SILENT
            )
            return

        sensor_list = [
            (n, db.get_threshold(n), self._cfg.sensors[n].unit,
             self._cfg.sensors[n].valid_min, self._cfg.sensors[n].valid_max,
             self._cfg.sensors[n].interval, self._cfg.sensors[n].decimals)
            for n in names
        ]
        try:
            buf = graph.build(sensor_list, hours=hours)
        except Exception as e:
            log.exception("graph.build failed")
            await self._app.bot.send_message(
                chat_id=reply_chat, text=f"Graph error: {e}", **_SILENT
            )
            return
        await self._app.bot.send_photo(
            chat_id=reply_chat, photo=buf, caption=f"Last {hours}h", **_SILENT
        )

    async def _cmd_last(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        reply_chat = await self._get_reply_chat(update)
        if reply_chat is None:
            return
        ts = self.last_mqtt_fn() if self.last_mqtt_fn else None
        if ts is None:
            text = "No sign of life from MQTT since startup."
        else:
            text = f"Last sign of life from MQTT: {_fmt_ts(ts)}"
        await self._app.bot.send_message(chat_id=reply_chat, text=text, **_SILENT)

    async def _cmd_lastalarms(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        reply_chat = await self._get_reply_chat(update)
        if reply_chat is None:
            return
        user_id = update.effective_user.id

        args = list(ctx.args)
        hours = 8
        if args and re.fullmatch(r"\d+[hH]", args[-1]):
            n = int(args[-1][:-1])
            if not 1 <= n <= 24:
                await self._app.bot.send_message(
                    chat_id=reply_chat,
                    text="Hours must be between 1 and 24 (e.g. 6h).",
                    **_SILENT,
                )
                return
            hours = n
            args = args[:-1]

        if not args:
            subscribed = set(db.get_digest_subscriptions(user_id))
            visible = set(self._cfg.visible_sensors(user_id))
            names = [n for n in self._cfg.sensors if n in subscribed and n in visible]
        else:
            names = self._resolve_sensors(args, user_id)

        if not names:
            await self._app.bot.send_message(
                chat_id=reply_chat, text="No matching sensors.", **_SILENT
            )
            return

        since = int(time.time()) - hours * 3600
        rows = db.get_alarms_since(names, since)
        if not rows:
            await self._app.bot.send_message(
                chat_id=reply_chat, text=f"No alarms in last {hours}h.", **_SILENT
            )
            return
        await self._app.bot.send_message(
            chat_id=reply_chat, text=self._fmt_alarms(rows), **_SILENT
        )

    async def _cmd_last5alarm(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        reply_chat = await self._get_reply_chat(update)
        if reply_chat is None:
            return
        user_id = update.effective_user.id

        if not ctx.args:
            await self._prompt_args(reply_chat, "last5alarm")
            return
        name = self._cfg.resolve_sensor(ctx.args[0])
        if not self._cfg.is_viewer(user_id, name):
            await self._app.bot.send_message(
                chat_id=reply_chat, text="Unknown sensor.", **_SILENT
            )
            return
        rows = db.get_last_alarms(sensor=name, n=5)
        await self._app.bot.send_message(
            chat_id=reply_chat, text=self._fmt_alarms(rows), **_SILENT
        )

    async def _cmd_ackoff(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        reply_chat = await self._get_reply_chat(update)
        if reply_chat is None:
            return
        user_id = update.effective_user.id

        if not ctx.args:
            await self._app.bot.send_message(
                chat_id=reply_chat, text="Usage: /ackOff <device>", **_SILENT
            )
            return

        device_key = ctx.args[0]
        if device_key not in self._cfg.devices:
            await self._app.bot.send_message(
                chat_id=reply_chat, text="Unknown device.", **_SILENT
            )
            return
        if not self._cfg.is_any_admin_of_device(user_id, device_key):
            await self._app.bot.send_message(
                chat_id=reply_chat, text="Not authorized.", **_SILENT
            )
            return

        db.silence_sensor(device_key)
        await self._app.bot.send_message(
            chat_id=reply_chat,
            text="Offline alarm acknowledged. Will auto-clear when device comes back online.",
            **_SILENT,
        )

    async def _cmd_forgetsensor(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        reply_chat = await self._get_reply_chat(update)
        if reply_chat is None:
            return
        user_id = update.effective_user.id

        if not ctx.args:
            await self._app.bot.send_message(
                chat_id=reply_chat, text="Usage: /forgetSensor <device>", **_SILENT
            )
            return

        device_key = ctx.args[0]
        if not self._cfg.is_superadmin(user_id):
            await self._app.bot.send_message(
                chat_id=reply_chat, text="Not authorized.", **_SILENT
            )
            return
        if device_key not in self._cfg.devices:
            await self._app.bot.send_message(
                chat_id=reply_chat, text="Unknown device.", **_SILENT
            )
            return

        sensor_names = [sc.name for sc in self._cfg.devices[device_key].fields.values()]
        db.forget_device(sensor_names, device_key)
        await self._app.bot.send_message(
            chat_id=reply_chat, text="Device data archived.", **_SILENT
        )

    async def _cmd_reloadconfig(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        reply_chat = await self._get_reply_chat(update)
        if reply_chat is None:
            return
        if not self._cfg.is_superadmin(update.effective_user.id):
            await self._app.bot.send_message(
                chat_id=reply_chat, text="Not authorized.", **_SILENT
            )
            return
        if self._reload_fn is None:
            await self._app.bot.send_message(
                chat_id=reply_chat, text="Reload not configured.", **_SILENT
            )
            return
        try:
            new = self._reload_fn()
        except Exception as e:
            await self._app.bot.send_message(
                chat_id=reply_chat, text=f"Reload failed: {e}", **_SILENT
            )
            return

        self._cfg.groups = new.groups
        self._cfg.superadmin = new.superadmin
        self._cfg.retention_days = new.retention_days
        self._cfg.alarm_threshold_repeat = new.alarm_threshold_repeat
        self._cfg.alarm_offline_repeat = new.alarm_offline_repeat
        self._cfg.debug = new.debug

        self._cfg.sensors.clear()
        self._cfg.sensors.update(new.sensors)
        self._cfg.devices.clear()
        self._cfg.devices.update(new.devices)

        for sc in new.sensors.values():
            if sc.default_alarm_high is not None and db.get_threshold(sc.name) is None:
                db.set_threshold(sc.name, sc.default_alarm_high)
            if sc.default_alarm_low is not None and db.get_threshold_low(sc.name) is None:
                db.set_threshold_low(sc.name, sc.default_alarm_low)

        await self._app.bot.send_message(
            chat_id=reply_chat,
            text="Config reloaded.\nNote: new sensor MQTT subscriptions require a restart.",
            **_SILENT,
        )

    async def _cmd_usersactivity(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        reply_chat = await self._get_reply_chat(update)
        if reply_chat is None:
            return
        if not self._cfg.is_superadmin(update.effective_user.id):
            await self._app.bot.send_message(
                chat_id=reply_chat, text="Not authorized.", **_SILENT
            )
            return
        rows = db.get_all_activity()
        if not rows:
            await self._app.bot.send_message(
                chat_id=reply_chat, text="No recorded activity.", **_SILENT
            )
            return
        now = int(time.time())
        lines = []
        for r in rows:
            who = r["full_name"] or "?"
            if r["username"]:
                who += f" (@{r['username']})"
            ago = _fmt_ago(now - r["last_seen"])
            lines.append(f"{who} [{r['user_id']}]\n  {_fmt_ts(r['last_seen'])} ({ago} ago)")
        await self._app.bot.send_message(
            chat_id=reply_chat, text="\n".join(lines), **_SILENT
        )

    async def _cmd_dbstats(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        reply_chat = await self._get_reply_chat(update)
        if reply_chat is None:
            return
        if not self._cfg.is_superadmin(update.effective_user.id):
            await self._app.bot.send_message(
                chat_id=reply_chat, text="Not authorized.", **_SILENT
            )
            return
        s = db.get_db_stats()

        def span_line(label: str, d: dict) -> str:
            line = f"{label}: {d['count']} rows"
            if d["min_ts"] and d["max_ts"]:
                line += f"\n  {_fmt_ts(d['min_ts'])} → {_fmt_ts(d['max_ts'])}"
            return line

        size = _fmt_bytes(s["file_bytes"]) if s["file_bytes"] is not None else s["file_error"]
        lines = [
            "📊 DB stats",
            f"file: {size}",
            f"reclaimable (VACUUM): {_fmt_bytes(s['free_bytes'])}",
            span_line("readings", s["readings"]),
            span_line("archive", s["archive"]),
        ]
        await self._app.bot.send_message(
            chat_id=reply_chat, text="\n".join(lines), **_SILENT
        )

    async def _cmd_csv(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        reply_chat = await self._get_reply_chat(update)
        if reply_chat is None:
            return

        if not ctx.args:
            await self._prompt_args(reply_chat, "csv")
            return

        args = list(ctx.args)
        hours = 8
        if args[-1].lower().endswith("h") and args[-1][:-1].isdigit():
            hours = max(1, min(24, int(args[-1].lower()[:-1])))
            args = args[:-1]
        if not args:
            await self._app.bot.send_message(
                chat_id=reply_chat, text="Usage: /csv <expr> [Nh]", **_SILENT
            )
            return

        names = self._resolve_sensors(args, update.effective_user.id)
        if not names:
            await self._app.bot.send_message(
                chat_id=reply_chat, text="No matching sensors.", **_SILENT
            )
            return

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["timestamp", "sensor", "value"])
        total = 0
        for name in names:
            rows = db.get_history(name, seconds=hours * 3600)
            for r in rows:
                writer.writerow([_fmt_ts(r["ts"]), name, r["value"]])
                total += 1

        if total == 0:
            await self._app.bot.send_message(
                chat_id=reply_chat, text=f"No data in last {hours}h.", **_SILENT
            )
            return

        data = buf.getvalue().encode()
        filename = f"sensors_{hours}h.csv"
        file_buf = io.BytesIO(data)
        file_buf.name = filename
        try:
            await self._app.bot.send_document(
                chat_id=reply_chat,
                document=file_buf,
                filename=filename,
                **_SILENT,
            )
        except Exception as e:
            log.exception("send_document failed")
            await self._app.bot.send_message(
                chat_id=reply_chat, text=f"CSV error: {e}", **_SILENT
            )

    async def _cmd_xlsx(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        reply_chat = await self._get_reply_chat(update)
        if reply_chat is None:
            return

        if not ctx.args:
            await self._prompt_args(reply_chat, "xlsx")
            return

        args = list(ctx.args)
        hours = 8
        if args[-1].lower().endswith("h") and args[-1][:-1].isdigit():
            hours = max(1, min(24, int(args[-1].lower()[:-1])))
            args = args[:-1]
        if not args:
            await self._app.bot.send_message(
                chat_id=reply_chat, text="Usage: /xlsx <expr> [Nh]", **_SILENT
            )
            return

        names = self._resolve_sensors(args, update.effective_user.id)
        if not names:
            await self._app.bot.send_message(
                chat_id=reply_chat, text="No matching sensors.", **_SILENT
            )
            return

        try:
            import openpyxl
            wb = openpyxl.Workbook()
            wb.remove(wb.active)
            total = 0
            for name in names:
                ws = wb.create_sheet(title=name[:31])
                ws.append(["timestamp", "value"])
                rows = db.get_history(name, seconds=hours * 3600)
                for r in rows:
                    ws.append([_fmt_ts(r["ts"]), r["value"]])
                    total += 1

            if total == 0:
                await self._app.bot.send_message(
                    chat_id=reply_chat, text=f"No data in last {hours}h.", **_SILENT
                )
                return

            buf = io.BytesIO()
            wb.save(buf)
            buf.seek(0)
            filename = f"sensors_{hours}h.xlsx"
            buf.name = filename
            await self._app.bot.send_document(
                chat_id=reply_chat,
                document=buf,
                filename=filename,
                **_SILENT,
            )
        except Exception as e:
            log.exception("xlsx failed")
            await self._app.bot.send_message(
                chat_id=reply_chat, text=f"XLSX error: {e}", **_SILENT
            )

    async def _set_user_commands(self):
        # User-level commands only; admin/superadmin commands stay out of the
        # autocomplete menu but their handlers still work when typed.
        cmds = [
            BotCommand("get", "show sensors (no args = digest)"),
            BotCommand("getalarm", "show alarm threshold(s)"),
            BotCommand("graph", "chart <expr> [Nh] (default 8h, max 24h; 72h admins)"),
            BotCommand("csv", "download readings as CSV"),
            BotCommand("xlsx", "download readings as Excel"),
            BotCommand("last", "last time anything arrived from MQTT"),
            BotCommand("lastalarms", "alarms in last N hours"),
            BotCommand("last5alarm", "last 5 alarms for a sensor"),
            BotCommand("digest", "manage daily digest subscriptions"),
            BotCommand("silent", "mute alarm DMs"),
            BotCommand("list", "list all sensors"),
            BotCommand("myid", "show your Telegram user ID"),
            BotCommand("exprsyntax", "expression syntax help"),
            BotCommand("help", "show command help"),
            BotCommand("start", "start the bot"),
        ]
        await self._app.bot.set_my_commands(cmds)

    async def run(self):
        await self._app.initialize()
        await self._app.start()
        me = await self._app.bot.get_me()
        self._bot_username = me.username
        await self._set_user_commands()
        await self._app.updater.start_polling(
            drop_pending_updates=True,
            poll_interval=self._cfg.poll_interval,
        )

    async def stop(self):
        await self._app.updater.stop()
        await self._app.stop()
        await self._app.shutdown()
