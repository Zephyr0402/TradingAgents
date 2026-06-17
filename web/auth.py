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
import re
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

# Comma-separated list of CIDRs whose X-Forwarded-For we trust. The
# defaults cover both same-host proxies (loopback) and the standard
# Docker bridge network ranges so the Caddy container in docker-compose
# can pass the real client IP through. Set
# TRADINGAGENTS_TRUSTED_PROXIES="10.0.0.0/8,127.0.0.1/32" if your proxy
# lives elsewhere. Without an explicit trust restriction any direct
# connection (port scans, misconfigured firewall) could spoof its IP
# and slip past the device whitelist.
_TRUSTED_PROXIES_ENV = os.environ.get(
    "TRADINGAGENTS_TRUSTED_PROXIES",
    "127.0.0.0/8,::1/128,172.16.0.0/12,10.0.0.0/8",
)
_TRUSTED_PROXIES: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
for _raw in _TRUSTED_PROXIES_ENV.split(","):
    _raw = _raw.strip()
    if not _raw:
        continue
    try:
        _TRUSTED_PROXIES.append(ipaddress.ip_network(_raw, strict=False))
    except ValueError:
        pass


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
    # Atomic write: tmp + os.replace, so a crash mid-write doesn't leave a
    # half-written file that would lock out every existing session.
    key = secrets.token_bytes(32)
    tmp = SECRETS_FILE.with_suffix(".tmp")
    tmp.write_bytes(key)
    os.chmod(tmp, 0o600)
    os.replace(tmp, SECRETS_FILE)
    return key


# ---------------------------------------------------------------------------
# Device whitelist
# ---------------------------------------------------------------------------


def _device_fingerprint(request: Request) -> str:
    """Stable but loose device fingerprint.

    Combines the first /24 of the IP with a normalized User-Agent that
    has volatile version numbers stripped out. Good enough to distinguish
    "Bevis's iPhone" from "Bevis's MacBook" from "random scanner" without
    storing PII, and robust against OS / browser upgrades that would
    otherwise re-fingerprint the same device.
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

    ua = _normalize_ua(request.headers.get("user-agent", "unknown"))
    raw = f"{ip_part}|{ua}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# Tokenize a User-Agent and drop segments that change with software updates.
# We keep device class + platform + engine; throw away version numbers and
# patch-level build identifiers.
_VERSION_TOKEN = re.compile(r"\d+(?:[._-]\d+)+")


def _normalize_ua(ua: str) -> str:
    if not ua:
        return "unknown"
    return _VERSION_TOKEN.sub("x", ua)[:200]


def _peer_is_trusted(request: Request) -> bool:
    """True if the immediate TCP peer is in the trusted-proxy set.

    Used both for X-Forwarded-For (in _client_ip) and for CF-IPCountry
    (in _country_from_request). Centralized so the policy lives in one
    place: a direct connection to the backend can lie about client IP /
    country headers, but a request that arrived via Caddy+Cloudflare
    is allowed to set them.
    """
    peer = request.client.host if request.client else "unknown"
    try:
        peer_ip = ipaddress.ip_address(peer)
    except ValueError:
        return False
    for net in _TRUSTED_PROXIES:
        if isinstance(net, ipaddress.IPv4Network) and isinstance(peer_ip, ipaddress.IPv4Address):
            if peer_ip in net:
                return True
        elif isinstance(net, ipaddress.IPv6Network) and isinstance(peer_ip, ipaddress.IPv6Address):
            if peer_ip in net:
                return True
    return False


def _client_ip(request: Request) -> str:
    """Pick the best client IP.

    Only trust X-Forwarded-For when the immediate peer is one of the
    configured trusted proxies (Caddy, by default loopback). Otherwise
    use request.client.host — otherwise any direct connection can spoof
    its IP and slip past the device whitelist.
    """
    peer = request.client.host if request.client else "unknown"
    if _peer_is_trusted(request):
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return peer


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

    Records two extra "where in the world" hints that the user can see in
    the Settings drawer as a sanity check on unfamiliar logins:

    * ``country`` — pulled from the ``CF-IPCountry`` header that Cloudflare
      adds at the edge. We only trust the header when the immediate peer
      is in ``_TRUSTED_PROXIES`` (so a direct hit to the backend can't
      forge a country). Country is two-letter ISO 3166-1 alpha-2
      (e.g. ``"US"``) or ``"XX"`` when Cloudflare couldn't determine it.
    * ``timezone`` — an IANA tz name like ``"America/Los_Angeles"`` sent
      by the client in the ``X-TZ`` header on the login request. Optional;
      browsers that don't supply it get ``"unknown"``. Sanity-checked
      against a small whitelist of valid prefixes to reject obvious
      garbage.
    """
    fp = _device_fingerprint(request)
    devices = _load_devices()
    if fp not in devices:
        devices[fp] = {
            "label": label or f"device-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M')}",
            "first_seen": datetime.now(timezone.utc).isoformat(),
            "user_agent": request.headers.get("user-agent", "unknown")[:200],
            "country": _country_from_request(request),
            "timezone": _timezone_from_request(request),
        }
        _save_devices(devices)
    return fp


# Only trust CF-IPCountry when it comes from a real Cloudflare edge.
# A direct connection to the backend could otherwise inject the header
# and spoof a country. We piggy-back on the same trusted-proxy check
# used for X-Forwarded-For via _peer_is_trusted().
def _country_from_request(request: Request) -> str:
    if not _peer_is_trusted(request):
        return "XX"
    raw = request.headers.get("cf-ipcountry", "").strip().upper()
    if not raw:
        return "XX"
    # Cloudflare sends "XX" when it can't determine the country; pass
    # that through unchanged. Otherwise it should be a 2-letter alpha-2
    # code; reject anything longer to avoid storing a header that got
    # concatenated with junk.
    if len(raw) != 2 or not raw.isalpha():
        return "XX"
    return raw


# Browser-supplied timezone. Anything that doesn't look like an IANA tz
# is dropped to "unknown" so a hostile client can't pollute the
# whitelist with arbitrary strings.
_VALID_TZ_PREFIXES = (
    "Africa/", "America/", "Antarctica/", "Asia/", "Atlantic/",
    "Australia/", "Europe/", "Indian/", "Pacific/",
)
_UTC_TZ = "UTC"


def _timezone_from_request(request: Request) -> str:
    raw = request.headers.get("x-tz", "").strip()
    if not raw or len(raw) > 64:
        return "unknown"
    if raw == _UTC_TZ:
        return raw
    if not any(raw.startswith(p) for p in _VALID_TZ_PREFIXES):
        return "unknown"
    return raw


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
