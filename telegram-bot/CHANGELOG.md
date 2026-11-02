# Changelog

## 0.1.0

- Runs as a Home Assistant app, with the existing database imported on first start.
- Two-way sync with Nightscout, including the old history and clinical settings.
- Uses CGM readings for status, dose suggestions and predictions.
- IOB and COB come from Nightscout. The bot's own numbers are the backup, on the same models.
- Dose suggestions use Nightscout's IOB, or the bot's when it knows of a newer dose.
- /status says when Nightscout is unreachable and the bot's IOB is in use.
- Glucose alerts on every reading, with recipients and limits set in the app's options.
- Removed the Mini App: Nightscout replaces its editing, and a dashboard link replaces its view.
