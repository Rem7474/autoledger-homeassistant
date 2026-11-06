"""Sensors fed by the AutoLedger read API (maintenance reminders, costs, pending charges)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import AutoLedgerDataUpdateCoordinator
from .vehicles import ListedVehicleEntity

PARALLEL_UPDATES = 0


def _distance_km(distance: dict[str, Any] | None) -> float | None:
    """Return a read API distance ({"value", "unit"}, always kilometres) as a number."""
    if not isinstance(distance, dict):
        return None
    try:
        return float(distance["value"])
    except (KeyError, TypeError, ValueError):
        return None


def _amount(amount: dict[str, Any] | None) -> Decimal | None:
    """Return a read API amount ({"amount": "12.34", "currency"}) as a Decimal."""
    if not isinstance(amount, dict):
        return None
    try:
        return Decimal(str(amount["amount"]))
    except (KeyError, InvalidOperation):
        return None


def _day(value: str | None) -> date | None:
    """Parse a YYYY-MM-DD day."""
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


class _VehicleReadSensor(
    ListedVehicleEntity, CoordinatorEntity[AutoLedgerDataUpdateCoordinator], SensorEntity
):
    """Base of the per-vehicle sensors fed by the read API."""

    _key: str
    _title: str

    def __init__(
        self,
        coordinator: AutoLedgerDataUpdateCoordinator,
        entry: ConfigEntry,
        vehicle_id: str,
        vehicle_name: str,
        device_info: DeviceInfo,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._vehicle_id = vehicle_id
        self._attr_name = f"{vehicle_name} {self._title}"
        self._attr_unique_id = f"{entry.entry_id}_{vehicle_id}_{self._key}"
        self._attr_device_info = device_info

    @property
    def _reminders(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        return data.get("reminders", {}).get(self._vehicle_id, {})

    @property
    def _summary(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        return data.get("summary", {}).get(self._vehicle_id, {})

    @property
    def _month(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        return data.get("monthly", {}).get(self._vehicle_id, {})


class AutoLedgerRemindersOverdueSensor(_VehicleReadSensor):
    """Number of maintenance reminders whose limit is passed."""

    _key = "reminders_overdue"
    _title = "Overdue Reminders"
    _attr_entity_registry_enabled_default = False
    _attr_icon = "mdi:wrench-clock"
    _status = "overdue"

    @property
    def native_value(self) -> int | None:
        """Return the number of reminders in this status."""
        value = self._reminders.get("overdue")
        return value if isinstance(value, int) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """List the reminders in this status."""
        return {
            "reminders": [
                {
                    "title": item.get("title"),
                    "category": item.get("category"),
                    "due_date": item.get("due_date"),
                    "remaining_days": item.get("remaining_days"),
                    "remaining_km": _distance_km(item.get("remaining_km")),
                }
                for item in self._reminders.get("reminders", [])
                if item.get("status") == self._status
            ]
        }


class AutoLedgerRemindersDueSoonSensor(AutoLedgerRemindersOverdueSensor):
    """Number of maintenance reminders inside their warning window."""

    _key = "reminders_due_soon"
    _title = "Reminders Due Soon"
    _attr_entity_registry_enabled_default = False
    _attr_icon = "mdi:wrench-outline"
    _status = "due_soon"

    @property
    def native_value(self) -> int | None:
        """Return the number of reminders due soon."""
        value = self._reminders.get("due_soon")
        return value if isinstance(value, int) else None


class AutoLedgerNextServiceDateSensor(_VehicleReadSensor):
    """Date of the closest maintenance reminder (past when it is overdue)."""

    _key = "next_service_date"
    _title = "Next Service Date"
    _attr_device_class = SensorDeviceClass.DATE

    def _closest(self) -> tuple[date, dict[str, Any]] | None:
        dated = [
            (day, item)
            for item in self._reminders.get("reminders", [])
            if (day := _day(item.get("due_date"))) is not None
        ]
        return min(dated, key=lambda pair: pair[0]) if dated else None

    @property
    def native_value(self) -> date | None:
        """Return the closest due date."""
        closest = self._closest()
        return closest[0] if closest else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Name the reminder the date comes from."""
        closest = self._closest()
        return {"reminder": closest[1].get("title")} if closest else {}


class AutoLedgerNextServiceDistanceSensor(_VehicleReadSensor):
    """Kilometres left before the closest distance-based reminder (negative once passed)."""

    _key = "next_service_distance"
    _title = "Next Service Distance"
    _attr_entity_registry_enabled_default = False
    _attr_device_class = SensorDeviceClass.DISTANCE
    _attr_native_unit_of_measurement = "km"
    _attr_suggested_display_precision = 0

    def _closest(self) -> tuple[float, dict[str, Any]] | None:
        measured = [
            (km, item)
            for item in self._reminders.get("reminders", [])
            if (km := _distance_km(item.get("remaining_km"))) is not None
        ]
        return min(measured, key=lambda pair: pair[0]) if measured else None

    @property
    def native_value(self) -> float | None:
        """Return the smallest remaining distance."""
        closest = self._closest()
        return closest[0] if closest else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Name the reminder the distance comes from."""
        closest = self._closest()
        return {"reminder": closest[1].get("title")} if closest else {}


class AutoLedgerMonthCostSensor(_VehicleReadSensor):
    """What the vehicle costs in the month in progress, all categories."""

    _key = "month_cost"
    _title = "Cost This Month"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_suggested_display_precision = 2

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return the currency of the vehicle."""
        total = self._month.get("costs", {}).get("total")
        return total.get("currency") if isinstance(total, dict) else None

    @property
    def native_value(self) -> Decimal | None:
        """Return the total of the month in progress."""
        return _amount(self._month.get("costs", {}).get("total"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Break the month down by category."""
        month = self._month
        if not month:
            return {}
        costs = month.get("costs", {})
        attrs: dict[str, Any] = {
            "month": month.get("month"),
            "in_progress": month.get("in_progress"),
        }
        for category in (
            "energy",
            "tolls",
            "maintenance",
            "insurance",
            "financing",
            "tires",
            "other",
        ):
            value = _amount(costs.get(category))
            attrs[category] = str(value) if value is not None else None
        return attrs


class AutoLedgerCostPerKmSensor(_VehicleReadSensor):
    """What a kilometre cost over the last twelve months, all categories."""

    _key = "cost_per_km"
    _title = "Cost Per Km"
    _attr_icon = "mdi:cash-fast"
    _attr_suggested_display_precision = 3

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return the currency of the vehicle per kilometre."""
        amount = self._summary.get("cost_per_km")
        currency = (
            amount.get("currency") if isinstance(amount, dict) else self._summary.get("currency")
        )
        return f"{currency}/km" if currency else None

    @property
    def native_value(self) -> Decimal | None:
        """Return the cost per km; unknown while the distance is."""
        return _amount(self._summary.get("cost_per_km"))


class AutoLedgerConsumptionKwhSensor(_VehicleReadSensor):
    """Electric energy used per 100 km."""

    _key = "consumption_kwh_100km"
    _title = "Consumption"
    _attr_icon = "mdi:lightning-bolt"
    _attr_native_unit_of_measurement = "kWh/100 km"
    _attr_suggested_display_precision = 1
    _field = "consumption_kwh_100km"

    @property
    def native_value(self) -> float | None:
        """Return the consumption; unknown without enough data."""
        value = self._summary.get(self._field)
        return float(value) if isinstance(value, (int, float)) else None


class AutoLedgerConsumptionFuelSensor(AutoLedgerConsumptionKwhSensor):
    """Fuel used per 100 km between full tanks."""

    _key = "consumption_l_100km"
    _title = "Fuel Consumption"
    _attr_icon = "mdi:gas-station"
    _attr_native_unit_of_measurement = "L/100 km"
    _field = "consumption_l_100km"


class AutoLedgerOdometerSensor(_VehicleReadSensor):
    """Odometer of the vehicle as AutoLedger holds it (the last reading it received)."""

    _key = "odometer"
    _title = "Odometer"
    _attr_device_class = SensorDeviceClass.DISTANCE
    _attr_native_unit_of_measurement = "km"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = 0

    @property
    def native_value(self) -> float | None:
        """Return the odometer; unknown while the server did not report one."""
        vehicle = (self.coordinator.data or {}).get("vehicles", {}).get(self._vehicle_id, {})
        value = vehicle.get("current_odometer")
        return float(value) if isinstance(value, (int, float)) and value > 0 else None


class AutoLedgerCompletenessSensor(_VehicleReadSensor):
    """How much of the vehicle's figures rests on recorded data."""

    _key = "completeness"
    _title = "Data Completeness"
    _attr_entity_registry_enabled_default = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:database-check-outline"
    _attr_native_unit_of_measurement = PERCENTAGE

    @property
    def native_value(self) -> int | None:
        """Return the score; unknown when the server has none."""
        value = self._summary.get("completeness_pct")
        return value if isinstance(value, int) else None


class AutoLedgerMonthDistanceSensor(_VehicleReadSensor):
    """Distance driven in the month in progress."""

    _key = "month_distance"
    _title = "Distance This Month"
    _attr_entity_registry_enabled_default = False
    _attr_device_class = SensorDeviceClass.DISTANCE
    _attr_native_unit_of_measurement = "km"
    _attr_suggested_display_precision = 0

    @property
    def native_value(self) -> float | None:
        """Return the distance; unknown on a server that does not report it."""
        return _distance_km(self._summary.get("month_distance"))


class _SummaryAmountSensor(_VehicleReadSensor):
    """A monetary figure of the summary, in the currency of the vehicle."""

    _attr_entity_registry_enabled_default = False
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_suggested_display_precision = 2
    _field: str

    def _amount_field(self) -> dict[str, Any] | None:
        value = self._summary.get(self._field)
        return value if isinstance(value, dict) else None

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return the currency of the vehicle."""
        amount = self._amount_field()
        return amount.get("currency") if amount else self._summary.get("currency")

    @property
    def native_value(self) -> Decimal | None:
        """Return the amount; unknown on a server that does not report it."""
        return _amount(self._amount_field())


class AutoLedgerLast12MonthsCostSensor(_SummaryAmountSensor):
    """Cash spent over the last twelve months, the figure behind the cost per km."""

    _key = "last_12_months_cost"
    _title = "Cost Last 12 Months"
    _field = "last_12_months_cost"


class AutoLedgerNetMonthCostSensor(_SummaryAmountSensor):
    """What the month costs once the passengers of the carpools have paid their share."""

    _key = "month_net"
    _title = "Net Cost This Month"
    _field = "month_net"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Give the carpool revenue the net figure subtracts."""
        revenue = _amount(self._summary.get("carpool_revenue_month"))
        return {"carpool_revenue": str(revenue)} if revenue is not None else {}


class AutoLedgerLastChargePricePerKwhSensor(_VehicleReadSensor):
    """What the latest charge cost per kWh."""

    _key = "last_charge_price_per_kwh"
    _title = "Last Charge Price Per kWh"
    _attr_entity_registry_enabled_default = False
    _attr_icon = "mdi:currency-eur"
    _attr_suggested_display_precision = 3

    def _price(self) -> dict[str, Any] | None:
        last = self._summary.get("last_charge")
        price = last.get("price_per_kwh") if isinstance(last, dict) else None
        return price if isinstance(price, dict) else None

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return the currency of the vehicle per kWh."""
        price = self._price()
        currency = price.get("currency") if price else self._summary.get("currency")
        return f"{currency}/kWh" if currency else None

    @property
    def native_value(self) -> Decimal | None:
        """Return the price; unknown while the cost of the charge is."""
        return _amount(self._price())


class _SectionSensor(_VehicleReadSensor):
    """A figure of an opt-in section of the summary (tires, battery, ownership, sources).

    The section is only read while one of its entities is enabled, so these are off by default.
    """

    _attr_entity_registry_enabled_default = False
    _section: str

    @property
    def _data(self) -> dict[str, Any]:
        section = self._summary.get(self._section)
        return section if isinstance(section, dict) else {}


class AutoLedgerTiresWearSensor(_SectionSensor):
    """Wear of the most worn tire mounted on the vehicle."""

    _key = "tires_wear"
    _title = "Tire Wear"
    _section = "tires"
    _attr_icon = "mdi:tire"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_suggested_display_precision = 0

    @property
    def native_value(self) -> float | None:
        """Return the wear; unknown while no tire is mounted or the server has no section."""
        value = self._data.get("worst_wear_pct")
        return (
            float(value) if isinstance(value, (int, float)) and self._data.get("mounted") else None
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Give the condition and the share of the lifespan driven."""
        data = self._data
        if not data:
            return {}
        return {
            "condition": data.get("worst_condition"),
            "life_used_pct": data.get("worst_life_used_pct"),
            "mounted": data.get("mounted"),
        }


class AutoLedgerTiresRemainingDistanceSensor(_SectionSensor):
    """Distance left before a mounted tire reaches the legal minimum."""

    _key = "tires_remaining_distance"
    _title = "Tire Distance Left"
    _section = "tires"
    _attr_icon = "mdi:tire"
    _attr_device_class = SensorDeviceClass.DISTANCE
    _attr_native_unit_of_measurement = "km"
    _attr_suggested_display_precision = 0

    @property
    def native_value(self) -> float | None:
        """Return the smallest distance left; unknown when it cannot be predicted."""
        return _distance_km(self._data.get("smallest_remaining_distance"))


class AutoLedgerTiresReplacementDateSensor(_SectionSensor):
    """Day the first mounted tire is expected to need replacing."""

    _key = "tires_replacement_date"
    _title = "Tire Replacement Date"
    _section = "tires"
    _attr_icon = "mdi:calendar-clock"
    _attr_device_class = SensorDeviceClass.DATE

    @property
    def native_value(self) -> date | None:
        """Return the day; unknown when no replacement can be predicted."""
        return _day(self._data.get("nearest_replacement_date"))


class AutoLedgerBatteryHealthSensor(_SectionSensor):
    """State of health of the traction battery."""

    _key = "battery_health"
    _title = "Battery Health"
    _section = "battery"
    _attr_icon = "mdi:battery-heart-variant"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    @property
    def native_value(self) -> float | None:
        """Return the latest health measure; unknown while the server has none."""
        value = self._data.get("latest_percent")
        return float(value) if isinstance(value, (int, float)) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Give the day of the measure and the capacities behind it."""
        data = self._data
        if not data:
            return {}
        return {
            "measured_on": data.get("latest_date"),
            "current_capacity_kwh": data.get("current_capacity_kwh"),
            "max_capacity_kwh": data.get("max_capacity_kwh"),
        }


class _SectionAmountSensor(_SectionSensor):
    """A monetary figure of an opt-in section, in the currency of the vehicle."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_suggested_display_precision = 2
    _field: str

    def _amount_field(self) -> dict[str, Any] | None:
        value = self._data.get(self._field)
        return value if isinstance(value, dict) else None

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return the currency of the vehicle."""
        amount = self._amount_field()
        return amount.get("currency") if amount else self._summary.get("currency")

    @property
    def native_value(self) -> Decimal | None:
        """Return the amount; unknown when the server cannot estimate it."""
        return _amount(self._amount_field())


class AutoLedgerOwnershipCostPerKmSensor(_SectionAmountSensor):
    """What a kilometre costs once the purchase is counted."""

    _key = "ownership_cost_per_km"
    _title = "Full Cost Per Km"
    _section = "ownership"
    _field = "full_cost_per_km"
    _attr_suggested_display_precision = 3

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return the currency of the vehicle per kilometre."""
        currency = super().native_unit_of_measurement
        return f"{currency}/km" if currency else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Say whether the depreciation is in the figure, and where a leased mileage stands."""
        data = self._data
        if not data:
            return {}
        attributes: dict[str, Any] = {"depreciation_known": data.get("depreciation_known")}
        if isinstance(data.get("mileage"), dict):
            attributes["mileage"] = data["mileage"]
        return attributes


class AutoLedgerVehicleValueSensor(_SectionAmountSensor):
    """Estimated resale value of the vehicle today."""

    _key = "vehicle_value"
    _title = "Estimated Value"
    _section = "ownership"
    _field = "current_value"


class AutoLedgerLeaseMileageDrivenSensor(_SectionSensor):
    """Distance driven since the start of a lease, against the allowance elapsed so far.

    Unknown for a vehicle whose contract has no mileage allowance.
    """

    _key = "lease_mileage_driven"
    _title = "Lease Distance Driven"
    _section = "ownership"
    _attr_device_class = SensorDeviceClass.DISTANCE
    _attr_native_unit_of_measurement = "km"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = 0

    @property
    def _mileage(self) -> dict[str, Any]:
        mileage = self._data.get("mileage")
        return mileage if isinstance(mileage, dict) else {}

    @property
    def native_value(self) -> float | None:
        """Return the distance driven under the contract."""
        return _distance_km(self._mileage.get("driven"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Give the allowance and how far over (positive) or under (negative) the pace is."""
        mileage = self._mileage
        if not mileage:
            return {}
        driven = _distance_km(mileage.get("driven"))
        to_date = _distance_km(mileage.get("allowance_to_date"))
        return {
            "allowance_to_date": to_date,
            "allowance_total": _distance_km(mileage.get("allowance_total")),
            "allowance_per_year": _distance_km(mileage.get("allowance_per_year")),
            "over_allowance": (
                round(driven - to_date, 1) if driven is not None and to_date is not None else None
            ),
        }


class AutoLedgerLeaseExcessProjectedSensor(_SectionAmountSensor):
    """What the kilometres above the allowance will cost at the end of the lease at this pace."""

    _key = "lease_excess_projected"
    _title = "Lease Projected Excess"
    _section = "ownership"
    _attr_suggested_display_precision = 2

    def _amount_field(self) -> dict[str, Any] | None:
        mileage = self._data.get("mileage")
        value = mileage.get("excess_projected") if isinstance(mileage, dict) else None
        return value if isinstance(value, dict) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Give what the excess already costs and the price charged per kilometre."""
        mileage = self._data.get("mileage")
        if not isinstance(mileage, dict):
            return {}
        accrued, price = mileage.get("excess_accrued"), mileage.get("excess_price_per_km")
        return {
            "excess_accrued": _amount(accrued),
            "excess_price_per_km": _amount(price),
        }


class _SectionTimestampSensor(_SectionSensor):
    """An instant of the "sources" section."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _field: str

    @property
    def native_value(self) -> datetime | None:
        """Return the instant; unknown when the server has none."""
        value = self._data.get(self._field)
        return dt_util.parse_datetime(value) if isinstance(value, str) else None


class AutoLedgerLastRecordSensor(_SectionTimestampSensor):
    """Most recent record of the vehicle, whoever wrote it."""

    _key = "last_record"
    _title = "Last Record"
    _section = "sources"
    _field = "last_record"


class AutoLedgerTeslaMateLastSyncSensor(_SectionTimestampSensor):
    """Last successful synchronization with the TeslaMate instance of the vehicle."""

    _key = "teslamate_last_sync"
    _title = "TeslaMate Last Sync"
    _section = "sources"
    _field = "last_sync"


class AutoLedgerPendingChargesSensor(
    CoordinatorEntity[AutoLedgerDataUpdateCoordinator], SensorEntity
):
    """Charging sessions pushed to the account that no vehicle claimed yet."""

    _attr_icon = "mdi:ev-plug-type2"
    _attr_name = "AutoLedger Charges To Qualify"

    def __init__(self, coordinator: AutoLedgerDataUpdateCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_pending_charges"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="AutoLedger",
            manufacturer="AutoLedger",
            model="Account",
            sw_version=(coordinator.data or {}).get("account", {}).get("server_version"),
        )

    @property
    def native_value(self) -> int | None:
        """Return the number of charging sessions waiting for a vehicle."""
        value = (self.coordinator.data or {}).get("account", {}).get("pending_charges")
        return value if isinstance(value, int) else None


class _TariffSensor(CoordinatorEntity[AutoLedgerDataUpdateCoordinator], SensorEntity):
    """A figure of the electricity price of the account (its default plan)."""

    _key: str
    _title: str

    def __init__(self, coordinator: AutoLedgerDataUpdateCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_name = f"AutoLedger {self._title}"
        self._attr_unique_id = f"{entry.entry_id}_{self._key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="AutoLedger",
            manufacturer="AutoLedger",
            model="Account",
        )

    @property
    def _tariff(self) -> dict[str, Any]:
        tariff = (self.coordinator.data or {}).get("tariff")
        return tariff if isinstance(tariff, dict) else {}

    @property
    def _now(self) -> dict[str, Any]:
        now = self._tariff.get("now")
        return now if isinstance(now, dict) else {}

    @property
    def _next(self) -> dict[str, Any]:
        change = self._tariff.get("next_change")
        return change if isinstance(change, dict) else {}


class _TariffPriceSensor(_TariffSensor):
    """A price per kWh, in the currency of the plan."""

    _attr_suggested_display_precision = 4

    def _price(self) -> dict[str, Any] | None:
        raise NotImplementedError

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return the currency of the plan per kWh."""
        price = self._price()
        currency = price.get("currency") if price else None
        return f"{currency}/kWh" if currency else None

    @property
    def native_value(self) -> Decimal | None:
        """Return the price; unknown when no plan prices the moment."""
        return _amount(self._price())


class AutoLedgerTariffPriceSensor(_TariffPriceSensor):
    """What a kWh costs at home right now."""

    _key = "tariff_price"
    _title = "Electricity Price"
    _attr_icon = "mdi:flash"

    def _price(self) -> dict[str, Any] | None:
        price = self._now.get("price_per_kwh")
        return price if isinstance(price, dict) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Give the plan the price comes from, and why there is none when it is unknown."""
        plan = self._tariff.get("plan")
        attributes: dict[str, Any] = {"plan": plan.get("name")} if isinstance(plan, dict) else {}
        if self._tariff.get("unpriced_reason"):
            attributes["unpriced_reason"] = self._tariff["unpriced_reason"]
        return attributes


class AutoLedgerTariffBandSensor(_TariffSensor):
    """Name of the band in force."""

    _key = "tariff_band"
    _title = "Electricity Band"
    _attr_entity_registry_enabled_default = False
    _attr_icon = "mdi:clock-outline"

    @property
    def native_value(self) -> str | None:
        """Return the band; unknown when no plan prices the moment."""
        band = self._now.get("band")
        return band if isinstance(band, str) else None


class AutoLedgerTariffNextChangeSensor(_TariffSensor):
    """When the price changes next."""

    _key = "tariff_next_change"
    _title = "Electricity Price Next Change"
    _attr_entity_registry_enabled_default = False
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self) -> datetime | None:
        """Return the instant; unknown for a flat plan."""
        at = self._next.get("at")
        return dt_util.parse_datetime(at) if isinstance(at, str) else None


class AutoLedgerTariffNextPriceSensor(_TariffPriceSensor):
    """The price from the next change on."""

    _key = "tariff_next_price"
    _title = "Electricity Next Price"
    _attr_entity_registry_enabled_default = False
    _attr_icon = "mdi:flash-outline"

    def _price(self) -> dict[str, Any] | None:
        price = self._next.get("price_per_kwh")
        return price if isinstance(price, dict) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Give the band that starts at the change."""
        band = self._next.get("band")
        return {"band": band} if band else {}


class _FleetEntity(CoordinatorEntity[AutoLedgerDataUpdateCoordinator]):
    """An entity of the household: all the vehicles of the account together."""

    _attr_entity_registry_enabled_default = False
    _key: str
    _title: str

    def __init__(self, coordinator: AutoLedgerDataUpdateCoordinator, entry: ConfigEntry) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._attr_name = f"AutoLedger {self._title}"
        self._attr_unique_id = f"{entry.entry_id}_{self._key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="AutoLedger",
            manufacturer="AutoLedger",
            model="Account",
        )

    @property
    def _fleet(self) -> dict[str, Any]:
        fleet = (self.coordinator.data or {}).get("fleet")
        return fleet if isinstance(fleet, dict) else {}

    @property
    def _budget(self) -> dict[str, Any] | None:
        budget = self._fleet.get("budget")
        return budget if isinstance(budget, dict) else None


class AutoLedgerFleetMonthCostSensor(_FleetEntity, SensorEntity):
    """What the vehicles of the fleet currency cost so far this month."""

    _key = "fleet_month_cost"
    _title = "Household Cost This Month"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_suggested_display_precision = 2

    def _total(self) -> dict[str, Any] | None:
        currency = self._fleet.get("fleet_currency")
        for total in self._fleet.get("current_month_totals") or []:
            if isinstance(total, dict) and total.get("currency") == currency:
                return total
        return None

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return the fleet currency."""
        return self._fleet.get("fleet_currency")

    @property
    def native_value(self) -> Decimal | None:
        """Return the cost; unknown while the server has not answered."""
        total = self._total()
        return _amount(total.get("current_month_cost")) if total else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Count the vehicles, and name the currencies left out of the sum."""
        total = self._total()
        if total is None:
            return {}
        return {
            "vehicles": total.get("vehicles"),
            "excluded_currencies": [
                other.get("currency")
                for other in self._fleet.get("current_month_totals") or []
                if isinstance(other, dict) and other is not total
            ],
        }


class AutoLedgerBudgetUsedSensor(_FleetEntity, SensorEntity):
    """Share of the monthly budget already spent."""

    _key = "fleet_budget_used"
    _title = "Budget Used"
    _attr_icon = "mdi:piggy-bank-outline"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_suggested_display_precision = 0

    @property
    def native_value(self) -> float | None:
        """Return the percentage; unknown when no budget is set."""
        budget = self._budget
        value = budget.get("used_pct") if budget else None
        return float(value) if isinstance(value, (int, float)) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Give the target and what is left of it."""
        budget = self._budget
        if not budget:
            return {}
        return {
            "monthly": str(_amount(budget.get("monthly"))),
            "spent": str(_amount(budget.get("spent_this_month"))),
            "remaining": str(_amount(budget.get("remaining"))),
        }
