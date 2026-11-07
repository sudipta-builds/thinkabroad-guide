"""Run Guide on http://127.0.0.1:8000 matching prod agent/browser/Kernel.

WhatsApp I/O is captured at /lab — Meta/Twilio are not messaged.
Uses a separate DB so local turns do not touch prod state.

  python scripts/run_local.py
  open http://127.0.0.1:8000/lab
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env", override=True)

# Prod-matching stack; only egress and DB path are local-safe.
os.environ["GUIDE_LOCAL"] = "1"
os.environ.setdefault("GUIDE_PUBLIC_URL", "http://127.0.0.1:8000")
os.environ.setdefault("GUIDE_DB_PATH", str(ROOT / "data" / "guide-local.db"))
os.environ.setdefault("GUIDE_LAB_PHONE", "919999000001")

# Settings are cached — clear after env overrides.
from app.config import get_settings

get_settings.cache_clear()
settings = get_settings()

(Path(settings.guide_db_path).parent).mkdir(parents=True, exist_ok=True)

print("Guide local lab (prod agent path)")
print(f"  UI:     http://127.0.0.1:8000/lab")
print(f"  Health: http://127.0.0.1:8000/health")
print(f"  DB:     {settings.guide_db_path}")
print(f"  Phone:  {settings.guide_lab_phone}")
print(f"  Gemini: {settings.search_ready}  DeepSeek: {settings.llm_ready}  Kernel: {settings.kernel_ready}")
print(f"  Proxy:  {settings.proxy_ready}  Vault: {settings.vault_ready}")
print("  WhatsApp egress: lab outbox only (GUIDE_LOCAL=1)")
print()

import uvicorn

uvicorn.run(
    "app.main:app",
    host="127.0.0.1",
    port=8000,
    reload=False,
    log_level=(settings.log_level or "INFO").lower(),
)
