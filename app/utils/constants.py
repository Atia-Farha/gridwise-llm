"""Shared constants."""

ALLOWED_DIRECTIVE_TYPES = {
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
}

ALLOWED_BATTERY_ACTIONS = {"charge", "discharge", "idle"}

NUMERIC_TOLERANCE = 0.01  # kWh / BDT — per spec §11.5
LP_EPSILON = 1e-6          # near-zero threshold for LP solution values
