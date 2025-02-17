"""Config flow for UniFi Network integration.

Provides user initiated configuration flow.
Discovery of UniFi Network instances hosted on UDM and UDM Pro devices
through SSDP. Reauthentication when issue with credentials are reported.
Configuration of options through options flow.
"""
import socket
from urllib.parse import urlparse

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components import ssdp
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
)
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.device_registry import format_mac

from .const import (
    CONF_ALLOW_BANDWIDTH_SENSORS,
    CONF_ALLOW_UPTIME_SENSORS,
    CONF_BLOCK_CLIENT,
    CONF_CONTROLLER,
    CONF_DETECTION_TIME,
    CONF_DPI_RESTRICTIONS,
    CONF_IGNORE_WIRED_BUG,
    CONF_POE_CLIENTS,
    CONF_SITE_ID,
    CONF_SSID_FILTER,
    CONF_TRACK_CLIENTS,
    CONF_TRACK_DEVICES,
    CONF_TRACK_WIRED_CLIENTS,
    DEFAULT_DPI_RESTRICTIONS,
    DEFAULT_POE_CLIENTS,
    DOMAIN as UNIFI_DOMAIN,
)
from .controller import get_controller
from .errors import AuthenticationRequired, CannotConnect

DEFAULT_PORT = 443
DEFAULT_SITE_ID = "default"
DEFAULT_VERIFY_SSL = False


MODEL_PORTS = {
    "UniFi Dream Machine": 443,
    "UniFi Dream Machine Pro": 443,
}


class UnifiFlowHandler(config_entries.ConfigFlow, domain=UNIFI_DOMAIN):
    """Handle a UniFi Network config flow."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return UnifiOptionsFlowHandler(config_entry)

    def __init__(self):
        """Initialize the UniFi Network flow."""
        self.config = {}
        self.site_ids = {}
        self.site_names = {}
        self.reauth_config_entry = None
        self.reauth_schema = {}

    async def async_step_user(self, user_input=None):
        """Handle a flow initialized by the user."""
        errors = {}

        if user_input is not None:

            self.config = {
                CONF_HOST: user_input[CONF_HOST],
                CONF_USERNAME: user_input[CONF_USERNAME],
                CONF_PASSWORD: user_input[CONF_PASSWORD],
                CONF_PORT: user_input.get(CONF_PORT),
                CONF_VERIFY_SSL: user_input.get(CONF_VERIFY_SSL),
                CONF_SITE_ID: DEFAULT_SITE_ID,
            }

            try:
                controller = await get_controller(
                    self.hass,
                    host=self.config[CONF_HOST],
                    username=self.config[CONF_USERNAME],
                    password=self.config[CONF_PASSWORD],
                    port=self.config[CONF_PORT],
                    site=self.config[CONF_SITE_ID],
                    verify_ssl=self.config[CONF_VERIFY_SSL],
                )

                sites = await controller.sites()

            except AuthenticationRequired:
                errors["base"] = "faulty_credentials"

            except CannotConnect:
                errors["base"] = "service_unavailable"

            else:
                self.site_ids = {site["_id"]: site["name"] for site in sites.values()}
                self.site_names = {site["_id"]: site["desc"] for site in sites.values()}

                if (
                    self.reauth_config_entry
                    and self.reauth_config_entry.unique_id in self.site_names
                ):
                    return await self.async_step_site(
                        {CONF_SITE_ID: self.reauth_config_entry.unique_id}
                    )

                return await self.async_step_site()

        if not (host := self.config.get(CONF_HOST, "")) and await async_discover_unifi(
            self.hass
        ):
            host = "unifi"

        data = self.reauth_schema or {
            vol.Required(CONF_HOST, default=host): str,
            vol.Required(CONF_USERNAME): str,
            vol.Required(CONF_PASSWORD): str,
            vol.Optional(
                CONF_PORT, default=self.config.get(CONF_PORT, DEFAULT_PORT)
            ): int,
            vol.Optional(CONF_VERIFY_SSL, default=DEFAULT_VERIFY_SSL): bool,
        }

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(data),
            errors=errors,
        )

    async def async_step_site(self, user_input=None):
        """Select site to control."""
        errors = {}

        if user_input is not None:

            unique_id = user_input[CONF_SITE_ID]
            self.config[CONF_SITE_ID] = self.site_ids[unique_id]
            # Backwards compatible config
            self.config[CONF_CONTROLLER] = self.config.copy()

            config_entry = await self.async_set_unique_id(unique_id)
            abort_reason = "configuration_updated"

            if self.reauth_config_entry:
                config_entry = self.reauth_config_entry
                abort_reason = "reauth_successful"

            if config_entry:
                controller = self.hass.data.get(UNIFI_DOMAIN, {}).get(
                    config_entry.entry_id
                )

                if controller and controller.available:
                    return self.async_abort(reason="already_configured")

                self.hass.config_entries.async_update_entry(
                    config_entry, data=self.config
                )
                await self.hass.config_entries.async_reload(config_entry.entry_id)
                return self.async_abort(reason=abort_reason)

            site_nice_name = self.site_names[unique_id]
            return self.async_create_entry(title=site_nice_name, data=self.config)

        if len(self.site_names) == 1:
            return await self.async_step_site(
                {CONF_SITE_ID: next(iter(self.site_names))}
            )

        return self.async_show_form(
            step_id="site",
            data_schema=vol.Schema(
                {vol.Required(CONF_SITE_ID): vol.In(self.site_names)}
            ),
            errors=errors,
        )

    async def async_step_reauth(self, data: dict):
        """Trigger a reauthentication flow."""
        config_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        self.reauth_config_entry = config_entry

        self.context["title_placeholders"] = {
            CONF_HOST: config_entry.data[CONF_HOST],
            CONF_SITE_ID: config_entry.title,
        }

        self.reauth_schema = {
            vol.Required(CONF_HOST, default=config_entry.data[CONF_HOST]): str,
            vol.Required(CONF_USERNAME, default=config_entry.data[CONF_USERNAME]): str,
            vol.Required(CONF_PASSWORD): str,
            vol.Required(CONF_PORT, default=config_entry.data[CONF_PORT]): int,
            vol.Required(
                CONF_VERIFY_SSL, default=config_entry.data[CONF_VERIFY_SSL]
            ): bool,
        }

        return await self.async_step_user()

    async def async_step_ssdp(self, discovery_info: ssdp.SsdpServiceInfo) -> FlowResult:
        """Handle a discovered UniFi device."""
        parsed_url = urlparse(discovery_info.ssdp_location)
        model_description = discovery_info.upnp[ssdp.ATTR_UPNP_MODEL_DESCRIPTION]
        mac_address = format_mac(discovery_info.upnp[ssdp.ATTR_UPNP_SERIAL])

        self.config = {
            CONF_HOST: parsed_url.hostname,
        }

        self._async_abort_entries_match({CONF_HOST: self.config[CONF_HOST]})

        await self.async_set_unique_id(mac_address)
        self._abort_if_unique_id_configured(updates=self.config)

        self.context["title_placeholders"] = {
            CONF_HOST: self.config[CONF_HOST],
            CONF_SITE_ID: DEFAULT_SITE_ID,
        }

        if (port := MODEL_PORTS.get(model_description)) is not None:
            self.config[CONF_PORT] = port

        return await self.async_step_user()


class UnifiOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle Unifi Network options."""

    def __init__(self, config_entry):
        """Initialize UniFi Network options flow."""
        self.config_entry = config_entry
        self.options = dict(config_entry.options)
        self.controller = None

    async def async_step_init(self, user_input=None):
        """Manage the UniFi Network options."""
        self.controller = self.hass.data[UNIFI_DOMAIN][self.config_entry.entry_id]
        self.options[CONF_BLOCK_CLIENT] = self.controller.option_block_clients

        if self.show_advanced_options:
            return await self.async_step_device_tracker()

        return await self.async_step_simple_options()

    async def async_step_simple_options(self, user_input=None):
        """For users without advanced settings enabled."""
        if user_input is not None:
            self.options.update(user_input)
            return await self._update_options()

        clients_to_block = {}

        for client in self.controller.api.clients.values():
            clients_to_block[
                client.mac
            ] = f"{client.name or client.hostname} ({client.mac})"

        return self.async_show_form(
            step_id="simple_options",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_TRACK_CLIENTS,
                        default=self.controller.option_track_clients,
                    ): bool,
                    vol.Optional(
                        CONF_TRACK_DEVICES,
                        default=self.controller.option_track_devices,
                    ): bool,
                    vol.Optional(
                        CONF_BLOCK_CLIENT, default=self.options[CONF_BLOCK_CLIENT]
                    ): cv.multi_select(clients_to_block),
                }
            ),
            last_step=True,
        )

    async def async_step_device_tracker(self, user_input=None):
        """Manage the device tracker options."""
        if user_input is not None:
            self.options.update(user_input)
            return await self.async_step_client_control()

        ssids = (
            set(self.controller.api.wlans)
            | {
                f"{wlan.name}{wlan.name_combine_suffix}"
                for wlan in self.controller.api.wlans.values()
                if not wlan.name_combine_enabled
            }
            | {
                wlan["name"]
                for ap in self.controller.api.devices.values()
                for wlan in ap.wlan_overrides
                if "name" in wlan
            }
        )
        ssid_filter = {ssid: ssid for ssid in sorted(ssids)}

        return self.async_show_form(
            step_id="device_tracker",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_TRACK_CLIENTS,
                        default=self.controller.option_track_clients,
                    ): bool,
                    vol.Optional(
                        CONF_TRACK_WIRED_CLIENTS,
                        default=self.controller.option_track_wired_clients,
                    ): bool,
                    vol.Optional(
                        CONF_TRACK_DEVICES,
                        default=self.controller.option_track_devices,
                    ): bool,
                    vol.Optional(
                        CONF_SSID_FILTER, default=self.controller.option_ssid_filter
                    ): cv.multi_select(ssid_filter),
                    vol.Optional(
                        CONF_DETECTION_TIME,
                        default=int(
                            self.controller.option_detection_time.total_seconds()
                        ),
                    ): int,
                    vol.Optional(
                        CONF_IGNORE_WIRED_BUG,
                        default=self.controller.option_ignore_wired_bug,
                    ): bool,
                }
            ),
            last_step=False,
        )

    async def async_step_client_control(self, user_input=None):
        """Manage configuration of network access controlled clients."""
        errors = {}

        if user_input is not None:
            self.options.update(user_input)
            return await self.async_step_statistics_sensors()

        clients_to_block = {}

        for client in self.controller.api.clients.values():
            clients_to_block[
                client.mac
            ] = f"{client.name or client.hostname} ({client.mac})"

        return self.async_show_form(
            step_id="client_control",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_BLOCK_CLIENT, default=self.options[CONF_BLOCK_CLIENT]
                    ): cv.multi_select(clients_to_block),
                    vol.Optional(
                        CONF_POE_CLIENTS,
                        default=self.options.get(CONF_POE_CLIENTS, DEFAULT_POE_CLIENTS),
                    ): bool,
                    vol.Optional(
                        CONF_DPI_RESTRICTIONS,
                        default=self.options.get(
                            CONF_DPI_RESTRICTIONS, DEFAULT_DPI_RESTRICTIONS
                        ),
                    ): bool,
                }
            ),
            errors=errors,
            last_step=False,
        )

    async def async_step_statistics_sensors(self, user_input=None):
        """Manage the statistics sensors options."""
        if user_input is not None:
            self.options.update(user_input)
            return await self._update_options()

        return self.async_show_form(
            step_id="statistics_sensors",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_ALLOW_BANDWIDTH_SENSORS,
                        default=self.controller.option_allow_bandwidth_sensors,
                    ): bool,
                    vol.Optional(
                        CONF_ALLOW_UPTIME_SENSORS,
                        default=self.controller.option_allow_uptime_sensors,
                    ): bool,
                }
            ),
            last_step=True,
        )

    async def _update_options(self):
        """Update config entry options."""
        return self.async_create_entry(title="", data=self.options)


async def async_discover_unifi(hass):
    """Discover UniFi Network address."""
    try:
        return await hass.async_add_executor_job(socket.gethostbyname, "unifi")
    except socket.gaierror:
        return None
