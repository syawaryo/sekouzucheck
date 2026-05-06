"""
parser.py - DXF to FloorData parser.

This is the ONLY module that imports ezdxf. It reads a DXF file and converts
the relevant entities into the FloorData dataclass consumed by check logic.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import TYPE_CHECKING

import ezdxf

from .models import (
    BeamLine,
    ColumnLine,
    DimLine,
    FloorData,
    GridLine,
    PnLabel,
    RawLine,
    RawText,
    RoomLabel,
    SlabLabel,
    SlabOutline,
    SlabZone,
    Sleeve,
    StepLine,
    WallLine,
)

# ---------------------------------------------------------------------------
# Building coordinate range (mm).  Entities outside this box are detail
# drawings or construction notes and should be excluded from checks.
# ---------------------------------------------------------------------------
BLDG_X_MIN = 0.0
BLDG_X_MAX = 80_000.0
BLDG_Y_MIN = 0.0
BLDG_Y_MAX = 38_000.0

# Search radius (mm) for associating label / FL texts with sleeve centers.
_LABEL_SEARCH_RADIUS = 1_500.0

# ---------------------------------------------------------------------------
# Regex patterns used in text association
# ---------------------------------------------------------------------------
_RE_PHI = re.compile(r"[φΦ]|\d+\s*[φΦ]|[φΦ]\s*\d+|外径|\d+A\b", re.IGNORECASE)
_RE_FL = re.compile(r"FL\s*[+\-]\s*\d+", re.IGNORECASE)

# Known equipment/pipe type codes (whitelist)
_RE_EQUIP_CODE = re.compile(
    r"^(RD|KD|KDK|KV|SD|CW[WRN]?|CDW|SP[D]?|EA|G\(|N2|CX|HS|WD|HR|CH[R]?|"
    r"CR|XS|OA|KEA|RA|SA|SOA|D:|C:|H:|V:|W:|R:|"
    r"P-UP)",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Equipment code → sleeve_type classification
#
#   "duct"   : air-handling ducts (空調/排煙ダクト)
#   "pipe"   : water / drain pipes (配管)
#   "cable"  : electrical pipes, cable racks (電気/ケーブルラック)
#   ""       : unknown / no code
#
# Codes are matched against the start of the attached label_text (the
# equipment code conventionally appears at the head of the annotation).
# ---------------------------------------------------------------------------
_SLEEVE_TYPE_DUCT = re.compile(
    r"^(EA|OA|SA|RA|KEA|SOA|SOA2?|EA2?|RA2?|SA2?)\b", re.IGNORECASE
)
_SLEEVE_TYPE_PIPE = re.compile(
    r"^(CW[WRN]?|CDW|RD|SD|SP[D]?|W:|HW|HR|CH[R]?|HS|WD|CX|N2|G\()",
    re.IGNORECASE,
)
_SLEEVE_TYPE_CABLE = re.compile(r"^(XS|KD|KDK|KV|CR)\b", re.IGNORECASE)


# Discipline → sleeve_type fallback (used when the text-code lookup fails).
# Matches the IFC classifier for parity across formats.
_DISCIPLINE_TO_TYPE = {"衛生": "pipe", "空調": "duct", "電気": "cable"}


def _classify_sleeve_type(
    label_text: str | None, discipline: str | None = None
) -> str:
    """Map the equipment code in *label_text* to a sleeve sub-type.

    Priority:
      1. Explicit equipment code at the start of label_text (EA/CW/…).
      2. Discipline fallback from the layer name (衛生/空調/電気).

    Returns "duct" / "pipe" / "cable" / "" (unclassified).
    """
    if label_text:
        lt = label_text.strip()
        if _SLEEVE_TYPE_DUCT.match(lt):
            return "duct"
        if _SLEEVE_TYPE_PIPE.match(lt):
            return "pipe"
        if _SLEEVE_TYPE_CABLE.match(lt):
            return "cable"
    if discipline:
        return _DISCIPLINE_TO_TYPE.get(discipline, "")
    return ""


# Matches pipe-diameter notation like "φ124", "Φ200", "125φ", "125A".
# These denote a ROUND pipe, so the sleeve is a round hole regardless of
# how the block geometry is drawn (hatched rectangle is common).
_RE_ROUND_LABEL = re.compile(
    r"(?:[φΦ]\s*\d+|\d+\s*[φΦ]|\d+\s*A\b|外径\s*\d+)", re.IGNORECASE
)


def _refine_sleeve_shape_from_label(sleeve: Sleeve) -> None:
    """If the attached label indicates a round pipe (φXXX / XXXA / 外径XXX),
    override shape to "round".

    This corrects a common DXF drawing convention: round pipe sleeves are
    sometimes drawn as a hatched rectangle in plan view (no CIRCLE entity
    inside the block), which our geometry pass tags as "rect". The label
    text is a more authoritative signal than the hatching rectangle.

    EXCEPTION: blocks named 箱 (= "box") are angular sleeves by construction.
    The round pipe that passes THROUGH a box sleeve doesn't make the hole
    round — the hole is a rectangle sized for the box. Keep rect.
    """
    if sleeve.shape == "round":
        return
    if "箱" in sleeve.id:
        return
    lt = sleeve.label_text or ""
    dt = sleeve.diameter_text or ""
    combined = f"{lt} {dt}"
    if _RE_ROUND_LABEL.search(combined):
        # Before collapsing geometry, capture the orientation clue: a round
        # pipe drawn with a highly-elongated rectangular hatching in plan is
        # almost certainly a horizontal pipe slot through a wall.
        w0 = sleeve.width or sleeve.diameter
        h0 = sleeve.height or sleeve.diameter
        if w0 > 0 and h0 > 0:
            aspect = max(w0, h0) / min(w0, h0)
            if aspect >= _ASPECT_HORIZONTAL_MIN:
                sleeve.orientation = "horizontal"
        sleeve.shape = "round"
        # Collapse width/height to the short side so it renders as a circle
        # sized by the real pipe diameter, not the hatching rectangle.
        if sleeve.diameter > 0:
            sleeve.width = sleeve.diameter
            sleeve.height = sleeve.diameter


# Aspect-ratio thresholds for DXF orientation heuristic.
# Real horizontal pipe/duct penetrations in plan view are very elongated
# (long thin slot showing pipe length through a wall). Square-ish rects
# are typically vertical square ducts.
_ASPECT_HORIZONTAL_MIN = 2.0   # ≥2:1 → confident horizontal (lowered from 3.0
                                #         to catch wall/beam penetrations that
                                #         are drawn at 2–3:1 aspect)
_ASPECT_VERTICAL_MAX = 1.3     # ≤1.3:1 (near-square) → confident vertical


def _infer_sleeve_orientation(sleeve: Sleeve) -> str:
    """Heuristic vertical/horizontal classifier for DXF sleeves.

    DXF plan view doesn't encode the pipe axis directly, so we infer from
    the 2D footprint + annotation text:

    - Round section                    → vertical pipe seen from above.
    - Rect on 衛生 layer               → horizontal (plumbing is always
                                         round; a rect footprint means the
                                         pipe runs sideways through a wall).
    - Label contains 外径 / 梁孔        → horizontal (drafter explicitly
                                         marked it as a wall/beam penetration).
    - Highly elongated (≥2:1)          → horizontal pipe slot through a wall.
    - Near-square rect (≤1.3:1)        → vertical square duct punch-through.
    - Everything else                  → "" (unknown; second pass may
                                         recover via pair-detection).
    """
    if sleeve.orientation:
        return sleeve.orientation  # respect earlier resolution (label override)
    if sleeve.shape == "round":
        return "vertical"

    # Plumbing (衛生) pipes are almost always round cross-section. A rect
    # footprint on 衛生 → the pipe is drawn sideways, i.e. horizontal.
    if sleeve.discipline == "衛生":
        return "horizontal"

    # Explicit annotation signals: 外径 or 梁孔 → the drafter is describing
    # a round pipe passing through the wall/beam sideways.
    combined = (sleeve.label_text or "") + " " + (sleeve.diameter_text or "")
    if "外径" in combined or "梁孔" in combined:
        return "horizontal"

    w = sleeve.width or sleeve.diameter
    h = sleeve.height or sleeve.diameter
    long_side = max(w, h)
    short_side = min(w, h)
    if short_side <= 0:
        return ""
    aspect = long_side / short_side
    if aspect >= _ASPECT_HORIZONTAL_MIN:
        return "horizontal"
    if aspect <= _ASPECT_VERTICAL_MAX:
        return "vertical"
    return ""


def _infer_orientation_from_pairs(sleeves: list[Sleeve]) -> None:
    """Second-pass heuristic: mark nearby same-sized sleeve pairs as horizontal.

    A horizontal pipe through a wall is drawn as two matching sleeve marks
    (entry + exit on either face of the wall). Pair rule:
    - Similar diameter (±10 %)
    - Centre-to-centre distance 100–500 mm (wall-thickness range)
    - Both currently unclassified
    """
    _WALL_MIN = 100.0
    _WALL_MAX = 500.0
    n = len(sleeves)
    for i in range(n):
        a = sleeves[i]
        if a.orientation:
            continue
        if a.diameter <= 0:
            continue
        for j in range(i + 1, n):
            b = sleeves[j]
            if b.orientation:
                continue
            if abs(a.diameter - b.diameter) / max(a.diameter, b.diameter) > 0.10:
                continue
            dx = a.center[0] - b.center[0]
            dy = a.center[1] - b.center[1]
            dist = math.hypot(dx, dy)
            if _WALL_MIN <= dist <= _WALL_MAX:
                a.orientation = "horizontal"
                b.orientation = "horizontal"
                break


def _point_to_segment_distance(
    px: float, py: float,
    x1: float, y1: float, x2: float, y2: float,
) -> float:
    """Shortest distance from point (px,py) to segment (x1,y1)-(x2,y2)."""
    dx = x2 - x1
    dy = y2 - y1
    if dx == 0 and dy == 0:
        return math.hypot(px - x1, py - y1)
    t = ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    fx = x1 + t * dx
    fy = y1 + t * dy
    return math.hypot(px - fx, py - fy)


def _infer_orientation_from_walls(
    sleeves: list[Sleeve], wall_lines: list[WallLine],
) -> None:
    """Third-pass heuristic: round sleeves sitting *on* a wall line are
    horizontal (wall penetration) rather than vertical (slab penetration).

    In a floor-plan DXF a horizontal pipe through a wall is often drawn as a
    single small circle on the wall centreline — indistinguishable from a
    vertical slab penetration without context. If the sleeve centre is
    within roughly half-a-wall-thickness of any wall segment, reclassify it
    as horizontal.

    Tolerance is per wall type because the actual wall thickness varies a
    lot — outer RC walls are 300-500mm, interior partitions 100-150mm —
    and a single 200mm threshold either misses 外壁 penetrations or
    false-positives every interior sleeve within 200mm of a 仕上 line.
    """
    if not wall_lines:
        return
    # Half-thickness tolerance — sleeve centre on the wall centreline can
    # land up to ~half the wall's actual thickness from any drawn line.
    # Outer walls span structure (300-500mm) plus interior/exterior finish
    # layers (50-100mm each side), so the centre of a sleeve drawn on the
    # wall can be 500-700mm from any single drawn line. Bumped to 600mm
    # after a user reported edge-of-building horizontal pipes still
    # rendering as round-vertical at 400mm tolerance.
    _TOL_BY_TYPE: dict[str, float] = {
        "外壁": 600.0,        # 既存躯体外壁 / RC 外壁 + 内外仕上
        "RC壁": 400.0,        # F105/F106 RC壁 + 仕上 + 取付ピッチ
        "ALC": 300.0,
        "PCa": 400.0,
        "パネル": 200.0,
        "LGS": 200.0,
        "CB": 250.0,
        # 壁心 (C151) は中心線。配管立上りは中心線の左右どちらかに
        # オフセットして描かれる (壁芯から ±300〜400mm) — 給排水
        # ライザーは「壁心の側」に寄せて配置されるのが慣習なので
        # 500mm まで広げないと拾い損ねる。
        "壁心": 500.0,
        "仕上": 150.0,
        "耐火被覆": 150.0,
        "不明": 300.0,
    }
    _DEFAULT_TOL = 300.0

    # Pipe-flow labels — these are horizontal-run pipes (drainage, water
    # supply, gas) by construction. Even when their distance to the wall
    # centreline exceeds the geometric tolerance, the label is strong
    # evidence that the sleeve passes through a wall horizontally.
    # Vertical risers don't normally carry these labels.
    _RE_PIPE_FLOW = re.compile(
        r"^(RD|KD|KDK|KV|CW[WRN]?|CDW|SD|SP[D]?|HW|HR|CHR?|HS|WD|N2|"
        r"D:|R:|H:|V:|W:|G\(|RA|EA|SA|OA|KEA|SOA)\b",
        re.IGNORECASE,
    )

    def _is_pipe_flow(label: str | None) -> bool:
        return bool(label and _RE_PIPE_FLOW.match(label))

    for s in sleeves:
        # Round sleeves on a wall line are reclassified even if the aspect
        # pass already tentatively set them to "vertical" — a wall centroid
        # is a stronger signal than the default-vertical fallback.
        if s.shape != "round":
            continue
        if s.orientation == "horizontal":
            continue
        sx, sy = s.center
        # Pipe-flow labelled sleeves get a generous label-aware tolerance
        # boost (×2). They're almost always horizontal runs through walls.
        is_flow = _is_pipe_flow(s.label_text)
        for w in wall_lines:
            base_tol = _TOL_BY_TYPE.get(w.wall_type, _DEFAULT_TOL)
            tol = base_tol * 2 if is_flow else base_tol
            x1, y1 = w.start
            x2, y2 = w.end
            if min(x1, x2) - tol > sx: continue
            if max(x1, x2) + tol < sx: continue
            if min(y1, y2) - tol > sy: continue
            if max(y1, y2) + tol < sy: continue
            if _point_to_segment_distance(sx, sy, x1, y1, x2, y2) <= tol:
                s.orientation = "horizontal"
                break


# ---------------------------------------------------------------------------
# Layer-lookup helpers
# ---------------------------------------------------------------------------

def _find_layers(doc: ezdxf.document.Drawing, suffix: str) -> list[str]:
    """Return all layer names whose name contains *suffix*."""
    return [
        layer.dxf.name
        for layer in doc.layers
        if suffix in layer.dxf.name
    ]


def _find_layers_any(doc: ezdxf.document.Drawing, keywords: list[str]) -> list[str]:
    """Return layer names that contain ANY of the given keywords."""
    result = []
    for layer in doc.layers:
        name = layer.dxf.name
        if any(kw in name for kw in keywords):
            result.append(name)
    return result


def _entities_on_layers(msp, layers: list[str], dxftype: str):
    """Yield entities of *dxftype* whose layer is in *layers* (set lookup)."""
    layer_set = set(layers)
    for entity in msp:
        if entity.dxftype() == dxftype and entity.dxf.layer in layer_set:
            yield entity


# ---------------------------------------------------------------------------
# Sleeve extraction
# ---------------------------------------------------------------------------

def _discipline_from_layer(layer_name: str) -> str:
    """Infer discipline (衛生/空調/電気/建築) from layer prefix."""
    for disc in ("衛生", "空調", "電気", "建築"):
        if disc in layer_name:
            return disc
    return ""


_BLDG_TOLERANCE = 10.0  # mm tolerance for floating-point at boundaries


# Detail-drawing fragments (legend panels, section cuts, title blocks,
# enlarged details) often sit on the same architectural layers as the real
# plan. A 10 m tolerance past the BLDG_X/Y bounds keeps real outer walls,
# entrance approach walls and thick 外壁 with finishes (the left-edge
# entrance on the sample needed > 5 m), while still rejecting detail
# clusters that live 20 m+ outside the footprint (e.g. y ≈ 100 k legend
# panels on sample sheets).
_PLAN_RANGE_TOL = 10000.0


def _point_in_building_bbox(x: float, y: float) -> bool:
    return (
        BLDG_X_MIN - _PLAN_RANGE_TOL <= x <= BLDG_X_MAX + _PLAN_RANGE_TOL
        and BLDG_Y_MIN - _PLAN_RANGE_TOL <= y <= BLDG_Y_MAX + _PLAN_RANGE_TOL
    )


def _segment_in_building_bbox(
    sx: float, sy: float, ex: float, ey: float,
) -> bool:
    """True iff both endpoints sit inside the building bbox + tolerance.

    Midpoint-only checks let long margin-to-plan segments slip through; using
    both endpoints is strict enough to reject the ~60 m outliers seen on
    Takenaka sample sheets while keeping legitimate 外壁 geometry.
    """
    return _point_in_building_bbox(sx, sy) and _point_in_building_bbox(ex, ey)


def _in_building_range(x: float, y: float) -> bool:
    """Strict building-extent check — alias for _point_in_building_bbox.

    Historically this was 2× the bbox to admit perimeter elements, but
    wall / slab / column extractors now all apply the strict ±2 m
    tolerance at their return statements, and the 2× version was letting
    sleeves / water gradients / PN labels through at 90 k coordinates
    (legend-panel position).
    """
    return _point_in_building_bbox(x, y)


def _resolve_entity_color(doc, entity) -> int | None:
    """
    Return the effective ACI color index of *entity* (1-255), or None if
    unknown.  Handles BYLAYER (256) by looking up the layer's color, and
    treats BYBLOCK (0) as "inherit" (returns None when we have no block
    context to inherit from).
    """
    raw = getattr(entity.dxf, "color", 256)
    if raw in (0, None):
        return None  # BYBLOCK with no block context
    if raw == 256:
        layer_name = getattr(entity.dxf, "layer", "")
        layer = doc.layers.get(layer_name) if layer_name else None
        if layer is None:
            return None
        layer_color = getattr(layer.dxf, "color", 7)
        return layer_color if layer_color > 0 else None
    return int(raw)


def _get_block_circles(doc, block_name: str) -> list[tuple[float, float, float]]:
    """
    Return list of (offset_x, offset_y, diameter) for every CIRCLE in the
    named block definition.  Returns empty list if the block is not found.
    """
    block = doc.blocks.get(block_name)
    if block is None:
        return []
    circles = []
    for ent in block:
        if ent.dxftype() == "CIRCLE":
            cx = ent.dxf.center.x
            cy = ent.dxf.center.y
            d = ent.dxf.radius * 2.0
            circles.append((cx, cy, d))
    return circles


def _get_block_bbox_size(doc, block_name: str) -> tuple[float, float, float, float] | None:
    """
    For blocks with no CIRCLE (box / rectangular sleeves), estimate the
    sleeve size from the outer outline.

    A block qualifies as a rectangular sleeve only if it carries evidence
    of a closed rectangular frame. Annotation / leader-arrow blocks that
    contain only an open polyline + a short line are rejected (they would
    otherwise inflate the bbox into a spurious "huge rect sleeve").

    Accepted evidence:
      1. At least one CLOSED LWPOLYLINE with ≥3 points → use that as bbox.
      2. At least 4 LINE entities → use union LINE bbox (supports the
         "4-sided frame + interior hatching" style).

    Returns (cx, cy, width, height) in block-local coordinates, or None if
    the block has no recognisable rectangular outline.
    """
    block = doc.blocks.get(block_name)
    if block is None:
        return None

    # Evidence 1: find the largest closed LWPOLYLINE (outer frame)
    best_frame: tuple[float, float, float, float] | None = None
    best_area = 0.0
    line_count = 0
    line_xs: list[float] = []
    line_ys: list[float] = []
    for ent in block:
        t = ent.dxftype()
        if t == "LWPOLYLINE" and getattr(ent, "is_closed", False):
            pts = list(ent.get_points())
            if len(pts) < 3:
                continue
            xs = [float(p[0]) for p in pts]
            ys = [float(p[1]) for p in pts]
            w = max(xs) - min(xs)
            h = max(ys) - min(ys)
            area = w * h
            if area > best_area:
                best_area = area
                cx = (min(xs) + max(xs)) / 2.0
                cy = (min(ys) + max(ys)) / 2.0
                best_frame = (cx, cy, w, h)
        elif t == "LINE":
            line_count += 1
            line_xs += [ent.dxf.start.x, ent.dxf.end.x]
            line_ys += [ent.dxf.start.y, ent.dxf.end.y]

    if best_frame is not None:
        return best_frame

    # Evidence 2: block with ≥4 LINEs → treat union LINE bbox as the frame.
    # 4 is the minimum needed to bound a rectangle; below that the block is
    # almost certainly a leader/annotation symbol, not a sleeve outline.
    if line_count >= 4 and line_xs:
        xmin, xmax = min(line_xs), max(line_xs)
        ymin, ymax = min(line_ys), max(line_ys)
        cx = (xmin + xmax) / 2.0
        cy = (ymin + ymax) / 2.0
        return (cx, cy, xmax - xmin, ymax - ymin)

    return None


def _extract_sleeves(doc, msp) -> list[Sleeve]:
    """
    Extract sleeves from sleeve layers.

    Three INSERT-block patterns are handled:

    1. Named ``スリーブ(S)-Z{uid}`` / ``スリーブ（鉄）-Z{uid}`` with a single
       CIRCLE centred at (0,0): INSERT.insert == sleeve centre, diameter from
       the circle radius × 2.

    2. Named ``INS-G{uid}`` (composite block): may contain many CIRCLEs each
       at an offset from the block origin.  The INSERT.insert is the block
       origin; the actual sleeve centre for each circle is
       INSERT.insert + circle_offset.

    3. Named ``箱（鉄）-Z{uid}`` / ``スリーブ（鉄）-Z{uid}`` with no CIRCLE but
       having LINE/LWPOLYLINE geometry (box or pipe-through-slab type):
       sleeve centre = INSERT.insert + bbox centre, diameter = minimum of
       bounding-box width and height (the cross-section short axis).

    Two standalone-entity patterns are also captured (for drawings that
    draw sleeves directly on the sleeve layer without using a block):

    4. Standalone CIRCLE on a sleeve layer → round sleeve.
    5. Standalone closed LWPOLYLINE on a sleeve layer → rectangular sleeve
       (diameter = min(width, height); width/height recorded).
    """
    sleeve_layers = _find_layers(doc, "スリーブ")

    # Cache block geometry to avoid re-reading the same block many times
    _circle_cache: dict[str, list[tuple[float, float, float]]] = {}
    _bbox_cache: dict[str, tuple[float, float, float, float] | None] = {}

    def get_circles(bname: str) -> list[tuple[float, float, float]]:
        if bname not in _circle_cache:
            _circle_cache[bname] = _get_block_circles(doc, bname)
        return _circle_cache[bname]

    def get_bbox(bname: str) -> tuple[float, float, float, float] | None:
        if bname not in _bbox_cache:
            _bbox_cache[bname] = _get_block_bbox_size(doc, bname)
        return _bbox_cache[bname]

    # Pattern 3 reached = INSERT has no CIRCLE inside, only LINE/LWPOLYLINE.
    # That is the definition of a rectangular sleeve in our drawings — round
    # sleeves are always represented by a CIRCLE. Aspect ratio is irrelevant:
    # a perfectly square (1:1) sleeve must still render as a square, not a circle.
    def _rect_shape_for_block(bname: str, w: float, h: float) -> str:
        return "rect"

    sleeves: list[Sleeve] = []
    uid_counter = 0
    insert_centers: list[tuple[float, float]] = []  # for dedup vs standalone

    for ins in _entities_on_layers(msp, sleeve_layers, "INSERT"):
        block_name: str = ins.dxf.name
        layer: str = ins.dxf.layer
        discipline = _discipline_from_layer(layer)
        ins_x: float = ins.dxf.insert.x
        ins_y: float = ins.dxf.insert.y

        # Determine scale factors (default 1.0)
        scale_x = getattr(ins.dxf, "xscale", 1.0) or 1.0
        scale_y = getattr(ins.dxf, "yscale", 1.0) or 1.0

        # Rotation (degrees → radians)
        rot_deg = getattr(ins.dxf, "rotation", 0.0) or 0.0
        rot_rad = math.radians(rot_deg)
        cos_r = math.cos(rot_rad)
        sin_r = math.sin(rot_rad)

        # Any INSERT on a sleeve layer is a sleeve candidate. Block-name
        # based filtering was removed because it excluded legitimate rect
        # sleeves using naming conventions like ``INS-G{uid}``
        # (vertical pipe-through-slab blocks).
        ins_color = _resolve_entity_color(doc, ins)
        circles = get_circles(block_name)

        # A block whose name contains 箱 (= "box") is a rectangular sleeve by
        # definition, even if it embeds a CIRCLE inside (drafters draw a small
        # circle to indicate the pipe cross-section within the box). Force the
        # rect path so these don't slip through as round.
        is_box_block = "箱" in block_name

        if circles and not is_box_block:
            # --- Pattern 1 & 2: CIRCLE-based sleeves ---
            for ox, oy, diameter in circles:
                # Apply scale then rotation
                lx = ox * scale_x
                ly = oy * scale_y
                wx = ins_x + lx * cos_r - ly * sin_r
                wy = ins_y + lx * sin_r + ly * cos_r

                if not _in_building_range(wx, wy):
                    continue

                uid_counter += 1
                sleeves.append(
                    Sleeve(
                        id=f"{block_name}_{uid_counter}",
                        center=(wx, wy),
                        diameter=diameter,
                        layer=layer,
                        discipline=discipline,
                        shape="round",
                        width=diameter,
                        height=diameter,
                        color=ins_color,
                    )
                )
                insert_centers.append((wx, wy))
        else:
            # --- Pattern 3: Rectangular / box sleeves (no CIRCLE) ---
            # Use the INSERT point itself as the sleeve centre (block local
            # origin is already at the centre for these blocks, but we also
            # apply the bbox centre offset just in case).
            bbox = get_bbox(block_name)
            if bbox is None:
                continue  # empty block, skip

            bcx, bcy, bw, bh = bbox
            # Diameter = minimum of width/height (the smaller cross-section)
            diameter = min(bw, bh) if min(bw, bh) > 0 else max(bw, bh)
            if diameter <= 0:
                continue

            # World position: INSERT + rotated bbox centre
            lx = bcx * scale_x
            ly = bcy * scale_y
            wx = ins_x + lx * cos_r - ly * sin_r
            wy = ins_y + lx * sin_r + ly * cos_r

            if not _in_building_range(wx, wy):
                continue

            # Rotated world-space width/height (only swap on 90°/270°)
            is_side_swapped = abs(math.sin(rot_rad)) > abs(math.cos(rot_rad))
            world_w = abs(bh * scale_y) if is_side_swapped else abs(bw * scale_x)
            world_h = abs(bw * scale_x) if is_side_swapped else abs(bh * scale_y)

            uid_counter += 1
            sleeves.append(
                Sleeve(
                    id=f"{block_name}_{uid_counter}",
                    center=(wx, wy),
                    diameter=diameter,
                    layer=layer,
                    discipline=discipline,
                    shape=_rect_shape_for_block(block_name, bw, bh),
                    width=world_w,
                    height=world_h,
                    color=ins_color,
                )
            )
            insert_centers.append((wx, wy))

    # -----------------------------------------------------------------
    # Pattern 4: standalone CIRCLE on a sleeve layer.
    # -----------------------------------------------------------------
    for ent in _entities_on_layers(msp, sleeve_layers, "CIRCLE"):
        cx = ent.dxf.center.x
        cy = ent.dxf.center.y
        if not _in_building_range(cx, cy):
            continue
        if _near_any(cx, cy, insert_centers):
            continue  # already captured via an INSERT

        diameter = ent.dxf.radius * 2.0
        if diameter <= 0:
            continue

        layer = ent.dxf.layer
        uid_counter += 1
        sleeves.append(
            Sleeve(
                id=f"standalone_circle_{uid_counter}",
                center=(cx, cy),
                diameter=diameter,
                layer=layer,
                discipline=_discipline_from_layer(layer),
                shape="round",
                width=diameter,
                height=diameter,
                color=_resolve_entity_color(doc, ent),
            )
        )

    # -----------------------------------------------------------------
    # Pattern 5: standalone closed LWPOLYLINE on a sleeve layer (rect).
    # -----------------------------------------------------------------
    for ent in _entities_on_layers(msp, sleeve_layers, "LWPOLYLINE"):
        if not getattr(ent, "is_closed", False):
            continue
        pts = list(ent.get_points())
        if len(pts) < 3:
            continue
        xs = [float(p[0]) for p in pts]
        ys = [float(p[1]) for p in pts]
        xmin, xmax = min(xs), max(xs)
        ymin, ymax = min(ys), max(ys)
        cx = (xmin + xmax) / 2.0
        cy = (ymin + ymax) / 2.0
        if not _in_building_range(cx, cy):
            continue
        if _near_any(cx, cy, insert_centers):
            continue  # already captured via an INSERT

        w = xmax - xmin
        h = ymax - ymin
        if w <= 0 or h <= 0:
            continue

        diameter = min(w, h)
        layer = ent.dxf.layer
        uid_counter += 1
        sleeves.append(
            Sleeve(
                id=f"standalone_rect_{uid_counter}",
                center=(cx, cy),
                diameter=diameter,
                layer=layer,
                discipline=_discipline_from_layer(layer),
                shape="rect",
                width=w,
                height=h,
                color=_resolve_entity_color(doc, ent),
            )
        )

    return sleeves


def _near_any(
    x: float, y: float, points: list[tuple[float, float]], tol: float = 50.0
) -> bool:
    """True if (x, y) is within *tol* mm of any point in *points*."""
    for px, py in points:
        if abs(px - x) <= tol and abs(py - y) <= tol:
            return True
    return False


# ---------------------------------------------------------------------------
# Grid line extraction
# ---------------------------------------------------------------------------

def _extract_grid_lines(doc, msp) -> list[GridLine]:
    """
    Extract grid (通り芯) LINEs.

    Matches any layer whose name contains 通心 / 通芯 / 通り心 / 通り芯,
    covering Takenaka-standard ``C131_通心`` / ``C131_通芯``, free-form
    ``[建築]通り芯`` / ``[建築]...通り心`` variants, and older 通り心 spellings.
    """
    grid_layers = _find_layers_any(doc, ["通心", "通芯", "通り心", "通り芯"])

    _H_DEDUP = 50.0
    _V_DEDUP = 50.0
    _MIN_GRID_LENGTH = 10_000.0  # grid lines must be ≥10m to filter noise

    h_positions: list[float] = []  # unique Y values for H lines
    v_positions: list[float] = []  # unique X values for V lines

    for line in _entities_on_layers(msp, grid_layers, "LINE"):
        sx, sy = line.dxf.start.x, line.dxf.start.y
        ex, ey = line.dxf.end.x, line.dxf.end.y

        dx = abs(ex - sx)
        dy = abs(ey - sy)
        length = math.hypot(dx, dy)

        # Filter out short lines (not actual grid lines)
        if length < _MIN_GRID_LENGTH:
            continue

        mx = (sx + ex) / 2.0
        my = (sy + ey) / 2.0

        if not _in_building_range(mx, my):
            continue

        if dx > dy:
            # Horizontal: position = Y
            pos = (sy + ey) / 2.0
            if all(abs(pos - p) > _H_DEDUP for p in h_positions):
                h_positions.append(pos)
        else:
            # Vertical: position = X
            pos = (sx + ex) / 2.0
            if all(abs(pos - p) > _V_DEDUP for p in v_positions):
                v_positions.append(pos)

    # Collect TEXT / MTEXT labels drawn on the same grid layers. The drafter
    # puts these inside circle bubbles at the ends of each axis — e.g. 'A'..'F'
    # for horizontal axes, '1'..'8' for vertical axes.
    label_texts: list[tuple[str, float, float]] = []
    for e in msp:
        if e.dxf.layer not in set(grid_layers):
            continue
        if e.dxftype() == "TEXT":
            txt = (e.dxf.text or "").strip()
            pos = e.dxf.insert
            if txt:
                label_texts.append((txt, float(pos.x), float(pos.y)))
        elif e.dxftype() == "MTEXT":
            txt = (e.text or "").strip()
            pos = e.dxf.insert
            if txt:
                label_texts.append((txt, float(pos.x), float(pos.y)))

    _LABEL_TOL = 500.0  # mm — bubble text typically sits ≤100mm off the axis

    def _label_for(pos_val: float, axis: str, fallback: str) -> str:
        """Pick the TEXT closest to `pos_val` along `axis` ('H' or 'V')."""
        best_txt, best_d = None, 1e18
        for txt, tx, ty in label_texts:
            d = abs(ty - pos_val) if axis == "H" else abs(tx - pos_val)
            if d < best_d and d <= _LABEL_TOL:
                best_d, best_txt = d, txt
        return best_txt if best_txt is not None else fallback

    grid_lines: list[GridLine] = []

    for i, pos in enumerate(sorted(h_positions)):
        grid_lines.append(GridLine(
            axis_label=_label_for(pos, "H", str(i + 1)),
            direction="H", position=pos,
        ))
    for i, pos in enumerate(sorted(v_positions)):
        grid_lines.append(GridLine(
            axis_label=_label_for(pos, "V", str(i + 1)),
            direction="V", position=pos,
        ))

    return grid_lines


# ---------------------------------------------------------------------------
# Wall line extraction
# ---------------------------------------------------------------------------

def _extract_wall_lines(doc, msp) -> list[WallLine]:
    """
    Extract wall-related LINEs and LWPOLYLINE segments.

    Wall layers:
    - ``C151_壁心``       → wall centre lines
    - ``F105_RC壁``       → RC wall outline (thin)
    - ``F106_RC壁``       → RC wall structure lines
    - ``A421_壁：ＲＣ``   → RC wall (architectural)
    - ``A422_壁：ＡＬＣ`` → ALC wall
    - ``A423_壁：PCa``    → Precast concrete wall
    - ``A424_壁：パネル`` → Panel wall
    - ``A441_壁：ＬＧＳ`` → LGS wall
    - ``A443_壁：ＣＢ``   → Concrete block wall
    - ``A521_壁：仕上``   → wall finish lines
    - ``A561_耐火被覆``   → fire-resistant covering
    - ``★既存躯体外壁``   → existing exterior wall
    """
    wall_keywords = [
        "C151_壁心",
        "C151_壁芯",
        "F105_RC壁",
        "F106_RC壁",
        "A421_壁",
        "A422_壁",
        "A423_壁",
        "A424_壁",
        "A441_壁",
        "A443_壁",
        "A521_壁",
        "A561_耐火被覆",
        "★既存躯体外壁",
        # Fallback — catch non-standard naming: [建築]壁, [建築]A401_壁 etc.
        "_壁",
        "_外壁",
        "_RC壁",
        "]壁",    # bracketed discipline prefix: [建築]壁
        "]外壁",
        "既存壁",
        "耐火被覆",
    ]
    wall_layers = _find_layers_any(doc, wall_keywords)

    def _wall_type(layer_name: str) -> str:
        # Exterior wall detection takes precedence so "既存躯体外壁" and any
        # other layer whose name contains 外壁 groups together under "外壁".
        if "外壁" in layer_name:
            return "外壁"
        if "壁心" in layer_name or "C151" in layer_name:
            return "壁心"
        if "RC壁" in layer_name or "F105" in layer_name or "F106" in layer_name:
            return "RC壁"
        if "A421" in layer_name and "ＲＣ" in layer_name:
            return "RC壁"
        if "仕上" in layer_name or "A521" in layer_name:
            return "仕上"
        if "ＡＬＣ" in layer_name or "ALC" in layer_name or "A422" in layer_name:
            return "ALC"
        if "PCa" in layer_name or "A423" in layer_name:
            return "PCa"
        if "パネル" in layer_name or "A424" in layer_name:
            return "パネル"
        if "A441" in layer_name or "ＬＧＳ" in layer_name:
            return "LGS"
        if "ＣＢ" in layer_name or "A443" in layer_name:
            return "CB"
        if "耐火被覆" in layer_name or "A561" in layer_name:
            return "耐火被覆"
        return "不明"

    wall_lines: list[WallLine] = []

    def _wall_in_building(sx: float, sy: float, ex: float, ey: float) -> bool:
        return _segment_in_building_bbox(sx, sy, ex, ey)

    for entity in msp:
        layer = entity.dxf.layer
        if layer not in set(wall_layers):
            continue

        wtype = _wall_type(layer)

        if entity.dxftype() == "LINE":
            sx, sy = entity.dxf.start.x, entity.dxf.start.y
            ex, ey = entity.dxf.end.x, entity.dxf.end.y
            if not _wall_in_building(sx, sy, ex, ey):
                continue
            wall_lines.append(
                WallLine(start=(sx, sy), end=(ex, ey), layer=layer, wall_type=wtype)
            )

        elif entity.dxftype() == "LWPOLYLINE":
            pts = list(entity.get_points())
            for i in range(len(pts) - 1):
                sx, sy = float(pts[i][0]), float(pts[i][1])
                ex, ey = float(pts[i + 1][0]), float(pts[i + 1][1])
                if not _wall_in_building(sx, sy, ex, ey):
                    continue
                wall_lines.append(
                    WallLine(start=(sx, sy), end=(ex, ey), layer=layer, wall_type=wtype)
                )
            if entity.is_closed and len(pts) >= 2:
                sx, sy = float(pts[-1][0]), float(pts[-1][1])
                ex, ey = float(pts[0][0]), float(pts[0][1])
                if not _wall_in_building(sx, sy, ex, ey):
                    continue
                wall_lines.append(
                    WallLine(start=(sx, sy), end=(ex, ey), layer=layer, wall_type=wtype)
                )

    return wall_lines


# ---------------------------------------------------------------------------
# Step / recess line extraction
# ---------------------------------------------------------------------------

def _extract_step_lines(doc, msp) -> list[StepLine]:
    """Extract RC slab step lines (F108_3_RCスラブ段差線).

    床ヌスミ (F108_5) is intentionally excluded and handled separately by
    :func:`_extract_recess_polygons` — it represents localised floor
    recesses with a distinct visual meaning.

    Two kinds of artefacts frequently appear on this layer and are filtered:

    - Small closed polylines (bounding-box diagonal < 500 mm) are
      step-direction arrow symbols, not step boundaries.
    - Very long straight segments (> 15 m) are building-perimeter / slab-edge
      lines that some drafters place on the step layer; they form the
      building outline, not a boundary between FL regions. Typical step
      segments in these sample drawings average 2–3 m.
    """
    step_keywords = [
        "F108_3_RCスラブ段差線",
        "段差線",
        "スラブ段差",
    ]
    step_layers = set(_find_layers_any(doc, step_keywords))

    MAX_STEP_SEG_LEN_SQ = 15000.0 ** 2  # mm^2

    def _too_long(sx: float, sy: float, ex: float, ey: float) -> bool:
        dx = ex - sx; dy = ey - sy
        return (dx * dx + dy * dy) > MAX_STEP_SEG_LEN_SQ

    step_lines: list[StepLine] = []

    for entity in msp:
        layer = entity.dxf.layer
        if layer not in step_layers:
            continue

        if entity.dxftype() == "LINE":
            sx, sy = entity.dxf.start.x, entity.dxf.start.y
            ex, ey = entity.dxf.end.x, entity.dxf.end.y
            if _too_long(sx, sy, ex, ey):
                continue
            if _in_building_range((sx + ex) / 2, (sy + ey) / 2):
                step_lines.append(StepLine(start=(sx, sy), end=(ex, ey), layer=layer))

        elif entity.dxftype() == "LWPOLYLINE":
            pts = [(float(p[0]), float(p[1])) for p in entity.get_points()]
            if len(pts) < 2:
                continue
            if entity.closed and len(pts) >= 3:
                xs = [p[0] for p in pts]
                ys = [p[1] for p in pts]
                diag2 = (max(xs) - min(xs)) ** 2 + (max(ys) - min(ys)) ** 2
                if diag2 < 500.0 ** 2:
                    continue
            for i in range(len(pts) - 1):
                sx, sy = pts[i]
                ex, ey = pts[i + 1]
                if _too_long(sx, sy, ex, ey):
                    continue
                if _in_building_range((sx + ex) / 2, (sy + ey) / 2):
                    step_lines.append(
                        StepLine(start=(sx, sy), end=(ex, ey), layer=layer)
                    )

    # Drop detail-drawing step fragments in sheet margins.
    return [
        s for s in step_lines
        if _segment_in_building_bbox(s.start[0], s.start[1], s.end[0], s.end[1])
    ]


def _extract_recess_polygons(doc, msp):
    """Extract 床ヌスミ (floor-recess) outlines as closed polygons (F108_5).

    Open line entities on this layer are ignored by design — a recess is
    conceptually a bounded area, so only closed polylines are meaningful.
    """
    from .models import RecessPolygon
    keywords = ["F108_5_床ヌスミ", "床ヌスミ"]
    layers = set(_find_layers_any(doc, keywords))

    polys: list[RecessPolygon] = []
    for entity in msp:
        if entity.dxf.layer not in layers:
            continue
        if entity.dxftype() != "LWPOLYLINE" or not entity.closed:
            continue
        pts = [(float(p[0]), float(p[1])) for p in entity.get_points()]
        if len(pts) < 3:
            continue
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        if not _in_building_range(cx, cy):
            continue
        polys.append(RecessPolygon(vertices=pts, layer=entity.dxf.layer))
    return polys


# ---------------------------------------------------------------------------
# Column line extraction
# ---------------------------------------------------------------------------

def _extract_column_lines(doc, msp) -> list[ColumnLine]:
    """
    Extract RC column outlines (F102_RC柱), S column outlines (F201_Ｓ柱),
    and wall finish lines (A521_壁：仕上) as ColumnLine objects.
    """
    col_keywords = [
        "F102_RC柱", "F101_RC柱",
        "F201_Ｓ柱", "F201_S柱",
        "A412_柱", "A411_柱", "A511_柱",
        "F204_鉄骨間柱",
        "間柱",                         # incl. エレベーター_間柱
        "エレベーター",                 # elevator frame steelwork
        "F203_ブレース", "ブレース",    # steel braces between columns
        "F108_7_根巻きコン", "根巻",    # column-base concrete collar
        "鉄骨ジョイント", "ジョイント",
        "鉄骨対応",
        "鉄筋_柱", "鉄器_柱",
        "A521_壁：仕上", "A521_壁:仕上",
        "_柱",
        "_RC柱",
        "_S柱", "_Ｓ柱",
        "]柱",
    ]
    col_layers = _find_layers_any(doc, col_keywords)

    col_lines: list[ColumnLine] = []

    # ARC approximation: S柱 profiles and elevator frame arcs are stored as
    # ARCs in DXF. Convert each ARC into a polyline of short segments so the
    # rounded portions render through the existing ColumnLine path.
    _ARC_SEGMENTS = 16
    col_layer_set = set(col_layers)

    def _push_line(sx: float, sy: float, ex: float, ey: float, lyr: str) -> None:
        col_lines.append(ColumnLine(start=(sx, sy), end=(ex, ey), layer=lyr))

    def _push_arc_from(cx: float, cy: float, r: float, sa: float, ea: float, lyr: str) -> None:
        if ea < sa:
            ea += 2.0 * math.pi
        step = (ea - sa) / _ARC_SEGMENTS
        prev_x = cx + r * math.cos(sa)
        prev_y = cy + r * math.sin(sa)
        for i in range(1, _ARC_SEGMENTS + 1):
            a = sa + step * i
            nx = cx + r * math.cos(a)
            ny = cy + r * math.sin(a)
            _push_line(prev_x, prev_y, nx, ny, lyr)
            prev_x, prev_y = nx, ny

    def _extract_from(entity, ox: float = 0.0, oy: float = 0.0,
                      sx: float = 1.0, sy: float = 1.0,
                      cos_r: float = 1.0, sin_r: float = 0.0,
                      depth: int = 0) -> None:
        """Pull column geometry from an entity, applying any nested INSERT
        transform accumulated along the way. Only the entity's OWN layer must
        be a column layer — INSERTs themselves can sit on any parent layer."""
        if depth > 3:
            return
        k = entity.dxftype()

        def _xf(px: float, py: float) -> tuple[float, float]:
            lx = px * sx
            ly = py * sy
            return (ox + lx * cos_r - ly * sin_r,
                    oy + lx * sin_r + ly * cos_r)

        layer = entity.dxf.layer

        if k == "INSERT":
            block = doc.blocks.get(entity.dxf.name)
            if block is None:
                return
            try:
                ix = float(entity.dxf.insert.x)
                iy = float(entity.dxf.insert.y)
            except Exception:
                return
            isx = float(getattr(entity.dxf, "xscale", 1.0) or 1.0)
            isy = float(getattr(entity.dxf, "yscale", 1.0) or 1.0)
            rot = math.radians(float(getattr(entity.dxf, "rotation", 0.0) or 0.0))
            new_cos = cos_r * math.cos(rot) - sin_r * math.sin(rot)
            new_sin = sin_r * math.cos(rot) + cos_r * math.sin(rot)
            ins_x, ins_y = _xf(ix, iy)
            for be in block:
                _extract_from(be, ins_x, ins_y, sx * isx, sy * isy,
                              new_cos, new_sin, depth + 1)
            return

        if layer not in col_layer_set:
            return

        try:
            if k == "LINE":
                s = entity.dxf.start
                e = entity.dxf.end
                a = _xf(float(s.x), float(s.y))
                b = _xf(float(e.x), float(e.y))
                _push_line(a[0], a[1], b[0], b[1], layer)
            elif k == "LWPOLYLINE":
                pts = [_xf(float(p[0]), float(p[1])) for p in entity.get_points()]
                for i in range(len(pts) - 1):
                    _push_line(pts[i][0], pts[i][1], pts[i+1][0], pts[i+1][1], layer)
                if getattr(entity, "is_closed", False) and len(pts) >= 2:
                    _push_line(pts[-1][0], pts[-1][1], pts[0][0], pts[0][1], layer)
            elif k == "ARC":
                cx, cy = _xf(float(entity.dxf.center.x), float(entity.dxf.center.y))
                r = float(entity.dxf.radius) * (abs(sx) + abs(sy)) / 2.0
                rot_off = math.atan2(sin_r, cos_r)
                sa = math.radians(float(entity.dxf.start_angle)) + rot_off
                ea = math.radians(float(entity.dxf.end_angle)) + rot_off
                _push_arc_from(cx, cy, r, sa, ea, layer)
        except Exception:
            return

    for entity in msp:
        _extract_from(entity)

    # Drop detail-drawing columns that land in the sheet margins.
    return [
        c for c in col_lines
        if _segment_in_building_bbox(c.start[0], c.start[1], c.end[0], c.end[1])
    ]


# ---------------------------------------------------------------------------
# Beam extraction — F103_RC梁 / F202_Ｓ梁 / 付帯梁
# ---------------------------------------------------------------------------

def _extract_beam_lines(doc, msp) -> list[BeamLine]:
    """Pull RC + Steel beam outlines from their conventional layers."""
    beam_keywords = [
        "F103_RC梁", "F104_RC梁", "F105_RC梁",
        "F202_Ｓ梁", "F202_S梁",
        "付帯梁",
        "_梁", "]梁",
    ]
    beam_layers = _find_layers_any(doc, beam_keywords)

    def _beam_type(layer_name: str) -> str:
        if "付帯" in layer_name:
            return "付帯梁"
        if "RC梁" in layer_name or "F103" in layer_name or "F104" in layer_name:
            return "RC梁"
        if "Ｓ梁" in layer_name or "S梁" in layer_name or "F202" in layer_name:
            return "S梁"
        return "不明"

    beam_lines: list[BeamLine] = []
    for entity in msp:
        layer = entity.dxf.layer
        if layer not in set(beam_layers):
            continue
        btype = _beam_type(layer)
        k = entity.dxftype()
        if k == "LINE":
            sx, sy = entity.dxf.start.x, entity.dxf.start.y
            ex, ey = entity.dxf.end.x, entity.dxf.end.y
            beam_lines.append(BeamLine(start=(sx, sy), end=(ex, ey), layer=layer, beam_type=btype))
        elif k == "LWPOLYLINE":
            pts = list(entity.get_points())
            for i in range(len(pts) - 1):
                sx, sy = float(pts[i][0]), float(pts[i][1])
                ex, ey = float(pts[i + 1][0]), float(pts[i + 1][1])
                beam_lines.append(BeamLine(start=(sx, sy), end=(ex, ey), layer=layer, beam_type=btype))
            if getattr(entity, "is_closed", False) and len(pts) >= 2:
                sx, sy = float(pts[-1][0]), float(pts[-1][1])
                ex, ey = float(pts[0][0]), float(pts[0][1])
                beam_lines.append(BeamLine(start=(sx, sy), end=(ex, ey), layer=layer, beam_type=btype))

    return [
        b for b in beam_lines
        if _segment_in_building_bbox(b.start[0], b.start[1], b.end[0], b.end[1])
    ]


# ---------------------------------------------------------------------------
# Dimension line extraction
# ---------------------------------------------------------------------------

def _extract_dim_lines(doc, msp) -> list[DimLine]:
    """
    Extract DIMENSION entities from modelspace.

    For each DIMENSION we record:
    - layer
    - measurement  (``actual_measurement`` attribute, falls back to 0.0)
    - defpoint1, defpoint2  (first two definition points)
    - text_override  (``text`` attribute if non-empty, else None)
    """
    dim_lines: list[DimLine] = []

    for entity in msp:
        if entity.dxftype() != "DIMENSION":
            continue

        try:
            layer = entity.dxf.layer

            measurement = entity.dxf.get("actual_measurement", 0.0)
            if measurement is None:
                measurement = 0.0

            try:
                dp = entity.dxf.defpoint
                defpoint1 = (dp.x, dp.y)
            except Exception:
                defpoint1 = (0.0, 0.0)

            try:
                dp2 = entity.dxf.defpoint2
                defpoint2 = (dp2.x, dp2.y)
            except Exception:
                defpoint2 = (0.0, 0.0)

            try:
                dp3 = entity.dxf.defpoint3
                defpoint3 = (dp3.x, dp3.y)
            except Exception:
                defpoint3 = (0.0, 0.0)

            angle: float | None = None
            try:
                raw_angle = entity.dxf.get("angle", None)
                if raw_angle is not None:
                    angle = float(raw_angle)
                else:
                    # DXF spec: missing angle on linear dim means 0 (horizontal)
                    dim_base_type = entity.dxf.get("dimtype", 0) & 0x0F
                    if dim_base_type == 0:
                        angle = 0.0
            except Exception:
                pass

            text_override: str | None = None
            try:
                txt = entity.dxf.text
                if txt and txt.strip() and txt.strip() != "<>":
                    text_override = txt.strip()
            except Exception:
                pass

            # Filter to building range using defpoint1
            if not _in_building_range(defpoint1[0], defpoint1[1]):
                # Also try defpoint2 if available
                if not _in_building_range(defpoint2[0], defpoint2[1]):
                    continue

            dim_lines.append(
                DimLine(
                    layer=layer,
                    measurement=float(measurement),
                    defpoint1=defpoint1,
                    defpoint2=defpoint2,
                    defpoint3=defpoint3,
                    angle=float(angle) if angle is not None else None,
                    text_override=text_override,
                )
            )
        except Exception:
            # Skip malformed DIMENSION entities
            continue

    # Drop dim lines whose reference points are outside the building bbox
    # (legend / title-block dimensions on the same layer).
    dim_lines = [
        d for d in dim_lines
        if _point_in_building_bbox(d.defpoint1[0], d.defpoint1[1])
        and _point_in_building_bbox(d.defpoint2[0], d.defpoint2[1])
        and _point_in_building_bbox(d.defpoint3[0], d.defpoint3[1])
    ]

    return dim_lines


# ---------------------------------------------------------------------------
# Text → Sleeve label association
# ---------------------------------------------------------------------------

def _attach_label_texts(sleeves: list[Sleeve], doc, msp, step_lines: list[StepLine] | None = None) -> None:
    """
    Associate TEXT entities near each sleeve with ``label_text`` and ``fl_text``.

    Strategy:
    - Collect all TEXT/MTEXT entities on sleeve-related layers that are within
      the building range.
    - For each sleeve, find the nearest text matching a φ-pattern → label_text.
    - For each sleeve, find the nearest text matching an FL-pattern → fl_text.
      * First look on the sleeve's own layer (existing logic, radius 1500 mm).
      * If no FL found there, fall back to the nearest FL zone point from slab
        label layers (F308_スラブ / スラブラベル) — no radius limit, pure
        nearest-neighbour.  The stored value is just the FL part (e.g.
        ``"FL-225"``), not the full slab label text.
    - Search radius: _LABEL_SEARCH_RADIUS (1500 mm) for sleeve-layer texts.
    - Y-coordinate proximity is weighted double (match spec: Y-coord priority).
    """
    sleeve_layers = set(_find_layers(doc, "スリーブ"))

    # Also include 衛生 pipe layers (e.g. [衛生]雨水(STPG)) which carry
    # equipment type codes (RD, KD, etc.) near sleeves
    pipe_layers = set(
        l.dxf.name for l in doc.layers
        if l.dxf.name.startswith("[衛生]") and "スリーブ" not in l.dxf.name
    )
    search_layers = sleeve_layers | pipe_layers

    # Build candidate text list from sleeve + pipe layers
    sleeve_candidates: list[tuple[float, float, str]] = []  # (x, y, text)

    for entity in msp:
        if entity.dxftype() not in ("TEXT", "MTEXT"):
            continue

        layer = entity.dxf.layer
        if layer not in search_layers:
            continue

        try:
            if entity.dxftype() == "TEXT":
                raw = entity.dxf.text or ""
            else:
                raw = entity.plain_mtext() or ""

            txt = raw.strip()
            if not txt:
                continue

            pos = entity.dxf.insert
            x, y = float(pos.x), float(pos.y)

            if not _in_building_range(x, y):
                continue

            sleeve_candidates.append((x, y, txt))
        except Exception:
            continue

    # Build FL zones from slab label layers for fallback nearest-zone lookup
    fl_zones = _build_fl_zones(doc, msp)

    for sleeve in sleeves:
        cx, cy = sleeve.center

        phi_hits: list[tuple[float, str]] = []  # (dist, txt) — all φ/外径 matches
        best_code_dist = float("inf")  # equipment type code (RD, CW, etc.)
        best_code_txt: str | None = None
        best_fl_dist = float("inf")
        best_fl_txt: str | None = None

        for tx, ty, txt in sleeve_candidates:
            # Y-priority weighted distance
            dx = tx - cx
            dy = (ty - cy) * 0.5  # halve Y so it's "double-weighted" in proximity
            dist = math.hypot(dx, dy)

            if dist > _LABEL_SEARCH_RADIUS:
                continue

            # Equipment type code: whitelist of known pipe/duct codes
            if (_RE_EQUIP_CODE.match(txt) and dist < best_code_dist):
                best_code_dist = dist
                best_code_txt = txt

            if _RE_PHI.search(txt):
                phi_hits.append((dist, txt))

            if _RE_FL.search(txt) and dist < best_fl_dist:
                best_fl_dist = dist
                best_fl_txt = txt

        # Sort φ hits by distance
        phi_hits.sort()
        best_phi_txt = phi_hits[0][1] if phi_hits else None

        # label_text: prefer equipment code, fall back to φ text (original behaviour)
        if best_code_txt is not None:
            sleeve.label_text = best_code_txt
        elif best_phi_txt is not None:
            sleeve.label_text = best_phi_txt

        # diameter_text: combine all nearby φ/外径 texts (they may be split
        # across multiple TEXT entities, e.g. "V 175φ" + "(外径180φ)100A").
        # Dedupe by text content to avoid emitting "φ124 φ124 φ124 φ124 φ124"
        # when the drafter copied the same callout multiple times.
        if phi_hits:
            seen: set[str] = set()
            uniq: list[str] = []
            for _, t in phi_hits:
                key = t.strip()
                if key and key not in seen:
                    seen.add(key)
                    uniq.append(t)
            sleeve.diameter_text = " ".join(uniq)

        if best_fl_txt is not None:
            # Found FL on the sleeve's own layer — use as-is
            sleeve.fl_text = best_fl_txt
        elif fl_zones:
            # Fallback: find the nearest slab FL zone point that is NOT
            # blocked by a step line (段差線 acts as a barrier).
            from .geometry import ray_blocked_by_steps
            step_segs = [(s.start, s.end) for s in step_lines]
            best_zone_dist = float("inf")
            best_zone_fl: str | None = None
            for zx, zy, fl_val in fl_zones:
                dist = math.hypot(zx - cx, zy - cy)
                if dist < best_zone_dist:
                    if not ray_blocked_by_steps((cx, cy), (zx, zy), step_segs):
                        best_zone_dist = dist
                        best_zone_fl = fl_val
            if best_zone_fl is not None:
                sleeve.fl_text = best_zone_fl


# ---------------------------------------------------------------------------
# FL zone helpers (slab label → positional FL lookup)
# ---------------------------------------------------------------------------

def _build_fl_zones(doc, msp) -> list[tuple[float, float, str]]:
    """
    Collect all TEXT/MTEXT entities on slab label layers (F308_スラブ,
    スラブラベル) that contain an FL pattern, and return them as a list of
    ``(x, y, fl_value)`` tuples where *fl_value* is the normalised FL string
    (e.g. ``"FL-225"``).

    These tuples form "FL zones": a sleeve with no FL text on its own layer can
    find the nearest zone point and inherit its FL value.
    """
    slab_layers = set(_find_layers_any(doc, ["F308_スラブ", "スラブラベル"]))
    fl_pattern = re.compile(r"FL\s*[+\-]\s*\d+", re.IGNORECASE)

    zones: list[tuple[float, float, str]] = []

    for entity in msp:
        if entity.dxf.layer not in slab_layers:
            continue
        if entity.dxftype() not in ("TEXT", "MTEXT"):
            continue

        try:
            if entity.dxftype() == "TEXT":
                raw = entity.dxf.text or ""
            else:
                raw = entity.plain_mtext() or ""

            pos = entity.dxf.insert
            x, y = float(pos.x), float(pos.y)

            if not _in_building_range(x, y):
                continue

            match = fl_pattern.search(raw)
            if match:
                # Normalise: remove spaces e.g. 'FL - 750' → 'FL-750'
                fl_value = re.sub(r"\s+", "", match.group(0)).upper()
                zones.append((x, y, fl_value))
        except Exception:
            continue

    return zones


# ---------------------------------------------------------------------------
# Slab zone heatmap extraction
# ---------------------------------------------------------------------------

def _parse_fl_value(fl_text: str) -> int | None:
    """Parse 'FL+40' or 'FL-360' into integer mm value."""
    m = re.match(r"FL\s*([+\-])\s*(\d+)", fl_text, re.IGNORECASE)
    if not m:
        return None
    sign = 1 if m.group(1) == "+" else -1
    return sign * int(m.group(2))


def _extract_slab_zones(doc, msp) -> list[SlabZone]:
    """
    Collect slab level texts from:
    1. F308_スラブ / スラブラベル layers (slab labels)
    2. 段差記号 layer (step level annotations like FL+40, FL-60)

    Returns SlabZone objects with position and parsed FL value.
    """
    fl_pattern = re.compile(r"FL\s*[+\-]\s*\d+", re.IGNORECASE)
    target_layers = set(
        _find_layers_any(doc, ["F308_スラブ", "スラブラベル"])
        + _find_layers_any(doc, ["段差記号", "段差"])
    )

    zones: list[SlabZone] = []
    seen: set[tuple[float, float, str]] = set()

    for entity in msp:
        if entity.dxf.layer not in target_layers:
            continue
        if entity.dxftype() not in ("TEXT", "MTEXT"):
            continue

        try:
            if entity.dxftype() == "TEXT":
                raw = entity.dxf.text or ""
            else:
                raw = entity.plain_mtext() or ""

            pos = entity.dxf.insert
            x, y = float(pos.x), float(pos.y)

            if not _in_building_range(x, y):
                continue

            match = fl_pattern.search(raw)
            if match:
                fl_text = re.sub(r"\s+", "", match.group(0)).upper()
                fl_val = _parse_fl_value(fl_text)
                if fl_val is None:
                    continue

                key = (round(x, 0), round(y, 0), fl_text)
                if key in seen:
                    continue
                seen.add(key)

                zones.append(SlabZone(x=x, y=y, fl_text=fl_text, fl_value=fl_val))
        except Exception:
            continue

    return zones


# ---------------------------------------------------------------------------
# P-N label extraction
# ---------------------------------------------------------------------------

def _extract_pn_labels(doc, msp) -> list[PnLabel]:
    """Extract P-N-xx texts from the drawing."""
    pn_pattern = re.compile(r"P-N-(\d+)")
    labels: list[PnLabel] = []
    for entity in msp:
        if entity.dxftype() != "TEXT":
            continue
        raw = (entity.dxf.text or "").strip()
        m = pn_pattern.search(raw)
        if not m:
            continue
        x, y = entity.dxf.insert.x, entity.dxf.insert.y
        if not _in_building_range(x, y):
            continue
        labels.append(PnLabel(x=x, y=y, text=raw, number=int(m.group(1))))
    labels.sort(key=lambda p: p.number)
    return labels


# ---------------------------------------------------------------------------
# P-N leader line extraction (LINE on 衛生通常 connecting to LWPOLYLINE frame)
# ---------------------------------------------------------------------------

def _extract_pn_pointers(
    doc, msp, pn_labels: list[PnLabel]
) -> dict[str, tuple[float, float]]:
    """
    For each P-N label, find its pointer (leader LINE or arrow INSERT) and
    return the far-end coordinate pointing toward the sleeve.

    Two pointer types coexist:
    1. [衛生]通常 LINE — one end touching the P-N frame, other end → sleeve
    2. [衛生]スリーブ INSERT — LWPOLY(3pts)+LINE arrow block near P-N, tip → sleeve

    Returns {pn_text: (far_x, far_y)}.
    """
    if not pn_labels:
        return {}

    衛生_layers = set(_find_layers_any(doc, ["衛生"]))
    通常_layers = {l for l in 衛生_layers if "通常" in l}
    スリーブ_layers = {l for l in 衛生_layers if "スリーブ" in l}

    result: dict[str, tuple[float, float]] = {}

    # --- Collect P-N frames (LWPOLY 4pts on 衛生通常) ---
    frames: list[tuple[float, float, list[tuple[float, float]]]] = []
    for entity in msp:
        if entity.dxftype() != "LWPOLYLINE":
            continue
        if entity.dxf.layer not in 通常_layers:
            continue
        pts = [(float(p[0]), float(p[1])) for p in entity.get_points()]
        if len(pts) != 4:
            continue
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        w, h = max(xs) - min(xs), max(ys) - min(ys)
        if 100 < w < 800 and 50 < h < 800:
            frames.append((sum(xs) / 4, sum(ys) / 4, pts))

    pn_frames: dict[str, list[tuple[float, float]]] = {}
    for pn in pn_labels:
        best_d = 500.0
        best_pts: list[tuple[float, float]] | None = None
        for fcx, fcy, fpts in frames:
            d = math.hypot(fcx - pn.x, fcy - pn.y)
            if d < best_d:
                best_d = d
                best_pts = fpts
        if best_pts is not None:
            pn_frames[pn.text] = best_pts

    # --- Source A: Leader LINEs on [衛生]通常 touching frame vertices ---
    lines_通常: list[tuple[float, float, float, float]] = []
    for entity in msp:
        if entity.dxftype() != "LINE" or entity.dxf.layer not in 通常_layers:
            continue
        sx, sy = float(entity.dxf.start.x), float(entity.dxf.start.y)
        ex, ey = float(entity.dxf.end.x), float(entity.dxf.end.y)
        if math.hypot(ex - sx, ey - sy) > 200:
            lines_通常.append((sx, sy, ex, ey))

    for pn_text, frame_pts in pn_frames.items():
        best_d = 100.0
        best_far: tuple[float, float] | None = None
        for sx, sy, ex, ey in lines_通常:
            for fpt in frame_pts:
                d_s = math.hypot(sx - fpt[0], sy - fpt[1])
                if d_s < best_d:
                    best_d = d_s
                    best_far = (ex, ey)
                d_e = math.hypot(ex - fpt[0], ey - fpt[1])
                if d_e < best_d:
                    best_d = d_e
                    best_far = (sx, sy)
        if best_far is not None:
            result[pn_text] = best_far

    # --- Source B: Arrow/Leader INSERTs on [衛生]スリーブ ---
    # Two sub-types:
    #  B1) Arrow INSERT with closed 3-pt LWPOLYLINE (triangle) + LINE
    #      → tip = vertex opposite longest edge of triangle
    #  B2) Leader INSERT with open LWPOLYLINE (bent leader line)
    #      → tip = first vertex, tail = last vertex (near P-N text)
    arrow_inserts: list[tuple[float, float, tuple[float, float]]] = []  # (origin_x, origin_y, tip_world)
    leader_inserts: list[tuple[tuple[float, float], tuple[float, float]]] = []  # (tip_world, tail_world)

    for entity in msp:
        if entity.dxftype() != "INSERT" or entity.dxf.layer not in スリーブ_layers:
            continue
        name = entity.dxf.name
        if any(kw in name for kw in ("スリーブ", "箱", "電気パイプ")):
            continue
        try:
            block = doc.blocks.get(name)
        except Exception:
            continue

        ix, iy = float(entity.dxf.insert.x), float(entity.dxf.insert.y)
        if not _in_building_range(ix, iy):
            continue

        # Classify block contents
        found_leader = False
        for be in block:
            if be.dxftype() != "LWPOLYLINE":
                continue
            pts = [(float(p[0]), float(p[1])) for p in be.get_points()]

            if not be.closed and len(pts) >= 2:
                # B2: Open polyline = bent leader line
                total_len = sum(
                    math.hypot(pts[i+1][0]-pts[i][0], pts[i+1][1]-pts[i][1])
                    for i in range(len(pts)-1)
                )
                if total_len >= 200:
                    tip_world = (ix + pts[0][0], iy + pts[0][1])
                    tail_world = (ix + pts[-1][0], iy + pts[-1][1])
                    leader_inserts.append((tip_world, tail_world))
                    found_leader = True

        if not found_leader:
            # B1: Arrow INSERT — find tip from triangle + LINE geometry
            tip_local = (0.0, 0.0)
            best_d = 0.0
            has_geom = False
            for be in block:
                if be.dxftype() == "LWPOLYLINE":
                    pts = [(float(p[0]), float(p[1])) for p in be.get_points()]
                    if len(pts) == 3:
                        edges = [
                            (math.hypot(pts[1][0]-pts[2][0], pts[1][1]-pts[2][1]), 0),
                            (math.hypot(pts[0][0]-pts[2][0], pts[0][1]-pts[2][1]), 1),
                            (math.hypot(pts[0][0]-pts[1][0], pts[0][1]-pts[1][1]), 2),
                        ]
                        longest_edge_opposite = max(edges, key=lambda e: e[0])[1]
                        tip_local = pts[longest_edge_opposite]
                        best_d = math.hypot(tip_local[0], tip_local[1])
                    else:
                        for p in pts:
                            d = math.hypot(p[0], p[1])
                            if d > best_d:
                                best_d = d
                                tip_local = p
                    has_geom = True
                elif be.dxftype() == "LINE":
                    for pt in (be.dxf.start, be.dxf.end):
                        px, py = float(pt.x), float(pt.y)
                        d = math.hypot(px, py)
                        if d > best_d:
                            best_d = d
                            tip_local = (px, py)
                    has_geom = True
            if has_geom and best_d >= 100:
                arrow_inserts.append((ix, iy, (ix + tip_local[0], iy + tip_local[1])))

    # Match arrow INSERTs (B1) to P-N labels
    used_arrows: set[int] = set()
    for pn in sorted(pn_labels, key=lambda p: p.number):
        if pn.text in result:
            continue
        best_arrow_d = float("inf")
        best_arrow_idx: int | None = None
        best_tip: tuple[float, float] | None = None
        for i, (aix, aiy, (tip_x, tip_y)) in enumerate(arrow_inserts):
            if i in used_arrows:
                continue
            d_origin = math.hypot(aix - pn.x, aiy - pn.y)
            d_tip = math.hypot(tip_x - pn.x, tip_y - pn.y)
            if d_origin < best_arrow_d and d_tip > d_origin:
                best_arrow_d = d_origin
                best_arrow_idx = i
                best_tip = (tip_x, tip_y)
        if best_arrow_idx is not None and best_tip is not None and best_arrow_d < 3000:
            result[pn.text] = best_tip
            used_arrows.add(best_arrow_idx)

    # Match leader INSERTs (B2) to P-N labels using global best-match.
    # Direction is determined at matching time: for each (pn, leader) pair,
    # the end nearer to the P-N text is the P-N side; the other end is the
    # sleeve tip.  We store both ends and resolve direction per-pair.
    candidates: list[tuple[float, int, str, tuple[float, float], tuple[float, float]]] = []

    for pn in pn_labels:
        if pn.text in result:
            continue
        for i, (end_a, end_b) in enumerate(leader_inserts):
            d_a = math.hypot(end_a[0] - pn.x, end_a[1] - pn.y)
            d_b = math.hypot(end_b[0] - pn.x, end_b[1] - pn.y)
            near_d = min(d_a, d_b)
            if near_d < 3000:
                candidates.append((near_d, i, pn.text, end_a, end_b))
    candidates.sort()

    used_leaders: set[int] = set()
    used_pn_texts: set[str] = set()
    for _d, leader_idx, pn_text, end_a, end_b in candidates:
        if leader_idx in used_leaders or pn_text in used_pn_texts:
            continue
        # Find which P-N label this is
        pn = next((p for p in pn_labels if p.text == pn_text), None)
        if pn is None:
            continue
        # Determine direction: end closer to P-N = tail, other = sleeve tip
        d_a = math.hypot(end_a[0] - pn.x, end_a[1] - pn.y)
        d_b = math.hypot(end_b[0] - pn.x, end_b[1] - pn.y)
        sleeve_end = end_b if d_a <= d_b else end_a
        result[pn_text] = sleeve_end
        used_leaders.add(leader_idx)
        used_pn_texts.add(pn_text)

    return result


# ---------------------------------------------------------------------------
# P-N to sleeve assignment
# ---------------------------------------------------------------------------

def _attach_pn_numbers(sleeves: list[Sleeve], pn_labels: list[PnLabel],
                       doc=None, msp=None) -> None:
    """
    Assign each P-N label to its corresponding sleeve.

    Three patterns in the DXF:
    1. P-N text + pointer (LINE or INSERT) → use pointer far-end for matching
    2. P-N text only (no pointer) → nearest-neighbour from text position
    3. Sleeve without P-N → not assigned

    P-N numbers are only assigned to 衛生 (plumbing) sleeves.
    空調/電気 sleeves do not carry P-N numbers.

    When a P-N label is matched to a sleeve, also look for diameter/FL texts
    near the P-N text and assign them to the sleeve if the sleeve doesn't
    already have them (from the initial label_text pass).
    """
    if not pn_labels or not sleeves:
        return

    used_sleeves: set[str] = set()
    used_pns: set[str] = set()

    # Pre-collect candidate texts near P-N labels for diameter/FL enrichment.
    # Key: pn.text → list of (dist, txt) within PN_ENRICH_RADIUS of P-N position
    _PN_ENRICH_RADIUS = 2000.0
    pn_nearby_texts: dict[str, list[tuple[float, str]]] = {}
    if doc is not None and msp is not None:
        # Collect all TEXT/MTEXT on sleeve + pipe layers
        _enrich_layers = set(_find_layers(doc, "スリーブ")) | {
            l.dxf.name for l in doc.layers
            if l.dxf.name.startswith("[衛生]") and "スリーブ" not in l.dxf.name
        }
        _enrich_texts: list[tuple[float, float, str]] = []
        for entity in msp:
            if entity.dxftype() not in ("TEXT", "MTEXT"):
                continue
            if entity.dxf.layer not in _enrich_layers:
                continue
            try:
                raw = (entity.dxf.text if entity.dxftype() == "TEXT" else entity.plain_mtext()) or ""
                raw = raw.strip()
                if not raw:
                    continue
                pos = entity.dxf.insert
                _enrich_texts.append((float(pos.x), float(pos.y), raw))
            except Exception:
                continue

        for pn in pn_labels:
            nearby = []
            for tx, ty, txt in _enrich_texts:
                d = math.hypot(tx - pn.x, ty - pn.y)
                if d < _PN_ENRICH_RADIUS:
                    nearby.append((d, txt))
            nearby.sort()
            pn_nearby_texts[pn.text] = nearby

    def _enrich_sleeve_from_pn(sleeve: Sleeve, pn_text: str) -> None:
        """Fill in diameter_text / fl_text from texts near the P-N label.

        Combines all φ/外径 texts near the P-N label (they may be split
        across multiple TEXT entities).
        """
        phi_texts: list[str] = []
        seen_phi: set[str] = set()
        best_fl: str | None = None
        for _d, txt in pn_nearby_texts.get(pn_text, []):
            if _RE_PHI.search(txt) and not re.match(r"P-N-", txt):
                key = txt.strip()
                if key and key not in seen_phi:
                    seen_phi.add(key)
                    phi_texts.append(txt)
            if best_fl is None and _RE_FL.search(txt):
                best_fl = txt
        if phi_texts:
            sleeve.diameter_text = " ".join(phi_texts)
        if best_fl is not None and sleeve.fl_text is None:
            sleeve.fl_text = best_fl

    # --- Phase 1: Pointer-based matching (LINE leaders + arrow INSERTs) ---
    # Use global best-match (greedy by smallest tip-to-sleeve distance)
    # to avoid earlier P-N numbers stealing sleeves meant for closer P-Ns.
    if doc is not None and msp is not None:
        leaders = _extract_pn_pointers(doc, msp, pn_labels)

        phase1_candidates: list[tuple[float, str, str, float, float]] = []  # (dist, pn_text, sleeve_id, tip_x, tip_y)
        for pn in pn_labels:
            if pn.text not in leaders:
                continue
            far_x, far_y = leaders[pn.text]
            for s in sleeves:
                if s.discipline != "衛生":
                    continue
                d = math.hypot(far_x - s.center[0], far_y - s.center[1])
                phase1_candidates.append((d, pn.text, s.id, far_x, far_y))
        phase1_candidates.sort()

        for d, pn_text, sleeve_id, far_x, far_y in phase1_candidates:
            if pn_text in used_pns or sleeve_id in used_sleeves:
                continue
            sleeve = next(s for s in sleeves if s.id == sleeve_id)
            sleeve.pn_number = pn_text
            _enrich_sleeve_from_pn(sleeve, pn_text)
            pn = next(p for p in pn_labels if p.text == pn_text)
            pn.arrow_verts = [(pn.x, pn.y), (far_x, far_y)]
            used_sleeves.add(sleeve_id)
            used_pns.add(pn_text)

    # --- Phase 2: Fallback nearest-neighbour for P-N without pointers ---
    for pn in sorted(pn_labels, key=lambda p: p.number):
        if pn.text in used_pns:
            continue
        best_dist = float("inf")
        best_sleeve: Sleeve | None = None
        for s in sleeves:
            if s.id in used_sleeves:
                continue
            if s.discipline != "衛生":
                continue
            dist = math.hypot(s.center[0] - pn.x, s.center[1] - pn.y)
            if dist < best_dist:
                best_dist = dist
                best_sleeve = s
        if best_sleeve is not None:
            best_sleeve.pn_number = pn.text
            _enrich_sleeve_from_pn(best_sleeve, pn.text)
            used_sleeves.add(best_sleeve.id)
            used_pns.add(pn.text)


# ---------------------------------------------------------------------------
# Slab label block extraction (S15, S16 etc. with level/thickness)
# ---------------------------------------------------------------------------

def _extract_slab_labels(doc, msp) -> list[SlabLabel]:
    """
    Extract slab info from INSERT blocks on F308_スラブ layers.
    Each block contains texts: slab number (S15), level (-60 or -545～-600),
    representative level ((-60)), thickness (165), (thickness).
    """
    layers = set(_find_layers_any(doc, ["F308_スラブ", "スラブラベル"]))
    labels: list[SlabLabel] = []

    for ins in msp:
        if ins.dxftype() != "INSERT" or ins.dxf.layer not in layers:
            continue
        x, y = ins.dxf.insert.x, ins.dxf.insert.y
        if not _in_building_range(x, y):
            continue

        block = doc.blocks.get(ins.dxf.name)
        if block is None:
            continue

        texts = [
            be.dxf.text.strip()
            for be in block
            if be.dxftype() == "TEXT" and be.dxf.text.strip() not in ("t", "h")
        ]

        slab_no = ""
        level = ""
        thickness = ""
        paren_values: list[str] = []

        for t in texts:
            if re.match(r"^D?S\d+", t):
                slab_no = t
            elif "\uff5e" in t or "～" in t or "~" in t:
                level = t
            elif re.match(r"^\(-?\+?\d+\)$", t):
                paren_values.append(re.search(r"-?\+?\d+", t).group())  # type: ignore[union-attr]
            elif re.match(r"^[+\-]?\d+$", t) and not slab_no:
                pass  # skip stray numbers before slab_no
            elif re.match(r"^[+\-]?\d+$", t) and not level:
                level = t

        # paren_values: first is level repr, second is thickness
        if len(paren_values) >= 2:
            if not level:
                level = paren_values[0]
            thickness = str(abs(int(paren_values[1])))
        elif len(paren_values) == 1:
            if not level:
                level = paren_values[0]

        # Try to get thickness from non-paren number after slab_no
        if not thickness:
            for t in texts:
                if re.match(r"^\d+$", t):
                    val = int(t)
                    if 50 <= val <= 500:
                        thickness = t
                        break

        if slab_no:
            labels.append(SlabLabel(x=x, y=y, slab_no=slab_no, level=level, thickness=thickness))

    # Drop detail-area slab labels (sheet margin).
    return [l for l in labels if _point_in_building_bbox(l.x, l.y)]


# ---------------------------------------------------------------------------
# Step level label extraction (段差記号テキスト)
# ---------------------------------------------------------------------------

def _extract_step_labels(doc, msp) -> list[SlabZone]:
    """
    Extract FL texts from step symbol layers (段差記号).
    These are labels like 'FL-60', 'FL±0' placed next to step lines
    indicating the level on each side of the step.
    """
    layers = set(_find_layers_any(doc, ["段差記号"]))
    fl_pattern = re.compile(r"FL\s*([±+\-])\s*(\d+)", re.IGNORECASE)
    labels: list[SlabZone] = []

    for entity in msp:
        if entity.dxf.layer not in layers:
            continue
        if entity.dxftype() != "TEXT":
            continue
        try:
            raw = (entity.dxf.text or "").strip()
            pos = entity.dxf.insert
            x, y = float(pos.x), float(pos.y)
            if not _point_in_building_bbox(x, y):
                continue

            match = fl_pattern.search(raw)
            if match:
                sign_char = match.group(1)
                num = int(match.group(2))
                if sign_char == "±":
                    val = 0
                elif sign_char == "+":
                    val = num
                else:
                    val = -num
                fl_text = f"FL{'+' if val > 0 else '±' if val == 0 else ''}{val if val != 0 else '0'}"
                labels.append(SlabZone(x=x, y=y, fl_text=fl_text, fl_value=val))
        except Exception:
            continue
    # Drop detail-area zone labels (sheet margin).
    return [l for l in labels if _point_in_building_bbox(l.x, l.y)]


# ---------------------------------------------------------------------------
# Slab outline extraction (RC立上り線 = slab edge lines)
# ---------------------------------------------------------------------------

def _extract_slab_outlines(doc, msp) -> list[SlabOutline]:
    """
    Extract slab outline lines from F108_2_RC立上り線 layer.
    These show where the slab edges / step-ups are.
    """
    layers = _find_layers_any(doc, ["F108_2_RC立上り", "F108_RC見え掛り"])
    outlines: list[SlabOutline] = []
    for entity in msp:
        if entity.dxf.layer not in set(layers):
            continue
        if entity.dxftype() == "LINE":
            sx, sy = entity.dxf.start.x, entity.dxf.start.y
            ex, ey = entity.dxf.end.x, entity.dxf.end.y
            if _in_building_range((sx + ex) / 2, (sy + ey) / 2):
                outlines.append(SlabOutline(start=(sx, sy), end=(ex, ey)))
    # Drop sheet-margin outlines (detail drawings).
    return [
        o for o in outlines
        if _segment_in_building_bbox(o.start[0], o.start[1], o.end[0], o.end[1])
    ]


# ---------------------------------------------------------------------------
# Base level definition detection
# ---------------------------------------------------------------------------

_RE_BASE_LEVEL_DEF = re.compile(r"\dFL\s*[＝=]")


def _detect_base_level_def(doc) -> bool:
    """Check if the drawing contains a base level definition (e.g. '1FL＝B1FL+5150').

    These are typically inside INSERT blocks (not directly in modelspace),
    so we search all block definitions.
    """
    for block in doc.blocks:
        for entity in block:
            if entity.dxftype() != "TEXT":
                continue
            try:
                txt = entity.dxf.text or ""
                if _RE_BASE_LEVEL_DEF.search(txt):
                    return True
            except Exception:
                continue
    return False


# ---------------------------------------------------------------------------
# Slab level extraction
# ---------------------------------------------------------------------------

def _extract_slab_level(doc, msp) -> str | None:
    """
    Return the most common slab level text (e.g. 'FL-750') from F308_スラブ
    and related layers within the building range.  Returns None if not found.
    """
    slab_layers = set(_find_layers_any(doc, ["F308_スラブ", "スラブラベル"]))

    from collections import Counter
    level_counts: Counter = Counter()

    fl_pattern = re.compile(r"FL\s*[+\-]\s*\d+", re.IGNORECASE)

    for entity in msp:
        if entity.dxf.layer not in slab_layers:
            continue
        if entity.dxftype() not in ("TEXT", "MTEXT"):
            continue

        try:
            if entity.dxftype() == "TEXT":
                raw = entity.dxf.text or ""
            else:
                raw = entity.plain_mtext() or ""

            pos = entity.dxf.insert
            if not _in_building_range(pos.x, pos.y):
                continue

            match = fl_pattern.search(raw)
            if match:
                # Normalise: remove spaces e.g. 'FL - 750' → 'FL-750'
                normalised = re.sub(r"\s+", "", match.group(0)).upper()
                level_counts[normalised] += 1
        except Exception:
            continue

    if not level_counts:
        return None

    # Return the most frequent value (the dominant slab level for this floor)
    return level_counts.most_common(1)[0][0]


# ---------------------------------------------------------------------------
# Water gradient extraction
# ---------------------------------------------------------------------------

def _extract_water_gradients(doc, msp) -> list:
    """Extract '水勾配' texts + arrow directions from A221_記入文字 layers."""
    from .models import WaterGradient

    layers = set()
    for layer in doc.layers:
        name = layer.dxf.name
        if "A221" in name and "記入" in name:
            layers.add(name)

    # Pass 1: collect gradient texts and arrow triangles
    grad_texts: list[tuple[float, float]] = []
    arrows: list[tuple[float, float, str]] = []  # (cx, cy, direction)

    for entity in msp:
        if entity.dxf.layer not in layers:
            continue
        try:
            if entity.dxftype() in ("TEXT", "MTEXT"):
                txt = entity.dxf.text if entity.dxftype() == "TEXT" else entity.plain_mtext()
                if txt and "水勾配" in txt:
                    pos = entity.dxf.insert
                    grad_texts.append((pos.x, pos.y))

            elif entity.dxftype() == "LWPOLYLINE":
                pts = list(entity.get_points())
                if len(pts) == 3 and not entity.closed:
                    coords = [(p[0], p[1]) for p in pts]
                    # Longest side's opposite vertex = arrow tip
                    sides = [
                        (math.hypot(coords[1][0] - coords[2][0], coords[1][1] - coords[2][1]), 0),
                        (math.hypot(coords[0][0] - coords[2][0], coords[0][1] - coords[2][1]), 1),
                        (math.hypot(coords[0][0] - coords[1][0], coords[0][1] - coords[1][1]), 2),
                    ]
                    _, tip_idx = max(sides, key=lambda s: s[0])
                    tip = coords[tip_idx]
                    base = [coords[i] for i in range(3) if i != tip_idx]
                    mid = ((base[0][0] + base[1][0]) / 2, (base[0][1] + base[1][1]) / 2)
                    dx = tip[0] - mid[0]
                    dy = tip[1] - mid[1]
                    angle = math.degrees(math.atan2(dy, dx))

                    if -45 <= angle < 45:
                        direction = "→"
                    elif 45 <= angle < 135:
                        direction = "↑"
                    elif -135 <= angle < -45:
                        direction = "↓"
                    else:
                        direction = "←"

                    cx = sum(c[0] for c in coords) / 3
                    cy = sum(c[1] for c in coords) / 3
                    arrows.append((cx, cy, direction))
        except Exception:
            continue

    # Pass 2: match each gradient text to nearest arrow within 1000mm
    results: list[WaterGradient] = []
    for gx, gy in grad_texts:
        direction = ""
        best_dist = 1000.0  # max match distance
        for ax, ay, d in arrows:
            dist = math.hypot(gx - ax, gy - ay)
            if dist < best_dist:
                best_dist = dist
                direction = d
        results.append(WaterGradient(x=gx, y=gy, direction=direction))

    # Drop gradients that live outside the building bbox (detail sheets
    # use the same A221 annotation layers at ±50 k coords).
    return [w for w in results if _point_in_building_bbox(w.x, w.y)]


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def parse_dxf(filepath: str | Path) -> FloorData:
    """
    Read a DXF file and return a :class:`FloorData` containing all parsed
    geometry needed by the check logic.

    Parameters
    ----------
    filepath:
        Path to the ``.dxf`` file.

    Returns
    -------
    FloorData
        Populated dataclass.
    """
    # Try strict readfile first; fall back to the recover mode when the
    # source DXF has structural issues like a truncated section or a
    # missing ENDSEC tag (we've seen this from third-party converters
    # that bail before writing the final footer). `recover.readfile`
    # silently fixes those inconsistencies and returns a usable Drawing.
    try:
        doc = ezdxf.readfile(str(filepath))
    except ezdxf.DXFStructureError:
        from ezdxf import recover
        doc, _auditor = recover.readfile(str(filepath))
    msp = doc.modelspace()

    sleeves = _extract_sleeves(doc, msp)

    grid_lines = _extract_grid_lines(doc, msp)
    wall_lines = _extract_wall_lines(doc, msp)
    step_lines = _extract_step_lines(doc, msp)

    _attach_label_texts(sleeves, doc, msp, step_lines=step_lines)
    for s in sleeves:
        s.sleeve_type = _classify_sleeve_type(s.label_text, s.discipline)
        # Reconcile the geometric shape with the φ/外径/XXA label.
        # DXF convention: a round pipe penetrating a wall is drawn in plan
        # view as a long thin hatched rectangle (the side projection of
        # the pipe). The block has no CIRCLE inside, so our geometry pass
        # tags it `shape=rect`. But its label says "φ216" — which is the
        # truth: the actual sleeve cross-section is round.
        # `_refine_sleeve_shape_from_label` rewrites such cases back to
        # round, while leaving real box openings (block name contains 箱,
        # or label has no diameter pattern) alone.
        _refine_sleeve_shape_from_label(s)
        s.orientation = _infer_sleeve_orientation(s)
    # Second pass: recover "unknown" sleeves via adjacency / pair detection.
    _infer_orientation_from_pairs(sleeves)
    # Third pass: round sleeves that land on a wall line are wall penetrations,
    # not slab penetrations — reclassify them as horizontal.
    _infer_orientation_from_walls(sleeves, wall_lines)
    column_lines = _extract_column_lines(doc, msp)
    beam_lines = _extract_beam_lines(doc, msp)
    dim_lines = _extract_dim_lines(doc, msp)
    pn_labels = _extract_pn_labels(doc, msp)
    _attach_pn_numbers(sleeves, pn_labels, doc=doc, msp=msp)
    slab_zones = _extract_slab_zones(doc, msp)
    slab_zones.extend(_extract_step_labels(doc, msp))
    slab_outlines = _extract_slab_outlines(doc, msp)
    slab_labels = _extract_slab_labels(doc, msp)
    slab_level = _extract_slab_level(doc, msp)
    water_gradients = _extract_water_gradients(doc, msp)
    has_base_level_def = _detect_base_level_def(doc)
    recess_polygons = _extract_recess_polygons(doc, msp)

    # FL-based classification of step lines: annotate each segment with the
    # FL value of the region on each side. Spurious lines (same FL both
    # sides) can then be hidden by the frontend to match what a design
    # reviewer expects to see.
    #
    # FL label source = slab_zones (段差記号 FL±NNN texts) + slab_labels
    # (SXX blocks carrying a level like "-60"). The SXX labels are spread
    # across the main floor area while 段差記号 clusters near boundaries —
    # using both gives near-total coverage.
    from .regions import classify_step_segments

    _lvl_re = re.compile(r"([±+\-])?\s*(\d+)")
    fl_sources = list(slab_zones)
    for sl in slab_labels:
        m = _lvl_re.search(sl.level)
        if not m:
            continue
        sign = m.group(1) or "+"
        val = int(m.group(2))
        if sign == "-":
            val = -val
        elif sign == "±":
            val = 0
        fl_sources.append(
            SlabZone(x=sl.x, y=sl.y, fl_text=f"FL{val:+d}", fl_value=val)
        )

    for cls in classify_step_segments(step_lines, fl_sources):
        cls.segment.side_a_fl = cls.side_a_fl
        cls.segment.side_b_fl = cls.side_b_fl
        cls.segment.fl_status = cls.status

    # raw passthrough kept as opt-in only — descending into every INSERT
    # block across 17k+ entities adds seconds to the parse and the UI can't
    # render that much SVG meaningfully. Room names are extracted directly
    # below; turn this back on when someone needs the full debug view.
    raw_lines: list[RawLine] = []
    raw_texts: list[RawText] = []
    room_labels = _extract_room_labels(doc, msp)

    return FloorData(
        sleeves=sleeves,
        grid_lines=grid_lines,
        wall_lines=wall_lines,
        step_lines=step_lines,
        column_lines=column_lines,
        beam_lines=beam_lines,
        dim_lines=dim_lines,
        slab_zones=slab_zones,
        slab_outlines=slab_outlines,
        recess_polygons=recess_polygons,
        slab_labels=slab_labels,
        pn_labels=pn_labels,
        water_gradients=water_gradients,
        raw_lines=raw_lines,
        raw_texts=raw_texts,
        room_labels=room_labels,
        slab_level=slab_level,
        has_base_level_def=has_base_level_def,
    )


def _extract_room_labels(doc, msp) -> list[RoomLabel]:
    """Pull TEXT / MTEXT on A211_室名 (room-name) layers.

    Restricted to the proper room-name layer so annotations elsewhere
    that happen to contain room-like words don't leak in.
    """
    layers = {
        l.dxf.name for l in doc.layers
        if "A211" in l.dxf.name or "室名" in l.dxf.name
    }
    out: list[RoomLabel] = []
    for e in msp:
        if e.dxf.layer not in layers:
            continue
        kind = e.dxftype()
        if kind == "TEXT":
            txt = (e.dxf.text or "").strip()
        elif kind == "MTEXT":
            try:
                txt = e.plain_text().strip()
            except Exception:
                txt = ""
        else:
            continue
        if not txt:
            continue
        pos = e.dxf.insert
        out.append(RoomLabel(
            x=float(pos.x), y=float(pos.y), text=txt,
            height=float(getattr(e.dxf, "height", 400.0) or getattr(e.dxf, "char_height", 400.0) or 400.0),
            rotation=float(getattr(e.dxf, "rotation", 0.0) or 0.0),
        ))
    return out


# ---------------------------------------------------------------------------
# Raw drawing passthrough — every line-ish / text entity grouped by layer.
# Lets the UI render room names, beam outlines, level bubbles, revision
# clouds etc. that the typed extractors above don't cover.
# ---------------------------------------------------------------------------

def _extract_raw_drawing(doc, msp) -> tuple[list[RawLine], list[RawText]]:
    lines: list[RawLine] = []
    texts: list[RawText] = []

    _ARC_STEPS = 24

    def _poly_from_arc(cx: float, cy: float, r: float, sa: float, ea: float) -> list[tuple[float, float]]:
        if ea < sa:
            ea += 2.0 * math.pi
        pts: list[tuple[float, float]] = []
        for i in range(_ARC_STEPS + 1):
            a = sa + (ea - sa) * i / _ARC_STEPS
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
        return pts

    def _poly_from_circle(cx: float, cy: float, r: float) -> list[tuple[float, float]]:
        pts: list[tuple[float, float]] = []
        for i in range(_ARC_STEPS + 1):
            a = 2.0 * math.pi * i / _ARC_STEPS
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
        return pts

    def _handle(entity, layer_override: str | None = None,
                offset_x: float = 0.0, offset_y: float = 0.0,
                scale_x: float = 1.0, scale_y: float = 1.0,
                cos_r: float = 1.0, sin_r: float = 0.0) -> None:
        layer = layer_override or entity.dxf.layer
        try:
            color = _resolve_entity_color(doc, entity)
        except Exception:
            color = None

        def _xf(px: float, py: float) -> tuple[float, float]:
            lx = px * scale_x
            ly = py * scale_y
            return (offset_x + lx * cos_r - ly * sin_r,
                    offset_y + lx * sin_r + ly * cos_r)

        k = entity.dxftype()
        try:
            if k == "LINE":
                s = entity.dxf.start
                e = entity.dxf.end
                lines.append(RawLine(
                    points=[_xf(s.x, s.y), _xf(e.x, e.y)],
                    layer=layer, color=color,
                ))
            elif k == "LWPOLYLINE":
                pts = [_xf(float(p[0]), float(p[1])) for p in entity.get_points()]
                if getattr(entity, "is_closed", False) and len(pts) >= 2:
                    pts.append(pts[0])
                if len(pts) >= 2:
                    lines.append(RawLine(points=pts, layer=layer, color=color))
            elif k == "POLYLINE":
                try:
                    pts = [_xf(float(v.dxf.location.x), float(v.dxf.location.y))
                           for v in entity.vertices]
                except Exception:
                    pts = []
                if getattr(entity, "is_closed", False) and len(pts) >= 2:
                    pts.append(pts[0])
                if len(pts) >= 2:
                    lines.append(RawLine(points=pts, layer=layer, color=color))
            elif k == "ARC":
                cx, cy = _xf(float(entity.dxf.center.x), float(entity.dxf.center.y))
                r = float(entity.dxf.radius) * (abs(scale_x) + abs(scale_y)) / 2.0
                rot_offset = math.atan2(sin_r, cos_r)
                sa = math.radians(float(entity.dxf.start_angle)) + rot_offset
                ea = math.radians(float(entity.dxf.end_angle)) + rot_offset
                lines.append(RawLine(
                    points=_poly_from_arc(cx, cy, r, sa, ea),
                    layer=layer, color=color,
                ))
            elif k == "CIRCLE":
                cx, cy = _xf(float(entity.dxf.center.x), float(entity.dxf.center.y))
                r = float(entity.dxf.radius) * (abs(scale_x) + abs(scale_y)) / 2.0
                lines.append(RawLine(
                    points=_poly_from_circle(cx, cy, r),
                    layer=layer, color=color,
                ))
            elif k == "TEXT":
                t = (entity.dxf.text or "").strip()
                if t:
                    tx, ty = _xf(float(entity.dxf.insert.x), float(entity.dxf.insert.y))
                    h = float(getattr(entity.dxf, "height", 200.0) or 200.0) * max(abs(scale_x), abs(scale_y))
                    rot = float(getattr(entity.dxf, "rotation", 0.0) or 0.0) + math.degrees(math.atan2(sin_r, cos_r))
                    texts.append(RawText(x=tx, y=ty, text=t, layer=layer,
                                         height=h, rotation=rot, color=color))
            elif k == "MTEXT":
                try:
                    t = entity.plain_text().strip()
                except Exception:
                    t = ""
                if t:
                    tx, ty = _xf(float(entity.dxf.insert.x), float(entity.dxf.insert.y))
                    h = float(getattr(entity.dxf, "char_height", 200.0) or 200.0) * max(abs(scale_x), abs(scale_y))
                    rot = float(getattr(entity.dxf, "rotation", 0.0) or 0.0) + math.degrees(math.atan2(sin_r, cos_r))
                    texts.append(RawText(x=tx, y=ty, text=t, layer=layer,
                                         height=h, rotation=rot, color=color))
            elif k == "INSERT":
                # Descend into block; BYLAYER entities inherit the INSERT's layer.
                block = doc.blocks.get(entity.dxf.name)
                if block is None:
                    return
                try:
                    ix = float(entity.dxf.insert.x)
                    iy = float(entity.dxf.insert.y)
                except Exception:
                    return
                sx = float(getattr(entity.dxf, "xscale", 1.0) or 1.0)
                sy = float(getattr(entity.dxf, "yscale", 1.0) or 1.0)
                rot = math.radians(float(getattr(entity.dxf, "rotation", 0.0) or 0.0))
                # Compose with the caller's transform.
                new_cos = cos_r * math.cos(rot) - sin_r * math.sin(rot)
                new_sin = sin_r * math.cos(rot) + cos_r * math.sin(rot)
                ox, oy = _xf(ix, iy)
                for be in block:
                    inner_layer = be.dxf.layer if be.dxf.layer != "0" else layer
                    _handle(be, inner_layer, ox, oy, scale_x * sx, scale_y * sy, new_cos, new_sin)
        except Exception:
            # Never let a single malformed entity break the parse.
            return

    for entity in msp:
        _handle(entity)

    return lines, texts
