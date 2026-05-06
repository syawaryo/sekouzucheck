"""category_adapter.py — POC: build typed FloorData fields from
universal-entity payload + LLM categorisation.

The point: the existing parser.py decides BOTH (a) "is this entity a wall?"
and (b) "what material?" via layer-name regex. We're moving (a) onto the
LLM (大分類), and keeping (b) as a tiny per-category helper.

If the LLM gets the layer→category mapping right, this should match
parser.py's output. The wall_type material attribute still comes from a
small regex on layer name — that's the "サブ属性" that category alone
can't supply.

Eventually checks.py would consume what this builds, replacing the
direct hand-off from parser.py. This file is a POC for one field
(wall_lines); the same pattern extends to columns, beams, slabs, dims
etc. one at a time.
"""

from __future__ import annotations

from typing import Any

from .models import WallLine
from .universal_parser import FlatEntity


# Categories that should feed into wall_lines. Stays in sync with the
# layer_classifier.USEFUL_CATEGORIES naming.
WALL_CATEGORIES = {"内壁", "外壁", "耐火壁・防火区画"}


def _wall_type_from_layer(layer_name: str) -> str:
    """Sub-attribute: derive wall material from layer name.

    Same regex table as parser.py's `_wall_type` — kept here because
    material is information the LLM category alone doesn't carry, but
    the layer name still does. When this adapter replaces parser.py,
    this helper moves with it.
    """
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
    if "耐火被覆" in layer_name or "A561" in layer_name or "耐火壁" in layer_name:
        return "耐火被覆"
    return "不明"


def extract_walls(
    entities: list[FlatEntity] | list[dict[str, Any]],
    layer_categories: dict[str, str],
    bbox_check: callable | None = None,
) -> list[WallLine]:
    """Build WallLine list from universal entities filtered by category.

    Parameters
    ----------
    entities:
        Universal entity stream (FlatEntity OR dict from /api/all_entities).
    layer_categories:
        layer name -> 大分類 name (output of layer_classifier).
    bbox_check:
        Optional callable (sx, sy, ex, ey) -> bool that drops segments
        outside the building. Caller provides building bbox derived from
        grid lines or wall extent. None disables the check.

    Returns
    -------
    list[WallLine] — same shape parser.py produces, so checks consume it
    unchanged.
    """
    out: list[WallLine] = []
    for raw in entities:
        # accept either dataclass or dict
        if isinstance(raw, dict):
            layer = raw.get("layer", "")
            t = raw.get("type", "")
            props = raw.get("props") or {}
        else:
            layer = raw.layer
            t = raw.type
            props = raw.props or {}

        if layer_categories.get(layer) not in WALL_CATEGORIES:
            continue

        wtype = _wall_type_from_layer(layer)

        if t == "LINE":
            start = props.get("start")
            end = props.get("end")
            if not start or not end:
                continue
            sx, sy = float(start[0]), float(start[1])
            ex, ey = float(end[0]), float(end[1])
            if bbox_check and not bbox_check(sx, sy, ex, ey):
                continue
            out.append(WallLine(start=(sx, sy), end=(ex, ey), layer=layer, wall_type=wtype))

        elif t in ("LWPOLYLINE", "POLYLINE"):
            verts = props.get("vertices") or []
            closed = bool(props.get("closed"))
            if len(verts) < 2:
                continue
            for i in range(len(verts) - 1):
                sx, sy = float(verts[i][0]), float(verts[i][1])
                ex, ey = float(verts[i + 1][0]), float(verts[i + 1][1])
                if bbox_check and not bbox_check(sx, sy, ex, ey):
                    continue
                out.append(WallLine(start=(sx, sy), end=(ex, ey), layer=layer, wall_type=wtype))
            if closed:
                sx, sy = float(verts[-1][0]), float(verts[-1][1])
                ex, ey = float(verts[0][0]), float(verts[0][1])
                if not (bbox_check and not bbox_check(sx, sy, ex, ey)):
                    out.append(WallLine(start=(sx, sy), end=(ex, ey), layer=layer, wall_type=wtype))

    return out
