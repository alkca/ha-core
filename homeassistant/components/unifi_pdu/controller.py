"""UniFi Network abstraction."""
import asyncio
from datetime import datetime, timedelta
import logging
import ssl

from aiohttp import CookieJar
import aiounifi
from aiounifi.controller import Controller
from aiounifi.interfaces.api_handlers import ItemEvent
import async_timeout

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_USERNAME,
)
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
import homeassistant.util.dt as dt_util

from .const import (
    PLATFORMS,
)
from .entity import UnifiEntity, UnifiEntityDescription
from .errors import CannotConnect, InvalidAuth

RETRY_TIMER = 15
CHECK_HEARTBEAT_INTERVAL = timedelta(seconds=1)

_LOGGER = logging.getLogger(__name__)


class UniFiController:
    """Manages a single UniFi Network instance."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        api: Controller,
    ) -> None:
        """Set up UniFi controller."""
        self.hass = hass
        self.config_entry = config_entry
        self.api = api

        self.host = config_entry.data[CONF_HOST]
        self.username = config_entry.data[CONF_USERNAME]
        self.password = config_entry.data[CONF_PASSWORD]
        self.site = "default"
        self.controller = None

        self.available = True

        self.site_id: str = ""
        self._site_name: str | None = None
        self._site_role: str | None = None

        self._cancel_heartbeat_check: CALLBACK_TYPE | None = None
        self._heartbeat_time: dict[str, datetime] = {}

        self.entities: dict[str, str] = {}
        self.known_objects: set[tuple[str, str]] = set()

    @property
    def site_role(self) -> str | None:
        """Return the site user role of this controller."""
        return self._site_role

    @callback
    def register_platform_add_entities(
        self,
        unifi_platform_entity: type[UnifiEntity],
        descriptions: tuple[UnifiEntityDescription, ...],
        async_add_entities: AddEntitiesCallback,
    ) -> None:
        """Subscribe to UniFi API handlers and create entities."""

        @callback
        def async_load_entities(description: UnifiEntityDescription) -> None:
            """Load and subscribe to UniFi endpoints."""
            api_handler = description.api_handler_fn(self.api)

            @callback
            def async_add_unifi_entity(obj_ids: list[str]) -> None:
                """Add UniFi entity."""
                async_add_entities(
                    [
                        unifi_platform_entity(obj_id, self, description)
                        for obj_id in obj_ids
                        if (description.key, obj_id) not in self.known_objects
                        if description.allowed_fn(self, obj_id)
                        if description.supported_fn(self, obj_id)
                    ]
                )

            async_add_unifi_entity(list(api_handler))

            @callback
            def async_create_entity(event: ItemEvent, obj_id: str) -> None:
                """Create new UniFi entity on event."""
                async_add_unifi_entity([obj_id])

            api_handler.subscribe(async_create_entity, ItemEvent.ADDED)

            @callback
            def async_options_updated() -> None:
                """Load new entities based on changed options."""
                async_add_unifi_entity(list(api_handler))

            self.config_entry.async_on_unload(
                async_dispatcher_connect(
                    self.hass, self.signal_options_update, async_options_updated
                )
            )

        for description in descriptions:
            async_load_entities(description)

    @property
    def signal_reachable(self) -> str:
        """Integration specific event to signal a change in connection status."""
        return f"unifi-reachable-{self.config_entry.entry_id}"

    @property
    def signal_options_update(self) -> str:
        """Event specific per UniFi entry to signal new options."""
        return f"unifi-options-{self.config_entry.entry_id}"

    @property
    def signal_heartbeat_missed(self) -> str:
        """Event specific per UniFi device tracker to signal new heartbeat missed."""
        return "unifi-heartbeat-missed"

    async def initialize(self) -> None:
        """Set up a UniFi Network instance."""
        await self.api.initialize()

        sites = await self.api.sites()
        for site in sites.values():
            if self.site == site["name"]:
                self.site_id = site["_id"]
                self._site_name = site["desc"]
                break

        description = await self.api.site_description()
        self._site_role = description[0]["site_role"]

        self._cancel_heartbeat_check = async_track_time_interval(
            self.hass, self._async_check_for_stale, CHECK_HEARTBEAT_INTERVAL
        )

    @callback
    def _async_check_for_stale(self, *_: datetime) -> None:
        """Check for any devices scheduled to be marked disconnected."""
        now = dt_util.utcnow()

        unique_ids_to_remove = []
        for unique_id, heartbeat_expire_time in self._heartbeat_time.items():
            if now > heartbeat_expire_time:
                async_dispatcher_send(
                    self.hass, f"{self.signal_heartbeat_missed}_{unique_id}"
                )
                unique_ids_to_remove.append(unique_id)

        for unique_id in unique_ids_to_remove:
            del self._heartbeat_time[unique_id]

    @callback
    def reconnect(self, log: bool = False) -> None:
        """Prepare to reconnect UniFi session."""
        if log:
            _LOGGER.info("Will try to reconnect to UniFi Network")
        self.hass.loop.create_task(self.async_reconnect())

    async def async_reconnect(self) -> None:
        """Try to reconnect UniFi Network session."""
        try:
            async with async_timeout.timeout(5):
                await self.api.login()
                self.api.start_websocket()

        except (
            asyncio.TimeoutError,
            aiounifi.BadGateway,
            aiounifi.ServiceUnavailable,
            aiounifi.AiounifiException,
        ):
            self.hass.loop.call_later(RETRY_TIMER, self.reconnect)

    @callback
    def shutdown(self, event: Event) -> None:
        """Wrap the call to unifi.close.

        Used as an argument to EventBus.async_listen_once.
        """
        self.api.stop_websocket()

    async def async_reset(self) -> bool:
        """Reset this controller to default state.

        Will cancel any scheduled setup retry and will unload
        the config entry.
        """
        self.api.stop_websocket()

        unload_ok = await self.hass.config_entries.async_unload_platforms(
            self.config_entry, PLATFORMS
        )

        if not unload_ok:
            return False

        if self._cancel_heartbeat_check:
            self._cancel_heartbeat_check()
            self._cancel_heartbeat_check = None

        return True


async def get_unifi_controller(
    hass: HomeAssistant, host: str, username: str, password: str
) -> Controller:
    """Create a controller object and verify authentication."""

    # Create the HTTP session and validate SSL
    # AK - Right now SSL is hardcoded to False, but it should be configurable
    verify_ssl = False
    ssl_context: ssl.SSLContext | bool = False

    if verify_ssl:
        session = aiohttp_client.async_get_clientsession(hass)
        if isinstance(verify_ssl, str):
            ssl_context = ssl.create_default_context(cafile=verify_ssl)
    else:
        session = aiohttp_client.async_create_clientsession(
            hass, verify_ssl=False, cookie_jar=CookieJar(unsafe=True)
        )

    controller = Controller(
        host=host,
        username=username,
        password=password,
        port=443,
        site="default",
        websession=session,
        ssl_context=ssl_context,
    )

    try:
        async with async_timeout.timeout(10):
            await controller.check_unifi_os()
            await controller.login()

    except aiounifi.Unauthorized as err:
        _LOGGER.warning(
            "Connected to UniFi Network at %s but not registered: %s", host, err
        )
        raise InvalidAuth from err

    except (
        asyncio.TimeoutError,
        aiounifi.BadGateway,
        aiounifi.ServiceUnavailable,
        aiounifi.RequestError,
        aiounifi.ResponseError,
    ) as err:
        _LOGGER.error("Error connecting to the UniFi Network at %s: %s", host, err)
        raise CannotConnect from err

    except aiounifi.LoginRequired as err:
        _LOGGER.warning(
            "Connected to UniFi Network at %s but login required: %s", host, err
        )
        raise InvalidAuth from err

    except aiounifi.AiounifiException as err:
        _LOGGER.exception("Unknown UniFi Network communication error occurred: %s", err)
        raise InvalidAuth from err

    return controller
