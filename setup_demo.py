"""Prepare local-only credentials and a short synthetic WAV for the portal."""
import math
import os
import secrets
import struct
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main():
    env = ROOT / ".env"
    if not env.exists():
        keys = (
            "MYSQL_ROOT_PASSWORD", "MYSQL_PASSWORD", "PARTNER_TOKEN",
            "DEMO_AGENT1_PASSWORD", "DEMO_AGENT2_PASSWORD",
        )
        with os.fdopen(os.open(env, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as out:
            out.write("DEMO_ADMIN_PASSWORD=admin\n")
            out.write("OPENAI_API_KEY=\n")
            for key in keys:
                out.write(f"{key}={secrets.token_urlsafe(32)}\n")
        print("Created .env with local credentials.")
    sample = ROOT / ".local" / "sample.wav"
    sample.parent.mkdir(exist_ok=True)
    (sample.parent / "recordings").mkdir(mode=0o700, exist_ok=True)
    if not sample.exists():
        with wave.open(str(sample), "wb") as audio:
            audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
            audio.writeframes(b"".join(
                struct.pack("<h", int(3600 * math.sin(2 * math.pi * 330 * i / 8000)))
                for i in range(8000)
            ))
    print("Ready. Run: docker compose up -d --build")


if __name__ == "__main__":
    main()
