"""Pending TV pairing and request throttling.

Everything here lives in memory only. A restart drops a pending pairing and
the rate counters; credential hashes live in the config entry, not here.

Nothing in this module logs the code, an HMAC, a key, a credential or an
envelope. Keep it that way.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from enum import StrEnum
import hmac
import ipaddress
import math
import secrets

from homeassistant.util import dt as dt_util

from . import crypto

from .const import PAIRING_TTL
MAX_CONTENDERS = 4  # TVs that may post into one pairing window
TV_NAME_MAX = 64


def _now() -> float:
    """Wall-clock seconds; patched by the test clock."""
    return dt_util.utcnow().timestamp()


class PairingStatus(StrEnum):
    """What a TV's poll reports (the protocol's `PairStatusResponse`).

    `rejected`: the admin chose "They do not match". `cancelled`: attempts
    used up, a second key verified the same code, or another TV in the same
    window was paired instead.
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class CodeResult(StrEnum):
    """Outcome of one code entry by the admin."""

    MATCH = "match"
    WRONG = "wrong"
    EXHAUSTED = "exhausted"
    AMBIGUOUS = "ambiguous"
    GONE = "gone"


class BusyError(Exception):
    """A pairing window is open and full, or closed to new keys."""


@dataclass(eq=False)
class Contender:
    """One TV that posted into the current pairing window."""

    tv_name: str
    public_key: bytes
    nonce: bytes
    mac: bytes
    pairing_id: str = field(default_factory=lambda: f"pr_{secrets.token_urlsafe(12)}")
    status: PairingStatus = PairingStatus.PENDING
    tv_id: str | None = None
    envelope: bytes | None = None
    source: str | None = None
    approved_at: float = 0.0

    def __repr__(self) -> str:
        # Never let a key, nonce or HMAC reach a log through repr().
        return f"Contender(tv_name={self.tv_name!r}, status={self.status})"


@dataclass(eq=False)
class PairingWindow:
    """One pairing code's life: opened by the first TV, closed by the admin or time."""

    window_id: str = field(default_factory=lambda: f"pr_{secrets.token_urlsafe(12)}")
    expires_at: float = field(default_factory=lambda: _now() + PAIRING_TTL)
    contenders: list[Contender] = field(default_factory=list)
    attempts: int = 0
    closed: bool = False
    matched: Contender | None = None

    @property
    def expired(self) -> bool:
        return _now() >= self.expires_at

    @property
    def open(self) -> bool:
        return not self.closed and not self.expired

    def close(self, status: PairingStatus) -> None:
        """Close the window; every contender still pending gets `status`."""
        self.closed = True
        for contender in self.contenders:
            if contender.status is PairingStatus.PENDING:
                contender.status = status

    def __repr__(self) -> str:
        return (
            f"PairingWindow(contenders={len(self.contenders)}, "
            f"attempts={self.attempts}, closed={self.closed})"
        )


class PairingManager:
    """Holds at most one pairing window."""

    def __init__(self) -> None:
        self._window: PairingWindow | None = None
        # The approved contender, kept reachable until its TV has polled
        # approved once or PAIRING_TTL has passed, even if a new window opens.
        self._approved: Contender | None = None

    @property
    def window(self) -> PairingWindow | None:
        """The window an admin can still enter a code for, if any."""
        window = self._window
        if window is None or not window.open:
            return None
        return window

    def start(
        self,
        tv_name: str,
        public_key: bytes,
        nonce: bytes,
        mac: bytes,
        source: str | None = None,
    ) -> Contender:
        """Register a TV's pairing request.

        The first TV opens a window. Another key posted while the window is
        open joins it as a contender, so a second key with a valid HMAC for
        the same code can be caught when the admin enters the code. Posting
        the same key again returns the same contender (a retried request).
        One source address holds at most one contender per window, so one
        address cannot fill the window.
        """
        window = self.window
        if window is None:
            window = self._window = PairingWindow()
        for contender in window.contenders:
            if hmac.compare_digest(contender.public_key, public_key):
                if hmac.compare_digest(contender.nonce, nonce) and hmac.compare_digest(
                    contender.mac, mac
                ):
                    return contender
                raise BusyError
        if window.matched is not None or len(window.contenders) >= MAX_CONTENDERS:
            raise BusyError
        if source is not None and any(c.source == source for c in window.contenders):
            raise BusyError
        contender = Contender(tv_name, public_key, nonce, mac, source=source)
        window.contenders.append(contender)
        return contender

    def lookup(self, pairing_id: str) -> Contender | None:
        """Find a contender by its pairing id, including closed windows."""
        window = self._window
        approved = self._approved
        if approved is not None and _now() >= approved.approved_at + PAIRING_TTL:
            approved = self._approved = None
        contenders = list(window.contenders) if window is not None else []
        if approved is not None and approved not in contenders:
            contenders.append(approved)
        found = None
        for contender in contenders:
            if (
                contender.status is PairingStatus.APPROVED
                and _now() >= contender.approved_at + PAIRING_TTL
            ):
                continue
            # Compare every id so timing says nothing about which matched.
            if hmac.compare_digest(
                contender.pairing_id.encode(), pairing_id.encode()
            ):
                found = contender
        return found

    def status(self, contender: Contender) -> PairingStatus:
        """A contender's status, turning a lapsed pending one into expired."""
        window = self._window
        if (
            contender.status is PairingStatus.PENDING
            and window is not None
            and window.expired
        ):
            return PairingStatus.EXPIRED
        return contender.status

    def check_code(
        self, window_id: str, code: str, max_attempts: int
    ) -> tuple[CodeResult, Contender | None]:
        """Check an admin-entered code against every key in the window."""
        window = self.window
        if window is None or window.window_id != window_id or window.matched:
            return CodeResult.GONE, None
        code = code.strip()
        verified = [
            c
            for c in window.contenders
            if crypto.verify_binding(code, c.public_key, c.nonce, c.mac)
        ]
        if len(verified) > 1:
            window.close(PairingStatus.CANCELLED)
            return CodeResult.AMBIGUOUS, None
        if len(verified) == 1:
            window.matched = verified[0]
            return CodeResult.MATCH, verified[0]
        window.attempts += 1
        if window.attempts >= max_attempts:
            window.close(PairingStatus.CANCELLED)
            return CodeResult.EXHAUSTED, None
        return CodeResult.WRONG, None

    def matched(self, window_id: str) -> Contender | None:
        """The contender whose code matched, while its window is still open."""
        window = self.window
        if window is None or window.window_id != window_id:
            return None
        return window.matched

    def approve(self, contender: Contender, tv_id: str, envelope: bytes) -> None:
        """Hand the sealed credential to the matched TV; others are cancelled."""
        window = self._window
        contender.status = PairingStatus.APPROVED
        contender.tv_id = tv_id
        contender.envelope = envelope
        contender.approved_at = _now()
        self._approved = contender
        if window is not None:
            window.close(PairingStatus.CANCELLED)

    def delivered(self, contender: Contender) -> None:
        """The TV has been told it is approved: the pairing is gone."""
        if self._approved is contender:
            self._approved = None
        window = self._window
        if window is not None and contender in window.contenders:
            window.contenders.remove(contender)

    def reject(self, window_id: str) -> None:
        """The admin said the words do not match."""
        window = self._window
        if window is not None and window.window_id == window_id:
            window.close(PairingStatus.REJECTED)

    def clear(self) -> None:
        """Drop all pairing state (unload)."""
        self._window = None
        self._approved = None


# Most sources tracked at once; the oldest are evicted beyond this.
MAX_SOURCES = 1024


_LOCAL_V6 = (
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)


def _source_key(source: str | None) -> str:
    """Rate-limit key: IPv4 per address; IPv6 per address when private,
    link-local or loopback (LAN devices share a /64), else per /64; anything
    else as-is."""
    if not source:
        return "unknown"
    try:
        addr = ipaddress.ip_address(source.partition("%")[0])
    except ValueError:
        return source
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped)
        if addr.is_loopback or any(addr in net for net in _LOCAL_V6):
            return str(addr)
        return str(ipaddress.ip_network((addr, 64), strict=False))
    return str(addr)


@dataclass
class _Bucket:
    hits: list[float] = field(default_factory=list)
    blocked_until: float = 0.0
    penalty: float = 0.0


class RateLimiter:
    """Sliding-window limit per source address and overall, with back-off.

    Over the limit, the source (or everyone, for the overall limit) is blocked
    for `backoff_start` seconds, doubling on each further breach up to
    `backoff_max`. A quiet `window` after a block resets the penalty.
    """

    def __init__(
        self,
        per_source: int,
        overall: int,
        window: float = 60.0,
        backoff_start: float = 60.0,
        backoff_max: float = 900.0,
    ) -> None:
        self._per_source = per_source
        self._overall = overall
        self._window = window
        self._backoff_start = backoff_start
        self._backoff_max = backoff_max
        self._sources: OrderedDict[str, _Bucket] = OrderedDict()
        self._next_prune = 0.0
        self._all = _Bucket()

    def _hit(self, bucket: _Bucket, limit: int, now: float) -> float:
        """0 if allowed, else seconds until the bucket opens again."""
        if now < bucket.blocked_until:
            return bucket.blocked_until - now
        if bucket.penalty and now >= bucket.blocked_until + self._window:
            bucket.penalty = 0.0
        bucket.hits = [t for t in bucket.hits if now - t < self._window]
        if len(bucket.hits) >= limit:
            bucket.penalty = min(
                self._backoff_max,
                bucket.penalty * 2 if bucket.penalty else self._backoff_start,
            )
            bucket.blocked_until = now + bucket.penalty
            bucket.hits.clear()
            return bucket.penalty
        bucket.hits.append(now)
        return 0.0

    def check(self, source: str | None) -> int:
        """Count one request. 0 means go ahead; otherwise refuse it with
        rate_limited and this many seconds as `retry_after`."""
        now = _now()
        if now < self._all.blocked_until:
            # Overall lock: refuse without touching the source map.
            wait = self._all.blocked_until - now
            return max(1, math.ceil(wait))
        key = _source_key(source)
        bucket = self._sources.get(key)
        if bucket is None:
            if now >= self._next_prune or len(self._sources) >= MAX_SOURCES:
                self._prune(now)
            while len(self._sources) >= MAX_SOURCES:
                del self._sources[next(iter(self._sources))]
            bucket = self._sources[key] = _Bucket()
        else:
            self._sources.move_to_end(key)
        wait = self._hit(bucket, self._per_source, now)
        if not wait:
            wait = self._hit(self._all, self._overall, now)
        return max(1, math.ceil(wait)) if wait else 0

    def allow(self, source: str | None) -> bool:
        """Count one request; False means refuse it."""
        return self.check(source) == 0

    def _prune(self, now: float) -> None:
        """Drop idle sources. Runs at most once per window, or when full."""
        self._next_prune = now + self._window
        for key in [
            k
            for k, b in self._sources.items()
            if now >= b.blocked_until
            and not (b.penalty and now < b.blocked_until + self._window)
            and all(now - t >= self._window for t in b.hits)
        ]:
            del self._sources[key]
