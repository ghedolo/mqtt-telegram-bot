import logging
from datetime import datetime
from typing import Optional

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

from .config import AppConfig
from . import db, graph

log = logging.getLogger(__name__)


def _is_admin(user_id: int, cfg: AppConfig) -> bool:
    return user_id in cfg.admin_ids


def _fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


class TelegramBot:
    def __init__(self, cfg: AppConfig):
        self._cfg = cfg
        self._app = Application.builder().token(cfg.telegram_token).build()
        self._app.add_handler(CommandHandler("list", self._cmd_list))
        self._app.add_handler(CommandHandler("get", self._cmd_get))
        self._app.add_handler(CommandHandler("setalarm", self._cmd_setalarm))
        self._app.add_handler(CommandHandler("getAlarm", self._cmd_getalarm))
        self._app.add_handler(CommandHandler("graph", self._cmd_graph))
        self._app.add_handler(CommandHandler("silence", self._cmd_silence))
        self._app.add_handler(CommandHandler("help", self._cmd_help))

    async def send(self, text: str):
        await self._app.bot.send_message(chat_id=self._cfg.telegram_group_id, text=text)

    async def _cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        text = (
            "/list — list all sensors\n"
            "/get <name> — get current value (no args = same as /list)\n"
            "/setAlarm <name> <value> — set alarm threshold (admin)\n"
            "/getAlarm [name] — show alarm threshold(s)\n"
            "/graph <name> — chart last 8h\n"
            "/silence <name> — silence offline alarm (admin)"
        )
        await update.message.reply_text(text)

    async def _list_all(self, update: Update):
        sensors = self._cfg.sensors
        rows = db.get_all_latest()
        thresholds = db.get_all_thresholds()
        if not sensors:
            await update.message.reply_text("No sensors configured.")
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
        await update.message.reply_text("\n\n".join(blocks), parse_mode="Markdown")

    async def _cmd_list(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await self._list_all(update)

    async def _cmd_get(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not ctx.args:
            await self._list_all(update)
            return

        name = ctx.args[0]
        if name not in self._cfg.sensors:
            await update.message.reply_text(f"Unknown sensor: {name}")
            return
        row = db.get_latest(name)
        if row is None:
            await update.message.reply_text(f"{name}: no data yet")
            return
        sc = self._cfg.sensors[name]
        unit = f" {sc.unit}" if sc.unit else ""
        thr = db.get_threshold(name)
        thr_str = f"\nAlarm: {thr}{unit}" if thr is not None else ""
        await update.message.reply_text(
            f"{name}: {row['value']:.1f}{unit}\n{_fmt_ts(row['ts'])}{thr_str}"
        )

    async def _cmd_setalarm(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not _is_admin(update.effective_user.id, self._cfg):
            await update.message.reply_text("Not authorized.")
            return

        if len(ctx.args) != 2:
            await update.message.reply_text("Usage: /setAlarm <sensor> <value>")
            return

        name = ctx.args[0]
        if name not in self._cfg.sensors:
            await update.message.reply_text(f"Unknown sensor: {name}")
            return

        try:
            value = float(ctx.args[1])
        except ValueError:
            await update.message.reply_text("Value must be a number.")
            return

        db.set_threshold(name, value)
        await update.message.reply_text(f"Threshold for {name} set to {value}")

    async def _cmd_getalarm(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not ctx.args:
            thresholds = db.get_all_thresholds()
            lines = []
            for name in self._cfg.sensors:
                thr = thresholds.get(name)
                lines.append(f"{name}: {thr}" if thr is not None else f"{name}: not set")
            await update.message.reply_text("\n".join(lines))
            return

        name = ctx.args[0]
        if name not in self._cfg.sensors:
            await update.message.reply_text(f"Unknown sensor: {name}")
            return

        thr = db.get_threshold(name)
        if thr is None:
            await update.message.reply_text(f"{name}: no alarm threshold set")
        else:
            await update.message.reply_text(f"{name}: alarm threshold = {thr}")

    async def _cmd_graph(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not ctx.args:
            await update.message.reply_text("Usage: /graph <sensor>")
            return

        name = ctx.args[0]
        if name not in self._cfg.sensors:
            await update.message.reply_text(f"Unknown sensor: {name}")
            return

        sc = self._cfg.sensors[name]
        thr = db.get_threshold(name)
        try:
            buf = graph.build(name, threshold=thr, unit=sc.unit)
        except Exception as e:
            log.exception("graph.build failed for %s", name)
            await update.message.reply_text(f"Graph error: {e}")
            return
        await update.message.reply_photo(photo=buf, caption=f"Sensor: {name} — last 8h")

    async def _cmd_silence(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not _is_admin(update.effective_user.id, self._cfg):
            await update.message.reply_text("Not authorized.")
            return

        if not ctx.args:
            await update.message.reply_text("Usage: /silence <sensor>")
            return

        name = ctx.args[0]
        if name not in self._cfg.sensors:
            await update.message.reply_text(f"Unknown sensor: {name}")
            return

        db.silence_sensor(name)
        await update.message.reply_text(
            f"{name} offline alarm silenced. Will auto-clear when sensor comes back."
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
