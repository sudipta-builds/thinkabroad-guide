# Hetzner booking browser (Chromium worker)

Render keeps WhatsApp + search. This VPS only runs Cleartrip/EaseMyTrip/Ixigo Chromium through DataImpulse.

## Your server

- Type: CX23, Ubuntu, Nuremberg
- Example IP: `YOUR_SERVER_IP` (from Hetzner → Servers)

## 1. SSH in (on your PC)

```bash
ssh root@YOUR_SERVER_IP
```

## 2. Install base packages

```bash
apt update && apt upgrade -y
apt install -y python3 python3-venv python3-pip git ufw
ufw allow OpenSSH
ufw allow 8000/tcp
ufw --force enable
```

(Port 8000 is temporary for the worker; we can put Caddy/HTTPS on 443 later.)

## 3. Clone Guide and install Playwright

```bash
cd /opt
git clone https://github.com/sudipta-builds/thinkabroad-guide.git
cd thinkabroad-guide
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install --with-deps chromium
```

## 4. Create `/opt/thinkabroad-guide/.env` on the VPS

```env
BOOKING_WORKER_TOKEN=pick-a-long-random-secret
LOG_LEVEL=INFO
# Leave proxy off for now — Hetzner loads Cleartrip fares direct after ~45s.
# DataImpulse often returns an empty Cleartrip shell from this host.
# BOOKING_FORCE_PROXY=1
# PROXY_HOST=gw.dataimpulse.com
# PROXY_PORT=10000
# PROXY_USERNAME=LOGIN__cr.in;sessid.guide1;sessttl.30
# PROXY_PASSWORD=
```

Pick any long random `BOOKING_WORKER_TOKEN` and save it — you will paste the same value into Render.

## 5. Smoke-test Cleartrip on the VPS

```bash
cd /opt/thinkabroad-guide
source .venv/bin/activate
python scripts/hetzner_smoke_cleartrip.py
```

You want: page URL contains `cleartrip`, and it prints that Book buttons were found (or at least real fares). If this fails, Hetzner cannot fix a bad proxy exit — change `sessid` and retry.

## 6. Start the booking worker

```bash
cd /opt/thinkabroad-guide
source .venv/bin/activate
uvicorn app.worker:app --host 0.0.0.0 --port 8000
```

Check from your PC:

```bash
curl http://YOUR_SERVER_IP:8000/health
```

## 7. Point Render at Hetzner

In Render → Environment, add:

| Key | Value |
|---|---|
| `BOOKING_WORKER_URL` | `http://YOUR_SERVER_IP:8000` |
| `BOOKING_WORKER_TOKEN` | *(same secret as on the VPS)* |

You can **remove** `PROXY_*` from Render after this (booking no longer uses Chromium there). Keep WhatsApp / DeepSeek / DB as they are.

Redeploy Render. Health should show `"booking_worker": true`.

## 8. Keep the worker running (systemd)

```bash
cat >/etc/systemd/system/guide-browser.service <<'EOF'
[Unit]
Description=Guide booking Chromium worker
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/thinkabroad-guide
EnvironmentFile=/opt/thinkabroad-guide/.env
ExecStart=/opt/thinkabroad-guide/.venv/bin/uvicorn app.worker:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now guide-browser
systemctl status guide-browser
```

## What you do next in chat

1. SSH in and run steps 2–5.
2. Paste the smoke-test output (no password).
3. If smoke is green, start the worker (6–8) and set Render env.
