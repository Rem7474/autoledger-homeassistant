# AutoLedger - Home Assistant Integration

[![Hassfest](https://github.com/Rem7474/autoledger-homeassistant/actions/workflows/validate.yml/badge.svg)](https://github.com/Rem7474/autoledger-homeassistant/actions/workflows/validate.yml)
[![Tests](https://github.com/Rem7474/autoledger-homeassistant/actions/workflows/tests.yml/badge.svg)](https://github.com/Rem7474/autoledger-homeassistant/actions/workflows/tests.yml)
[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/default)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Official Home Assistant custom component integration for [AutoLedger](https://github.com/Rem7474/AutoLedger).

Connect your Home Assistant instance to AutoLedger to automatically detect EV charging sessions, calculate energy and financial analytics, handle dynamic solar anti-bounce interruptions, and view key vehicle metrics directly on your Lovelace dashboards.

---

## ⚡ Key Features

- **Multi-Vehicle Support**: Associate as many vehicles from your AutoLedger instance as needed.
- **Assisted HA Device Discovery**: Select your car device (Tesla, MG iSmart, Renault, OBD-II, etc.) and let AutoLedger auto-detect battery SoC, odometer, and charging state entities.
- **Independent Charger & Sub-meter Support**: Link dedicated wallboxes or energy monitors (Wallbox, Easee, Shelly Pro EM, Zaptec, etc.). Supports both **Total Increasing (cumulative kWh)** and **Session Energy** counter modes.
- **Dynamic Solar Anti-Bounce Debounce Engine**: EV charging on solar power frequently pauses due to passing clouds or household load shedding. The built-in configurable debounce timer (15s - 300s, default 60s) prevents fragmented micro-sessions by consolidating them into a single cohesive charging session.
- **Financial & Efficiency Sensors**:
  - `sensor.<vehicle>_last_charge_cost`: Monetary cost of the last charging session.
  - `sensor.<vehicle>_cost_per_100km`: Smoothed average financial efficiency (€/100km or currency/100km).
  - `sensor.<vehicle>_sync_status`: Synchronization status (`ok`, `pending`, `error`).
  - `sensor.<vehicle>_charging_state`: Real-time state (`idle`, `charging`, `cooling_down`).
- **Native HA Services**:
  - `autoledger.sync`: Force an immediate synchronization with the AutoLedger server.
  - `autoledger.submit_charge`: Manually log or automate charging session submissions from HA scripts.

---

## 🏗️ Architecture

```mermaid
flowchart TD
    subgraph HomeAssistant["Home Assistant Instance"]
        subgraph LocalEntities["Local HA Telemetry & Meters"]
            CarBattery["Battery SoC (%)"]
            CarOdo["Odometer (km)"]
            CarPlug["Charging / Cable Status"]
            WallboxEnergy["Energy Meter (kWh)"]
        end

        subgraph Integration["Custom Component: autoledger"]
            ConfigFlow["Config & Options Flow"]
            StateTracker["Charge Session Tracker (Debounce 60s)"]
            Coordinator["AutoLedgerDataUpdateCoordinator (15m Polling)"]
            APIClient["AutoLedgerApiClient (aiohttp)"]
            Sensors["Exposed Sensors (Cost, Efficiency, Status)"]
        end
    end

    subgraph Backend["AutoLedger Server (Rem7474/AutoLedger)"]
        APIHealth["GET /api/health or /api/vehicles"]
        APIEvents["POST /api/integrations/homeassistant/event"]
        APIVehicles["GET /api/vehicles/{id}"]
    end

    LocalEntities -->|State Change Event| StateTracker
    StateTracker -->|Charging Session Complete| APIClient
    Coordinator -->|Data Refresh| APIClient
    APIClient -->|Bearer Token HTTP| Backend
    Backend -->|Financial Metrics JSON| Coordinator
    Coordinator -->|Update State| Sensors
```

---

## 📦 Installation

### Option 1: Via HACS (Recommended)

1. Ensure [HACS (Home Assistant Community Store)](https://hacs.xyz/) is installed.
2. In Home Assistant, open **HACS** > **Integrations**.
3. Click the three dots icon `⋮` in the top right corner and select **Custom repositories**.
4. Add the repository:
   - **Repository**: `https://github.com/Rem7474/autoledger-homeassistant`
   - **Type**: `Integration`
5. Click **Add**, locate **AutoLedger** in the integration list, and click **Download**.
6. Restart Home Assistant.

### Option 2: Manual Installation

1. Download the latest release `.zip` from GitHub.
2. Extract and copy the `custom_components/autoledger` directory into your Home Assistant `config/custom_components/` directory.
3. Restart Home Assistant.

---

## ⚙️ Configuration

### 1. Initial Connection (`ConfigEntry`)

1. In Home Assistant, navigate to **Settings** > **Devices & Services**.
2. Click **+ Add Integration** and search for **AutoLedger**.
3. Fill in your server details:
   - **Server URL**: The base URL of your AutoLedger instance (e.g., `http://192.168.1.100:8080` or `https://autoledger.yourdomain.com`).
   - **API Token**: Your AutoLedger Personal Access Token or Bearer Token.
   - **Verify SSL Certificate**: Enable or disable according to your local setup (useful if using self-signed internal certificates).
4. Click **Submit**. The integration validates the connection immediately.

### 2. Vehicle Mapping (`OptionsFlow`)

Once the integration is created, click **Configure** on the AutoLedger integration card:

1. Select **Add a vehicle mapping** from the menu.
2. Choose your vehicle from the list synchronized from your AutoLedger server.
3. *(Optional)* Select an associated **Home Assistant Device** (e.g. your vehicle's device from the Tesla or MG integration) to automatically pre-fill entity fields.
4. Verify or adjust the entities:
   - **Charging Status Sensor** *(Required)*: Binary sensor or sensor indicating when the car is charging (e.g. `binary_sensor.my_car_charging`).
   - **Battery State of Charge (%)**: Battery level sensor (e.g. `sensor.my_car_battery_level`).
   - **Vehicle Odometer (km)**: Odometer distance sensor (e.g. `sensor.my_car_odometer`).
   - **Charging Energy Sensor (kWh)**: Energy sensor from your wallbox or sub-meter (e.g. `sensor.shelly_em_charging_energy`).
   - **Energy Sensor Counter Mode**:
     - `Total Increasing (Cumulative kWh)`: For counters that accumulate total energy over time.
     - `Session Energy (Resets per charge)`: For meters that reset to 0 at the start of each charge.
   - **Default Location Tag**: Default location string sent to AutoLedger (default: `home`).
   - **Solar Anti-Bounce Debounce (seconds)**: Delay timer to wait before finalizing a session (adjustable between 15s and 300s, default: 60s).

---

## ☀️ Solar Anti-Bounce Debounce Explained

Charging electric vehicles with solar surplus (PV diversion) often involves temporary interruptions when clouds pass over solar arrays or household appliances (ovens, heat pumps) kick in.

Standard energy loggers often register multiple tiny sessions of a few minutes, cluttering logs and distorting statistics. AutoLedger solves this:

1. **Charge Started**: The car enters charging state. Initial odometer, initial SoC, and initial energy counter reading are recorded.
2. **Temporary Pause / Cloud**: When charging drops to `idle` / `off`, the debounce timer arms (e.g., 60 seconds) without closing the session. The status changes to `cooling_down`.
3. **Resumption**: If charging resumes before the timer expires, the timer is aborted and the session continues seamlessly.
4. **Completion**: If the timer expires or the cable is disconnected, the total energy added (`energy_final - energy_start`) and final SoC are computed and transmitted to AutoLedger in a single event.

---

## 📊 Exposed Sensors

Each configured vehicle creates a dedicated Device in Home Assistant with the following entities:

| Sensor Entity ID | Device Class | Unit | Description |
|---|---|---|---|
| `sensor.<vehicle>_last_charge_cost` | `monetary` | `€` / `$` | Total financial cost of the last charging session. |
| `sensor.<vehicle>_cost_per_100km` | - | `€/100km` | Smoothed average operating energy cost per 100 km calculated by AutoLedger. |
| `sensor.<vehicle>_sync_status` | - | - | Synchronization status: `ok`, `pending`, or `error`. |
| `sensor.<vehicle>_charging_state` | - | - | Real-time state: `idle`, `charging`, or `cooling_down` (debouncing). |

---

## 🛠️ Home Assistant Services

### Service `autoledger.sync`
Force an immediate metrics refresh from your AutoLedger backend.

```yaml
action: autoledger.sync
data:
  entry_id: "optional_config_entry_id"
```

### Service `autoledger.submit_charge`
Manually push a charging session or trigger submissions via external automations / NFC tags.

```yaml
action: autoledger.submit_charge
data:
  vehicle_id: "c1f7a4b0-9b3e-4b2a-8921-729c4ef76a01"
  energy_kwh: 32.45
  cost: 7.50
  odometer_km: 45120
  soc_start: 20
  soc_end: 80
  location: "home"
```

---

## 🎨 Lovelace Dashboard Examples

### Example: Vehicle Summary Card (Mushroom Cards)

```yaml
type: vertical-stack
cards:
  - type: custom:mushroom-title-card
    title: Tesla Model Y
    subtitle: AutoLedger Financial & Energy Tracking

  - type: horizontal-stack
    cards:
      - type: custom:mushroom-entity-card
        entity: sensor.tesla_model_y_last_charge_cost
        name: Last Charge
        icon: mdi:ev-station
        icon_color: green

      - type: custom:mushroom-entity-card
        entity: sensor.tesla_model_y_cost_per_100km
        name: Cost / 100km
        icon: mdi:cash-multiple
        icon_color: blue

  - type: custom:mushroom-entity-card
    entity: sensor.tesla_model_y_charging_state
    name: State
    icon: mdi:lightning-bolt
```

### Example: Standard Entities Card

```yaml
type: entities
title: AutoLedger EV Status
entities:
  - entity: sensor.tesla_model_y_charging_state
    name: Charging State
  - entity: sensor.tesla_model_y_last_charge_cost
    name: Last Session Cost
  - entity: sensor.tesla_model_y_cost_per_100km
    name: Average Cost / 100km
  - entity: sensor.tesla_model_y_sync_status
    name: Sync Status
```

---

## 🧪 Testing & Validation

The codebase includes an automated unit test suite with high coverage:

```bash
# Run tests
pytest
```

---

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
