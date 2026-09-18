# Delivery moved to Telegram

The daily workflow now sends reports only to Telegram. See [Daily Telegram setup](DAILY_TELEGRAM.md) for the active configuration, schedule and verification steps.

`EMAIL_FROM`, `EMAIL_TO` and `EMAIL_PASSWORD` are no longer referenced by the daily workflow. The standalone legacy SMTP script remains in the repository but is not run by the automation. Existing email secrets are not needed for Telegram.
