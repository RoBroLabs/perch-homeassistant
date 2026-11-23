"""Config flow: one Perch entry per Home Assistant; each TV is a subentry.

Setting up Perch asks nothing. Pairing a TV asks only for the code the TV
shows, then for a yes or no on four words.
"""

from __future__ import annotations

import base64
import secrets
from typing import Any

import voluptuous as vol

from homeassistant.components import persistent_notification
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers.selector import (
    AreaSelector,
    AreaSelectorConfig,
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from . import crypto
from .const import (
    CONF_ADVANCED,
    CONF_ALLOW_CLOUD,
    CONF_CAMERAS,
    CONF_CREDENTIAL_HASH,
    CONF_DOORBELL,
    CONF_DOORBELL_CAMERA,
    CONF_LET_IN,
    CONF_LET_IN_ALLOWED,
    CONF_LET_IN_ENABLED,
    CONF_LET_IN_PIN,
    CONF_LET_IN_PIN_CONFIRM,
    CONF_LET_IN_PIN_HASH,
    CONF_LET_IN_SCRIPT,
    CONF_MOVIE_SCENE,
    CONF_NOTIFY_INCLUDE_TITLE,
    CONF_NOTIFY_REQUEST_READY,
    CONF_NOTIFY_SERVICES,
    CONF_PAUSED_SCENE,
    CONF_PHONE,
    CONF_PAIRING_ATTEMPTS,
    CONF_PUBLIC_KEY,
    CONF_RESTORE_TIMEOUT,
    CONF_ROOM_ID,
    CONF_ROOMS,
    CONF_SCENES,
    CONF_TV_ID,
    DEFAULT_ALLOW_CLOUD,
    DEFAULT_PAIRING_ATTEMPTS,
    DEFAULT_RESTORE_TIMEOUT,
    DOMAIN,
    MAX_PAIRING_ATTEMPTS,
    MAX_RESTORE_TIMEOUT,
    MIN_PAIRING_ATTEMPTS,
    MIN_RESTORE_TIMEOUT,
    SUBENTRY_TV,
)
from .notify import registered_services
from .pairing import CodeResult
from .runtime import get_runtime
from .words import async_get_words

NOTIFICATION_ID = "perch_pairing_aborted"


class PerchConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add Perch. Nothing to type."""

    VERSION = 1

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_TV: PairTvFlow}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return PerchOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return self.async_create_entry(title="Perch", data={})
        return self.async_show_form(step_id="user", data_schema=vol.Schema({}))


_MARKDOWN = str.maketrans("", "", "[]()*_`<>#!|\r\n")


def _plain(name: str) -> str:
    """A TV's name with Markdown characters and newlines removed, for a flow
    description. The TV chooses its name, so it must not shape the text."""
    return name.translate(_MARKDOWN).strip()


def _minutes(low: int, high: int) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=low, max=high, step=1, mode=NumberSelectorMode.BOX
        )
    )


_SCENE_NAME_GUESS = {
    CONF_MOVIE_SCENE: "movie night",
    CONF_PAUSED_SCENE: "paused",
}


class PerchOptionsFlow(OptionsFlow):
    """What the whole house offers to every TV, in two steps.

    Step 1 picks what is offered. Step 2 picks the doorbell camera and the
    Movie night and Paused scenes, only from what step 1 picked.
    """

    _options: dict[str, Any]
    _reset_let_in: bool = False
    _let_in_shown: bool | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        current = self.config_entry.options
        errors: dict[str, str] = {}
        if user_input is not None:
            advanced = user_input.get(CONF_ADVANCED, {})
            phone = user_input.get(CONF_PHONE, {})
            let_in = user_input.get(CONF_LET_IN, {})
            options = {
                CONF_CAMERAS: user_input.get(CONF_CAMERAS, []),
                CONF_SCENES: user_input.get(CONF_SCENES, []),
                CONF_RESTORE_TIMEOUT: int(
                    user_input.get(CONF_RESTORE_TIMEOUT, DEFAULT_RESTORE_TIMEOUT)
                ),
                CONF_PAIRING_ATTEMPTS: int(
                    advanced.get(CONF_PAIRING_ATTEMPTS, DEFAULT_PAIRING_ATTEMPTS)
                ),
                CONF_ALLOW_CLOUD: bool(
                    advanced.get(CONF_ALLOW_CLOUD, DEFAULT_ALLOW_CLOUD)
                ),
            }
            if user_input.get(CONF_DOORBELL):
                options[CONF_DOORBELL] = user_input[CONF_DOORBELL]
            errors = self._phone_options(phone, options) or await self._let_in_options(
                let_in, options
            )
            if not errors:
                self._options = options
                return await self.async_step_details()

        def suggested(key: str):
            value = current.get(key)
            return {"suggested_value": value} if value else None

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_CAMERAS, description=suggested(CONF_CAMERAS)
                ): EntitySelector(EntitySelectorConfig(domain="camera", multiple=True)),
                vol.Optional(
                    CONF_DOORBELL, description=suggested(CONF_DOORBELL)
                ): EntitySelector(
                    EntitySelectorConfig(domain=["binary_sensor", "event"])
                ),
                vol.Optional(
                    CONF_SCENES, description=suggested(CONF_SCENES)
                ): EntitySelector(EntitySelectorConfig(domain="scene", multiple=True)),
                vol.Required(
                    CONF_RESTORE_TIMEOUT,
                    default=current.get(CONF_RESTORE_TIMEOUT, DEFAULT_RESTORE_TIMEOUT),
                ): _minutes(MIN_RESTORE_TIMEOUT, MAX_RESTORE_TIMEOUT),
                vol.Required(CONF_ADVANCED): section(
                    vol.Schema(
                        {
                            vol.Required(
                                CONF_PAIRING_ATTEMPTS,
                                default=current.get(
                                    CONF_PAIRING_ATTEMPTS, DEFAULT_PAIRING_ATTEMPTS
                                ),
                            ): _minutes(MIN_PAIRING_ATTEMPTS, MAX_PAIRING_ATTEMPTS),
                            vol.Required(
                                CONF_ALLOW_CLOUD,
                                default=current.get(
                                    CONF_ALLOW_CLOUD, DEFAULT_ALLOW_CLOUD
                                ),
                            ): BooleanSelector(),
                        }
                    ),
                    {"collapsed": True},
                ),
                vol.Required(CONF_PHONE, default={}): section(
                    self._phone_schema(current), {"collapsed": True}
                ),
                vol.Required(CONF_LET_IN, default={}): section(
                    self._let_in_schema(current), {"collapsed": True}
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)

    def _phone_schema(self, current: Any) -> vol.Schema:
        services = registered_services(self.hass)
        picked = [s for s in current.get(CONF_NOTIFY_SERVICES, []) if s in services]
        return vol.Schema(
            {
                vol.Required(CONF_NOTIFY_SERVICES, default=picked): SelectSelector(
                    SelectSelectorConfig(
                        options=[SelectOptionDict(value=s, label=s) for s in services],
                        multiple=True,
                    )
                ),
                vol.Required(
                    CONF_NOTIFY_REQUEST_READY,
                    default=bool(current.get(CONF_NOTIFY_REQUEST_READY, False)),
                ): BooleanSelector(),
                vol.Required(
                    CONF_NOTIFY_INCLUDE_TITLE,
                    default=bool(current.get(CONF_NOTIFY_INCLUDE_TITLE, False)),
                ): BooleanSelector(),
            }
        )

    def _let_in_on(self) -> bool:
        runtime = get_runtime(self.hass)
        if runtime is not None and runtime.let_in is not None:
            return runtime.let_in.switched_on()
        return bool(self.config_entry.options.get(CONF_LET_IN_ENABLED, False))

    def _let_in_schema(self, current: Any) -> vol.Schema:
        script = current.get(CONF_LET_IN_SCRIPT)
        secret = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
        # What the form shows now: only switching it from off to on is a re-enable.
        self._let_in_shown = self._let_in_on()
        return vol.Schema(
            {
                vol.Required(CONF_LET_IN_ENABLED, default=self._let_in_on()): BooleanSelector(),
                vol.Optional(
                    CONF_LET_IN_SCRIPT,
                    description={"suggested_value": script} if script else None,
                ): EntitySelector(EntitySelectorConfig(domain="script")),
                vol.Optional(CONF_LET_IN_PIN): secret,
                vol.Optional(CONF_LET_IN_PIN_CONFIRM): secret,
            }
        )

    async def _let_in_options(
        self, let_in: dict[str, Any], options: dict[str, Any]
    ) -> dict[str, str]:
        """Copy the let-in section into `options`. A new PIN is stored only as a
        slow salted hash; the PIN itself is never kept."""
        current = self.config_entry.options
        enabled = bool(let_in.get(CONF_LET_IN_ENABLED, False))
        script = let_in.get(CONF_LET_IN_SCRIPT) or None
        pin = let_in.get(CONF_LET_IN_PIN) or ""
        confirm = let_in.get(CONF_LET_IN_PIN_CONFIRM) or ""
        pin_hash = current.get(CONF_LET_IN_PIN_HASH)
        if pin or confirm:
            if pin != confirm:
                return {"base": "let_in_pin_mismatch"}
            if not crypto.valid_pin(pin):
                return {"base": "let_in_pin_format"}
            pin_hash = await self.hass.async_add_executor_job(crypto.hash_pin, pin)
        del pin, confirm
        if enabled and not script:
            return {"base": "let_in_needs_script"}
        if enabled and not pin_hash:
            return {"base": "let_in_needs_pin"}
        if enabled and not options.get(CONF_DOORBELL):
            return {"base": "let_in_needs_doorbell"}
        options[CONF_LET_IN_ENABLED] = enabled
        if script:
            options[CONF_LET_IN_SCRIPT] = script
        if pin_hash:
            options[CONF_LET_IN_PIN_HASH] = pin_hash
        # Turning it on while it shows off (switched off, or turned off by a
        # lockout) is the admin's re-enable: every lockout is forgotten.
        shown = self._let_in_shown if self._let_in_shown is not None else self._let_in_on()
        self._reset_let_in = enabled and not shown
        return {}

    async def _finish(self, data: dict[str, Any]) -> ConfigFlowResult:
        runtime = get_runtime(self.hass)
        if self._reset_let_in and runtime is not None and runtime.let_in is not None:
            await runtime.let_in.async_reset()
        return self.async_create_entry(data=data)

    def _phone_options(self, phone: dict[str, Any], options: dict[str, Any]) -> dict[str, str]:
        """Copy the phone section into `options`; errors if it does not hold up."""
        services = list(dict.fromkeys(phone.get(CONF_NOTIFY_SERVICES) or []))
        if any(s not in registered_services(self.hass) for s in services):
            return {"base": "notify_unknown"}
        ready = bool(phone.get(CONF_NOTIFY_REQUEST_READY, False))
        if ready and not services:
            return {"base": "notify_needs_service"}
        options[CONF_NOTIFY_SERVICES] = services
        options[CONF_NOTIFY_REQUEST_READY] = ready
        options[CONF_NOTIFY_INCLUDE_TITLE] = bool(
            phone.get(CONF_NOTIFY_INCLUDE_TITLE, False)
        )
        return {}

    def _choice(self, entity_ids: list[str]) -> SelectSelector:
        options = []
        for entity_id in entity_ids:
            state = self.hass.states.get(entity_id)
            options.append(
                SelectOptionDict(
                    value=entity_id, label=state.name if state else entity_id
                )
            )
        return SelectSelector(SelectSelectorConfig(options=options, multiple=False))

    async def async_step_details(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        chosen = self._options
        cameras = chosen[CONF_CAMERAS]
        scenes = chosen[CONF_SCENES]
        can_camera = bool(cameras) and CONF_DOORBELL in chosen
        if not can_camera and not scenes:
            return await self._finish(chosen)

        current = self.config_entry.options
        errors: dict[str, str] = {}
        if user_input is not None:
            movie = user_input.get(CONF_MOVIE_SCENE)
            if movie and movie == user_input.get(CONF_PAUSED_SCENE):
                errors["base"] = "same_scene"
            else:
                for key in (CONF_DOORBELL_CAMERA, CONF_MOVIE_SCENE, CONF_PAUSED_SCENE):
                    if user_input.get(key):
                        chosen[key] = user_input[key]
                return await self._finish(chosen)

        def suggested(key: str, pool: list[str]):
            value = (user_input or {}).get(key) or current.get(key)
            if value not in pool and user_input is None:
                value = None
                guess = _SCENE_NAME_GUESS.get(key)
                if guess:
                    value = next(
                        (
                            e
                            for e in pool
                            if (st := self.hass.states.get(e))
                            and st.name.strip().lower() == guess
                        ),
                        None,
                    )
            return {"suggested_value": value} if value in pool else None

        fields: dict[Any, Any] = {}
        if can_camera:
            fields[
                vol.Optional(
                    CONF_DOORBELL_CAMERA,
                    description=suggested(CONF_DOORBELL_CAMERA, cameras),
                )
            ] = self._choice(cameras)
        if scenes:
            for key in (CONF_MOVIE_SCENE, CONF_PAUSED_SCENE):
                fields[
                    vol.Optional(key, description=suggested(key, scenes))
                ] = self._choice(scenes)
        return self.async_show_form(
            step_id="details", data_schema=vol.Schema(fields), errors=errors
        )


class PairTvFlow(ConfigSubentryFlow):
    """Pair a TV: the code, then the four words."""

    _window_id: str | None = None

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Set this TV's room and the other rooms it may control."""
        subentry = self._get_reconfigure_subentry()
        if user_input is not None:
            room = user_input[CONF_ROOM_ID]
            rooms = [a for a in user_input.get(CONF_ROOMS, []) if a != room]
            return self.async_update_and_abort(
                self._get_entry(),
                subentry,
                data_updates={
                    CONF_ROOM_ID: room,
                    CONF_ROOMS: [room, *rooms],
                    CONF_LET_IN_ALLOWED: bool(user_input.get(CONF_LET_IN_ALLOWED, False)),
                },
            )
        room = subentry.data.get(CONF_ROOM_ID)
        others = [a for a in subentry.data.get(CONF_ROOMS, []) if a != room]
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_ROOM_ID, description={"suggested_value": room}
                ): AreaSelector(),
                vol.Optional(
                    CONF_ROOMS, description={"suggested_value": others}
                ): AreaSelector(AreaSelectorConfig(multiple=True)),
                vol.Required(
                    CONF_LET_IN_ALLOWED,
                    default=subentry.data.get(CONF_LET_IN_ALLOWED) is True,
                ): BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=schema,
            description_placeholders={"tv_name": _plain(subentry.title)},
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        runtime = get_runtime(self.hass)
        if runtime is None:
            return self.async_abort(reason="not_loaded")
        window = runtime.pairing.window
        if window is None or window.matched is not None:
            return self.async_abort(reason="no_pending")
        if self._window_id is None:
            self._window_id = window.window_id
        elif self._window_id != window.window_id:
            return self.async_abort(reason="no_pending")

        errors: dict[str, str] = {}
        if user_input is not None:
            max_attempts = self._get_entry().options.get(
                CONF_PAIRING_ATTEMPTS, DEFAULT_PAIRING_ATTEMPTS
            )
            result, _ = runtime.pairing.check_code(
                self._window_id, str(user_input.get("code", "")), max_attempts
            )
            if result is CodeResult.MATCH:
                return await self.async_step_confirm()
            if result is CodeResult.AMBIGUOUS:
                persistent_notification.async_create(
                    self.hass,
                    "Two TVs answered the same Perch pairing code, so the "
                    "pairing was stopped. Someone else on your network may be "
                    "trying to pair. Start again on the TV you want to pair.",
                    title="Perch pairing stopped",
                    notification_id=NOTIFICATION_ID,
                )
                return self.async_abort(reason="aborted")
            if result is CodeResult.EXHAUSTED:
                return self.async_abort(reason="too_many_attempts")
            if result is CodeResult.GONE:
                return self.async_abort(reason="no_pending")
            errors["code"] = "invalid_code"

        names = ", ".join(_plain(c.tv_name) for c in window.contenders)
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("code"): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.TEXT)
                    )
                }
            ),
            description_placeholders={"tv_name": names},
            errors=errors,
        )

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        runtime = get_runtime(self.hass)
        contender = (
            None
            if runtime is None or self._window_id is None
            else runtime.pairing.matched(self._window_id)
        )
        if contender is None:
            return self.async_abort(reason="no_pending")
        words = crypto.fingerprint_words(
            contender.public_key, await async_get_words(self.hass)
        )
        return self.async_show_menu(
            step_id="confirm",
            menu_options=["match", "mismatch"],
            description_placeholders={
                "tv_name": _plain(contender.tv_name),
                "words": " ".join(words),
            },
        )

    async def async_step_mismatch(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        runtime = get_runtime(self.hass)
        if runtime is not None and self._window_id is not None:
            runtime.pairing.reject(self._window_id)
        return self.async_abort(reason="mismatch")

    async def async_step_match(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        runtime = get_runtime(self.hass)
        contender = (
            None
            if runtime is None or self._window_id is None
            else runtime.pairing.matched(self._window_id)
        )
        if contender is None:
            return self.async_abort(reason="no_pending")
        credential = crypto.new_credential()
        envelope = crypto.seal_credential(
            credential, contender.public_key, contender.nonce
        )
        tv_id = f"tv_{secrets.token_hex(8)}"
        credential_hash = crypto.credential_hash(credential)
        del credential
        runtime.pairing.approve(contender, tv_id, envelope)
        return self.async_create_entry(
            title=contender.tv_name,
            data={
                CONF_TV_ID: tv_id,
                CONF_PUBLIC_KEY: base64.b64encode(contender.public_key).decode(),
                CONF_CREDENTIAL_HASH: credential_hash,
            },
            unique_id=tv_id,
        )
