"""universal_parser.py — flatten every entity in a DXF/IFC into a uniform list.

Unlike `parser.py`, this does not interpret entities into domain types
(Sleeve, WallLine, …). It walks the source and emits a flat record per
entity with `{layer, type, subtype, pos, handle, props}` so the UI can
display "every element on every layer" without losing anything.

For DXF:
  - Walks Model Space + every Paper Space layout.
  - Top-level entities are emitted as-is (INSERT stays as INSERT, with
    block name in `subtype`).  This keeps the panel grouped at a useful
    level — the block-as-unit *is* what the drafter placed.
  - For each top-level INSERT we ALSO emit the constituent geometry of
    its block via `recursive_decompose`, so the user can see "what's
    inside" — e.g. the CIRCLE inside a スリーブ block, the LINEs inside
    a 図面枠 block.  These constituents inherit the parent INSERT's
    layer (BYLAYER/BYBLOCK rules handled by ezdxf).
  - Pulls ATTRIBs separately when an INSERT carries them.

For IFC:
  - Walks every IfcProduct via ifcopenshell.
  - layer name comes from ObjectType / PredefinedType when available.
  - position comes from ObjectPlacement origin.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class FlatEntity:
    """One row in the universal entity list."""
    handle: str           # DXF handle / IFC GlobalId
    layer: str            # DXF layer / IFC class+name
    type: str             # DXF entity type / IFC class
    subtype: str = ""     # text content / block name / equipment code
    pos: tuple[float, float] | None = None
    props: dict[str, Any] = field(default_factory=dict)


@dataclass
class UniversalDump:
    source: str           # "dxf" or "ifc"
    summary: dict[str, Any] = field(default_factory=dict)
    entities: list[FlatEntity] = field(default_factory=list)


# ---------------------------------------------------------------------------
# DXF
# ---------------------------------------------------------------------------

def _flatten_dxf(filepath: str | Path) -> UniversalDump:
    import ezdxf

    try:
        doc = ezdxf.readfile(str(filepath))
    except ezdxf.DXFStructureError:
        from ezdxf import recover
        doc, _ = recover.readfile(str(filepath))

    entities: list[FlatEntity] = []
    type_count: dict[str, int] = {}
    layer_count: dict[str, int] = {}

    def _push(e, *, override_layer: str | None = None,
              parent_handle: str | None = None) -> None:
        t = e.dxftype()
        layer = override_layer or getattr(e.dxf, "layer", "")
        try:
            handle = getattr(e.dxf, "handle", "") or ""
        except Exception:
            handle = ""

        subtype = ""
        pos: tuple[float, float] | None = None
        props: dict[str, Any] = {}

        try:
            if t == "INSERT":
                subtype = getattr(e.dxf, "name", "")
                pos = (float(e.dxf.insert.x), float(e.dxf.insert.y))
                props["rotation"] = float(getattr(e.dxf, "rotation", 0.0) or 0.0)
                props["xscale"] = float(getattr(e.dxf, "xscale", 1.0) or 1.0)
                props["yscale"] = float(getattr(e.dxf, "yscale", 1.0) or 1.0)
            elif t == "TEXT":
                txt = (e.dxf.text or "").strip()
                subtype = txt
                pos = (float(e.dxf.insert.x), float(e.dxf.insert.y))
                props["height"] = float(getattr(e.dxf, "height", 0.0) or 0.0)
                props["rotation"] = float(getattr(e.dxf, "rotation", 0.0) or 0.0)
            elif t == "MTEXT":
                try:
                    txt = e.plain_text().strip()
                except Exception:
                    txt = ""
                subtype = txt
                pos = (float(e.dxf.insert.x), float(e.dxf.insert.y))
            elif t == "LINE":
                pos = (float(e.dxf.start.x), float(e.dxf.start.y))
                props["start"] = pos
                props["end"] = (float(e.dxf.end.x), float(e.dxf.end.y))
            elif t == "CIRCLE":
                pos = (float(e.dxf.center.x), float(e.dxf.center.y))
                props["radius"] = float(e.dxf.radius)
            elif t == "ARC":
                pos = (float(e.dxf.center.x), float(e.dxf.center.y))
                props["radius"] = float(e.dxf.radius)
                props["start_angle"] = float(getattr(e.dxf, "start_angle", 0.0))
                props["end_angle"] = float(getattr(e.dxf, "end_angle", 0.0))
            elif t == "ELLIPSE":
                pos = (float(e.dxf.center.x), float(e.dxf.center.y))
            elif t in ("LWPOLYLINE", "POLYLINE"):
                try:
                    if t == "LWPOLYLINE":
                        raw_pts = list(e.get_points())
                        # get_points returns (x, y, start_width, end_width, bulge);
                        # keep only x,y for the geometry consumer.
                        flat = [(float(p[0]), float(p[1])) for p in raw_pts]
                    else:
                        flat = [
                            (float(v.dxf.location.x), float(v.dxf.location.y))
                            for v in e.vertices
                        ]
                    if flat:
                        pos = flat[0]
                        xs = [p[0] for p in flat]
                        ys = [p[1] for p in flat]
                        props["bbox"] = [min(xs), min(ys), max(xs), max(ys)]
                        props["vertex_count"] = len(flat)
                        props["closed"] = bool(getattr(e, "is_closed", False) or getattr(e, "closed", False))
                        # Full vertex list — needed by the drawing view to
                        # render every polyline on a layer regardless of
                        # whether it's closed.
                        props["vertices"] = [list(p) for p in flat]
                except Exception:
                    pass
            elif t == "POINT":
                pos = (float(e.dxf.location.x), float(e.dxf.location.y))
            elif t == "DIMENSION":
                try:
                    pos = (float(e.dxf.defpoint.x), float(e.dxf.defpoint.y))
                except Exception:
                    pass
                props["measurement"] = float(getattr(e.dxf, "actual_measurement", 0.0) or 0.0)
                props["text"] = getattr(e.dxf, "text", "") or ""
            elif t == "HATCH":
                props["pattern"] = getattr(e.dxf, "pattern_name", "") or ""
                # Extract boundary loops as polylines so the drawing view
                # can fill them. ezdxf exposes both PolylinePath (with
                # `vertices`) and EdgePath (with `edges`); we flatten both
                # to their vertex / endpoint lists. Pattern lines (the
                # actual diagonal hatching) are NOT recreated — too costly
                # to mirror in SVG. Outlining the boundary + faint fill
                # gives the user the same "where is the slab" signal.
                try:
                    loops: list[list[list[float]]] = []
                    for path in e.paths:
                        pts: list[list[float]] = []
                        if hasattr(path, "vertices") and getattr(path, "vertices", None):
                            for v in path.vertices:
                                pts.append([float(v[0]), float(v[1])])
                        elif hasattr(path, "edges"):
                            for ed in path.edges:
                                # Line edges have start/end; arcs have
                                # center+radius. We sample the endpoints
                                # of each edge — close enough for layout.
                                start = getattr(ed, "start", None)
                                if start is not None and len(start) >= 2:
                                    pts.append([float(start[0]), float(start[1])])
                                end = getattr(ed, "end", None)
                                if end is not None and len(end) >= 2:
                                    pts.append([float(end[0]), float(end[1])])
                        if len(pts) >= 3:
                            loops.append(pts)
                    if loops:
                        props["loops"] = loops
                        # Use the first loop's first vertex as a stable
                        # position so spatial joins work.
                        pos = (float(loops[0][0][0]), float(loops[0][0][1]))
                except Exception:
                    pass
            elif t == "VIEWPORT":
                try:
                    pos = (float(e.dxf.center.x), float(e.dxf.center.y))
                except Exception:
                    pass
        except Exception:
            pass

        if parent_handle:
            props["parent_handle"] = parent_handle

        entities.append(FlatEntity(
            handle=handle, layer=layer, type=t, subtype=subtype, pos=pos, props=props,
        ))
        type_count[t] = type_count.get(t, 0) + 1
        layer_count[layer] = layer_count.get(layer, 0) + 1

    # Pre-compute the type breakdown of each named block so the INSERT
    # row can be annotated with `block_inner` counts (UI shows
    # "中身 N 件 (LINE:14, CIRCLE:22, …)").
    #
    # We **fully expand every INSERT** below — the block's geometric
    # children (LINE / CIRCLE / ARC / LWPOLYLINE / HATCH) and its text
    # children (TEXT / MTEXT / ATTRIB / ATTDEF) all get emitted as their
    # own rows, tagged with `parent_handle` so the data tab can fold
    # them under the INSERT and the drawing view can render them. The
    # earlier "expand only blocks containing text" gate dropped the
    # geometry of every decoration block (面取り記号, 通り芯バブル,
    # スラブ番号フレーム など), which the user explicitly asked to
    # surface — "INSERT も含めて網羅的に取得" was the requirement.
    block_inner_counts: dict[str, dict[str, int]] = {}
    for blk in doc.blocks:
        if blk.name.startswith("*"):
            continue
        types: dict[str, int] = {}
        for child in blk:
            ct = child.dxftype()
            types[ct] = types.get(ct, 0) + 1
        if types:
            block_inner_counts[blk.name] = types

    # All known DXF entity types we know how to flatten in `_push`.
    # Anything in this set is worth emitting as a separate row when it
    # appears as a recursed child of an INSERT.
    EMITTABLE_CHILD_TYPES = {
        "LINE", "CIRCLE", "ARC", "ELLIPSE",
        "LWPOLYLINE", "POLYLINE", "POINT",
        "TEXT", "MTEXT", "ATTRIB", "ATTDEF",
        "HATCH", "DIMENSION",
    }

    # Text-only subset — used downstream to roll up child values onto the
    # INSERT row as `inner_texts`.
    TEXT_INNER = {"TEXT", "MTEXT", "ATTRIB", "ATTDEF"}

    from ezdxf.disassemble import recursive_decompose

    # Walk Model Space + every Paper Space layout
    for layout in doc.layouts:
        for e in layout:
            _push(e)
            insert_idx = len(entities) - 1  # remember where the INSERT row sits

            if e.dxftype() != "INSERT":
                continue

            try:
                parent_handle = getattr(e.dxf, "handle", "") or ""
            except Exception:
                parent_handle = ""

            # Texts/values collected from this INSERT's children — both
            # ATTRIBs and decomposed in-block TEXT/MTEXT. We attach them
            # to the INSERT row as `inner_texts` so the data tab can show
            # the symbol+value as one logical thing (e.g. "FL ブロック → -565")
            # even when the value lives in a separate child entity.
            inner_texts: list[str] = []

            # ATTRIBs hang off INSERTs; expose them as separate rows AND
            # capture their values onto the parent.
            try:
                for a in e.attribs:
                    _push(a, override_layer=getattr(e.dxf, "layer", ""),
                          parent_handle=parent_handle)
                    val = (getattr(a.dxf, "text", "") or "").strip()
                    if val:
                        inner_texts.append(val)
            except Exception:
                pass

            # Annotate the INSERT row with block_inner counts.
            bname = getattr(e.dxf, "name", "")
            inner = block_inner_counts.get(bname)
            if inner:
                entities[insert_idx].props["block_inner"] = inner

            # Expand the block when it's small enough that recursive_decompose
            # won't blow up memory. Skipping huge decoration blocks (図枠,
            # detail-fragments, デッキ受け patterns with 200+ lines apiece)
            # keeps the universal dump under Render's 512MB free-tier RAM
            # budget — production was 502'ing on full expansion of the 1F
            # sheet because each top-level INSERT triggers another walk
            # through the block reference graph.
            #
            # The blocks we actually care about expanding are tiny:
            #   sleeve mark = CIRCLE + 2 LINEs                      (3 entities)
            #   通り芯 bubble = CIRCLE + TEXT                        (2)
            #   F308 slab marker = HATCH + INSERT/CIRCLE + TEXT      (3-5)
            #   FL level bubble = LINE + INSERT + HATCH + CIRCLE + TEXT (5)
            #   段差記号 = LINE + ARC                                (2-4)
            # Whereas 図枠 / detail blocks have hundreds of children. The
            # 50-entity cap admits the former and rejects the latter.
            inner_total = sum(inner.values()) if inner else 0
            if inner_total > 0 and inner_total <= 50:
                parent_layer = getattr(e.dxf, "layer", "")
                try:
                    for child in recursive_decompose([e]):
                        ct = child.dxftype()
                        if ct == "INSERT":
                            continue
                        if ct not in EMITTABLE_CHILD_TYPES:
                            continue
                        cl = getattr(child.dxf, "layer", "") or ""
                        override = parent_layer if cl == "0" else None
                        _push(child, override_layer=override,
                              parent_handle=parent_handle)
                        if ct in TEXT_INNER:
                            if ct == "MTEXT":
                                try:
                                    val = (child.plain_text() or "").strip()
                                except Exception:
                                    val = ""
                            else:
                                val = (getattr(child.dxf, "text", "") or "").strip()
                            if val:
                                inner_texts.append(val)
                except Exception:
                    pass

            if inner_texts:
                entities[insert_idx].props["inner_texts"] = inner_texts

    summary = {
        "entity_count": len(entities),
        "type_count": type_count,
        "layer_count": len(layer_count),
        "layers": sorted(layer_count.keys()),
        "header": {
            "version": doc.header.get("$ACADVER", ""),
            "insunits": doc.header.get("$INSUNITS", 0),
            "extmin": list(doc.header.get("$EXTMIN", (0, 0, 0))),
            "extmax": list(doc.header.get("$EXTMAX", (0, 0, 0))),
            "saved_by": doc.header.get("$LASTSAVEDBY", ""),
        },
        "block_count": len(list(doc.blocks)),
    }

    return UniversalDump(source="dxf", summary=summary, entities=entities)


# ---------------------------------------------------------------------------
# IFC
# ---------------------------------------------------------------------------

def _flatten_ifc(paths: list[str | Path]) -> UniversalDump:
    import ifcopenshell

    entities: list[FlatEntity] = []
    type_count: dict[str, int] = {}
    layer_count: dict[str, int] = {}

    files: list = []
    for p in paths:
        files.append(ifcopenshell.open(str(p)))

    for f in files:
        for prod in f.by_type("IfcProduct"):
            t = prod.is_a()  # e.g. IfcWall, IfcColumn
            try:
                handle = prod.GlobalId or ""
            except Exception:
                handle = ""

            obj_type = (getattr(prod, "ObjectType", None) or "").strip()
            name = (getattr(prod, "Name", None) or "").strip()
            tag = (getattr(prod, "Tag", None) or "").strip()
            predef = (getattr(prod, "PredefinedType", None) or "").strip()

            # "layer" surrogate for IFC: best human-readable categorisation
            layer = obj_type or predef or t

            subtype = name or tag or predef

            # Position from ObjectPlacement
            pos: tuple[float, float] | None = None
            try:
                place = prod.ObjectPlacement
                if place and place.is_a("IfcLocalPlacement"):
                    rel = place.RelativePlacement
                    if rel and rel.Location:
                        coords = rel.Location.Coordinates
                        # IFC is meters; convert to mm to match DXF convention
                        if len(coords) >= 2:
                            pos = (float(coords[0]) * 1000.0, float(coords[1]) * 1000.0)
            except Exception:
                pass

            props: dict[str, Any] = {}
            if predef: props["predefined_type"] = predef
            if tag:    props["tag"] = tag
            if name:   props["name"] = name

            entities.append(FlatEntity(
                handle=handle, layer=layer, type=t, subtype=subtype, pos=pos, props=props,
            ))
            type_count[t] = type_count.get(t, 0) + 1
            layer_count[layer] = layer_count.get(layer, 0) + 1

    summary = {
        "entity_count": len(entities),
        "type_count": type_count,
        "layer_count": len(layer_count),
        "layers": sorted(layer_count.keys()),
        "header": {
            "files": [str(p) for p in paths],
        },
        "block_count": 0,
    }

    return UniversalDump(source="ifc", summary=summary, entities=entities)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def flatten(path: str | Path | list[str | Path]) -> UniversalDump:
    """Flatten a DXF or IFC file (or list of IFCs) into a UniversalDump."""
    if isinstance(path, (list, tuple)):
        # Assume IFC list (matches parse_ifc convention)
        return _flatten_ifc(list(path))
    p = Path(path)
    if p.suffix.lower() == ".dxf":
        return _flatten_dxf(p)
    if p.suffix.lower() == ".ifc":
        return _flatten_ifc([p])
    raise ValueError(f"Unsupported file type: {p.suffix}")


def to_dict(dump: UniversalDump) -> dict:
    """JSON-serialisable representation."""
    return {
        "source": dump.source,
        "summary": dump.summary,
        "entities": [
            {
                "handle": e.handle,
                "layer": e.layer,
                "type": e.type,
                "subtype": e.subtype,
                "pos": list(e.pos) if e.pos else None,
                "props": e.props,
            }
            for e in dump.entities
        ],
    }
