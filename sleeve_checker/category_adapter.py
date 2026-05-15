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
WALL_CATEGORIES = {"躯体壁", "乾式壁", "耐火壁・防火区画"}


def _wall_material_from_layer(layer_name: str) -> str:
    """Sub-attribute: derive wall material from layer name.

    Delegates to parser._wall_material to keep classification logic in
    one place. Re-exported here because category_adapter is a POC for
    moving wall extraction off parser.py.
    """
    from .parser import _wall_material
    return _wall_material(layer_name)


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

        material = _wall_material_from_layer(layer)

        def _w(sx, sy, ex, ey):
            return WallLine(
                start=(sx, sy), end=(ex, ey),
                layer=layer, wall_type=material, material=material,
            )

        if t == "LINE":
            start = props.get("start")
            end = props.get("end")
            if not start or not end:
                continue
            sx, sy = float(start[0]), float(start[1])
            ex, ey = float(end[0]), float(end[1])
            if bbox_check and not bbox_check(sx, sy, ex, ey):
                continue
            out.append(_w(sx, sy, ex, ey))

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
                out.append(_w(sx, sy, ex, ey))
            if closed:
                sx, sy = float(verts[-1][0]), float(verts[-1][1])
                ex, ey = float(verts[0][0]), float(verts[0][1])
                if not (bbox_check and not bbox_check(sx, sy, ex, ey)):
                    out.append(_w(sx, sy, ex, ey))

    return out
