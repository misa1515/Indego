"""Bosch Indego Mower integration."""
import asyncio
import logging
import time
from datetime import datetime, timedelta

import homeassistant.util.dt
import voluptuous as vol
from homeassistant.core import HomeAssistant, CoreState
from homeassistant.exceptions import HomeAssistantError
from homeassistant.config_entries import ConfigEntry
from homeassistant.components.binary_sensor import (
    DEVICE_CLASS_CONNECTIVITY,
    DEVICE_CLASS_PROBLEM,
)
from homeassistant.const import (
    CONF_DEVICE_CLASS,
    CONF_ICON,
    CONF_ID,
    CONF_NAME,
    CONF_TYPE,
    CONF_UNIT_OF_MEASUREMENT,
    DEVICE_CLASS_BATTERY,
    DEVICE_CLASS_TIMESTAMP,
    EVENT_HOMEASSISTANT_STARTED,
    EVENT_HOMEASSISTANT_STOP,
    STATE_ON,
    STATE_UNKNOWN,
    TEMP_CELSIUS,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_call_later
from homeassistant.util.dt import utcnow
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.config_entry_oauth2_flow import OAuth2Session, async_get_config_entry_implementation
from homeassistant.helpers.event import async_track_point_in_time
from pyIndego import IndegoAsyncClient

from .binary_sensor import IndegoBinarySensor
from .const import (
    STATUS_UPDATE_FAILURE_DELAY_TIME,
    BINARY_SENSOR_TYPE,
    CONF_MOWER_SERIAL,
    CONF_MOWER_NAME,
    CONF_SERVICES_REGISTERED,
    CONF_ATTR,
    CONF_SEND_COMMAND,
    CONF_SMARTMOWING,
    DEFAULT_NAME_COMMANDS,
    DOMAIN,
    ENTITY_ALERT,
    ENTITY_BATTERY,
    ENTITY_LAST_COMPLETED,
    ENTITY_LAWN_MOWED,
    ENTITY_MOWER_STATE,
    ENTITY_MOWER_STATE_DETAIL,
    ENTITY_MOWING_MODE,
    ENTITY_NEXT_MOW,
    ENTITY_ONLINE,
    ENTITY_RUNTIME,
    ENTITY_UPDATE_AVAILABLE,
    INDEGO_PLATFORMS,
    SENSOR_TYPE,
    SERVICE_NAME_COMMAND,
    SERVICE_NAME_SMARTMOW,
)
from .sensor import IndegoSensor

_LOGGER = logging.getLogger(__name__)

SERVICE_SCHEMA_COMMAND = vol.Schema({
    vol.Optional(CONF_MOWER_SERIAL): cv.string,
    vol.Required(CONF_SEND_COMMAND): cv.string
})

SERVICE_SCHEMA_SMARTMOWING = vol.Schema({
    vol.Optional(CONF_MOWER_SERIAL): cv.string,
    vol.Required(CONF_SMARTMOWING): cv.string
})


def FUNC_ICON_MOWER_ALERT(state):
    if state:
        if int(state) > 0 or state == STATE_ON:
            return "mdi:alert-outline"
    return "mdi:check-circle-outline"


ENTITY_DEFINITIONS = {
    ENTITY_ONLINE: {
        CONF_TYPE: BINARY_SENSOR_TYPE,
        CONF_NAME: "online",
        CONF_ICON: "mdi:cloud-check",
        CONF_DEVICE_CLASS: DEVICE_CLASS_CONNECTIVITY,
        CONF_ATTR: [],
    },
    ENTITY_UPDATE_AVAILABLE: {
        CONF_TYPE: BINARY_SENSOR_TYPE,
        CONF_NAME: "update available",
        CONF_ICON: "mdi:download-outline",
        CONF_DEVICE_CLASS: None,
        CONF_ATTR: [],
    },
    ENTITY_ALERT: {
        CONF_TYPE: BINARY_SENSOR_TYPE,
        CONF_NAME: "alert",
        CONF_ICON: FUNC_ICON_MOWER_ALERT,
        CONF_DEVICE_CLASS: DEVICE_CLASS_PROBLEM,
        CONF_ATTR: ["alerts_count"],
    },
    ENTITY_MOWER_STATE: {
        CONF_TYPE: SENSOR_TYPE,
        CONF_NAME: "mower state",
        CONF_ICON: "mdi:robot-mower-outline",
        CONF_DEVICE_CLASS: None,
        CONF_UNIT_OF_MEASUREMENT: None,
        CONF_ATTR: ["last_updated"],
    },
    ENTITY_MOWER_STATE_DETAIL: {
        CONF_TYPE: SENSOR_TYPE,
        CONF_NAME: "mower state detail",
        CONF_ICON: "mdi:robot-mower-outline",
        CONF_DEVICE_CLASS: None,
        CONF_UNIT_OF_MEASUREMENT: None,
        CONF_ATTR: [
            "last_updated",
            "state_number",
            "state_description",
        ],
    },
    ENTITY_BATTERY: {
        CONF_TYPE: SENSOR_TYPE,
        CONF_NAME: "battery %",
        CONF_ICON: "battery",
        CONF_DEVICE_CLASS: DEVICE_CLASS_BATTERY,
        CONF_UNIT_OF_MEASUREMENT: "%",
        CONF_ATTR: [
            "last_updated",
            "voltage_V",
            "discharge_Ah",
            "cycles",
            f"battery_temp_{TEMP_CELSIUS}",
            f"ambient_temp_{TEMP_CELSIUS}",
        ],
    },
    ENTITY_LAWN_MOWED: {
        CONF_TYPE: SENSOR_TYPE,
        CONF_NAME: "lawn mowed",
        CONF_ICON: "mdi:grass",
        CONF_DEVICE_CLASS: None,
        CONF_UNIT_OF_MEASUREMENT: "%",
        CONF_ATTR: [
            "last_updated",
            "last_completed_mow",
            "next_mow",
            "last_session_operation_min",
            "last_session_cut_min",
            "last_session_charge_min",
        ],
    },
    ENTITY_LAST_COMPLETED: {
        CONF_TYPE: SENSOR_TYPE,
        CONF_NAME: "last completed",
        CONF_ICON: "mdi:calendar-check",
        CONF_DEVICE_CLASS: DEVICE_CLASS_TIMESTAMP,
        CONF_UNIT_OF_MEASUREMENT: "ISO8601",
        CONF_ATTR: [],
    },
    ENTITY_NEXT_MOW: {
        CONF_TYPE: SENSOR_TYPE,
        CONF_NAME: "next mow",
        CONF_ICON: "mdi:calendar-clock",
        CONF_DEVICE_CLASS: DEVICE_CLASS_TIMESTAMP,
        CONF_UNIT_OF_MEASUREMENT: "ISO8601",
        CONF_ATTR: [],
    },
    ENTITY_MOWING_MODE: {
        CONF_TYPE: SENSOR_TYPE,
        CONF_NAME: "mowing mode",
        CONF_ICON: "mdi:alpha-m-circle-outline",
        CONF_DEVICE_CLASS: None,
        CONF_UNIT_OF_MEASUREMENT: None,
        CONF_ATTR: [],
    },
    ENTITY_RUNTIME: {
        CONF_TYPE: SENSOR_TYPE,
        CONF_NAME: "mowtime total",
        CONF_ICON: "mdi:information-outline",
        CONF_DEVICE_CLASS: None,
        CONF_UNIT_OF_MEASUREMENT: "h",
        CONF_ATTR: [
            "total_mowing_time_h",
            "total_charging_time_h",
            "total_operation_time_h",
        ],
    },
}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Load a config entry."""
    hass.data.setdefault(DOMAIN, {})

    entry_implementation = await async_get_config_entry_implementation(hass, entry)
    oauth_session = OAuth2Session(hass, entry, entry_implementation)
    indego_hub = hass.data[DOMAIN][entry.entry_id] = IndegoHub(
        entry.data[CONF_MOWER_NAME],
        oauth_session,
        entry.data[CONF_MOWER_SERIAL],
        hass,
    )

    async def load_platforms():
        _LOGGER.debug("Loading platforms")
        await asyncio.gather(
            *(
                hass.config_entries.async_forward_entry_setup(entry, platform)
                for platform in INDEGO_PLATFORMS
            )
        )

    try:
        await indego_hub.update_generic_data_and_load_platforms(load_platforms)
    except AttributeError as e:
        _LOGGER.warning("Login unsuccessful: %s", e)
        return False

    def find_instance_for_mower_service_call(call):
        mower_serial = call.data.get(CONF_MOWER_SERIAL, None)
        if mower_serial is None:
            # Return the first instance when params is missing for backwards compatibility.
            return hass.data[DOMAIN][hass.data[DOMAIN][CONF_SERVICES_REGISTERED]]

        for config_entry_id in hass.data[DOMAIN]:
            if config_entry_id == CONF_SERVICES_REGISTERED:
                continue

            instance = hass.data[DOMAIN][config_entry_id]
            if instance.serial == mower_serial:
                return instance

        raise HomeAssistantError("No mower instance found for serial '%s'" % mower_serial)

    async def async_send_command(call):
        """Handle the mower command service call."""
        instance = find_instance_for_mower_service_call(call)
        command = call.data.get(CONF_SEND_COMMAND, DEFAULT_NAME_COMMANDS)
        _LOGGER.debug("Indego.send_command service called, with command: %s", command)
        await instance._indego_client.put_command(command)
        await instance._update_state()

    async def async_send_smartmowing(call):
        """Handle the smartmowing service call."""
        instance = find_instance_for_mower_service_call(call)
        enable = call.data.get(CONF_SMARTMOWING, DEFAULT_NAME_COMMANDS)
        _LOGGER.debug("Indego.send_smartmowing service called, set to %s", enable)
        await instance._indego_client.put_mow_mode(enable)
        await instance._update_generic_data()

    # In HASS we can have multiple Indego component instances as long as the mower serial is unique.
    # So the mower services should only need to be registered for the first instance.
    if CONF_SERVICES_REGISTERED not in hass.data[DOMAIN]:
        _LOGGER.debug("Initializing mower service for config entry '%s'", entry.entry_id)

        hass.services.async_register(
            DOMAIN,
            SERVICE_NAME_COMMAND,
            async_send_command,
            schema=SERVICE_SCHEMA_COMMAND
        )

        hass.services.async_register(
            DOMAIN,
            SERVICE_NAME_SMARTMOW,
            async_send_smartmowing,
            schema=SERVICE_SCHEMA_SMARTMOWING,
        )

        hass.data[DOMAIN][CONF_SERVICES_REGISTERED] = entry.entry_id

    else:
        _LOGGER.debug("Indego mower services already registered. Skipping for config entry '%s'", entry.entry_id)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""

    unload_ok = await hass.config_entries.async_unload_platforms(entry, INDEGO_PLATFORMS)
    if not unload_ok:
        return False

    if CONF_SERVICES_REGISTERED in hass.data[DOMAIN] and hass.data[DOMAIN][CONF_SERVICES_REGISTERED] == entry.entry_id:
        del hass.data[DOMAIN][CONF_SERVICES_REGISTERED]

    await hass.data[DOMAIN][entry.entry_id].async_shutdown()
    del hass.data[DOMAIN][entry.entry_id]

    return True


class IndegoHub:
    """Class for the IndegoHub, which controls the sensors and binary sensors."""

    def __init__(self, name: str, session: OAuth2Session, serial: str, hass: HomeAssistant):
        """Initialize the IndegoHub.

        Args:
            name (str): the name of the mower for entities
            session (OAuth2Session): the Bosch SingleKey ID OAuth session
            serial (str): serial of the mower, is used for uniqueness
            hass (HomeAssistant): HomeAssistant instance

        """
        self._mower_name = name
        self._serial = serial
        self._hass = hass
        self._unsub_refresh_state = None
        self._refresh_state_task = None
        self._refresh_10m_remover = None
        self._refresh_24h_remover = None
        self._shutdown = False
        self._latest_alert = None
        self.entities = {}

        async def async_token_refresh() -> str:
            await session.async_ensure_token_valid()
            return session.token["access_token"]

        self._indego_client = IndegoAsyncClient(
            token=session.token["access_token"],
            token_refresh_method=async_token_refresh,
            serial=self._serial,
            session=async_get_clientsession(hass),
        )

    def _create_entities(self, device_info):
        """Create sub-entities and add them to Hass."""

        _LOGGER.debug("Creating entities")

        for entity_key, entity in ENTITY_DEFINITIONS.items():
            if entity[CONF_TYPE] == SENSOR_TYPE:
                self.entities[entity_key] = IndegoSensor(
                    f"indego_{self._serial}_{entity_key}",
                    f"{self._mower_name} {entity[CONF_NAME]}",
                    entity[CONF_ICON],
                    entity[CONF_DEVICE_CLASS],
                    entity[CONF_UNIT_OF_MEASUREMENT],
                    entity[CONF_ATTR],
                    device_info
                )

            elif entity[CONF_TYPE] == BINARY_SENSOR_TYPE:
                self.entities[entity_key] = IndegoBinarySensor(
                    f"indego_{self._serial}_{entity_key}",
                    f"{self._mower_name} {entity[CONF_NAME]}",
                    entity[CONF_ICON],
                    entity[CONF_DEVICE_CLASS],
                    entity[CONF_ATTR],
                    device_info
                )

    async def update_generic_data_and_load_platforms(self, load_platforms):
        """Update the generic mower data, so we can create the HA platforms for the Indego component."""
        _LOGGER.debug("Getting generic data for device info.")
        generic_data = await self._update_generic_data()

        device_info = DeviceInfo(
            identifiers={(DOMAIN, self._serial)},
            serial_number=self._serial,
            manufacturer="Bosch",
            name=self._mower_name,
            model=generic_data.bareToolnumber if generic_data else None,
            sw_version=generic_data.alm_firmware_version if generic_data else None,
        )

        self._create_entities(device_info)
        await load_platforms()

        if self._hass.state == CoreState.running:
            # HA has already been started (this probably an integration reload).
            # Perform initial update right away...
            self._hass.async_create_task(self._initial_update())

        else:
            # HA is still starting, delay the initial update...
            self._hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STARTED, self._initial_update
            )

        self._hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self.async_shutdown)

    async def _initial_update(self, _=None):
        """Do the initial update and create all entities."""
        _LOGGER.debug("Starting initial update.")

        self._create_refresh_state_task()
        await asyncio.gather(*[self.refresh_10m(), self.refresh_24h()])

        try:
            _LOGGER.debug("Refreshing initial operating data.")
            await self._update_operating_data()

        except Exception as e:
            _LOGGER.info("Update operating data got an exception: %s", e)

    async def async_shutdown(self, _=None):
        """Remove all future updates, cancel tasks and close the client."""
        if self._shutdown:
            return

        _LOGGER.debug("Starting shutdown.")
        self._shutdown = True

        self._cancel_delayed_refresh_state()

        if self._refresh_state_task:
            self._refresh_state_task.cancel()
            await self._refresh_state_task
            self._refresh_state_task = None

        if self._refresh_10m_remover:
            self._refresh_10m_remover()

        if self._refresh_24h_remover:
            self._refresh_24h_remover()

        await self._indego_client.close()

    async def refresh_state(self):
        """Update the state, if necessary update operating data and recall itself."""
        _LOGGER.debug("Refreshing state.")
        self._cancel_delayed_refresh_state()

        update_start_time = time.time()
        try:
            await self._update_state()

        except Exception as e:
            _LOGGER.info("Update state got an exception: %s", e)

        # Detect quick call failure.
        # To prevent infinite update loops in case of server or authentication errors.
        update_failed = not self._indego_client.state and (time.time() - update_start_time) < 10

        if self._shutdown:
            return

        if self._indego_client.state:
            state = self._indego_client.state.state
            if (500 <= state <= 799) or (state in (257, 260)):
                try:
                    _LOGGER.debug("Refreshing operating data.")
                    await self._update_operating_data()

                except Exception as e:
                    _LOGGER.info("Update operating data got an exception: %s", e)

            if self._indego_client.state.error != self._latest_alert:
                self._latest_alert = self._indego_client.state.error
                try:
                    _LOGGER.debug("Refreshing alerts, to get new alert.")
                    await self._update_alerts()

                except Exception as e:
                    _LOGGER.info("Update alert got an exception: %s", e)

        if update_failed:
            _LOGGER.debug("Delaying next status update with %i seconds due to previous failure...", STATUS_UPDATE_FAILURE_DELAY_TIME)
            when = datetime.now() + timedelta(seconds=(STATUS_UPDATE_FAILURE_DELAY_TIME if update_failed else 0))
            self._unsub_refresh_state = async_track_point_in_time(self.hass, self._create_refresh_state_task, when)

        else:
            self._create_refresh_state_task()

    def _create_refresh_state_task(self):
        """Create a task to refresh the mower state."""
        self._refresh_state_task = self._hass.async_create_task(self.refresh_state())

    def _cancel_delayed_refresh_state(self):
        """Cancel a delayed refresh state callback (if any exists)."""
        if self._unsub_refresh_state is None:
            return

        self._unsub_refresh_state()
        self._unsub_refresh_state = None

    async def refresh_10m(self, _=None):
        """Refresh Indego sensors every 10m."""
        _LOGGER.debug("Refreshing 10m.")

        results = await asyncio.gather(
            *[
                self._update_generic_data(),
                self._update_alerts(),
                self._update_last_completed_mow(),
                self._update_next_mow(),
            ],
            return_exceptions=True,
        )

        next_refresh = 600
        index = 0
        for res in results:
            if res and isinstance(res, BaseException):
                try:
                    raise res
                except Exception as e:
                    _LOGGER.warning("Uncaught error: %s on index: %s", e, index)
            index += 1

        self._refresh_10m_remover = async_call_later(
            self._hass, next_refresh, self.refresh_10m
        )

    async def refresh_24h(self, _=None):
        """Refresh Indego sensors every 24h."""
        _LOGGER.debug("Refreshing 24h.")

        try:
            await self._update_updates_available()

        except Exception as e:
            _LOGGER.info("Update updates available got an exception: %s", e)

        self._refresh_24h_remover = async_call_later(self._hass, 86400, self.refresh_24h)

    async def _update_operating_data(self):
        await self._indego_client.update_operating_data()

        # dependent state updates
        _LOGGER.info(f"Updating operating data")
        if self._indego_client.operating_data:
            self.entities[ENTITY_ONLINE].state = self._indego_client._online
            self.entities[
                ENTITY_BATTERY
            ].state = self._indego_client.operating_data.battery.percent_adjusted

            # dependent attribute updates
            self.entities[ENTITY_BATTERY].add_attribute(
                {
                    "last_updated": homeassistant.util.dt.as_local(utcnow()).strftime(
                        "%Y-%m-%d %H:%M"
                    ),
                    "voltage_V": self._indego_client.operating_data.battery.voltage,
                    "discharge_Ah": self._indego_client.operating_data.battery.discharge,
                    "cycles": self._indego_client.operating_data.battery.cycles,
                    f"battery_temp_{TEMP_CELSIUS}": self._indego_client.operating_data.battery.battery_temp,
                    f"ambient_temp_{TEMP_CELSIUS}": self._indego_client.operating_data.battery.ambient_temp,
                }
            )

        else:
            self.entities[ENTITY_ONLINE].state = self._indego_client._online

    async def _update_state(self):
        await self._indego_client.update_state(longpoll=True, longpoll_timeout=300)

        # dependent state updates
        if self._shutdown:
            return

        if not self._indego_client.state:
            return  # State update failed

        self.entities[ENTITY_MOWER_STATE].state = self._indego_client.state_description
        self.entities[
            ENTITY_MOWER_STATE_DETAIL
        ].state = self._indego_client.state_description_detail
        self.entities[ENTITY_LAWN_MOWED].state = self._indego_client.state.mowed
        self.entities[ENTITY_RUNTIME].state = self._indego_client.state.runtime.total.cut
        self.entities[ENTITY_BATTERY].charging = (
            True if self._indego_client.state_description_detail == "Charging" else False
        )

        # dependent attribute updates
        self.entities[ENTITY_MOWER_STATE].add_attribute(
            {
                "last_updated": homeassistant.util.dt.as_local(utcnow()).strftime(
                    "%Y-%m-%d %H:%M"
                )
            }
        )

        self.entities[ENTITY_MOWER_STATE_DETAIL].add_attribute(
            {
                "last_updated": homeassistant.util.dt.as_local(utcnow()).strftime(
                    "%Y-%m-%d %H:%M"
                ),
                "state_number": self._indego_client.state.state,
                "state_description": self._indego_client.state_description_detail,
            }
        )

        self.entities[ENTITY_LAWN_MOWED].add_attribute(
            {
                "last_updated": homeassistant.util.dt.as_local(utcnow()).strftime(
                    "%Y-%m-%d %H:%M"
                ),
                "last_session_operation_min": self._indego_client.state.runtime.session.operate,
                "last_session_cut_min": self._indego_client.state.runtime.session.cut,
                "last_session_charge_min": self._indego_client.state.runtime.session.charge,
            }
        )

        self.entities[ENTITY_RUNTIME].add_attribute(
            {
                "total_operation_time_h": self._indego_client.state.runtime.total.operate,
                "total_mowing_time_h": self._indego_client.state.runtime.total.cut,
                "total_charging_time_h": self._indego_client.state.runtime.total.charge,
            }
        )

    async def _update_generic_data(self):
        await self._indego_client.update_generic_data()

        # dependent state updates
        if self._indego_client.generic_data:
            if ENTITY_MOWING_MODE in self.entities:
                self.entities[
                    ENTITY_MOWING_MODE
                ].state = self._indego_client.generic_data.mowing_mode_description

        return self._indego_client.generic_data

    async def _update_alerts(self):
        await self._indego_client.update_alerts()

        # dependent state updates
        if self._indego_client.alerts:
            self.entities[ENTITY_ALERT].state = self._indego_client.alerts_count > 0

            self.entities[ENTITY_ALERT].add_attribute(
                {"alerts_count": self._indego_client.alerts_count, }
            )

        else:
            self.entities[ENTITY_ALERT].state = 0

        j = len(self._indego_client.alerts)
        # _LOGGER.info(f"Structuring ALERTS.{j}")
        for i in range(j):
            self.entities[ENTITY_ALERT].add_attribute(
                {
                    self._indego_client.alerts[i].date.strftime("%Y-%m-%d %H:%M"): str(
                        self._indego_client.alerts[i].alert_description
                    ),
                }
            )

    async def _update_updates_available(self):
        await self._indego_client.update_updates_available()

        # dependent state updates
        self.entities[ENTITY_UPDATE_AVAILABLE].state = self._indego_client.update_available

    async def _update_last_completed_mow(self):
        await self._indego_client.update_last_completed_mow()

        if self._indego_client.last_completed_mow:
            self.entities[
                ENTITY_LAST_COMPLETED
            ].state = self._indego_client.last_completed_mow.isoformat()

            self.entities[ENTITY_LAST_COMPLETED].add_attribute(
                {
                    "last_completed_mow": self._indego_client.last_completed_mow.strftime(
                        "%Y-%m-%d %H:%M"
                    )
                }
            )

            self.entities[ENTITY_LAWN_MOWED].add_attribute(
                {
                    "last_completed_mow": self._indego_client.last_completed_mow.strftime(
                        "%Y-%m-%d %H:%M"
                    )
                }
            )

    async def _update_next_mow(self):
        await self._indego_client.update_next_mow()

        if self._indego_client.next_mow:
            self.entities[ENTITY_NEXT_MOW].state = self._indego_client.next_mow.isoformat()

            self.entities[ENTITY_NEXT_MOW].add_attribute(
                {"next_mow": self._indego_client.next_mow.strftime("%Y-%m-%d %H:%M")}
            )

            self.entities[ENTITY_LAWN_MOWED].add_attribute(
                {"next_mow": self._indego_client.next_mow.strftime("%Y-%m-%d %H:%M")}
            )

    @property
    def serial(self) -> str:
        return self._serial
