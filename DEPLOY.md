# Guide — what you do in Render + Twilio

Code is ready in `C:\thinkabroad-guide`. Finish these steps in the dashboards.

## 1. Push this repo to GitHub

```powershell
cd C:\thinkabroad-guide
git config user.email "you@example.com"
git config user.name "Your Name"
git add -A
git commit -m "Guide WhatsApp agent: live search, screenshots, vault"
# Create empty GitHub repo "thinkabroad-guide", then:
git remote add origin https://github.com/YOUR_USER/thinkabroad-guide.git
git push -u origin main
```

## 2. New Render Web Service (same account as ThinkAbroad)

- **New → Web Service** → connect `thinkabroad-guide`
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
| `DEEPSEEK_API_KEY` | Same as ThinkAbroad — chat/CV only |
| `BRAVE_API_KEY` | Same as ThinkAbroad — live job search only |
| `VAULT_SECRET_KEY` | New long random string (or Render generate) |
| `GUIDE_DB_PATH` | `/var/data/guide.db` |
| `GUIDE_PUBLIC_URL` | `https://YOUR-GUIDE-SERVICE.onrender.com` (set after first deploy) |

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
4. `APPLY 1` → screenshot again + cover draft → `YES`

## What I cannot do from here

- Create the Render service in your account  
- Paste secrets into Render  
- Change Twilio webhook  
- Create GitHub remote without your login (`gh` not installed; git user identity not set)  
