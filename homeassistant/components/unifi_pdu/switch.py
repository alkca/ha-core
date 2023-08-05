"""Switch platform for UniFi Network integration.

Support to control outlets on UniFi SmartPower devices.
"""
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
import logging
import random
from typing import Any, Generic

import aiounifi
from aiounifi.interfaces.api_handlers import ItemEvent
from aiounifi.interfaces.outlets import Outlets
from aiounifi.models.api import ApiItemT
from aiounifi.models.device import DeviceSetOutletRelayRequest
from aiounifi.models.event import Event
from aiounifi.models.outlet import Outlet

from homeassistant.components.switch import (
    SwitchDeviceClass,
    SwitchEntity,
    SwitchEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN as UNIFI_DOMAIN
from .controller import UniFiController
from .entity import (
    HandlerT,
    SubscriptionT,
    UnifiEntity,
    UnifiEntityDescription,
    async_device_available_fn,
    async_device_device_info_fn,
)

_LOGGER = logging.getLogger(__name__)


async def async_outlet_control_fn(
    api: aiounifi.Controller, obj_id: str, target: bool
) -> None:
    """Control outlet relay."""
    mac, _, index = obj_id.partition("_")
    device = api.devices[mac]
    await api.request(DeviceSetOutletRelayRequest.create(device, int(index), target))


# async def async_is_on_fn(controller: UniFiController, outlet: Outlet) -> bool:
#     """Check if outlet is on."""
#     return outlet.relay_state


@dataclass
class UnifiSwitchEntityDescriptionMixin(Generic[HandlerT, ApiItemT]):
    """Validate and load entities from different UniFi handlers."""

    control_fn: Callable[[aiounifi.Controller, str, bool], Coroutine[Any, Any, None]]
    is_on_fn: Callable[[UniFiController, ApiItemT], bool]


@dataclass
class UnifiSwitchEntityDescription(
    SwitchEntityDescription,
    UnifiEntityDescription[HandlerT, ApiItemT],
    UnifiSwitchEntityDescriptionMixin[HandlerT, ApiItemT],
):
    """Class describing UniFi switch entity."""

    custom_subscribe: Callable[[aiounifi.Controller], SubscriptionT] | None = None
    only_event_for_state_change: bool = False


ENTITY_DESCRIPTIONS: tuple[UnifiSwitchEntityDescription, ...] = (
    UnifiSwitchEntityDescription[Outlets, Outlet](
        key="Outlet control",
        device_class=SwitchDeviceClass.OUTLET,
        has_entity_name=True,
        allowed_fn=lambda controller, obj_id: True,
        api_handler_fn=lambda api: api.outlets,
        available_fn=async_device_available_fn,
        control_fn=async_outlet_control_fn,
        device_info_fn=async_device_device_info_fn,
        event_is_on=None,
        event_to_subscribe=None,
        # is_on_fn=async_is_on_fn,
        is_on_fn=lambda controller, outlet: outlet.relay_state,
        # is_on_fn=lambda controller, outlet: bool(random.getrandbits(1)),
        name_fn=lambda outlet: outlet.name,
        object_fn=lambda api, obj_id: api.outlets[obj_id],
        supported_fn=lambda c, obj_id: True,
        # AK - Look for alternative to see if this supports outlet control
        # supported_fn=lambda c, obj_id: c.api.outlets[obj_id].has_relay,
        unique_id_fn=lambda controller, obj_id: f"{obj_id.split('_', 1)[0]}-outlet-{obj_id.split('_', 1)[1]}",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up switches for UniFi Network integration."""
    controller: UniFiController = hass.data[UNIFI_DOMAIN][config_entry.entry_id]

    # AK - Check to see whether admin is needed for Outlet control
    # if controller.site_role != "admin":
    #     return

    controller.register_platform_add_entities(
        UnifiSwitchEntity, ENTITY_DESCRIPTIONS, async_add_entities
    )


class UnifiSwitchEntity(UnifiEntity[HandlerT, ApiItemT], SwitchEntity):
    """Base representation of a UniFi switch."""

    entity_description: UnifiSwitchEntityDescription[HandlerT, ApiItemT]
    only_event_for_state_change = False

    # @property
    # def is_on(self) -> bool:
    #     """If the switch is currently on or off."""
    #     return self._is_on

    @callback
    def async_initiate_state(self) -> None:
        """Initiate entity state."""
        self.async_update_state(ItemEvent.ADDED, self._obj_id)
        self.only_event_for_state_change = (
            self.entity_description.only_event_for_state_change
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on switch."""
        await self.entity_description.control_fn(
            self.controller.api, self._obj_id, True
        )
        self._attr_is_on = True

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off switch."""
        await self.entity_description.control_fn(
            self.controller.api, self._obj_id, False
        )
        self._attr_is_on = False

    @callback
    def async_update_state(self, event: ItemEvent, obj_id: str) -> None:
        """Update entity state.

        Update attr_is_on.
        """
        if self.only_event_for_state_change:
            return

        description = self.entity_description
        obj = description.object_fn(self.controller.api, self._obj_id)
        if (is_on := description.is_on_fn(self.controller, obj)) != self.is_on:
            self._attr_is_on = is_on

    @callback
    def async_event_callback(self, event: Event) -> None:
        """Event subscription callback."""
        if event.mac != self._obj_id:
            return

        description = self.entity_description
        assert isinstance(description.event_to_subscribe, tuple)
        assert isinstance(description.event_is_on, tuple)

        if event.key in description.event_to_subscribe:
            self._attr_is_on = event.key in description.event_is_on
        self._attr_available = description.available_fn(self.controller, self._obj_id)
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Register callbacks."""
        await super().async_added_to_hass()

        if self.entity_description.custom_subscribe is not None:
            self.async_on_remove(
                self.entity_description.custom_subscribe(self.controller.api)(
                    self.async_signalling_callback, ItemEvent.CHANGED
                ),
            )

    async def async_update(self) -> None:
        """Update switch."""
        self._attr_is_on = bool(random.getrandbits(1))
