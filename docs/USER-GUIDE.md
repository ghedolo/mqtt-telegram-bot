# New user guide

A simple guide for someone who barely knows Telegram and wants to start
receiving sensor readings and alarms from the bot. Example: **Maria Rossi**,
who has just signed up for Telegram.

_Versione italiana: [GUIDA-UTENTE.md](GUIDA-UTENTE.md)._

---

## In a nutshell

The bot lives inside **Telegram** (the messaging app). It sends you sensor
values and alerts (alarms, blackouts) as **private messages**. Before you can
receive them you must do two things: get an administrator to **enable** you,
and **activate** the private chat with the bot.

---

## Step 1 — Install Telegram and create an account

1. Download **Telegram** from your phone's app store (or go to https://telegram.org).
2. Sign up with your phone number and pick a display name (e.g. *Maria Rossi*).

## Step 2 — Reach the bot

Ask the administrator for:
- the **Telegram group link** for the project, **or**
- the **bot's direct link** (something like `t.me/TheBotName`).

Open it and tap to join. You don't need to understand anything else yet.

## Step 3 — Find your user code (ID) and give it to the administrator

The bot only shows you data if the administrator has **enabled** you. To do
that they need your **ID** (a number).

1. Send the bot (in the group or in private) the command:

   ```
   /myid
   ```

2. The bot replies with a line like: `Your Telegram ID: 123456789`.
3. **Copy that number and send it to the administrator** (by message, email, however you like).
4. Wait for their confirmation: they add you to their lists and grant access to the sensors that concern you.

> Without this step the bot will show you no data at all, even if you start it.

## Step 4 — Activate private messages with the bot

Replies and alerts arrive **in the private chat**, so it must be activated once.

**Way A (from the group):**
1. Send any command in the group, for example `/list`.
2. The bot posts a message with an **"Avvia bot"** (Start the bot) button.
3. Tap it: the bot's private chat opens.
4. Inside that chat press **Start** at the bottom.

**Way B (directly):**
1. Open the bot's chat.
2. Press **Start** at the bottom, or type `/start`.

Either way the bot replies **"Bot activated"** (or *Registration complete*): you're in.

## Step 5 — Subscribe to the alerts you care about

Now use the commands **in the private chat with the bot**:

| Command | What it does |
|---|---|
| `/list` | Shows everything you can see: sensors with values, and at the bottom the available **blackout groups** |
| `/get` | The current values of your sensors |
| `/digest <name> on` | **Subscribe**: you'll get the daily summary for that sensor. E.g. `/digest SM2_UTA1_T on` |
| `/digest <blackout_id> on` | Subscribe to a group's **blackout alerts**. E.g. `/digest R2 on` (ids are shown at the bottom of `/list`) |
| `/digest` | Shows what you're already subscribed to |
| `/graph <name>` | Sends you a trend chart |
| `/silent <name> <hours>h` | Mutes a sensor's alerts for N hours (e.g. `/silent SM2_UTA1_T 8h`) |
| `/help` | List of all commands |

A sensor name is `device_field`, for example `SM2_UTA1_T` (device `SM2_UTA1`,
temperature field `T`). Case does not matter.

---

## Good things to know

- **Replies arrive privately and silently.** Don't expect a sound for every
  reply: open the bot's chat to see them.
- **See no data?** The administrator probably hasn't enabled you yet (go back
  to Step 3) — sensor access is decided by them.
- **Subscriptions:** you start subscribed to nothing. The daily summary and
  blackout alerts only reach the sensors/groups you subscribed to with
  `/digest ... on`.
- **Using Telegram Web?** Sometimes the page doesn't refresh on its own: if a
  command stays on a single check mark and you don't see the reply, reload with
  **Cmd/Ctrl+R** — it's a browser limitation, not the bot.

For the full command list and technical details see the [README](../README.md).
