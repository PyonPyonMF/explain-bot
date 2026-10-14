"""Exact Celsius/Fahrenheit/Kelvin conversion. No network, no external state."""

ABSOLUTE_ZERO = {"C": -273.15, "F": -459.67, "K": 0.0}


def to_kelvin(value, unit):
    if unit == "C":
        return value + 273.15
    if unit == "F":
        return (value - 32.0) * 5.0 / 9.0 + 273.15
    return value


def from_kelvin(kelvin, unit):
    if unit == "C":
        return kelvin - 273.15
    if unit == "F":
        return (kelvin - 273.15) * 9.0 / 5.0 + 32.0
    return kelvin


def run(args, ctx):
    value, from_unit, to_unit = args["value"], args["from_unit"], args["to_unit"]
    if value < ABSOLUTE_ZERO[from_unit]:
        ctx.fail(f"below_absolute_zero: {value} degrees {from_unit} is colder than absolute zero")
    kelvin = max(to_kelvin(value, from_unit), 0.0)  # absorb float roundoff on an already-validated input
    return {"value": round(from_kelvin(kelvin, to_unit), 6), "unit": to_unit}
