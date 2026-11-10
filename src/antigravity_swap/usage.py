"""Quota readings, from the same two RPCs the Antigravity CLI uses.

    POST /v1internal:loadCodeAssist            -> {cloudaicompanionProject}
    POST /v1internal:retrieveUserQuotaSummary  -> {groups: [{displayName, buckets: [
           {bucketId, window, remainingFraction, resetTime}]}]}

Buckets seen so far: ``gemini-5h`` and ``gemini-weekly`` (Gemini models), ``3p-5h`` and
``3p-weekly`` (Claude and GPT models through agy). agy reports what is *left*; aswap shows
what is *used*, so the numbers read like claude-swap's.

Not every limit is visible here. Accounts also hit an "Individual quota reached" ceiling
that this endpoint does not report; ``aswap exec`` catches that one when it happens.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from antigravity_swap import __version__
from antigravity_swap.credentials import parse_rfc3339

HOSTS = ("daily-cloudcode-pa.googleapis.com", "cloudcode-pa.googleapis.com")
TIMEOUT = 15
WINDOW_ORDER = ("gemini-5h", "gemini-weekly", "3p-5h", "3p-weekly")


class UsageError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind  # "auth", "http-<code>", "network", "empty"


def _call(host: str, path: str, body: dict, token: str) -> dict:
    req = urllib.request.Request(
        f"https://{host}{path}",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": f"antigravity-swap/{__version__}",
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_via_agy(cred_raw: str | None = None, is_active: bool = False, gdir: Path | None = None) -> dict | None:
    """Run agy -p /usage --output-format json to get exact quota including Gemini."""
    try:
        from antigravity_swap.agy import find_agy, dir_args
        agy_cmd = find_agy()
    except Exception:
        return None

    tmp_dir = None
    try:
        if is_active:
            cmd = agy_cmd + ["-p", "/usage", "--output-format", "json"]
        elif gdir is not None:
            cmd = agy_cmd + dir_args(gdir) + ["-p", "/usage", "--output-format", "json"]
        elif cred_raw:
            tmp_dir = Path(tempfile.mkdtemp(prefix="aswap-usage-"))
            tok_dir = tmp_dir / "antigravity-cli"
            tok_dir.mkdir(parents=True, exist_ok=True)
            (tok_dir / "antigravity-oauth-token").write_text(cred_raw, encoding="utf-8")
            cmd = agy_cmd + dir_args(tmp_dir) + ["-p", "/usage", "--output-format", "json"]
        else:
            return None

        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            data = json.loads(r.stdout.strip())
            groups = normalize(data)
            if groups:
                return {"ts": time.time(), "project": "agy-cli", "host": "agy", "groups": groups}
    except Exception:
        pass
    finally:
        if tmp_dir and tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
    return None


def fetch(token: str, project: str | None = None, cred_raw: str | None = None,
          is_active: bool = False, gdir: Path | None = None) -> dict:
    """Fetch and normalize one account's quota. Raises UsageError."""
    agy_reading = fetch_via_agy(cred_raw=cred_raw, is_active=is_active, gdir=gdir)
    if agy_reading:
        return agy_reading

    last: UsageError | None = None
    for host in HOSTS:
        proj = project
        for attempt in range(2):
            try:
                if not proj:
                    proj = _call(host, "/v1internal:loadCodeAssist", {}, token).get("cloudaicompanionProject")
                if not proj:
                    last = UsageError("empty", f"{host}: no project in loadCodeAssist")
                    break
                resp = _call(host, "/v1internal:retrieveUserQuotaSummary", {"project": proj}, token)
                return {"ts": time.time(), "project": proj, "host": host, "groups": normalize(resp)}
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    raise UsageError("auth", "access token rejected (401)") from None
                if proj and attempt == 0 and e.code in (403, 404):
                    proj = None  # a stale cached project; ask for it again
                    continue
                last = UsageError(f"http-{e.code}", f"HTTP {e.code} from {host}")
                break
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
                last = UsageError("network", f"{host}: {e}")
                break
    raise last or UsageError("network", "no host answered")


def normalize(resp: dict) -> list[dict]:
    raw_groups = resp.get("groups")
    if raw_groups is None and "command" in resp:
        raw_groups = resp.get("command", {}).get("data", {}).get("groups")
    groups = []
    for g in raw_groups or []:
        buckets = []
        for b in g.get("buckets") or []:
            rem = b.get("remainingFraction") if b.get("remainingFraction") is not None else b.get("remaining_fraction")
            if b.get("disabled") or rem is None:
                continue
            b_id = b.get("bucketId") or b.get("id") or ""
            r_time = b.get("resetTime") or b.get("reset_time") or ""
            buckets.append({
                "bucket_id": b_id,
                "window": b.get("window") or "",
                "remaining_fraction": float(rem),
                "reset_time": r_time,
            })
        if buckets:
            d_name = g.get("displayName") or g.get("name") or ""
            groups.append({"display_name": d_name, "buckets": buckets})
    return groups


def windows(reading: dict | None) -> dict[str, dict]:
    """{bucket_id: {"used": pct, "resets_at": epoch|None}} for one reading."""
    if not reading:
        return {}
    out: dict[str, dict] = {}
    for g in (reading or {}).get("groups") or []:
        for b in g.get("buckets") or []:
            used = round((1.0 - float(b.get("remaining_fraction", 1.0))) * 100.0, 1)
            out[b.get("bucket_id", "")] = {"used": max(0.0, min(100.0, used)),
                                           "resets_at": parse_rfc3339(b.get("reset_time"))}
    return out


def in_pool(bucket_id: str, pool: str) -> bool:
    if pool == "all":
        return True
    if pool == "3p":
        return bucket_id.startswith("3p-")
    return bucket_id.startswith("gemini-")


def binding_used(reading: dict | None, pool: str) -> float | None:
    """The most-used window in the pool: the one that will stop you first."""
    vals = [w["used"] for b, w in windows(reading).items() if in_pool(b, pool)]
    return max(vals) if vals else None


def weekly_reset(reading: dict | None, pool: str) -> float | None:
    ts = [w["resets_at"] for b, w in windows(reading).items()
          if in_pool(b, pool) and b.endswith("weekly") and w["resets_at"]]
    return min(ts) if ts else None


def earliest_recovery(reading: dict | None, pool: str, threshold: float) -> float | None:
    """When the binding window in the pool drops back under ``threshold``: its reset."""
    ts = [w["resets_at"] for b, w in windows(reading).items()
          if in_pool(b, pool) and w["used"] >= threshold and w["resets_at"]]
    return max(ts) if ts else None
