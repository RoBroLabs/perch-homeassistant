"""Letting someone in: `perch/let_in`.

The companion's checks are the boundary. A TV, however it was modified, can only
ask; the door opens only when every check below passes, in this order:

1. The feature is set up and on (script, PIN, doorbell, switch) and not turned
   off by a lockout. Else `disabled`.
2. This TV is on the allow-list. Else `not_allowed`.
3. No timed lockout is running. Else `locked_out`.
4. A ring the companion saw itself in the last 2 minutes, not used yet. Else
   `no_ring`. A restart forgets the ring.
5. The sealed PIN opens for this connection and command id. Else error
   `invalid_format` (not counted: only the TV's own credential can seal).
6. The PIN matches the stored scrypt hash (constant time). Else `wrong_pin`; the
   third wrong PIN in a row locks let-in for 15 minutes, then 1 hour, then turns it
   off until the admin turns it on again in options. Lockouts are kept across
   restarts and each one tells the admin's phones.

Then the ring is used up and the admin's script runs with a fresh Context. Every
request, allowed or refused, is a logbook entry naming the TV. The PIN is never
logged, stored or returned. Any role the TV claims is ignored.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
from typing import Any

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, Context, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import crypto
from .const import (
    CONF_DOORBELL,
    CONF_LET_IN_ENABLED,
    CONF_LET_IN_PIN_HASH,
    CONF_LET_IN_SCRIPT,
    DOMAIN,
    LET_IN_LOCKOUTS,
    LET_IN_SCRIPT_TIMEOUT,
    LET_IN_SEALED_LEN,
    LET_IN_STORE_KEY,
    LET_IN_TRIES,
    RING_WINDOW,
)

_LOGGER = logging.getLogger(__name__)

EVENT_LOGBOOK_ENTRY = "logbook_entry"
NOTICE_ID = "perch_let_in_lockout"
_MARKUP = str.maketrans("", "", "[]()*_`<>#!|\r\n")

_WHY = {
    None: "let someone in",
    "disabled": "was refused: let-in is off",
    "not_allowed": "was refused: this TV is not allowed to let people in",
    "locked_out": "was refused: let-in is locked after wrong PINs",
    "no_ring": "was refused: no recent doorbell ring",
    "wrong_pin": "was refused: wrong PIN",
    "invalid_format": "was refused: the request could not be read",
    "failed": "could not run the let-in script",
}


@dataclass
class Session:
    """What one authenticated connection needs to open a sealed PIN."""

    server: X25519PrivateKey
    challenge: bytes
    tv_pub: bytes
    stored_hash: str
    last_let_in_id: int = 0


@dataclass
class _State:
    failures: int = 0  # wrong PINs since the last lockout or success
    level: int = 0  # lockouts so far; reset only when the admin turns let-in on
    locked_until: float | None = None  # UTC epoch seconds
    disabled: bool = False  # turned off by the last lockout step

    def as_dict(self) -> dict[str, Any]:
        return {
            "failures": self.failures,
            "level": self.level,
            "locked_until": self.locked_until,
            "disabled": self.disabled,
        }


def _plain(name: str) -> str:
    return name.translate(_MARKUP).strip() or "A TV"


@dataclass
class LetIn:
    """Let-in state for the loaded entry."""

    hass: HomeAssistant
    entry: ConfigEntry
    state: _State = field(default_factory=_State)
    _used_ring: datetime | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _unlock_timer: CALLBACK_TYPE | None = None

    def __post_init__(self) -> None:
        self._store: Store[dict[str, Any]] = Store(self.hass, 1, LET_IN_STORE_KEY)

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        until = data.get("locked_until")
        self.state = _State(
            failures=int(data.get("failures") or 0),
            level=int(data.get("level") or 0),
            locked_until=float(until) if isinstance(until, (int, float)) else None,
            disabled=bool(data.get("disabled", False)),
        )
        self._arm_unlock_push()

    @callback
    def async_unload(self) -> None:
        if self._unlock_timer:
            self._unlock_timer()
            self._unlock_timer = None

    # --- state the manifest and the options flow read ---------------------------

    def set_up(self) -> bool:
        """Script, PIN, doorbell and the switch are all set."""
        options = self.entry.options
        return bool(
            options.get(CONF_LET_IN_ENABLED)
            and options.get(CONF_LET_IN_SCRIPT)
            and options.get(CONF_LET_IN_PIN_HASH)
            and options.get(CONF_DOORBELL)
        )

    def switched_on(self) -> bool:
        """What the options form shows: on in options and not turned off by a lockout."""
        return bool(self.entry.options.get(CONF_LET_IN_ENABLED)) and not self.state.disabled

    def locked(self) -> bool:
        until = self.state.locked_until
        return until is not None and dt_util.utcnow().timestamp() < until

    def available_for(self, tv_allowed: bool) -> bool:
        return (
            self.set_up()
            and not self.state.disabled
            and tv_allowed
            and not self.locked()
        )

    async def async_reset(self) -> None:
        """The admin turned let-in on again: forget every lockout."""
        self.state = _State()
        await self._store.async_save(self.state.as_dict())
        persistent_notification.async_dismiss(self.hass, NOTICE_ID)

    # --- the command -----------------------------------------------------------

    def _fresh_ring(self) -> datetime | None:
        """The last ring if it is under 2 minutes old and not used yet."""
        from .runtime import get_runtime  # noqa: PLC0415 - avoid an import cycle

        runtime = get_runtime(self.hass)
        ring = runtime.hub.last_ring if runtime and runtime.hub else None
        if ring is None or ring == self._used_ring:
            return None
        age = dt_util.utcnow() - ring
        return ring if timedelta(0) <= age <= RING_WINDOW else None

    async def async_request(
        self,
        *,
        tv_name: str,
        tv_allowed: bool,
        session: Session | None,
        command_id: int,
        sealed_pin: Any,
    ) -> dict[str, Any]:
        """Answer one `perch/let_in`. Returns the result; raises `LetInError`
        for an error frame."""
        async with self._lock:
            context = Context()
            outcome = await self._decide(
                tv_allowed, session, command_id, sealed_pin, context
            )
            self._log(tv_name, outcome, context)
            if outcome == "invalid_format":
                raise LetInError("invalid_format", "sealed_pin does not open")
            if outcome == "failed":
                raise LetInError("unavailable", "the let-in script did not run")
            if outcome in ("locked_out", "disabled") and self._just_locked:
                self._just_locked = False
                self._tell_admin(tv_name)
            return {"opened": outcome is None, "refusal": outcome}

    _just_locked: bool = False

    async def _decide(
        self,
        tv_allowed: bool,
        session: Session | None,
        command_id: int,
        sealed_pin: Any,
        context: Context,
    ) -> str | None:
        sealed = _b64(sealed_pin)
        if sealed is None or len(sealed) != LET_IN_SEALED_LEN or session is None:
            return "invalid_format"
        if not self.set_up() or self.state.disabled:
            return "disabled"
        if not tv_allowed:
            return "not_allowed"
        if self.locked():
            return "locked_out"
        ring = self._fresh_ring()
        if ring is None:
            return "no_ring"
        # Each command id opens at most one seal on this connection, in order.
        if command_id <= session.last_let_in_id:
            return "invalid_format"
        session.last_let_in_id = command_id
        pin = crypto.open_sealed_pin(
            session.server,
            session.stored_hash,
            session.challenge,
            session.tv_pub,
            command_id,
            sealed,
        )
        if pin is None:
            return "invalid_format"
        good = await self.hass.async_add_executor_job(
            crypto.verify_pin, pin, self.entry.options.get(CONF_LET_IN_PIN_HASH)
        )
        del pin
        if not good:
            return await self._wrong_pin()
        # Use up the ring that was checked, then run the script.
        self._used_ring = ring
        if self.state.failures:
            self.state.failures = 0
            await self._store.async_save(self.state.as_dict())
        script = self.entry.options.get(CONF_LET_IN_SCRIPT)
        try:
            async with asyncio.timeout(LET_IN_SCRIPT_TIMEOUT):
                await self.hass.services.async_call(
                    "script",
                    "turn_on",
                    {"entity_id": script},
                    blocking=True,
                    context=context,
                )
        except Exception:  # noqa: BLE001 - any failure means the door did not open
            _LOGGER.warning("Perch could not run the let-in script")
            return "failed"
        return None

    async def _wrong_pin(self) -> str:
        state = self.state
        state.failures += 1
        if state.failures < LET_IN_TRIES:
            await self._store.async_save(state.as_dict())
            return "wrong_pin"
        state.failures = 0
        state.level += 1
        if state.level > len(LET_IN_LOCKOUTS):
            state.disabled = True
            state.locked_until = None
        else:
            span = LET_IN_LOCKOUTS[state.level - 1]
            state.locked_until = (dt_util.utcnow() + span).timestamp()
        await self._store.async_save(state.as_dict())
        self._just_locked = True
        self._arm_unlock_push()
        self._manifest_changed()
        return "disabled" if state.disabled else "locked_out"

    # --- side effects ------------------------------------------------------------

    def _log(self, tv_name: str, outcome: str | None, context: Context) -> None:
        """A logbook entry naming the TV. Never the PIN."""
        self.hass.bus.async_fire(
            EVENT_LOGBOOK_ENTRY,
            {
                "name": "Perch",
                "message": f"{_plain(tv_name)} {_WHY.get(outcome, 'was refused')}",
                "domain": DOMAIN,
                "entity_id": self.entry.options.get(CONF_LET_IN_SCRIPT) or None,
            },
            context=context,
        )

    def _tell_admin(self, tv_name: str) -> None:
        name = _plain(tv_name)
        if self.state.disabled:
            message = (
                f"Wrong let-in PIN typed {LET_IN_TRIES} times again on {name}. "
                "Letting someone in from Perch is now off. Turn it on again in the "
                "Perch options in Home Assistant."
            )
        else:
            span = LET_IN_LOCKOUTS[self.state.level - 1]
            minutes = int(span.total_seconds() // 60)
            wait = "1 hour" if minutes == 60 else f"{minutes} minutes"
            message = (
                f"Wrong let-in PIN typed {LET_IN_TRIES} times on {name}. "
                f"Letting someone in from Perch is locked for {wait}."
            )
        _LOGGER.warning("Perch let-in locked after wrong PINs (step %s)", self.state.level)
        persistent_notification.async_create(
            self.hass, message, title="Perch let-in locked", notification_id=NOTICE_ID
        )
        from .runtime import get_runtime  # noqa: PLC0415

        runtime = get_runtime(self.hass)
        if runtime is not None and runtime.notifier is not None:
            self.hass.async_create_background_task(
                runtime.notifier.async_admin_notice(message), "perch let-in notice"
            )

    def _manifest_changed(self) -> None:
        from .runtime import get_runtime  # noqa: PLC0415

        runtime = get_runtime(self.hass)
        if runtime is not None and runtime.hub is not None:
            runtime.hub.entry_changed()

    def _arm_unlock_push(self) -> None:
        """When a timed lockout ends, tell the TVs to refetch the manifest."""
        if self._unlock_timer:
            self._unlock_timer()
            self._unlock_timer = None
        until = self.state.locked_until
        if until is None:
            return
        delay = until - dt_util.utcnow().timestamp()
        if delay <= 0:
            return

        @callback
        def _ended(_now: Any) -> None:
            self._unlock_timer = None
            self._manifest_changed()

        self._unlock_timer = async_call_later(self.hass, delay + 1, _ended)


class LetInError(Exception):
    """An error frame for `perch/let_in`."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _b64(value: Any) -> bytes | None:
    if not isinstance(value, str) or len(value) > 200:
        return None
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None
