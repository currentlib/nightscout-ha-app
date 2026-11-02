# Diabetes Telegram Bot

Meals, insulin and AI chat in Telegram, connected to the Nightscout app.

**Dose suggestions are estimates, not medical advice.** Don't rely on this as your only
low-glucose alarm: Telegram or your internet connection can fail.

## Setup

Run these steps in order, and stop the old copy of the bot first. A bot token can only run
in one place at a time.

1. **Stop the old bot,** and let it finish cleanly so its database is complete.
2. **Install this app** and fill in its required options. Dummy values are fine for now. Start
   it once so its folder is created, then stop it.
3. **Copy your database (optional).** Copy `diabetes.db` to
   `addon_configs/63acfdf2_telegram-bot/diabetes.db` using Samba or the File editor app. On the
   next start the app takes it and renames your copy to `diabetes.db.imported`.
4. **Create a Nightscout token.** In Nightscout, open **Admin Tools**, then:
   - Add a role named `telegram-bot` with the permissions
     `*:*:read,api:treatments:*,api:profile:*`.
   - Add a subject with that role, and copy its token.

   Pasting your Nightscout API secret also works, but it gives the bot full admin rights.
5. **Fill in the options:** bot token, OpenAI key, the patient's Telegram ID and the
   Nightscout token. To find a Telegram ID, message `@userinfobot` in Telegram.
6. **Start the app.** Turn on **Watchdog** so it restarts if it ever stops.

## What syncs

| Data | Direction |
|---|---|
| Sensor glucose | Nightscout to the bot, every 30 seconds |
| Meals and manual glucose | Both ways |
| Rapid insulin | Both ways |
| Long-acting insulin | Bot to Nightscout, as a note so it isn't counted as IOB |
| ICR, ISF, target, DIA | Both ways |
| Photos, macros detail, AI chat and memory | Stay in the bot |

**The old history goes to Nightscout automatically** on first sync, in batches of 200 rows
every 30 seconds. Manual glucose appears as finger-stick points. It doesn't count toward
sensor statistics such as time in range. Only the patient's rows are sent.

**To fix a mistake,** delete or edit the treatment in Nightscout, in **Reports > Treatments**.
Changes from the last 7 days reach the bot within a minute.

**To change ICR, ISF, target or DIA,** use Nightscout's **Profile Editor**. If a value changes
during the day there, the bot uses the one active now.

## Where the numbers come from

| Number | Source | If Nightscout is unreachable |
|---|---|---|
| Glucose | Sensor through Nightscout | Latest manual reading |
| IOB | Nightscout, or the bot if it knows of a newer dose | The bot, on the same curve |
| COB | Nightscout, plus meals still syncing | The bot, on nearly the same model |
| Forecast for predicted alerts | The bot, using insulin and meals | The same |
| Dose suggestion | The bot, using the numbers above | The same |

`/status` says when the bot is using its own IOB. It switches after 3 minutes without
an answer from Nightscout.

Nightscout has no meal dose calculator, and its AR2 forecast only follows the glucose
trend, so it can't see insulin you just took. That's why those two stay in the bot.

## Alerts

Set everything in the app's **Configuration** tab, then restart the app. Add one recipient
for each person or group, and choose which alerts each one gets. Each person must press
**Start** in the bot once, or Telegram won't let the bot message them. With no recipients,
all alerts go to the patient.

| Alert | When | Repeats |
|---|---|---|
| Urgent low | Sensor below 3.1 | Every 10 minutes |
| Low | Sensor below 3.9 | Every 30 minutes |
| High | Sensor above 13.9 | Every 30 minutes |
| Predicted | Forecast for 30 minutes ahead is below 4.0 or above 10.0 | Every 30 minutes |
| No data | No new reading for 30 minutes, or Nightscout is down | Every hour |

Limits are in mmol/L. You also get a message when a problem clears. The predicted high
often fires after meals, so raise it if that's too much. For sirens, lights or phone
notifications, use a Home Assistant automation on the `sensor.nightscout_*` entities.

## Dashboard button

Set `dashboard_url` to any web address, such as your Home Assistant dashboard. Without one,
the button says the link isn't set up.

## Backups

Backups stop the bot briefly and include its database. Alerts pause during that time. If
you restore both apps, the sync catches up by itself.
