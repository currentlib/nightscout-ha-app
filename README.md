# Nightscout for Home Assistant

A Home Assistant app that runs [Nightscout](https://github.com/nightscout/cgm-remote-monitor)
and its own MongoDB on Home Assistant OS, included in HA backups.

Built for a 64-bit Raspberry Pi 4 with 4 GB of RAM.

| App | What it does | Setup |
|---|---|---|
| Nightscout | CGM data, graphs and Home Assistant entities | [`nightscout/DOCS.md`](nightscout/DOCS.md) |
| Diabetes Telegram Bot | Meals, insulin, AI chat and glucose alerts, synced with Nightscout | [`telegram-bot/DOCS.md`](telegram-bot/DOCS.md) |

## Install

[![Add repository to Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fcurrentlib%2Fnightscout-ha-app)

1. In Home Assistant, open **Settings > Apps > Repositories** and add this repository's URL.
2. Install **Nightscout**, set `api_secret`, and start it.
3. With the Mosquitto broker app, it appears in Home Assistant as a device with its own entities.
   Paste [`home-assistant/dashboards/nightscout_dashboard.yaml`](home-assistant/dashboards/nightscout_dashboard.yaml) into a new dashboard for a ready-made view.

Not affiliated with the Nightscout project. Not a medical device.
