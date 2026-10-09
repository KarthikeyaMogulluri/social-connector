"""Reads and writes settings in .env, fresh on every call.

Reading fresh means a token saved by oauth.py works in an already-running
server, with no restart. The .env file wins over process env vars.
"""
import os
import re
from pathlib import Path

from dotenv import dotenv_values

ENV = Path(os.environ.get("SOCIAL_ENV_FILE") or Path(__file__).with_name(".env"))

# Control characters plus Unicode line separators (NEL, LS, PS): any of them would
# split a line or inject an extra setting.
_LINE_BREAKS = re.compile("[" + chr(0) + "-" + chr(0x1F) + chr(0x85) + chr(0x2028) + chr(0x2029) + "]")  # built with chr(): invisible characters must not live in source


def _read() -> dict:
    if not ENV.exists():
        return {}
    # utf-8-sig: Windows Notepad adds an invisible BOM that would corrupt the first key.
    # interpolate=False: a literal ${...} in a token must stay literal.
    return dotenv_values(ENV, encoding="utf-8-sig", interpolate=False)


def get(name: str) -> str:
    return _read().get(name) or os.environ.get(name, "")


def save(key: str, value: str) -> None:
    value = _LINE_BREAKS.sub("", value)
    lines = []
    if ENV.exists():
        lines = ENV.read_text(encoding="utf-8-sig").splitlines()
    lines = [ln for ln in lines if not ln.startswith(f"{key}=")]
    # Single quotes make python-dotenv take the value literally (no #-comments, no $VAR expansion).
    quoted = "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
    lines.append(f"{key}={quoted}")
    # Write to a temp file then swap it in, so a crash mid-write cannot leave a half-written .env.
    tmp = ENV.with_name(ENV.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, ENV)


def secrets_in_use() -> list[str]:
    """Every token/secret currently configured, longest first, so errors can be scrubbed."""
    file_values = _read()
    found = set()
    for name in set(os.environ) | set(file_values):
        if name.endswith(("_TOKEN", "_SECRET")):
            value = file_values.get(name) or os.environ.get(name, "")
            if len(value) >= 8:
                found.add(value)
    return sorted(found, key=len, reverse=True)  # a secret containing another must be replaced first


def redact(text: str) -> str:
    for s in secrets_in_use():
        text = text.replace(s, "***")
    return text
