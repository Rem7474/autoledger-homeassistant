"""Constants for the AutoLedger Home Assistant integration."""

from homeassistant.const import Platform

DOMAIN = "autoledger"

# Configuration keys
CONF_HOST = "host"
CONF_API_KEY = "api_key"
CONF_VERIFY_SSL = "verify_ssl"
CONF_VEHICLES = "vehicles"
CONF_CHARGERS = "chargers"

# Vehicle mapping configuration keys
CONF_VEHICLE_ID = "vehicle_id"
CONF_VEHICLE_NAME = "vehicle_name"
CONF_DEVICE_ID = "device_id"
CONF_BATTERY_SOC_ENTITY = "battery_soc_entity"
CONF_ODOMETER_ENTITY = "odometer_entity"

# Charger configuration keys
CONF_CHARGER_ID = "charger_id"
CONF_CHARGER_NAME = "charger_name"
CONF_CHARGING_STATUS_ENTITY = "charging_status_entity"
CONF_ENERGY_METER_ENTITY = "energy_meter_entity"
CONF_ENERGY_METER_TYPE = "energy_meter_type"
CONF_LOCATION_ENTITY = "location_entity"
CONF_CHARGING_LOCATION = "charging_location"
CONF_DEBOUNCE_SECONDS = "debounce_seconds"
CONF_ASSIGNMENT_MODE = "assignment_mode"
CONF_LINKED_VEHICLE_ID = "linked_vehicle_id"
CONF_VEHICLE_SELECT_ENTITY = "vehicle_select_entity"

# Assignment modes
ASSIGNMENT_MODE_FIXED = "fixed"
ASSIGNMENT_MODE_INPUT_SELECT = "input_select"
ASSIGNMENT_MODE_CORRELATION = "correlation"
ASSIGNMENT_MODE_UNASSIGNED = "unassigned"
ASSIGNMENT_MODES = [
    ASSIGNMENT_MODE_FIXED,
    ASSIGNMENT_MODE_INPUT_SELECT,
    ASSIGNMENT_MODE_CORRELATION,
    ASSIGNMENT_MODE_UNASSIGNED,
]

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
DEFAULT_ASSIGNMENT_MODE = ASSIGNMENT_MODE_FIXED
DEFAULT_TRIP_END_DEBOUNCE_SECONDS = 300
MIN_DEBOUNCE_SECONDS = 15
MAX_DEBOUNCE_SECONDS = 300

# Event types
# Routes an API token opens on the AutoLedger server
INTEGRATION_API = "/api/integrations/homeassistant"

EVENT_TYPE_CHARGING_SESSION_END = "charging_session_end"
EVENT_TYPE_ODOMETER_UPDATE = "odometer_update"

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
ATTR_LAST_ENERGY_KWH = "last_energy_kwh"
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
    Platform.BUTTON,
    Platform.SENSOR,
]
