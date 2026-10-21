# Guide

WhatsApp travel agent. Say a route and it replies immediately with live booking links and screenshots. It asks permission before it agrees to book.

No web UI. Flight search does not wait on an LLM.

## Conversation

1. Say it like you would to a person: `Jorhat to Bengaluru`, then `2nd December one way`
2. Guide keeps the trip in mind. If it has to guess the route, it asks you to confirm before searching
3. A clear route and date gets flight links and screenshots, with a line that nothing is booked
4. `book the first one` asks before it agrees. `YES` is the only time it says yes, and it stops before payment
5. Wording comes from DeepSeek, or Gemini if DeepSeek is down. The booking decision stays in code

Optional passenger details: `SAVE PROFILE Full Name | email | phone`

## Providers

| Service | Role |
|--------|------|
| **DeepSeek** | WhatsApp wording. Gemini is the fallback |
| **Twilio** | WhatsApp text, emoji reaction, screenshot images |
| **MakeMyTrip / Google Flights / Skyscanner** | Live booking links |
| **mShots** | Page screenshots attached to the reply |

## Deploy

See [DEPLOY.md](DEPLOY.md). Render build installs Chromium via Playwright.

Required env: `TWILIO_*`, `DEEPSEEK_API_KEY`, `GEMINI_API_KEY`, `VAULT_SECRET_KEY`, `GUIDE_PUBLIC_URL`, `GUIDE_DB_PATH`.
