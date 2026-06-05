import logging
from datetime import datetime
from typing import Callable, Optional

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

from .config import AppConfig
from . import db, graph

log = logging.getLogger(__name__)

_SILENT = {"disable_notification": True}


def _is_admin(user_id: int, cfg: AppConfig) -> bool:
    return user_id in cfg.admin_ids


def _fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


class TelegramBot:
    def __init__(self, cfg: AppConfig, reload_fn: Optional[Callable[[], AppConfig]] = None):
        self._cfg = cfg
        self._reload_fn = reload_fn
        self._app = Application.builder().token(cfg.telegram_token).build()
        self._app.add_handler(CommandHandler("list", self._cmd_list))
        self._app.add_handler(CommandHandler("get", self._cmd_get))
        self._app.add_handler(CommandHandler("setalarm", self._cmd_setalarm))
        self._app.add_handler(CommandHandler("getAlarm", self._cmd_getalarm))
        self._app.add_handler(CommandHandler("graph", self._cmd_graph))
        self._app.add_handler(CommandHandler("ackOff", self._cmd_ackoff))
        self._app.add_handler(CommandHandler("help", self._cmd_help))
        self._app.add_handler(CommandHandler("myid", self._cmd_myid))
        self._app.add_handler(CommandHandler("lastAlarm", self._cmd_lastalarm))
        self._app.add_handler(CommandHandler("last5Alarm", self._cmd_last5alarm))
        self._app.add_handler(CommandHandler("forgetSensor", self._cmd_forgetsensor))
        self._app.add_handler(CommandHandler("reloadConfig", self._cmd_reloadconfig))

    async def send(self, text: str):
        await self._app.bot.send_message(chat_id=self._cfg.telegram_group_id, text=text)

    def _fmt_alarms(self, rows) -> str:
        if not rows:
            return "No alarms recorded."
        return "\n".join(
            f"[{_fmt_ts(r['ts'])}] {r['message']}" for r in rows
        )

    async def _cmd_lastalarm(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        sensor = ctx.args[0] if ctx.args else None
        if sensor and sensor not in self._cfg.sensors:
            await update.effective_chat.send_message("Unknown sensor.", **_SILENT)
            return
        rows = db.get_last_alarms(sensor=sensor, n=1)
        await update.effective_chat.send_message(self._fmt_alarms(rows), **_SILENT)

    async def _cmd_last5alarm(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not ctx.args:
            await update.effective_chat.send_message("Usage: /last5Alarm <sensor>", **_SILENT)
            return
        name = ctx.args[0]
        if name not in self._cfg.sensors:
            await update.effective_chat.send_message("Unknown sensor.", **_SILENT)
            return
        rows = db.get_last_alarms(sensor=name, n=5)
        await update.effective_chat.send_message(self._fmt_alarms(rows), **_SILENT)

    async def _cmd_myid(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.effective_chat.send_message(f"Your Telegram ID: {update.effective_user.id}", **_SILENT)

    async def _cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        text = (
            "Commands:\n"
            "/list — list all sensors\n"
            "/get <name> — get current value (no args = same as /list)\n"
            "/getAlarm [name] — show alarm threshold(s)\n"
            "/graph <name> — chart last 8h\n"
            "/lastAlarm [name] — last alarm (all sensors or one)\n"
            "/last5Alarm <name> — last 5 alarms for a sensor\n"
            "/myid — show your Telegram user ID"
        )
        if _is_admin(update.effective_user.id, self._cfg):
            text += (
                "\n\nAdmin commands:\n"
                "/setAlarm <name> <value> — set alarm threshold\n"
                "/ackOff <name> — acknowledge offline alarm (auto-clears when sensor reconnects)\n"
                "/forgetSensor <name> — delete all data for a sensor\n"
                "/reloadConfig — reload sensors.yaml and credentials.yaml"
            )
        await update.effective_chat.send_message(text, **_SILENT)

    async def _list_all(self, update: Update):
        sensors = self._cfg.sensors
        rows = db.get_all_latest()
        thresholds = db.get_all_thresholds()
        if not sensors:
            await update.effective_chat.send_message("No sensors configured.", **_SILENT)
            return
        seen = {r["sensor"]: r for r in rows}
        blocks = []
        for name, sc in sensors.items():
            r = seen.get(name)
            block = f"*{name}*"
            if r:
                thr = thresholds.get(name)
                unit = f" {sc.unit}" if sc.unit else ""
                thr_str = f"  (alarm: {thr}{unit})" if thr is not None else ""
                block += f"\n  {r['value']:.1f}{unit}  {_fmt_ts(r['ts'])}{thr_str}"
            else:
                block += "\n  no data"
            blocks.append(block)
        await update.effective_chat.send_message("\n\n".join(blocks), parse_mode="Markdown", **_SILENT)

    async def _cmd_list(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await self._list_all(update)

    async def _cmd_get(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not ctx.args:
            await self._list_all(update)
            return

        name = ctx.args[0]
        if name not in self._cfg.sensors:
            await update.effective_chat.send_message("Unknown sensor.", **_SILENT)
            return
        row = db.get_latest(name)
        if row is None:
            await update.effective_chat.send_message("No data yet.", **_SILENT)
            return
        sc = self._cfg.sensors[name]
        unit = f" {sc.unit}" if sc.unit else ""
        thr = db.get_threshold(name)
        thr_str = f"\nAlarm: {thr}{unit}" if thr is not None else ""
        await update.effective_chat.send_message(
            f"{name}: {row['value']:.1f}{unit}\n{_fmt_ts(row['ts'])}{thr_str}", **_SILENT
        )

    async def _cmd_setalarm(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not _is_admin(update.effective_user.id, self._cfg):
            await update.effective_chat.send_message("Not authorized.", **_SILENT)
            return

        if len(ctx.args) != 2:
            await update.effective_chat.send_message("Usage: /setAlarm <sensor> <value>", **_SILENT)
            return

        name = ctx.args[0]
        if name not in self._cfg.sensors:
            await update.effective_chat.send_message("Unknown sensor.", **_SILENT)
            return

        try:
            value = float(ctx.args[1])
        except ValueError:
            await update.effective_chat.send_message("Value must be a number.", **_SILENT)
            return

        db.set_threshold(name, value)
        await update.effective_chat.send_message("Threshold updated.", **_SILENT)

    async def _cmd_getalarm(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not ctx.args:
            thresholds = db.get_all_thresholds()
            lines = []
            for name in self._cfg.sensors:
                thr = thresholds.get(name)
                lines.append(f"{name}: {thr}" if thr is not None else f"{name}: not set")
            await update.effective_chat.send_message("\n".join(lines), **_SILENT)
            return

        name = ctx.args[0]
        if name not in self._cfg.sensors:
            await update.effective_chat.send_message("Unknown sensor.", **_SILENT)
            return

        thr = db.get_threshold(name)
        if thr is None:
            await update.effective_chat.send_message("No alarm threshold set.", **_SILENT)
        else:
            await update.effective_chat.send_message(f"Alarm threshold: {thr}", **_SILENT)

    async def _cmd_graph(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not ctx.args:
            await update.effective_chat.send_message("Usage: /graph <sensor>", **_SILENT)
            return

        name = ctx.args[0]
        if name not in self._cfg.sensors:
            await update.effective_chat.send_message("Unknown sensor.", **_SILENT)
            return

        sc = self._cfg.sensors[name]
        thr = db.get_threshold(name)
        try:
            buf = graph.build(name, threshold=thr, unit=sc.unit)
        except Exception as e:
            log.exception("graph.build failed for %s", name)
            await update.effective_chat.send_message(f"Graph error: {e}", **_SILENT)
            return
        await update.effective_chat.send_photo(photo=buf, caption="Last 8h", **_SILENT)

    async def _cmd_ackoff(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not _is_admin(update.effective_user.id, self._cfg):
            await update.effective_chat.send_message("Not authorized.", **_SILENT)
            return

        if not ctx.args:
            await update.effective_chat.send_message("Usage: /ackOff <sensor>", **_SILENT)
            return

        name = ctx.args[0]
        if name not in self._cfg.sensors:
            await update.effective_chat.send_message("Unknown sensor.", **_SILENT)
            return

        db.silence_sensor(name)
        await update.effective_chat.send_message(
            "Offline alarm acknowledged. Will auto-clear when sensor comes back online.", **_SILENT
        )

    async def _cmd_forgetsensor(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not _is_admin(update.effective_user.id, self._cfg):
            await update.effective_chat.send_message("Not authorized.", **_SILENT)
            return

        if not ctx.args:
            await update.effective_chat.send_message("Usage: /forgetSensor <sensor>", **_SILENT)
            return

        name = ctx.args[0]
        if name not in self._cfg.sensors:
            await update.effective_chat.send_message("Unknown sensor.", **_SILENT)
            return

        db.forget_sensor(name)
        await update.effective_chat.send_message("Sensor data deleted.", **_SILENT)

    async def _cmd_reloadconfig(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not _is_admin(update.effective_user.id, self._cfg):
            await update.effective_chat.send_message("Not authorized.", **_SILENT)
            return
        if self._reload_fn is None:
            await update.effective_chat.send_message("Reload not configured.", **_SILENT)
            return
        try:
            new = self._reload_fn()
        except Exception as e:
            await update.effective_chat.send_message(f"Reload failed: {e}", **_SILENT)
            return

        self._cfg.admin_ids = new.admin_ids
        self._cfg.retention_days = new.retention_days
        self._cfg.alarm_threshold_repeat = new.alarm_threshold_repeat
        self._cfg.alarm_offline_repeat = new.alarm_offline_repeat
        self._cfg.debug = new.debug

        # update sensors in-place so run_offline_checks sees the change
        for name in list(self._cfg.sensors):
            if name not in new.sensors:
                del self._cfg.sensors[name]
        for name, sc in new.sensors.items():
            if name not in self._cfg.sensors:
                self._cfg.sensors[name] = sc
                if sc.default_alarm is not None and db.get_threshold(name) is None:
                    db.set_threshold(name, sc.default_alarm)
            else:
                self._cfg.sensors[name] = sc

        await update.effective_chat.send_message(
            "Config reloaded.\nNote: new sensor MQTT subscriptions require a restart.", **_SILENT
        )

    async def run(self):
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(
            drop_pending_updates=True,
            poll_interval=self._cfg.poll_interval,
        )

    async def stop(self):
        await self._app.updater.stop()
        await self._app.stop()
        await self._app.shutdown()
