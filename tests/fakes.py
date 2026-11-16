"""A fake-light harness: `light.turn_on` and `light.turn_off` that write states
under the call's context, plus the real `scene` integration so `scene.apply`
and `scene.turn_on` work."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.setup import async_setup_component

_COLOUR = (
    "color_mode",
    "color_temp_kelvin",
    "hs_color",
    "rgb_color",
    "rgbw_color",
    "rgbww_color",
    "xy_color",
)


class FakeLights:
    """Registers the light services; `calls` lists (service, entity, data)."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        hass.services.async_register("light", "turn_on", self._turn_on)
        hass.services.async_register("light", "turn_off", self._turn_off)

    def _ids(self, call: ServiceCall) -> list[str]:
        ids = call.data.get("entity_id", [])
        return [ids] if isinstance(ids, str) else list(ids)

    def _turn_on(self, call: ServiceCall) -> None:
        for entity_id in self._ids(call):
            old = self.hass.states.get(entity_id)
            attrs = dict(old.attributes) if old else {}
            data = {k: v for k, v in call.data.items() if k != "entity_id"}
            self.calls.append(("turn_on", entity_id, data))
            if "brightness_pct" in data:
                attrs["brightness"] = round(data["brightness_pct"] * 255 / 100)
            elif "brightness" in data:
                attrs["brightness"] = data["brightness"]
            for key in _COLOUR:
                if key in data:
                    attrs[key] = data[key]
            self.hass.states.async_set(entity_id, "on", attrs, context=call.context)

    def _turn_off(self, call: ServiceCall) -> None:
        for entity_id in self._ids(call):
            old = self.hass.states.get(entity_id)
            attrs = {
                k: v
                for k, v in (old.attributes if old else {}).items()
                if k != "brightness"
            }
            self.calls.append(("turn_off", entity_id, {}))
            self.hass.states.async_set(entity_id, "off", attrs, context=call.context)

    def set(self, entity_id: str, on: bool = True, brightness: int | None = None, **attrs):
        """Set a light as a person would: no Perch context."""
        extra = dict(attrs)
        if on and brightness is not None:
            extra["brightness"] = brightness
        self.hass.states.async_set(entity_id, "on" if on else "off", extra)

    def level(self, entity_id: str) -> tuple[str, int | None]:
        state = self.hass.states.get(entity_id)
        return state.state, state.attributes.get("brightness")


async def setup_lights(
    hass: HomeAssistant, scenes: list[dict[str, Any]] | None = None
) -> FakeLights:
    """Fake lights plus the real scene integration. `scenes` is a list of
    {"name": ..., "entities": {...}} for the homeassistant scene platform."""
    fake = FakeLights(hass)
    config = {
        "scene": [{"platform": "homeassistant", "states": scenes}]
        if scenes
        else [{"platform": "homeassistant", "states": [{"name": "unused", "entities": {"light.unused": "off"}}]}]
    }
    assert await async_setup_component(hass, "scene", config)
    await hass.async_block_till_done()
    return fake
