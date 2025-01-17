import asyncio
import async_timeout
import aiohttp
import logging
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import Throttle
from homeassistant.const import (
    DEVICE_CLASS_ENERGY,
    ATTR_UNIT_OF_MEASUREMENT,
    ATTR_DEVICE_CLASS,
)
from homeassistant.components.utility_meter.const import (
    SERVICE_SELECT_TARIFF,
    DOMAIN as UTIL_METER_DOMAIN,
    ATTR_TARIFF,
)
from homeassistant.components.recorder.const import DATA_INSTANCE
from homeassistant.core import (
    Context,
    callback as ha_callback,
)
from datetime import datetime, timedelta
from dateutil import tz
import json
import re
from time import time
import urllib

from .const import (
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_LIGHT_AS_SWITCH,
    DEFAULT_GENERATE_ENERGY_METERS,
    DEFAULT_ENERGY_METER_PERIOD,
    DEFAULT_TARIFF_PLAN,
    DEFAULT_HQ_PLAN_NAME,
    DOMAIN,
    CONF_HIGH_PERIODS,
    CONF_TARIFF,
)

_LOGGER = logging.getLogger(__name__)

# These attributes will log their value when device is updated when debug is enabled
# Useful for debugging
LOGGED_ATTRIBUTES = ['CurrentTemperature', 'TargetTemperature', 'Power', 'Heating']
TARIF_TYPE_REX = re.compile(r'(sensor.hilo_energy_.*)_(low|medium|high)')


class Hilo:
    _username = None
    _password = None
    _access_token = None
    _location_id = None

    _base_url = "https://apim.hiloenergie.com"
    _api_end = "v1/api"
    _automation_url = f"{_base_url}/Automation/{_api_end}"
    _gd_service_url = f"{_base_url}/GDService/{_api_end}"
    _subscription_key = "20eeaedcb86945afa3fe792cea89b8bf"
    _token_expiration = None
    _timeout = 30
    _verify = True
    devices = []

    def __init__(
        self,
        username,
        password,
        hass,
        scan_interval=DEFAULT_SCAN_INTERVAL,
        light_as_switch=DEFAULT_LIGHT_AS_SWITCH,
        generate_energy_meters=DEFAULT_GENERATE_ENERGY_METERS,
        energy_meter_period=DEFAULT_ENERGY_METER_PERIOD,
        hq_plan_name=DEFAULT_HQ_PLAN_NAME,
        tariff_plan=DEFAULT_TARIFF_PLAN,
    ):
        self._username = username
        self._password = urllib.parse.quote(password, safe="!@#$%^&*()")
        self._hass = hass
        self.scan_interval = scan_interval
        self.light_as_switch = light_as_switch
        self.generate_energy_meters = generate_energy_meters
        self.energy_meter_period = energy_meter_period
        self.hq_plan_name = hq_plan_name
        self.tariff_plan = tariff_plan
        self.async_update = Throttle(self.scan_interval)(self._async_update)
        self.refresh_token = Throttle(timedelta(seconds=120))(self._refresh_token)
        self.current_cost = float(0.0000)

    async def location_url(self, gd=False):
        self._location_id = await self.get_location_id()
        base = self._automation_url
        if gd:
            base = self._gd_service_url
        return f"{base}/Locations/{self._location_id}"

    @property
    def headers(self):
        return {
            "Ocp-Apim-Subscription-Key": self._subscription_key,
            "authorization": f"Bearer {self._access_token}",
        }

    @property
    def high_times(self):
        for period, data in CONF_HIGH_PERIODS.items():
            if data["from"] <= datetime.now().time() <= data["to"]:
                return True
        return False

    async def async_call(self, url, method="get", headers={}, data={}, allowed_status=[200], retry=3):
        async def try_again(err: str):
            if retry < 1:
                _LOGGER.error(f"Unable to {method} {url}: {err}")
                raise HomeAssistantError("Retry limit reached")
            _LOGGER.error(f"Retry #{retry - 1}: {err}")
            return await self.async_call(
                url, method=method, headers=headers, data=data, retry=retry - 1
            )

        # _LOGGER.debug(f"Request {method} {url}")
        try:
            session = async_get_clientsession(self._hass, self._verify)
            with async_timeout.timeout(self._timeout):
                resp = await getattr(session, method)(url, headers=headers, data=data)
            _LOGGER.debug(f"Response: {resp.status} {resp.text}")
            if resp.status == 401:
                if "oauth2" not in url:
                    await self.refresh_token(True)
                    return await try_again(f"{resp.url} Token is expired, trying again")
                else:
                    _LOGGER.error(
                        "Access denied when refreshing token, unloading integration. Bad username / password"
                    )
                    self._hass.services.async_remove(DOMAIN)
                    raise HomeAssistantError("Wrong username / password")
            if resp.status not in allowed_status:
                _LOGGER.error(f"{method} on {url} failed: {resp.status} {resp.text}")
                return await try_again(f"{url} returned {resp.status}")
        except (asyncio.TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.error(f"{method} {url} failed")
            _LOGGER.exception(err)
            return await try_again(err)
        try:
            data = await resp.json()
            #_LOGGER.debug(f"Raw response dict: {data}")
        except aiohttp.client_exceptions.ContentTypeError:
            _LOGGER.warning(f"{resp.url} returned {resp.status} non-json: {resp.text}")
            return resp.text
        except Exception as e:
            _LOGGER.exception(e)
            return await try_again(f"{resp.url} returned {resp.status}: {resp.text}")
        return data

    async def _request(self, url, method="get", headers={}, data={}):
        await self.refresh_token()
        if not headers:
            headers = self.headers
        if method == "put":
            headers = {**headers, **{"Content-Type": "application/json"}}
        try:
            out = await self.async_call(url, method, headers, data)
        except HomeAssistantError as e:
            _LOGGER.exception(e)
            raise
        return out

    async def get_access_token(self):
        url = (
            "https://hilodirectoryb2c.b2clogin.com/"
            "hilodirectoryb2c.onmicrosoft.com/oauth2/"
            "v2.0/token?p=B2C_1A_B2C_1_PasswordFlow"
        )
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        body = {
            "grant_type": "password",
            "scope": "openid 9870f087-25f8-43b6-9cad-d4b74ce512e1 offline_access",
            "client_id": "9870f087-25f8-43b6-9cad-d4b74ce512e1",
            "response_type": "token id_token",
            "username": self._username,
            "password": self._password,
        }
        _LOGGER.debug("Calling oauth2 url")
        req = await self.async_call(url, method="post", headers=headers, data=body)
        return req.get("access_token", None)

    async def _refresh_token(self, force=False):
        expiration = self._token_expiration if self._token_expiration else time() - 200
        time_to_expire = time() - expiration
        _LOGGER.debug(
            f"Refreshing token, force: {force} Expiration: "
            f"{datetime.fromtimestamp(expiration)} "
            f"Time to expire: {time_to_expire}"
        )
        if force or not self._access_token or time() > expiration:
            self._token_expiration = time() + 3000
            self._access_token = await self.get_access_token()
            if not self._access_token:
                return False
        return True

    async def get_location_id(self):
        if self._location_id:
            return self._location_id
        url = f"{self._automation_url}/Locations"
        req = await self._request(url)
        return req[0]["id"]

    async def get_gateway(self):
        url = f"{await self.location_url()}/Gateways/Info"
        req = await self._request(url)
        # [
        #   {
        #     "onlineStatus": "Online",
        #     "lastStatusTimeUtc": "2021-11-08T01:43:15Z",
        #     "zigBeePairingActivated": false,
        #     "dsn": "xxx",
        #     "installationCode": "xxx",
        #     "sepMac": "xxx",
        #     "firmwareVersion": "2.1.2",
        #     "localIp": null,
        #     "zigBeeChannel": 19
        #   }
        # ]
        saved_attrs = [
            "zigBeePairingActivated",
            "zigBeeChannel",
            "firmwareVersion",
            "onlineStatus"
        ]

        gw = {
            "name": "hilo_gateway",
            "Disconnected": {
                "value": not req[0].get("onlineStatus") == "Online"
            },
            "type": "Gateway",
            "supportedAttributes": ", ".join(saved_attrs),
            "settableAttributes": "",
            "id": self._location_id,
            "category": "Gateway",
        }
        for attr in saved_attrs:
            gw[attr] = {"value": req[0].get(attr)}
        return gw

    async def get_events(self):
        # TODO(dvd): Leveraging the phases
        # [{
        #     'progress': 'inProgress',
        #     'isParticipating': True,
        #     'isConfigurable': False,
        #     'id': 107,
        #     'period': 'pm',
        #     'phases': {
        #       'preheatStartDateUTC': '2021-11-25T20:00:00Z',
        #       'preheatEndDateUTC': '2021-11-25T22:00:00Z',
        #       'reductionStartDateUTC': '2021-11-25T22:00:00Z',
        #       'reductionEndDateUTC': '2021-11-26T02:00:00Z',
        #       'recoveryStartDateUTC': '2021-11-26T02:00:00Z',
        #       'recoveryEndDateUTC': '2021-11-26T02:50:00Z'
        #     }
        # }]
        url = f"{await self.location_url(True)}/Events?active=true"
        req = await self._request(url)
        _LOGGER.debug(f"Events: {req}")
        from_zone = tz.tzutc()
        to_zone = tz.tzlocal()
        now = datetime.now()
        current_event = False
        if len(req):
            if req[0].get('progress', "NotInProgress") == "inProgress":
                current_event = True
        #for r in req:
        #    start_time = datetime.strptime(r.get("reductionStartDateUTC"), "%Y-%m-%dT%H:%M:%SZ")\
        #                       .replace(tzinfo=from_zone)\
        #                       .astimzone(to_zone)
        #    end_time = datetime.strptime(r.get("reductionEndDateUTC"), "%Y-%m-%dT%H:%M:%SZ")\
        #                       .replace(tzinfo=from_zone)\
        #                       .astimzone(to_zone)
        #    if start_time <= now <= end_time:
        #        _LOGGER.info(f"Hilo event currently active: {r}")
        #        current_event = True
        return current_event

    def get_dev_or_new(self, v):
        return next(
            (x for x in self.devices if x.device_id == v["id"]), Device(self)
        )
 
    async def add_device(self, v):
        device = self.get_dev_or_new(v)
        await device._set_hilo_attributes(**v)
        if not device in self.devices:
            self.devices.append(device)
 
    async def get_devices(self):
        """Get list of all devices"""
        url = f"{await self.location_url()}/Devices"
        req = await self._request(url)
        for i, v in enumerate(req):
            await self.add_device(v)
        await self.add_device(await self.get_gateway())

    async def _async_update(self):
        # self.get_events()
        _LOGGER.info("Pulling all devices")
        await self.get_devices()

    async def async_update_all_devices(self):
        _LOGGER.info("Updating attributes for all devices")
        await self.get_devices()
        for d in self.devices:
            await d.async_update_device()

    def set_state(self, entity, state, new_attrs={}, keep_state=False, force=False):
        params = f"entity={entity}, state={state}, new_attrs={new_attrs}, keep_state={keep_state}"
        current = self._hass.states.get(entity)
        if not current:
            if not force:
                _LOGGER.warning(
                    f"Unable to set state because there's no current: {params}"
                )
                return
            attrs = {}
        else:
            attrs = current.as_dict()["attributes"]
        _LOGGER.debug(f"Setting state {params} {current}")
        attrs["last_update"] = datetime.now()
        attrs = {**attrs, **new_attrs}
        if keep_state and current:
            state = current.state
        if "Cost" in attrs:
            attrs["Cost"] = state
        self._hass.states.async_set(entity, state, attrs)

    def check_tarif(self):
        tarif = "low"
        base_sensor = "sensor.hilo_energy_total_daily_low"
        energy_used = self._hass.states.get(base_sensor)
        if not energy_used:
            _LOGGER.warning(f"check_tarif: Unable to find state for {base_sensor}")
            return tarif
        plan_name = self.hq_plan_name
        tarif_config = CONF_TARIFF.get(plan_name)
        current_cost = self._hass.states.get("sensor.hilo_rate_current")
        try:
            if float(energy_used.state) >= tarif_config.get("low_threshold"):
                tarif = "medium"
        except ValueError:
            _LOGGER.warning(f"Unable to restore a valid state of {base_sensor}: {energy_used.state}")
            pass
        if tarif_config.get("high") > 0 and self.high_times:
            tarif = "high"
        target_cost = self._hass.states.get(f"sensor.hilo_rate_{tarif}")
        if target_cost.state != current_cost.state:
            _LOGGER.debug(
                f"check_tarif: Updating current cost, was {current_cost.state} now {target_cost.state}"
            )
            self.set_state("sensor.hilo_rate_current", target_cost.state)
        _LOGGER.debug(
            f"check_tarif: Current plan: {plan_name} Target Tarif: {tarif} Energy used: {energy_used.state} Peak: {self.high_times}"
        )
        utility_entities = {}
        for state in self._hass.states.async_all():
            entity = state.entity_id
            self.set_tarif(entity, state.state, tarif)
            if not entity.startswith("sensor.hilo_energy") or entity.endswith("_cost"):
                continue
            self.fix_utility_sensor(entity, state)

    @ha_callback
    def fix_utility_sensor(self, entity, state):
        """not sure why this doesn't get created with a proper device_class"""
        current_state = state.as_dict()
        attrs = current_state.get("attributes", {})
        if not attrs.get("source"):
            _LOGGER.debug(f"No source entity defined on {entity}: {current_state}")
            return
        parent_unit = self._hass.states.get(attrs.get("source"))
        if not parent_unit:
            _LOGGER.warning(f"Unable to find state for parent unit: {current_state}")
            return
        new_attrs = {
            ATTR_UNIT_OF_MEASUREMENT: parent_unit.as_dict()
            .get("attributes", {})
            .get(ATTR_UNIT_OF_MEASUREMENT),
            ATTR_DEVICE_CLASS: DEVICE_CLASS_ENERGY,
        }
        if not all(a in attrs.keys() for a in new_attrs.keys()):
            _LOGGER.warning(
                f"Fixing utility sensor: {entity} {current_state} new_attrs: {new_attrs}"
            )
            self.set_state(entity, None, new_attrs=new_attrs, keep_state=True)

    @ha_callback
    def set_tarif(self, entity, current, new):
        if entity.startswith("utility_meter.hilo_energy") and current != new:
            _LOGGER.debug(
                f"check_tarif: Changing tarif of {entity} from {current} to {new}"
            )
            context = Context()
            data = {ATTR_TARIFF: new, "entity_id": entity}
            self._hass.async_create_task(
                self._hass.services.async_call(
                    UTIL_METER_DOMAIN, SERVICE_SELECT_TARIFF, data, context=context
                )
            )


class Device:
    def __init__(self, hilo):
        self._h = hilo
        self._entity = None

    async def _set_hilo_attributes(self, **kw):
        self.name = kw.get("name")
        self.device_type = kw.get("type")
        self.supported_attributes = kw.get("supportedAttributes").split(", ")
        self.settable_attributes = kw.get("settableAttributes")
        self.device_id = kw.get("id")
        self.category = kw.get("category")
        self._tag = f"[Device {self.name} ({self.device_type})]"
        self._device_url = f"{await self._h.location_url()}/Devices/{self.device_id}"
        self._raw_attributes = {}
        _LOGGER.debug(f"{self._tag} Setting attributes {kw}")
        # All devices like SmokeDetectors don't have the disconnected attribute
        # but it can be fetched
        if "Disconnected" not in self.supported_attributes:
            self.supported_attributes.append("Disconnected")
        if "None" in self.supported_attributes:
            self.supported_attributes.remove("None")

    async def get_device_attributes(self):
        if self.device_type == "Gateway":
            req = await self._h.get_gateway()
        else:
            url = f"{self._device_url}/Attributes"
            req = await self._h._request(url)
        if len(req.items()):
            self._raw_attributes = {k.lower(): v for k, v in req.items()}
            _LOGGER.debug(f"{self._tag} get_device_attributes (raw): {self._raw_attributes}")
        else:
            _LOGGER.debug(f"{self._tag} Empty data returned by hilo")
            if len(self._raw_attributes) == 0:
                _LOGGER.debug("Retrying to get attributes")
                await self.get_device_attributes()

    async def set_attribute(self, key, value):
        if self.device_type == "Gateway":
            return
        _LOGGER.debug(f"{self._tag} setting remote attribute {key} to {value}")
        setattr(self, key, value)
        url = f"{self._device_url}/Attributes"
        await self._h._request(url, method="put", data=json.dumps({key: str(value)}))

    async def async_update_device(self):
        await self.get_device_attributes()
        _LOGGER.debug(
            f"{self._tag} update_device attributes: {self.supported_attributes} "
        )
        self._last_update = datetime.today().strftime("%d-%m-%Y %H:%M")
        for x in self.supported_attributes:
            value = self._raw_attributes.get(x.lower(), {}).get("value", None)
            if x in LOGGED_ATTRIBUTES:
                _LOGGER.debug(f"{self._tag} setting local attribute {x} to {value}")
            setattr(self, x, value)
        self._h.check_tarif()

    def __eq__(self, other):
        return self.device_id == other.device_id
