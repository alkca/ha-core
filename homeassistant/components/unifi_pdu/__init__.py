"""The Unifi Smart PDU Pro integration."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from .const import (
    DOMAIN,
    PLATFORMS,
)
from .controller import UniFiController, get_unifi_controller
from .errors import CannotConnect, InvalidAuth


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Unifi Smart PDU Pro from a config entry."""

    hass.data.setdefault(DOMAIN, {})

    # 1. Create API instance
    # 2. Validate the API connection (and authentication)
    # 3. Store an API object for your platforms to access
    # hass.data[DOMAIN][entry.entry_id] = MyApi(...)

    # Initialize connection to Unifi Controller
    try:
        api = await get_unifi_controller(
            hass, entry.data["host"], entry.data["username"], entry.data["password"]
        )
        controller = UniFiController(
            hass,
            entry,
            api,
        )
        await controller.initialize()

    except CannotConnect as err:
        raise ConfigEntryNotReady from err

    except InvalidAuth as err:
        raise ConfigEntryAuthFailed from err

    hass.data[DOMAIN][entry.entry_id] = controller
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    api.start_websocket()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, controller.shutdown)
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok
