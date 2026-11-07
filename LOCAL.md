# Local lab = prod agent on port 8000

Same FastAPI app, `agent.handle_message` → `travel_agent.handle_turn`, Kernel/Gemini/proxy/vault from your `.env`.  
Only difference: WhatsApp messages are **not** sent to Meta/Twilio — they land in the lab outbox.

## Run

```bash
python scripts/run_local.py
```

Open **http://127.0.0.1:8000/lab**

Checks:

- http://127.0.0.1:8000/health → `"local_lab": true`
- http://127.0.0.1:8000/lab/health → `"whatsapp_egress": "lab_outbox"`

## Env (set by the runner)

| Key | Local value | Why |
|-----|-------------|-----|
| `GUIDE_LOCAL` | `1` | Capture WhatsApp → `/lab` |
| `GUIDE_PUBLIC_URL` | `http://127.0.0.1:8000` | Screenshots load in the lab UI |
| `GUIDE_DB_PATH` | `data/guide-local.db` | Do not overwrite prod DB |
| `GUIDE_LAB_PHONE` | `919999000001` | Synthetic WhatsApp user |

Keep prod keys in `.env` as usual: `GEMINI_API_KEY`, `DEEPSEEK_API_KEY`, `KERNEL_API_KEY`, `VAULT_SECRET_KEY`, `PROXY_*`, etc.

## Before push

1. Exercise the bug in `/lab` (e.g. `confirm` on the name modal).
2. `python -m unittest tests.test_regressions`
3. Commit / push only when local + suite are green.

Prod Render should **not** set `GUIDE_LOCAL`.
