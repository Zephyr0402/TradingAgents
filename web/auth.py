"""Auth: shared password + device whitelist for the personal web UI.

Security model:
  - One shared password, set via TRADINGAGENTS_WEB_PASSWORD env var (required).
  - On first successful login from a (user-agent, ip-prefix) pair, the device
    fingerprint is registered in devices.yaml. Subsequent logins from
    unregistered devices are rejected until an existing device owner adds them.
  - A signed session cookie (itsdangerous) replaces the password check on
    subsequent requests. Cookie lifetime: 30 days.
  - Session secret: TRADINGAGENTS_WEB_SECRET env var. Generated on first run
    and persisted to secrets.key if absent.

This is intentionally not a real auth system — it's "keep drive-by scanners
out, and make me add a friend's phone manually." A determined attacker with
the password gets in regardless of the device list.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import yaml
from fastapi import HTTPException, Request, Response, status
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

WEB_DATA_DIR = Path(
    os.environ.get("TRADINGAGENTS_WEB_DATA_DIR", "/home/appuser/.tradingagents/web")
)
DEVICES_FILE = WEB_DATA_DIR / "devices.yaml"
SECRETS_FILE = WEB_DATA_DIR / "secrets.key"
DB_FILE = WEB_DATA_DIR / "tasks.db"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SESSION_COOKIE = "ta_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days
COOKIE_SECURE = os.environ.get("TRADINGAGENTS_WEB_COOKIE_SECURE", "1") != "0"


def get_password() -> str:
    """Return the shared password. Hard-fails if unset."""
    pwd = os.environ.get("TRADINGAGENTS_WEB_PASSWORD")
    if not pwd:
        raise RuntimeError(
            "TRADINGAGENTS_WEB_PASSWORD is not set. Refusing to start the web UI."
        )
    return pwd


def get_session_secret() -> bytes:
    """Return the session signing key. Generated and persisted on first run."""
    WEB_DATA_DIR.mkdir(parents=True, exist_ok=True)
    if SECRETS_FILE.exists():
        return SECRETS_FILE.read_bytes()
    key = secrets.token_bytes(32)
    SECRETS_FILE.write_bytes(key)
    os.chmod(SECRETS_FILE, 0o600)
    return key


# ---------------------------------------------------------------------------
# Device whitelist
# ---------------------------------------------------------------------------


def _device_fingerprint(request: Request) -> str:
    """Stable but loose device fingerprint.

    Combines the first /16 of the IP with the User-Agent. Good enough to
    distinguish "Bevis's iPhone" from "Bevis's MacBook" from "random scanner"
    without storing PII.
    """
    ip = _client_ip(request)
    try:
        network = ipaddress.ip_network(f"{ip}/32", strict=False)
        # /24 for IPv4, /48 for IPv6 — coarse enough to survive cellular IP
        # rotation on a single device, fine enough to split a household.
        if isinstance(network, ipaddress.IPv4Network):
            prefix = network.supernet(prefixlen_diff=8).network_address
            ip_part = str(prefix)
        else:
            prefix = network.supernet(prefixlen_diff=80).network_address
            ip_part = str(prefix)
    except ValueError:
        ip_part = "unknown"

    ua = request.headers.get("user-agent", "unknown")
    raw = f"{ip_part}|{ua}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _client_ip(request: Request) -> str:
    """Pick the best client IP, trusting X-Forwarded-For only from local proxies."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _load_devices() -> dict[str, dict]:
    if not DEVICES_FILE.exists():
        return {}
    return yaml.safe_load(DEVICES_FILE.read_text()) or {}


def _save_devices(devices: dict[str, dict]) -> None:
    WEB_DATA_DIR.mkdir(parents=True, exist_ok=True)
    DEVICES_FILE.write_text(yaml.safe_dump(devices, sort_keys=True))
    os.chmod(DEVICES_FILE, 0o600)


def register_device(request: Request, label: Optional[str] = None) -> str:
    """Register the current device and return its fingerprint.

    Called automatically on first successful password login.
    """
    fp = _device_fingerprint(request)
    devices = _load_devices()
    if fp not in devices:
        devices[fp] = {
            "label": label or f"device-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M')}",
            "first_seen": datetime.now(timezone.utc).isoformat(),
            "user_agent": request.headers.get("user-agent", "unknown")[:200],
        }
        _save_devices(devices)
    return fp


def list_devices() -> dict[str, dict]:
    return _load_devices()


def remove_device(fingerprint: str) -> bool:
    devices = _load_devices()
    if fingerprint in devices:
        del devices[fingerprint]
        _save_devices(devices)
        return True
    return False


def is_device_allowed(request: Request) -> bool:
    return _device_fingerprint(request) in _load_devices()


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

_signer: Optional[TimestampSigner] = None


def _get_signer() -> TimestampSigner:
    global _signer
    if _signer is None:
        _signer = TimestampSigner(get_session_secret())
    return _signer


def issue_session(response: Response) -> None:
    """Set a signed session cookie on the response."""
    token = _get_signer().sign(b"ok").decode()
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        path="/",
    )


def clear_session(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def check_session(request: Request) -> bool:
    """Return True if the request carries a valid, unexpired session cookie."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return False
    try:
        # TimestampSigner enforces max_age; using session max-age here too.
        _get_signer().unsign(token, max_age=SESSION_MAX_AGE)
        return True
    except (BadSignature, SignatureExpired):
        return False


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


def verify_password(submitted: str) -> bool:
    """Constant-time password comparison."""
    expected = get_password().encode()
    actual = submitted.encode()
    return hmac.compare_digest(expected, actual)


async def require_session(request: Request) -> None:
    """FastAPI dependency: reject the request if no valid session."""
    # Static assets and login endpoint are mounted outside this dependency,
    # so reaching require_session means the route is authenticated-only.
    if not check_session(request):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    if not is_device_allowed(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Device not registered",
        )
