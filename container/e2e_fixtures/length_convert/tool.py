METRES = {"m": 1, "km": 1000, "cm": 0.01, "in": 0.0254, "ft": 0.3048, "mi": 1609.344}


def run(args, ctx):
    if args["value"] < 0:
        ctx.fail("negative lengths are not supported")
    value = args["value"] * METRES[args["from_unit"]] / METRES[args["to_unit"]]
    return {"value": round(value, 6), "unit": args["to_unit"]}
