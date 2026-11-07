"""SHOM Balisage marks in Open Waters Seamap's tile schema.

SHOM (Licence Ouverte 2.0) carries the official French marks. Each mark is
written as Seamap writes its own OSM marks, so Seamap's symbols draw it; light
sectors go to the `light` layer. Ported from the option E half of
spikes/chart-pilots/tools/build_marks.py (2026-11-06), without option D.

Vendored unchanged from tactician `tools/charts/shom.py` (main at eaf6d88,
2026-11-07).
"""

from __future__ import annotations

import math
from collections import defaultdict

# S-57 enumerations, in Seamap's (OSM's) value names
COLOUR = {
    "1": "W",
    "2": "B",
    "3": "R",
    "4": "G",
    "5": "Bu",
    "6": "Y",
    "7": "Gy",
    "8": "Br",
    "9": "Am",
    "10": "Vi",
    "11": "Or",
    "12": "M",
}
NAME = {
    "1": "white",
    "2": "black",
    "3": "red",
    "4": "green",
    "5": "blue",
    "6": "yellow",
    "7": "grey",
    "8": "brown",
    "9": "amber",
    "10": "violet",
    "11": "orange",
    "12": "magenta",
}
LETTER_NAME = {
    "W": "white",
    "R": "red",
    "G": "green",
    "Y": "yellow",
    "Bu": "blue",
    "Or": "orange",
    "Am": "amber",
    "Vi": "violet",
}
LITCHR = {
    1: "F",
    2: "Fl",
    3: "LFl",
    4: "Q",
    5: "VQ",
    6: "UQ",
    7: "Iso",
    8: "Oc",
    9: "IQ",
    10: "IVQ",
    11: "IUQ",
    12: "Mo",
    13: "FFl",
    14: "FlLFl",
    15: "OcFl",
    16: "FLFl",
    17: "Al.Oc",
    18: "Al.LFl",
    19: "Al.Fl",
    20: "Al.Gr",
    25: "Q+LFl",
    26: "VQ+LFl",
    27: "UQ+LFl",
    28: "Al",
    29: "Al.FFl",
}
SM_BOYSHP = {
    1: "conical",
    2: "can",
    3: "spherical",
    4: "pillar",
    5: "spar",
    6: "barrel",
    7: "super-buoy",
    8: "ice_buoy",
}
SM_BCNSHP = {1: "stake", 2: "withy", 3: "tower", 4: "lattice", 5: "pile", 6: "cairn", 7: "buoyant"}
SM_TOPSHP = {
    1: "cone_point_up",
    2: "cone_point_down",
    3: "sphere",
    4: "2_spheres",
    5: "cylinder",
    6: "board",
    7: "x-shape",
    8: "cross",
    9: "cube_point_up",
    10: "2_cones_point_together",
    11: "2_cones_base_together",
    12: "rhombus",
    13: "2_cones_up",
    14: "2_cones_down",
    15: "besom_point_up",
    16: "besom_point_down",
    17: "flag",
    18: "sphere_over_rhombus",
    19: "square",
    20: "rectangle_horizontal",
    21: "rectangle_vertical",
    22: "trapezium_up",
    23: "trapezium_down",
    24: "triangle_point_up",
    25: "triangle_point_down",
    26: "circle",
    27: "2_upright_crosses",
    28: "t-shape",
    29: "triangle_point_up_over_circle",
    30: "upright_cross_over_circle",
    31: "rhombus_over_circle",
    32: "circle_over_triangle_point_up",
}
SM_COLPAT = {1: "horizontal", 2: "vertical", 3: "diagonal", 4: "squared", 5: "stripes", 6: "border"}
SM_CATLAM = {
    1: "port",
    2: "starboard",
    3: "preferred_channel_starboard",
    4: "preferred_channel_port",
}
SM_CATCAM = {1: "north", 2: "east", 3: "south", 4: "west"}
SM_CATLMK = {
    1: "cairn",
    2: "cemetery",
    3: "chimney",
    4: "dish_aerial",
    5: "flagstaff",
    6: "flare_stack",
    7: "mast",
    8: "windsock",
    9: "monument",
    10: "column",
    11: "memorial",
    12: "obelisk",
    13: "statue",
    14: "cross",
    15: "dome",
    16: "radar_scanner",
    17: "tower",
    18: "windmill",
    19: "windmotor",
    20: "spire",
    21: "boulder",
}
SM_TYPE = {
    "boylat": "buoy_lateral",
    "boycar": "buoy_cardinal",
    "boyspp": "buoy_special_purpose",
    "boyisd": "buoy_isolated_danger",
    "boysaw": "buoy_safe_water",
    "bcnlat": "beacon_lateral",
    "bcncar": "beacon_cardinal",
    "bcnspp": "beacon_special_purpose",
    "bcnisd": "beacon_isolated_danger",
    "daymar": "beacon_special_purpose",
}
# defaults where SHOM gives no colour or topmark (IALA region A)
SM_CARDINAL = {
    "north": ("black_yellow", "2_cones_up"),
    "south": ("yellow_black", "2_cones_down"),
    "east": ("black_yellow_black", "2_cones_base_together"),
    "west": ("yellow_black_yellow", "2_cones_point_together"),
}
MARK_LAYERS = (
    "boylat",
    "boycar",
    "boyspp",
    "boyisd",
    "boysaw",
    "bcnlat",
    "bcncar",
    "bcnspp",
    "bcnisd",
    "daymar",
)


def names(code):
    return "_".join(
        NAME.get(c.strip(), "") for c in str(code or "").split(",") if c.strip() in NAME
    )


def colours(code):
    return "-".join(COLOUR.get(c.strip(), "?") for c in str(code or "").split(",") if c.strip())


def offset(lon, lat, bearing, nm):
    b = math.radians(bearing)
    return [
        lon + nm * math.sin(b) / (60 * math.cos(math.radians(lat))),
        lat + nm * math.cos(b) / 60,
    ]


def light_label(lights):
    """lights: dicts with chr, grp, col, per, rng. Grouped by rhythm, colours joined."""
    by = defaultdict(lambda: {"col": [], "rng": []})
    for l in lights:
        k = (l["chr"], l["grp"], l["per"])
        for c in l["col"]:
            if c not in by[k]["col"]:
                by[k]["col"].append(c)
        if l["rng"]:
            by[k]["rng"].append(l["rng"])
    parts = []
    for (chr_, grp, per), v in by.items():
        if not chr_:
            continue
        grp = grp if grp and grp not in ("()", "(1)") else ""
        order = "WRGY"
        col = "".join(
            sorted(
                (c for c in v["col"] if len(c) == 1),
                key=lambda c: order.find(c) if c in order else 9,
            )
        )
        col = "" if col == "W" else col  # charts leave a white-only light's colour out
        s = f"{chr_}{grp}{col}" if grp or not col else f"{chr_}.{col}"
        if per:
            s += f"{per:g}s" if s.endswith(")") else f".{per:g}s"
        if v["rng"]:
            lo, hi = min(v["rng"]), max(v["rng"])
            s += f"{hi:g}M" if lo == hi else f"{hi:g}-{lo:g}M"
        parts.append(s)
    return " ".join(parts)


def convert(shom: dict):
    """SHOM WFS layers -> (marks, sectors). marks: [(lon, lat, props)] for the
    `seamark` layer; sectors: [(coords, props)] line strings for `light`."""
    marks, sectors = [], []
    lights_by_id = {f["properties"]["inspireid"]: f for f in shom["lights"]["features"]}
    tops = {f["properties"]["inspireid"]: f for f in shom["topmar"]["features"]}
    # sibling lights at one position (a sectored light is one record per sector)
    at = defaultdict(list)
    for i, f in lights_by_id.items():
        x, y = f["geometry"]["coordinates"][:2]
        at[(round(x * 5e4), round(y * 5e4))].append(i)
    used = set()

    def light(f):
        p = f["properties"]
        return {
            "chr": LITCHR.get(p.get("litchr"), ""),
            "grp": p.get("siggrp") or "",
            "col": colours(p.get("colour")).split("-") if p.get("colour") else [],
            "per": p.get("sigper"),
            "rng": p.get("valnmr"),
            "s1": p.get("sectr1"),
            "s2": p.get("sectr2"),
        }

    def linked(p, lon, lat):
        out = []
        for i in [i for i in str(p.get("lights") or "").split(",") if i]:
            if i in lights_by_id and i not in used:
                used.add(i)
                out.append(light(lights_by_id[i]))
        kx, ky = round(lon * 5e4), round(lat * 5e4)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for i in at.get((kx + dx, ky + dy), []):
                    x, y = lights_by_id[i]["geometry"]["coordinates"][:2]
                    if i not in used and abs(x - lon) < 2e-5 and abs(y - lat) < 2e-5:
                        used.add(i)
                        out.append(light(lights_by_id[i]))
        return out

    def add_sectors(lon, lat, lights, major, mtype):
        r_arc = 1.0 if major else 0.5
        base = {"type": mtype, "family": "major_aid", "cell_rank": 0}
        for l in lights:
            a, b = l.get("s1"), l.get("s2")
            if a is None or b is None or abs((b - a) % 360) < 0.01:
                continue
            start, end = (a + 180) % 360, (b + 180) % 360
            span = (end - start) % 360
            n = max(4, int(span / 3))
            col = LETTER_NAME.get(l["col"][0], "white") if l["col"] else "white"
            arc = [offset(lon, lat, start + span * i / n, r_arc) for i in range(n + 1)]
            sectors.append(
                (arc, {**base, "subtype": "sector", "color": col, "range": str(l["rng"] or "")})
            )
            for bearing in (start, end):
                sectors.append(
                    (
                        [[lon, lat], offset(lon, lat, bearing, r_arc * 1.6)],
                        {**base, "subtype": "leg"},
                    )
                )

    def mark(lon, lat, props, lights, major=False):
        p = {"cell_rank": 0, **{k: v for k, v in props.items() if v not in (None, "")}}
        if lights:
            cols = [c for l in lights for c in l["col"]]
            # one colour: that colour; several (sectored): white, as S-52's LIGHTS06 does
            p["light_color"] = (
                LETTER_NAME.get(cols[0], "white") if cols and len(set(cols)) == 1 else "white"
            )
            p["seamark:light:colour"] = (
                ";".join(dict.fromkeys(LETTER_NAME.get(c, "white") for c in cols)) or "white"
            )
            p["light"] = light_label(lights)
            rng = [l["rng"] for l in lights if l["rng"]]
            if rng:
                p["light_range"] = max(rng)
            add_sectors(lon, lat, lights, major, p["type"])
        marks.append((lon, lat, p))

    for layer in MARK_LAYERS:
        for f in shom[layer]["features"]:
            p = f["properties"]
            lon, lat = f["geometry"]["coordinates"][:2]
            lights = linked(p, lon, lat)
            sm = {"type": SM_TYPE[layer], "name": p.get("objnam"), "color": names(p.get("colour"))}
            sm["family"] = "major_aid" if layer[3:] in ("car", "isd", "saw") else "minor_aid"
            sm["shape"] = (
                (
                    SM_BOYSHP.get(p.get("boyshp"), "pillar")
                    if layer.startswith("boy")
                    else SM_BCNSHP.get(p.get("bcnshp"), "stake")
                )
                if layer != "daymar"
                else "board"
            )
            if layer in ("boylat", "bcnlat"):
                sm["category"] = SM_CATLAM.get(p.get("catlam"), "")
            if layer in ("boycar", "bcncar"):
                sm["category"] = SM_CATCAM.get(p.get("catcam"), "north")
                sm["color"] = sm["color"] or SM_CARDINAL[sm["category"]][0]
            if "_" in sm["color"]:
                pat = p.get("colpat")
                sm["color_pattern"] = (
                    SM_COLPAT.get(int(str(pat).split(",")[0]), "horizontal")
                    if pat
                    else "horizontal"
                )
            t = tops.get(p.get("topmar") or "")
            if t:
                sm["topmark_shape"] = SM_TOPSHP.get(t["properties"].get("topshp"), "")
                tc = names(t["properties"].get("colour"))
                sm["topmark_color"] = tc
                if "_" in tc:
                    sm["topmark_color_pattern"] = "horizontal"
            elif layer in ("boycar", "bcncar"):
                sm["topmark_shape"], sm["topmark_color"] = SM_CARDINAL[sm["category"]][1], "black"
            elif layer in ("boyisd", "bcnisd"):
                sm["topmark_shape"], sm["topmark_color"] = "2_spheres", "black"
            elif layer == "boysaw":
                sm["topmark_shape"], sm["topmark_color"] = "sphere", "red"
            mark(lon, lat, sm, lights)

    for f in shom["lndmrk"]["features"]:
        p = f["properties"]
        lon, lat = f["geometry"]["coordinates"][:2]
        lights = linked(p, lon, lat)
        major = any((l["rng"] or 0) >= 15 for l in lights)
        cat = SM_CATLMK.get(int(str(p.get("catlmk") or "17").split(",")[0]), "tower")
        if lights:
            sm = {
                "type": "light_major" if major else "light_minor",
                "family": "major_aid" if major else "minor_aid",
                "name": p.get("objnam"),
            }
        else:
            sm = {
                "type": "landmark",
                "family": "structure",
                "category": cat,
                "name": p.get("objnam"),
            }
            fn = {"20": "church", "21": "chapel"}.get(str(p.get("functn") or "").split(",")[0])
            if fn:
                sm["function"] = fn
            if p.get("convis") == 1:
                sm["seamark:landmark:conspicuity"] = "conspicuous"
        mark(lon, lat, sm, lights, major)

    # lights on no structure of their own
    groups = defaultdict(list)
    for i, f in lights_by_id.items():
        if i in used:
            continue
        x, y = f["geometry"]["coordinates"][:2]
        groups[(round(x, 5), round(y, 5))].append(light(f))
    for (lon, lat), lights in groups.items():
        major = any((l["rng"] or 0) >= 15 for l in lights)
        mark(
            lon,
            lat,
            {
                "type": "light_major" if major else "light_minor",
                "family": "major_aid" if major else "minor_aid",
            },
            lights,
            major,
        )
    return marks, sectors
