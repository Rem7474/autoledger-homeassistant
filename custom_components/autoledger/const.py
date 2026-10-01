"""Constants for the AutoLedger Home Assistant integration."""

from homeassistant.const import Platform

DOMAIN = "autoledger"

# Configuration keys
CONF_HOST = "host"
CONF_API_KEY = "api_key"
CONF_VERIFY_SSL = "verify_ssl"
CONF_VEHICLES = "vehicles"

# Vehicle mapping configuration keys
CONF_VEHICLE_ID = "vehicle_id"
CONF_VEHICLE_NAME = "vehicle_name"
CONF_DEVICE_ID = "device_id"
CONF_BATTERY_SOC_ENTITY = "battery_soc_entity"
CONF_ODOMETER_ENTITY = "odometer_entity"
CONF_CHARGING_STATUS_ENTITY = "charging_status_entity"
CONF_ENERGY_METER_ENTITY = "energy_meter_entity"
CONF_ENERGY_METER_TYPE = "energy_meter_type"
CONF_LOCATION_ENTITY = "location_entity"
CONF_CHARGING_LOCATION = "charging_location"
CONF_DEBOUNCE_SECONDS = "debounce_seconds"

# Energy meter modes
ENERGY_METER_TYPE_TOTAL_INCREASING = "total_increasing"
ENERGY_METER_TYPE_SESSION = "session"

ENERGY_METER_TYPES = [
    ENERGY_METER_TYPE_TOTAL_INCREASING,
    ENERGY_METER_TYPE_SESSION,
]

# Defaults
DEFAULT_VERIFY_SSL = True
DEFAULT_DEBOUNCE_SECONDS = 60
DEFAULT_CHARGING_LOCATION = "home"
DEFAULT_SCAN_INTERVAL_MINUTES = 15
MIN_DEBOUNCE_SECONDS = 15
MAX_DEBOUNCE_SECONDS = 300

# Charging states
STATE_IDLE = "idle"
STATE_CHARGING = "charging"
STATE_COOLING_DOWN = "cooling_down"

# Sync status values
SYNC_STATUS_OK = "ok"
SYNC_STATUS_PENDING = "pending"
SYNC_STATUS_ERROR = "error"

# Attributes
ATTR_ENERGY_KWH = "energy_kwh"
ATTR_DURATION_MINUTES = "duration_minutes"
ATTR_DATE = "date"
ATTR_CURRENCY = "currency"
ATTR_LAST_SUCCESSFUL_SYNC = "last_successful_sync"
ATTR_LAST_ERROR_MESSAGE = "last_error_message"
ATTR_PENDING_EVENTS_COUNT = "pending_events_count"
ATTR_SESSION_START_TIME = "session_start_time"
ATTR_DEBOUNCE_ACTIVE = "debounce_active"
ATTR_ENERGY_START_KWH = "energy_start_kwh"

# Supported platforms
PLATFORMS: list[Platform] = [
    Platform.SENSOR,
]
