"""Support for Rinnai water heater."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.water_heater import (
    WaterHeaterEntity,
    WaterHeaterEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import RinnaiCoordinator
from .entity import RinnaiEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Rinnai water heater based on a config entry."""
    coordinator: RinnaiCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = []
    for device_id in coordinator.data["devices"]:
        device = coordinator.get_device(device_id)
        if not device or not device.config:
            continue
            
        if wh_configs := device.config.entities.get("water_heater"):
            for config in wh_configs:
                entities.append(RinnaiWaterHeaterEntity(coordinator, device_id, config))

    _LOGGER.debug("Setting up %d water_heater entities", len(entities))
    async_add_entities(entities)


class RinnaiWaterHeaterEntity(RinnaiEntity, WaterHeaterEntity):
    """Representation of a Rinnai water heater entity."""

    def __init__(self, coordinator: RinnaiCoordinator, device_id: str, config: dict[str, Any]) -> None:
        """Initialize the water heater entity."""
        super().__init__(coordinator, device_id, config)

        self._attr_supported_features = WaterHeaterEntityFeature.TARGET_TEMPERATURE
        self._attr_temperature_unit = UnitOfTemperature.CELSIUS
        
        # Mandatory configuration - no defaults
        self._attr_min_temp = config["min_temp"]
        self._attr_max_temp = config["max_temp"]
        self._attr_target_temperature_step = config["step"]
        
        self._state_attribute = config["state_attribute"]
        self._relative_temperature_control = config.get("relative_temperature_control")
        self._command_topic = config.get("command_topic")
        if not self._relative_temperature_control and not self._command_topic:
            raise KeyError("command_topic")
        # "hex2" → 2-char (G56 style: 40°C → "28")
        # "hex4" → 4-char (E-series style: 40°C → "2800")
        self._temp_format = config.get("temp_format", "hex2")
        
        # Operation mode name from config, default to "Hot Water" if not specified (display only)
        self._operation_mode = config.get("operation_mode", "Hot Water")
        self._changing_operation_template = config.get("changing_operation_template")
        self._attr_operation_list = [self._operation_mode]
        self._attr_current_operation = self._operation_mode

        self._update_attributes()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._update_attributes()
        self.async_write_ha_state()

    def _update_attributes(self) -> None:
        """Update entity attributes based on coordinator data."""
        device = self._device
        if not device:
            self._attr_available = False
            return
            
        self._attr_available = device.online

        # Update temperature
        try:
            self._attr_target_temperature = self.get_state_value(self._state_attribute)
        except (ValueError, TypeError) as err:
            _LOGGER.warning(
                "Device %s: failed to parse water heater temperature (attr=%s): %s",
                self._device_id, self._state_attribute, err,
            )
            self._attr_target_temperature = 0
        self._attr_current_temperature = self._attr_target_temperature

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperature."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return

        temperature = int(temperature)
        if temperature < self.min_temp or temperature > self.max_temp:
            _LOGGER.warning(
                "Device %s: temperature %s°C out of range [%s, %s]",
                self._device_id, temperature, self.min_temp, self.max_temp,
            )
            return

        if self._relative_temperature_control:
            await self._async_set_relative_temperature(temperature)
            return

        hex_temperature = hex(temperature)[2:].upper().zfill(2)
        if self._temp_format == "hex4":
            hex_temperature = hex_temperature + "00"
        command = {self._command_topic: hex_temperature}

        success = await self.coordinator.async_send_command(self._device_id, command)

        if success:
            self._attr_target_temperature = float(temperature)
            self.async_write_ha_state()

    def _current_temperature(self) -> int | None:
        """Return current target temperature from device state."""
        try:
            current = self.get_state_value(self._state_attribute)
            return int(current) if current is not None else None
        except (ValueError, TypeError):
            return None

    def _allowed_temperatures_for_current_mode(self) -> list[int] | None:
        """Return configured allowed temperatures for the current raw mode."""
        control = self._relative_temperature_control or {}
        allowed_by_mode = control.get("allowed_temps_by_mode")
        if not allowed_by_mode:
            return None

        mode_attribute = control.get("mode_attribute", "operation_mode")
        raw_mode = self.get_state_value(mode_attribute)
        if raw_mode is None:
            _LOGGER.warning(
                "Device %s: cannot validate temperature without %s",
                self._device_id,
                mode_attribute,
            )
            return []

        allowed = allowed_by_mode.get(str(raw_mode).upper())
        if allowed is None:
            _LOGGER.warning(
                "Device %s: unknown operation mode %s for temperature validation",
                self._device_id,
                raw_mode,
            )
            return []

        return [int(temp) for temp in allowed]

    async def _async_set_relative_temperature(self, temperature: int) -> None:
        """Set target temperature via configured relative up/down commands."""
        control = self._relative_temperature_control or {}
        command_key = control.get("command_key")
        increase_value = control.get("increase")
        decrease_value = control.get("decrease")
        if not command_key or increase_value is None or decrease_value is None:
            _LOGGER.warning(
                "Device %s: invalid relative temperature control config",
                self._device_id,
            )
            return

        allowed_temps = self._allowed_temperatures_for_current_mode()
        if allowed_temps is not None and temperature not in allowed_temps:
            _LOGGER.warning(
                "Device %s: temperature %sC is not allowed for current mode",
                self._device_id,
                temperature,
            )
            return

        current = self._current_temperature()
        if current is None:
            _LOGGER.warning(
                "Device %s: cannot set relative temperature without current state",
                self._device_id,
            )
            return

        if current == temperature:
            return

        max_steps = abs(temperature - current)
        if allowed_temps and current in allowed_temps and temperature in allowed_temps:
            max_steps = abs(
                allowed_temps.index(temperature) - allowed_temps.index(current)
            )
        max_steps = max(max_steps, 1)
        refresh_retries = self._relative_refresh_retries(control)

        self._set_changing_operation(temperature)
        try:
            for _ in range(max_steps):
                current = self._current_temperature()
                if current is None:
                    return
                if current == temperature:
                    self._attr_target_temperature = float(temperature)
                    self.async_write_ha_state()
                    return

                command_value = increase_value if temperature > current else decrease_value
                success = await self.coordinator.async_send_command(
                    self._device_id, {command_key: command_value}
                )
                if not success:
                    return

                previous = current
                for _ in range(refresh_retries):
                    await self._async_refresh_after_relative_temperature_step()
                    current = self._current_temperature()
                    if current == temperature or current != previous:
                        break
                if current == temperature:
                    self._attr_target_temperature = float(temperature)
                    self.async_write_ha_state()
                    return
                if current == previous:
                    _LOGGER.warning(
                        "Device %s: temperature did not change after relative command; stopping",
                        self._device_id,
                    )
                    return
        finally:
            self._restore_operation()

    def _set_changing_operation(self, temperature: int) -> None:
        """Show a configured in-progress operation while relative control runs."""
        if not self._changing_operation_template:
            return

        operation = self._changing_operation_template.format(temperature=temperature)
        self._attr_operation_list = [self._operation_mode, operation]
        self._attr_current_operation = operation
        self.async_write_ha_state()

    def _restore_operation(self) -> None:
        """Restore the configured normal operation label."""
        if self._attr_current_operation == self._operation_mode:
            return

        self._attr_operation_list = [self._operation_mode]
        self._attr_current_operation = self._operation_mode
        self.async_write_ha_state()

    @staticmethod
    def _relative_refresh_retries(control: dict[str, Any]) -> int:
        """Return how many times to poll state after a relative step."""
        try:
            return max(1, int(control.get("refresh_retries", 1)))
        except (ValueError, TypeError):
            return 1

    async def _async_refresh_after_relative_temperature_step(self) -> None:
        """Refresh state after a relative temperature command when possible."""
        control = self._relative_temperature_control or {}
        try:
            delay = float(control.get("step_delay_seconds", 0))
        except (ValueError, TypeError):
            delay = 0
        if delay > 0:
            await asyncio.sleep(delay)

        refresh_device_state = getattr(self.coordinator, "async_refresh_device_state", None)
        if refresh_device_state:
            if await refresh_device_state(self._device_id):
                self._update_attributes()
                return

        refresh = getattr(self.coordinator, "async_request_refresh", None)
        if refresh:
            await refresh()
        self._update_attributes()
