"""Home Assistant MQTT discovery (retained configs, stable unique_ids).

Entity names deliberately duplicate the list used by the historic readers so
that every unique_id is stable across program versions.
"""

from . import config, mqtt


def availability_fields(address):
    """Availability (LWT) fields shared by every discovered entity.

    Home Assistant marks an entity *unavailable* as soon as the retained
    availability topic carries the "not available" payload - which is what
    the broker publishes (via the client's last will) when the logger dies.
    """
    return {
        "availability_topic":    config.availability_topic(address),
        "payload_available":     config.payload_available(),
        "payload_not_available": config.payload_not_available(),
    }


def ha_device(address):
    """Return the HA device dict - matching the historic unique-id scheme
    so that HA recognises the same entities across program versions."""
    return {
        "identifiers":  [f"dtsu666_{address}"],
        "name":         f"CHINT DTSU666 #{address}",
        "manufacturer": "CHINT",
        "model":        "DTSU666",
    }


# Each entry = (json_path_prefix, unit, device_class).
#   json_path_prefix -> where in the actual MQTT payload the value lives:
#     "data"          -> payload["data"]["Pt"]  (17 main electrical)
#     "power_factor"  -> payload["power_factor"]["PFt"]  (4 PF)
#     "frequency"     -> payload["frequency"]["Freq"]     (1 Freq)
_HA_ELECTRICAL = {
    "Pt":   ("data",          "W",    "power"),
    "Pa":   ("data",          "W",    "power"),
    "Pb":   ("data",          "W",    "power"),
    "Pc":   ("data",          "W",    "power"),
    "Ua":   ("data",          "V",    "voltage"),
    "Ub":   ("data",          "V",    "voltage"),
    "Uc":   ("data",          "V",    "voltage"),
    "Uab":  ("data",          "V",    "voltage"),
    "Ubc":  ("data",          "V",    "voltage"),
    "Uca":  ("data",          "V",    "voltage"),
    "Ia":   ("data",          "A",    "current"),
    "Ib":   ("data",          "A",    "current"),
    "Ic":   ("data",          "A",    "current"),
    "Qt":   ("data",          "var",  None),
    "Qa":   ("data",          "var",  None),
    "Qb":   ("data",          "var",  None),
    "Qc":   ("data",          "var",  None),
    "PFt":  ("power_factor",  "",     "power_factor"),
    "PFa":  ("power_factor",  "",     "power_factor"),
    "PFb":  ("power_factor",  "",     "power_factor"),
    "PFc":  ("power_factor",  "",     "power_factor"),
    "Freq": ("frequency",     "Hz",   "frequency"),
}

_HA_ENERGY_NAMES = [
    "ImpEp", "ExpEp",
    "ImpEpA", "ImpEpB", "ImpEpC",
    "ExpEpA", "ExpEpB", "ExpEpC",
    "NetImpEp", "NetExpEp",
]

_HA_PERIODS = [
    "current_day", "previous_day",
    "current_week", "previous_week",
    "current_month", "previous_month",
]

_HA_PERIOD_FIELDS = [
    "total_kwh", "vt_kwh", "nt_kwh",
    "vt_corrected", "nt_corrected",
]  # absolute_kwh omitted - redundant with the main ImpEp sensor


def publish_ha_discovery(address):
    """Publish retained HA MQTT discovery configs for one meter.
    unique_id values match the historic scheme - do not change them."""
    base = mqtt._topic_base(address)
    device = ha_device(address)

    # ---- operational status sensor (authoritative; replaces Connection) ----
    # Purge the legacy Connection binary_sensor from any existing install.
    mqtt.mqtt_publish(
        f"homeassistant/binary_sensor/dtsu666_{address}_connection/config",
        "", retain=True)
    status_uid = f"dtsu666_{address}_status"
    mqtt.mqtt_publish(f"homeassistant/sensor/{status_uid}/config", {
        "name":               "Status",
        "state_topic":        f"{base}/energy",
        "value_template":     "{{ value_json.status }}",
        "unique_id":          status_uid,
        "default_entity_id":  f"sensor.chint_dtsu666_{address}_status",
        "device":             device,
        **availability_fields(address),
    }, retain=True)

    # ---- system sensors ----------------------------------------
    sys_names = [
        ("dtsu666_{address}_date",   "System date",    "{{ value_json.date }}",
         "system_date"),
        ("dtsu666_{address}_time",   "System time",    "{{ value_json.time }}",
         "system_time"),
        ("dtsu666_{address}_tariff", "Current tariff", "{{ value_json.tariff }}",
         "current_tariff"),
    ]
    for uid_template, label, tmpl, oid_part in sys_names:
        uid = uid_template.format(address=address)
        mqtt.mqtt_publish(f"homeassistant/sensor/{uid}/config", {
            "name": label, "state_topic": f"{base}/system",
            "value_template": tmpl, "unique_id": uid,
            "default_entity_id": f"sensor.chint_dtsu666_{address}_{oid_part}",
            "device": device,
            **availability_fields(address),
        }, retain=True)

    # ---- period sensors ----------------------------------------
    period_titles = {
        "current_day": "Today", "previous_day": "Yesterday",
        "current_week": "Current Week", "previous_week": "Previous Week",
        "current_month": "Current Month", "previous_month": "Previous Month",
    }
    field_titles = {"total_kwh": "Total", "vt_kwh": "VT", "nt_kwh": "NT"}
    period_order = ["current_day", "current_week", "current_month",
                    "previous_day", "previous_week", "previous_month"]
    corrected_fields = {"vt_corrected": ("VT corr.", "vt_corrected"),
                        "nt_corrected": ("NT corr.", "nt_corrected")}
    period_oid = {"current_day": "today", "previous_day": "yesterday",
                  "current_week": "current_week", "previous_week": "previous_week",
                  "current_month": "current_month", "previous_month": "previous_month"}
    field_oid = {"total_kwh": "total", "vt_kwh": "vt", "nt_kwh": "nt",
                 "vt_corrected": "vt_corr", "nt_corrected": "nt_corr"}
    for period in period_order:
        for field in _HA_PERIOD_FIELDS:
            if field.startswith(("vt_corrected", "nt_corrected")):
                if period != "current_day":
                    continue  # corrected only on current_day
                label, _ = corrected_fields[field]
                tmpl_field = field
            else:
                label = field_titles[field]
                tmpl_field = field
            uid = f"dtsu666_{address}_period_{period}_{tmpl_field}"
            oid_part = f"{period_oid[period]}_{field_oid[tmpl_field]}"
            mqtt.mqtt_publish(f"homeassistant/sensor/{uid}/config", {
                "name":               f"{period_titles[period]} {label}",
                "state_topic":        f"{base}/energy",
                "value_template":     f"{{{{ value_json.periods.{period}.{tmpl_field} }}}}",
                "unit_of_measurement": "kWh",
                "device_class":       "energy",
                "state_class":        "measurement",
                "unique_id":          uid,
                "default_entity_id":  f"sensor.chint_dtsu666_{address}_{oid_part}",
                "device":             device,
                **availability_fields(address),
            }, retain=True)

    # ---- energy sensors ----------------------------------------
    energy_names = {
        "ImpEp": "Imported Total", "ExpEp": "Exported Total",
        "ExpEpA": "ExpEpA", "ExpEpB": "ExpEpB", "ExpEpC": "ExpEpC",
        "NetExpEp": "NetExpEp",
        "ImpEpA": "ImpEpA", "ImpEpB": "ImpEpB", "ImpEpC": "ImpEpC",
        "NetImpEp": "NetImpEp",
    }
    energy_oid = {
        "ImpEp":    "total_forward_active_energy",
        "ExpEp":    "total_reverse_active_energy",
        "ImpEpA":   "phase_a_forward_active_energy",
        "ImpEpB":   "phase_b_forward_active_energy",
        "ImpEpC":   "phase_c_forward_active_energy",
        "ExpEpA":   "phase_a_reverse_active_energy",
        "ExpEpB":   "phase_b_reverse_active_energy",
        "ExpEpC":   "phase_c_reverse_active_energy",
        "NetImpEp": "net_forward_active_energy",
        "NetExpEp": "net_reverse_active_energy",
    }
    for name in ("ImpEp", "ExpEp", "ExpEpA", "ExpEpB", "ExpEpC",
                 "NetExpEp", "ImpEpA", "ImpEpB", "ImpEpC", "NetImpEp"):
        uid = f"dtsu666_{address}_{name.lower()}"
        mqtt.mqtt_publish(f"homeassistant/sensor/{uid}/config", {
            "name":               energy_names[name],
            "state_topic":        f"{base}/energy",
            "value_template":     f"{{{{ value_json.energy.{name} }}}}",
            "unit_of_measurement": "kWh",
            "device_class":       "energy",
            "state_class":        "total_increasing",
            "unique_id":          uid,
            "default_entity_id":  f"sensor.chint_dtsu666_{address}_{energy_oid[name]}",
            "device":             device,
            **availability_fields(address),
        }, retain=True)

    # ---- electrical sensors ------------------------------------
    el_names = {
        "Ua": "Voltage L1", "Ub": "Voltage L2", "Uc": "Voltage L3",
        "Uab": "Voltage L1-L2", "Ubc": "Voltage L2-L3", "Uca": "Voltage L3-L1",
        "Ia": "Current L1", "Ib": "Current L2", "Ic": "Current L3",
        "Pa": "Power L1", "Pb": "Power L2", "Pc": "Power L3",
        "Pt": "Power Total", "Freq": "Line frequency",
        "Qa": "Qa", "Qb": "Qb", "Qc": "Qc", "Qt": "Qt",
        "PFa": "PFa", "PFb": "PFb", "PFc": "PFc", "PFt": "PFt",
    }
    el_oid = {
        "Ua": "phase_a_voltage", "Ub": "phase_b_voltage", "Uc": "phase_c_voltage",
        "Uab": "line_voltage_a_b", "Ubc": "line_voltage_b_c", "Uca": "line_voltage_c_a",
        "Ia": "phase_a_current", "Ib": "phase_b_current", "Ic": "phase_c_current",
        "Pa": "phase_a_active_power", "Pb": "phase_b_active_power",
        "Pc": "phase_c_active_power", "Pt": "total_active_power",
        "Freq": "frequency",
        "Qa": "phase_a_reactive_power", "Qb": "phase_b_reactive_power",
        "Qc": "phase_c_reactive_power", "Qt": "total_reactive_power",
        "PFa": "phase_a_power_factor", "PFb": "phase_b_power_factor",
        "PFc": "phase_c_power_factor", "PFt": "total_power_factor",
    }
    el_order = ["Ua","Ub","Uc","Uab","Ubc","Uca",
                "Ia","Ib","Ic","Pa","Pb","Pc","Pt","Freq",
                "Qa","Qb","Qc","Qt","PFa","PFb","PFc","PFt"]
    for name in el_order:
        path, unit, dev_class = _HA_ELECTRICAL[name]
        uid = f"dtsu666_{address}_{name.lower()}"
        cfg = {
            "name":               el_names[name],
            "state_topic":        f"{base}/electrical",
            "value_template":     f"{{{{ value_json.{path}.{name} }}}}",
            "unit_of_measurement": unit,
            "unique_id":          uid,
            "default_entity_id":  f"sensor.chint_dtsu666_{address}_{el_oid[name]}",
            "device":             device,
            **availability_fields(address),
        }
        if dev_class:
            cfg["device_class"] = dev_class
        mqtt.mqtt_publish(f"homeassistant/sensor/{uid}/config", cfg, retain=True)

    # ---- cost sensors (ElectricityCost device) ------------------
    cost_device = {
        "identifiers":  ["electricity_cost"],
        "name":         "ElectricityCost",
        "manufacturer": "HEP",
        "model":        "Tariff",
    }
    cost_periods = [
        ("current_day",     "Cost Today"),
        ("previous_day",    "Cost Yesterday"),
        ("current_week",    "Cost Current Week"),
        ("previous_week",   "Cost Last Week"),
        ("current_month",   "Cost Current Month"),
        ("previous_month",  "Cost Last Month"),
        ("last_6_months",   "Cost last 6 months"),
        ("last_year",       "Cost last year"),
    ]
    for short, label in cost_periods:
        uid = f"cost_{address}_{short}"
        mqtt.mqtt_publish(f"homeassistant/sensor/{uid}/config", {
            "name":                label,
            "state_topic":         f"Electricity/cost/{address}",
            "value_template":      f"{{{{ value_json.periods.{short}.total }}}}",
            "unit_of_measurement": "EUR",
            "device_class":        "monetary",
            "state_class":         "measurement",
            "unique_id":           uid,
            "device":              cost_device,
            **availability_fields(address),
        }, retain=True)
