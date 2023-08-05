"""Errors for the UniFi Network integration."""
from homeassistant.exceptions import HomeAssistantError


class UnifiException(HomeAssistantError):
    """Base class for UniFi Network exceptions."""


class CannotConnect(UnifiException):
    """Error to indicate we cannot connect."""


class InvalidAuth(UnifiException):
    """Error to indicate there is invalid auth."""
