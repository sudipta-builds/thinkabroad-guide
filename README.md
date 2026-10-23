# Guide

WhatsApp travel agent for Guwahati to Delhi. DeepSeek reads whatever you type. Chromium searches, signs in, fills the passenger form, and can reach payment.

## Conversation

1. Say the trip the way you would to a person, for example Guwahati to Delhi the first week of December, or just "want to fly"
2. Guide asks what you care about if you have not said: cheapest, time of day, fewer stops
3. It opens live sites itself. If one blocks it, it tries another and tells you
4. You get a recommendation from prices that actually loaded, plus a screenshot
5. Say which flight, in any wording ("the 12:30 one", "book it", "go ahead"). It opens that fare
6. If a site needs a login it does not have, it tells you. Send `SAVE cleartrip you@email.com yourpassword`
7. If the form needs a phone, email, name, DOB, gender, or OTP, it asks for that field and continues
8. On payment it stops for your approval. UPI you finish on your phone; a vault card can be filled if you say pay. CVV and OTP are typed once and not stored

## Sellers

Guide picks among Cleartrip, EaseMyTrip, Ixigo, and MakeMyTrip by which page loads and which fare is best. Name a seller to prefer it. A bot wall is treated as a failed seller.

## Vault

One encrypted vault per WhatsApp number: site logins, UPI, and card number. CVV and OTP are never written to disk.

Optional shortcuts:

- `SAVE PROFILE Full Name | email | phone | DOB | Gender`
- `SAVE cleartrip you@email.com yourpassword`

## Providers

| Service | Role |
|--------|------|
| **DeepSeek** | Decides the next step and writes WhatsApp replies |
| **Chromium / Playwright** | Opens pages, clicks, types, screenshots |
| **DataImpulse** | Indian residential proxy for booking on Render |
| **Twilio / Meta** | WhatsApp delivery |
| **Vault** | Per-number encrypted logins, UPI, and card. No CVV or OTP |

## Deploy

See [DEPLOY.md](DEPLOY.md). Health should show `"version": "0.5.0"`, `"chromium": true`, and `"proxy": true` when `PROXY_*` is set.

Required env: WhatsApp (`WHATSAPP_*` or `TWILIO_*`), `DEEPSEEK_API_KEY`, `VAULT_SECRET_KEY`, `GUIDE_PUBLIC_URL`, `GUIDE_DB_PATH`, `PLAYWRIGHT_BROWSERS_PATH`, and for production booking `PROXY_HOST`, `PROXY_PORT`, `PROXY_USERNAME`, `PROXY_PASSWORD`.
