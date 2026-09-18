"""Startup lifecycle: meter initialisation and the first full read."""

from . import commissioning
from .cost import publish_cost
from .database import create_initial_meter, get_active_meter, record_energy
from .modbus import (
    float32,
    read_basic,
    read_electrical,
    read_energy,
    read_frequency,
    read_power_factor,
    zero_electrical,
)
from .publishing import (
    energy_flat,
    publish_electrical,
    publish_energy,
    publish_energy_disconnected,
    publish_meter,
    publish_system,
)
from .time_utils import now_local


def initialize_meter(db, modbus, address):
    """Return the active meter row, creating it if this is a first run.

    Does NOT store a baseline reading - that is done by initial_read.
    """
    meter = get_active_meter(db, address)
    if meter is not None:
        return meter

    print(f"\n[Meter {address}] No instance found. Reading initial ImpEp …")
    registers = modbus.read_registers(address, 0x101E, 2)
    initial = float32(registers, 0)
    meter_id = create_initial_meter(db, address, initial)
    meter = db.execute(
        "SELECT * FROM meters WHERE id = ?", (meter_id,)
    ).fetchone()
    print(f"[Meter {address}] Created {meter['instance_name']}")
    print(f"[Meter {address}] Initial ImpEp = {initial:.3f} kWh")
    return meter


def initial_read(db, modbus, meter):
    """Startup read: basic, electrical, energy -> publish everything."""
    addr = meter["modbus_address"]

    # --- basic registers (meter topic, retained) ---
    try:
        basic = read_basic(modbus, addr)
        publish_meter(meter, basic, True)
    except Exception as exc:  # noqa: BLE001
        print(f"[{addr}] Basic read failed: {exc}")
        publish_meter(meter, {}, False)

    # --- electrical ---
    try:
        elec = read_electrical(modbus, addr)
        pf = read_power_factor(modbus, addr)
        freq = read_frequency(modbus, addr)
        publish_electrical(meter, elec, True, pf, freq)
    except Exception as exc:  # noqa: BLE001
        print(f"[{addr}] Electrical read failed: {exc}")
        publish_electrical(meter, zero_electrical(), False)

    # --- energy ---
    ts = now_local()
    try:
        energy = read_energy(modbus, addr)
        abs_kwh = energy["ImpEp"]["value"]
        accounting = record_energy(db, meter, ts, abs_kwh)
        # compute the state AFTER recording so the first publish reflects the
        # fresh reading (not the stale pre-restart state)
        state = commissioning.operational_state(db, meter["id"], ts)
        publish_energy(meter, abs_kwh, accounting, db,
                       energy_values=energy_flat(energy), state=state)
        publish_system(meter, db, state); publish_cost(meter, db)
    except Exception as exc:  # noqa: BLE001
        print(f"[{addr}] Energy read failed: {exc}")
        publish_energy_disconnected(meter, db, "disconnected")
