# Guide — what you do in Render + Twilio

Code is ready in `C:\thinkabroad-guide`. Finish these steps in the dashboards.

## 1. GitHub — done

Repo: https://github.com/sudipta-builds/thinkabroad-guide  
Branch `main` is pushed.

## 2. New Render Web Service (same account as ThinkAbroad) — you do this

- **New → Web Service** → connect `sudipta-builds/thinkabroad-guide`
- Instance: **Starter** (not free — free sleeps and breaks WhatsApp)
- Build: `pip install -r requirements.txt`
- Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- **Disk**: 1 GB at `/var/data`
- Health check path: `/health`

### Environment variables (copy from ThinkAbroad where noted)

| Key | Source |
|-----|--------|
| `TWILIO_ACCOUNT_SID` | Same as ThinkAbroad |
| `TWILIO_AUTH_TOKEN` | Same as ThinkAbroad |
| `TWILIO_WHATSAPP_FROM` | Same as ThinkAbroad (or a new Guide number) |
| `DEEPSEEK_API_KEY` | Same as ThinkAbroad — chat/CV |
| `GEMINI_API_KEY` | Same as ThinkAbroad `Gemini_API_KEY` — live job search |
| `GEMINI_MODEL` | `gemini-flash-lite-latest` |
| `VAULT_SECRET_KEY` | New long random string (or Render generate) |
| `GUIDE_DB_PATH` | Prefer `/var/data/guide.db` **only if** you attached a disk at `/var/data`. Otherwise use `data/guide.db` (or omit — Guide falls back automatically). |
| `PYTHON_VERSION` | `3.12.8` (optional but recommended) |
| `GUIDE_PUBLIC_URL` | `https://YOUR-GUIDE-SERVICE.onrender.com` (set after first deploy) |

**Disk:** Render → Guide service → **Disks** → add disk, mount path `/var/data`. If you skip the disk, remove `GUIDE_DB_PATH=/var/data/...` or set it to `data/guide.db`.

## 3. Twilio WhatsApp webhook

Twilio Console → Messaging → WhatsApp sandbox / sender:

- When a message comes in: `https://YOUR-GUIDE-SERVICE.onrender.com/whatsapp`
- Method: **HTTP POST**

**Important:** If the same number still points at ThinkAbroad, inbound chat will go there instead. Either:

- Point this number’s webhook at Guide, or  
- Use the WhatsApp **sandbox** for Guide first, or  
- Buy/add a second WhatsApp sender for Guide.

## 4. Smoke test

1. Text `HI` on WhatsApp → Guide welcome + ask country  
2. Type a country → describe a job  
3. You should get **live links + screenshots** of top results  
4. `SAVE PROFILE Your Name | you@email.com | +phone`
5. `APPLY 1` → CV PDF/Word + screenshot → `YES` → Guide attempts real browser submit

## Build note

Flight search uses booking links + mShots screenshots. Do **not** add `playwright install chromium` to the Render build — that download often stalls deploys for 10–20+ minutes and is unused by the travel agent.

## What I cannot do from here

- Create the Render service in your account  
- Paste secrets into Render  
- Change Twilio webhook  
- Create GitHub remote without your login  
