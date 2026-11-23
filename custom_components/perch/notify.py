"""Tell my phone: the one path Perch uses to reach the household's phones.

The admin picks the `notify.*` services in options. The TV never names a service and
never supplies wording: it picks a fixed kind, and the words are the companion's.
The same helper sends the admin the let-in lockout notices.
"""

from __future__ import annotations

import asyncio
from collections import deque
import hashlib
import logging
import re
import time
from typing import Any
import unicodedata

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    CONF_NOTIFY_INCLUDE_TITLE,
    CONF_NOTIFY_REQUEST_READY,
    CONF_NOTIFY_SERVICES,
    NOTIFY_EXCLUDED,
    NOTIFY_OVERALL_HOUR,
    NOTIFY_PER_TV_HOUR,
    NOTIFY_REMEMBER,
    NOTIFY_STORE_KEY,
    NOTIFY_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)

KINDS = ("request_ready",)
TITLE = "Perch"
REQUEST_READY = "Your request is ready to watch"
REQUEST_READY_NAMED = "{name} is ready to watch"
TITLE_ID = re.compile(r"^[A-Za-z0-9._:/-]{1,128}$")
TITLE_NAME_MAX = 100  # characters kept after cleaning
TITLE_NAME_RAW_MAX = 200  # longest name accepted on the wire
_MARKUP = str.maketrans("", "", "[]()*_`<>#!|~\\")
_HOUR = 3600.0
_LINK = re.compile(r"\S*(?:://|www\.)\S*", re.IGNORECASE)


def registered_services(hass: HomeAssistant) -> list[str]:
    """`notify.<name>` for every notify service Perch may use, sorted."""
    return sorted(
        f"notify.{name}"
        for name in hass.services.async_services_for_domain("notify")
        if name not in NOTIFY_EXCLUDED
    )


def clean_title_name(raw: Any) -> str | None:
    """A title name safe for a lock screen, or None if nothing usable is left.

    Control, format and private-use characters are dropped, markup characters are
    removed, whitespace is collapsed and the result is cut to 100 characters.
    """
    if not isinstance(raw, str) or not 0 < len(raw) <= TITLE_NAME_RAW_MAX:
        return None
    kept = "".join(
        " " if ch.isspace() else ch
        for ch in raw
        if ch.isspace() or not unicodedata.category(ch).startswith("C")
    )
    text = " ".join(_LINK.sub(" ", kept).translate(_MARKUP).split())
    return text[:TITLE_NAME_MAX].strip() or None


class _Window:
    """A sliding one-hour count."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.times: deque[float] = deque()

    def full(self, now: float) -> bool:
        while self.times and now - self.times[0] >= _HOUR:
            self.times.popleft()
        return len(self.times) >= self.limit

    def add(self, now: float) -> None:
        self.times.append(now)


class PhoneNotifier:
    """Sends Perch's fixed messages to the services the admin picked."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._store: Store[dict[str, Any]] = Store(hass, 1, NOTIFY_STORE_KEY)
        # sha256(kind:title_id) hex -> when it was sent (epoch seconds), oldest first.
        self._sent: dict[str, float] = {}
        self._per_tv: dict[str, _Window] = {}
        self._overall = _Window(NOTIFY_OVERALL_HOUR)
        self._lock = asyncio.Lock()

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        sent = data.get("sent")
        if isinstance(sent, dict):
            items = [
                (k, float(v))
                for k, v in sent.items()
                if isinstance(k, str) and isinstance(v, (int, float))
            ]
            items.sort(key=lambda kv: kv[1])
            self._sent = dict(items[-NOTIFY_REMEMBER:])

    def services(self) -> list[str]:
        """The admin's picks that are still registered."""
        wanted = self.entry.options.get(CONF_NOTIFY_SERVICES) or []
        have = set(registered_services(self.hass))
        return [s for s in wanted if s in have]

    def request_ready_available(self) -> bool:
        return bool(self.entry.options.get(CONF_NOTIFY_REQUEST_READY)) and bool(
            self.services()
        )

    async def async_send(self, message: str) -> bool:
        """Send `message` to every picked service. True if at least one took it."""
        services = self.services()
        if not services:
            return False
        results = await asyncio.gather(
            *(self._call(service, message) for service in services)
        )
        return any(results)

    async def _call(self, service: str, message: str) -> bool:
        _, name = service.split(".", 1)
        try:
            async with asyncio.timeout(NOTIFY_TIMEOUT):
                await self.hass.services.async_call(
                    "notify",
                    name,
                    {"title": TITLE, "message": message},
                    blocking=True,
                    context=Context(),
                )
        except TimeoutError:
            _LOGGER.warning("Perch could not reach %s in time", service)
            return False
        except Exception:  # noqa: BLE001 - any failure is "not sent"
            _LOGGER.warning("Perch could not send to %s", service)
            return False
        return True

    async def async_admin_notice(self, message: str) -> None:
        """A notice for the admin (let-in lockouts). Never rate limited; a failure
        is logged and otherwise ignored."""
        if not await self.async_send(message):
            _LOGGER.warning("Perch has no phone to tell about let-in")

    async def async_request_ready(
        self, tv_id: str, title_id: str, title_name: str | None
    ) -> dict[str, Any]:
        """The `perch/notify` result for kind `request_ready`. Raises nothing;
        the caller maps `None` from a failed send to an error."""
        async with self._lock:
            if not self.request_ready_available():
                return {"sent": False, "refusal": "not_set_up"}
            key = hashlib.sha256(f"request_ready:{title_id}".encode()).hexdigest()
            if key in self._sent:
                return {"sent": False, "refusal": "already_sent"}
            now = time.monotonic()
            per_tv = self._per_tv.setdefault(tv_id, _Window(NOTIFY_PER_TV_HOUR))
            if per_tv.full(now) or self._overall.full(now):
                return {"sent": False, "refusal": "rate_limited"}
            per_tv.add(now)
            self._overall.add(now)
            message = REQUEST_READY
            if title_name and self.entry.options.get(CONF_NOTIFY_INCLUDE_TITLE):
                message = REQUEST_READY_NAMED.format(name=title_name)
            if not await self.async_send(message):
                return {"sent": False, "refusal": None}
            self._sent[key] = time.time()
            while len(self._sent) > NOTIFY_REMEMBER:
                self._sent.pop(next(iter(self._sent)))
            await self._store.async_save({"sent": dict(self._sent)})
            return {"sent": True, "refusal": None}
