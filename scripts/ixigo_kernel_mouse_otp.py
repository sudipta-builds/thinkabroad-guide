"""Run the WhatsApp Ixigo login locally. Same begin_login path, no second connection.

Paste the 6-digit code into data/diagnostics/ixigo-kernel-mouse-otp/OTP_READY.txt
or set IXIGO_OTP before starting. The code is never printed.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env", override=True)

from app.kernel_otp import begin_login, offer_live_otp  # noqa: E402

OUT = ROOT / "data" / "diagnostics" / "ixigo-kernel-mouse-otp"
OTP_FILE = OUT / "OTP_READY.txt"
PHONE = (os.getenv("IXIGO_LOGIN_PHONE") or "+918638586171").strip()
OTP_WAIT_SEC = int(os.getenv("IXIGO_OTP_WAIT_SEC") or "300")


def _watch(session_id: str) -> None:
    deadline = time.monotonic() + OTP_WAIT_SEC
    while time.monotonic() < deadline:
        env = (os.getenv("IXIGO_OTP") or "").strip()
        if env:
            os.environ.pop("IXIGO_OTP", None)
            offer_live_otp(session_id, env)
            return
        if OTP_FILE.is_file():
            raw = OTP_FILE.read_text(encoding="utf-8").strip()
            try:
                OTP_FILE.unlink()
            except OSError:
                pass
            if raw:
                offer_live_otp(session_id, raw)
                return
        time.sleep(0.4)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)

    def on_code_requested(result: dict[str, str]) -> None:
        print("OTP SENT. Paste only the 6 digits.", flush=True)
        threading.Thread(target=_watch, args=(result["session_id"],), daemon=True).start()

    result = begin_login(
        seller="ixigo",
        whatsapp_phone=PHONE,
        explicit=PHONE,
        on_code_requested=on_code_requested,
        otp_wait_sec=OTP_WAIT_SEC,
    )
    status = str(result.get("status") or "")
    print("VERDICT", status, flush=True)
    if result.get("detail"):
        print(result["detail"], flush=True)
    return 0 if status == "logged_in" else 3


if __name__ == "__main__":
    raise SystemExit(main())
