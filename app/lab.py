"""Local lab: same Guide agent as prod, WhatsApp I/O captured on :8000.

GUIDE_LOCAL=1 keeps KERNEL / Gemini / proxy / vault identical to prod.
Only outbound WhatsApp is redirected into an in-memory outbox for /lab.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.config import get_settings
from app.reply import Reply

logger = logging.getLogger(__name__)

router = APIRouter(tags=["lab"])

_LOCK = threading.Lock()
_SEQ = 0
_OUTBOX: list[dict[str, Any]] = []
_TYPING = False
_TYPING_UNTIL = 0.0
# Bumps once per process so a stale lab tab (after=N from before restart) resets.
_BOOT = int(time.time())

LAB_PHONE_DEFAULT = "919999000001"

_LAB_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Guide lab (local = prod agent)</title>
<style>
  :root { --bg:#0f1419; --panel:#1a2332; --text:#e7ecf3; --muted:#8b9bb4; --accent:#3d8bfd; --me:#1f3d2f; }
  * { box-sizing: border-box; }
  body { margin:0; font-family: "Segoe UI", system-ui, sans-serif; background:var(--bg); color:var(--text); }
  header { padding:12px 16px; border-bottom:1px solid #243044; background:var(--panel); }
  header strong { font-size:1.05rem; }
  header span { color:var(--muted); font-size:0.85rem; margin-left:8px; }
  #log { height: calc(100vh - 140px); overflow:auto; padding:16px; display:flex; flex-direction:column; gap:10px; }
  .bubble { max-width: min(720px, 92%); padding:10px 12px; border-radius:12px; white-space:pre-wrap; line-height:1.35; }
  .user { align-self:flex-end; background:var(--me); }
  .bot { align-self:flex-start; background:var(--panel); border:1px solid #2a3a52; }
  .bubble img { display:block; max-width:100%; margin-top:8px; border-radius:8px; }
  .meta { color:var(--muted); font-size:0.75rem; margin-top:4px; }
  #typing { align-self:flex-start; color:var(--muted); font-size:0.9rem; font-style:italic; min-height:1.2em; opacity:0; transition:opacity .15s; }
  #typing.on { opacity:1; }
  form { display:flex; gap:8px; padding:12px 16px; border-top:1px solid #243044; background:var(--panel); }
  input { flex:1; padding:10px 12px; border-radius:8px; border:1px solid #334155; background:#0b1220; color:var(--text); }
  button { padding:10px 16px; border:0; border-radius:8px; background:var(--accent); color:#fff; font-weight:600; cursor:pointer; }
  button.ghost { background:#243044; color:var(--text); }
  button:disabled { opacity:0.5; cursor:wait; }
</style>
</head>
<body>
<header>
  <strong>Guide lab</strong>
  <span>Same code path as WhatsApp / prod · phone <code id="phone"></code> · outbox only (no double paint)</span>
</header>
<div id="log"></div>
<div id="typing">Guide is typing…</div>
<form id="f">
  <input id="msg" autocomplete="off" placeholder="Type like WhatsApp: confirm, continue, 781001 Assam…" />
  <button type="submit" id="go">Send</button>
  <button type="button" id="fresh" class="ghost">New user</button>
</form>
<script>
const log = document.getElementById('log');
const phoneEl = document.getElementById('phone');
const typingEl = document.getElementById('typing');
let after = 0;
let boot = 0;
let phone = '';
const seen = new Set();
let pollBusy = false;

function addBubble(role, text, media, ts, id) {
  if (id != null) {
    const key = boot + ':' + id;
    if (seen.has(key)) return;
    seen.add(key);
  }
  const d = document.createElement('div');
  d.className = 'bubble ' + (role === 'user' ? 'user' : 'bot');
  d.textContent = text || '';
  (media || []).forEach(u => {
    const img = document.createElement('img');
    img.src = u;
    img.alt = 'shot';
    d.appendChild(img);
  });
  const m = document.createElement('div');
  m.className = 'meta';
  m.textContent = new Date((ts || Date.now()/1000) * 1000).toLocaleTimeString();
  d.appendChild(m);
  log.appendChild(d);
  log.scrollTop = log.scrollHeight;
}

async function refreshOutbox() {
  if (pollBusy) return;
  pollBusy = true;
  try {
    const r = await fetch('/lab/outbox?after=' + after);
    const data = await r.json();
    phone = data.phone || phone;
    phoneEl.textContent = phone;
    typingEl.classList.toggle('on', !!data.typing);
    // Server restart (or after cursor past the outbox) — resync from the start.
    const serverBoot = Number(data.boot || 0);
    const maxId = Number(data.max_id || 0);
    if ((serverBoot && boot && serverBoot !== boot) || (after > 0 && maxId < after)) {
      after = 0;
      seen.clear();
      boot = serverBoot;
      const r2 = await fetch('/lab/outbox?after=0');
      const data2 = await r2.json();
      for (const item of (data2.items || [])) {
        after = Math.max(after, item.id || 0);
        addBubble('bot', item.text, item.media_urls || [], item.ts, item.id);
      }
      return;
    }
    if (serverBoot) boot = serverBoot;
    for (const item of (data.items || [])) {
      after = Math.max(after, item.id || 0);
      addBubble('bot', item.text, item.media_urls || [], item.ts, item.id);
    }
  } finally {
    pollBusy = false;
  }
}

document.getElementById('f').addEventListener('submit', async (e) => {
  e.preventDefault();
  const input = document.getElementById('msg');
  const btn = document.getElementById('go');
  const text = (input.value || '').trim();
  if (!text) return;
  addBubble('user', text);
  input.value = '';
  btn.disabled = true;
  typingEl.classList.add('on');
  try {
    const r = await fetch('/lab/message', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ text }),
    });
    const data = await r.json();
    if (!data.ok) {
      addBubble('bot', 'Lab error: ' + (data.error || r.status));
    }
    // Prod paints only via WhatsApp send — never also paint data.reply here.
    await refreshOutbox();
  } catch (err) {
    addBubble('bot', 'Lab request failed: ' + err);
  } finally {
    btn.disabled = false;
    input.focus();
  }
});

document.getElementById('fresh').addEventListener('click', async () => {
  const btn = document.getElementById('fresh');
  btn.disabled = true;
  try {
    const r = await fetch('/lab/new-user', { method: 'POST' });
    const data = await r.json();
    addBubble('bot', data.reply || data.error || 'Could not reset.');
  } catch (err) {
    addBubble('bot', 'New user reset failed: ' + err);
  } finally {
    btn.disabled = false;
  }
});

refreshOutbox();
setInterval(refreshOutbox, 800);
</script>
</body>
</html>
"""


def lab_enabled() -> bool:
    return bool(get_settings().guide_local)


def lab_phone() -> str:
    raw = (get_settings().guide_lab_phone or "").strip() or LAB_PHONE_DEFAULT
    return "".join(ch for ch in raw if ch.isdigit()) or LAB_PHONE_DEFAULT


def set_typing(active: bool = True, hold_seconds: float = 25.0) -> None:
    """Mirror Meta/Twilio typing bubble into the lab UI."""
    global _TYPING, _TYPING_UNTIL
    with _LOCK:
        _TYPING = bool(active)
        _TYPING_UNTIL = time.time() + hold_seconds if active else 0.0


def typing_active() -> bool:
    with _LOCK:
        if _TYPING and time.time() <= _TYPING_UNTIL:
            return True
        return False


def capture_outbound(to_phone: str, reply: Reply) -> bool:
    """Store what would have been sent on WhatsApp. Always succeeds in lab mode."""
    global _SEQ, _TYPING, _TYPING_UNTIL
    with _LOCK:
        _SEQ += 1
        _TYPING = False
        _TYPING_UNTIL = 0.0
        item = {
            "id": _SEQ,
            "ts": time.time(),
            "phone": "".join(ch for ch in (to_phone or "") if ch.isdigit()) or lab_phone(),
            "text": (reply.text or "").strip(),
            "media_urls": list(reply.media_urls or [])[:3],
            "emoji": (reply.emoji or "").strip(),
        }
        _OUTBOX.append(item)
        if len(_OUTBOX) > 500:
            del _OUTBOX[: len(_OUTBOX) - 500]
        logger.info("Lab outbox #%s to=%s media=%s", item["id"], item["phone"], len(item["media_urls"]))
        return True


def outbox_since(after_id: int = 0) -> list[dict[str, Any]]:
    with _LOCK:
        return [dict(item) for item in _OUTBOX if int(item.get("id") or 0) > after_id]


def outbox_max_id() -> int:
    with _LOCK:
        return int(_SEQ)


@router.get("/lab", response_class=HTMLResponse)
def lab_page() -> HTMLResponse:
    if not lab_enabled():
        return HTMLResponse(
            "<p>Lab off. Set GUIDE_LOCAL=1 and restart uvicorn on port 8000.</p>",
            status_code=404,
        )
    return HTMLResponse(_LAB_HTML)


@router.get("/lab/outbox")
def lab_outbox(after: int = 0) -> JSONResponse:
    if not lab_enabled():
        return JSONResponse({"ok": False, "error": "lab_off"}, status_code=404)
    items = outbox_since(after)
    return JSONResponse(
        {
            "ok": True,
            "phone": lab_phone(),
            "typing": typing_active(),
            "boot": _BOOT,
            "max_id": outbox_max_id(),
            "items": items,
        }
    )


@router.post("/lab/new-user")
def lab_new_user() -> JSONResponse:
    """Drop passenger, login, and billing on the lab phone. Keep the trip."""
    if not lab_enabled():
        return JSONResponse({"ok": False, "error": "lab_off"}, status_code=404)
    from app.kernel_otp import release_saved_kernel
    from app.travel_agent import _load, _save, reset_lab_traveller

    phone = lab_phone()
    state = _load(phone)
    release_saved_kernel(str(state.get("kernel_session_id") or ""))
    fresh = reset_lab_traveller(state)
    _save(phone, fresh)
    reply = (
        "Cleared this lab profile. Passenger, login, and billing are gone. "
        "The trip stays if you already picked one. Reply Ixigo to log in with a mobile and OTP. Nothing paid."
    )
    return JSONResponse({"ok": True, "phone": phone, "reply": reply})


@router.post("/lab/message")
async def lab_message(request: Request) -> JSONResponse:
    """Same inbound path as Meta WhatsApp webhook → agent → send_whatsapp."""
    if not lab_enabled():
        return JSONResponse({"ok": False, "error": "lab_off"}, status_code=404)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    text = str((payload or {}).get("text") or "").strip()
    phone = lab_phone()
    if not text:
        return JSONResponse({"ok": False, "error": "empty"}, status_code=400)

    from app.reply import for_send
    from app.twilio_wa import send_whatsapp
    from app.webhook import _build_reply

    message_sid = f"lab_{int(time.time() * 1000)}"
    set_typing(True)
    reply = _build_reply(phone, text, message_sid)
    reply = for_send(reply, reacted=False)
    if (reply.text or "").strip() or reply.media_urls:
        send_whatsapp(phone, reply)
    with _LOCK:
        outbox_id = _SEQ
    return JSONResponse(
        {
            "ok": True,
            "phone": phone,
            "outbox_id": outbox_id,
            "reply": {
                "text": reply.text,
                "media_urls": list(reply.media_urls or []),
                "emoji": reply.emoji,
            },
        }
    )


@router.get("/lab/health")
def lab_health() -> JSONResponse:
    settings = get_settings()
    return JSONResponse(
        {
            "ok": True,
            "lab": lab_enabled(),
            "phone": lab_phone() if lab_enabled() else "",
            "public_url": settings.guide_public_url,
            "matches_prod_agent": True,
            "whatsapp_egress": "lab_outbox" if lab_enabled() else "meta_or_twilio",
        }
    )
