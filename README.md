# Guide

WhatsApp travel agent. DeepSeek chooses what to do. Chromium opens live booking pages, logs in when needed, fills passenger forms, and stops before payment.

## Conversation

1. Say a trip the way you would to a person, for example Jorhat to Bengaluru on a date
2. Guide asks what you care about if you have not said: cheapest, time of day, fewer stops
3. It opens live sites itself. If one blocks it, it tries another and tells you
4. You get a recommendation from prices that actually loaded, plus a screenshot
5. For booking it asks for name, email, phone, date of birth, and gender, saves them, fills the form, and shows the total. It never pays

Optional shortcuts:

- `SAVE PROFILE Full Name | email | phone | DOB | Gender`
- `SAVE cleartrip you@email.com yourpassword`

## Providers

| Service | Role |
|--------|------|
| **DeepSeek** | Decides the next step and writes WhatsApp replies |
| **Chromium / Playwright** | Opens pages, clicks, types, screenshots |
| **Twilio** | WhatsApp delivery |
| **Vault** | Encrypted site logins and passenger profile |

## Deploy

See [DEPLOY.md](DEPLOY.md). Health should show `"version": "0.3.0"` and `"chromium": true`.

Required env: `TWILIO_*`, `DEEPSEEK_API_KEY`, `VAULT_SECRET_KEY`, `GUIDE_PUBLIC_URL`, `GUIDE_DB_PATH`, `PLAYWRIGHT_BROWSERS_PATH`.
