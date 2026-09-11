from __future__ import annotations

import json
import os
import time
from pathlib import Path

CAPS = {"modeA": 1, "modeB": 2}


def limits_path() -> Path:
    env = os.environ.get("DLC_LIMITS_PATH")
    if env:
        return Path(env)
    return Path.home() / ".dlc" / "limits.json"


def enforced() -> bool:
    return os.environ.get("DLC_ENFORCE_LIMITS", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _load() -> dict:
    p = limits_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("date") != _today():
            raise ValueError
        used = data.get("used")
        if not isinstance(used, dict):
            raise ValueError
        return {"date": data["date"],
                "used": {m: int(used.get(m, 0)) for m in CAPS}}
    except Exception:
        return {"date": _today(), "used": {m: 0 for m in CAPS}}


def _save(data: dict) -> None:
    p = limits_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass


def state() -> dict:
    data = _load()
    used = data["used"]
    return {
        "enforced": enforced(),
        "date": data["date"],
        "caps": dict(CAPS),
        "used": dict(used),
        "remaining": {m: max(0, CAPS[m] - used.get(m, 0)) for m in CAPS},
    }


def allowed(mode: str) -> bool:
    if mode not in CAPS:
        return True
    if not enforced():
        return True
    return _load()["used"].get(mode, 0) < CAPS[mode]


def consume(mode: str) -> dict:
    if mode in CAPS:
        data = _load()
        data["used"][mode] = data["used"].get(mode, 0) + 1
        _save(data)
    return state()


def refund(mode: str) -> dict:
    if mode in CAPS:
        data = _load()
        data["used"][mode] = max(0, data["used"].get(mode, 0) - 1)
        _save(data)
    return state()