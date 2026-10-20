# Guide

WhatsApp-only personal AI that finds **and applies** to jobs (including sponsored roles).

No web UI. No ThinkAbroad linkage.

## Conversation

1. `HI` → ask country (free text)
2. User describes the job they want
3. Live Gemini search → links + screenshots
4. `SAVE PROFILE Name | email | phone` (required to submit)
5. Optional: `SAVE LINKEDIN email password`
6. `CV` → real **PDF + Word** CV (sent on WhatsApp when `GUIDE_PUBLIC_URL` is set)
7. `APPLY N` → preview + CV files → `YES` → **Playwright submits** (Greenhouse / Lever / generic forms), uploads CV, fills cover note, returns result screenshot

Sites with CAPTCHA / heavy auth may not fully submit; Guide says so clearly.

## Providers

| Service | Role |
|--------|------|
| **DeepSeek** | Chat, CV text, cover notes |
| **Gemini** | Live job search |
| **Twilio** | WhatsApp |
| **Playwright (Chromium)** | Real form submit + CV upload |

## Deploy

See [DEPLOY.md](DEPLOY.md). Render build installs Chromium via Playwright.

Required env: `TWILIO_*`, `DEEPSEEK_API_KEY`, `GEMINI_API_KEY`, `VAULT_SECRET_KEY`, `GUIDE_PUBLIC_URL`, `GUIDE_DB_PATH`.
