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

    # Pre-compute "what's inside each named block".
    # We use this both to annotate INSERTs (block_inner counts) and to
    # decide whether to *expand* the block contents during the walk.
    #
    # Expansion rule: a block should be expanded if it (or anything it
    # contains transitively, via nested INSERTs) carries TEXT / MTEXT /
    # ATTRIB / ATTDEF. Those text values are semantically meaningful
    # (P-N number, axis label "A"/"1", FL value "FL-565" …) and need to
    # surface as their own rows. Pure geometric blocks (only LINE / CIRCLE
    # / ARC) are decoration — leave them collapsed.
    EXPANDABLE_INNER = {"TEXT", "MTEXT", "ATTRIB", "ATTDEF"}

    # Step 1: direct children — types per block + child block names.
    block_inner_counts: dict[str, dict[str, int]] = {}
    direct_text: dict[str, bool] = {}
    direct_child_blocks: dict[str, set[str]] = {}
    for blk in doc.blocks:
        if blk.name.startswith("*"):
            continue
        types: dict[str, int] = {}
        children: set[str] = set()
        for child in blk:
            ct = child.dxftype()
            types[ct] = types.get(ct, 0) + 1
            if ct == "INSERT":
                cname = getattr(child.dxf, "name", "")
                if cname and not cname.startswith("*"):
                    children.add(cname)
        if types:
            block_inner_counts[blk.name] = types
            direct_text[blk.name] = any(t in EXPANDABLE_INNER for t in types)
            direct_child_blocks[blk.name] = children

    # Step 2: transitive expansion check — DFS over the block reference
    # graph with memoisation + cycle guard.
    block_should_expand: dict[str, bool] = {}

    def _has_text_recursive(name: str, visiting: set[str]) -> bool:
        if name in block_should_expand:
            return block_should_expand[name]
        if name in visiting:
            return False  # cycle: assume no until something else proves it
        visiting.add(name)
        result = direct_text.get(name, False)
        if not result:
            for child in direct_child_blocks.get(name, ()):
                if _has_text_recursive(child, visiting):
                    result = True
                    break
        visiting.discard(name)
        block_should_expand[name] = result
        return result

    for bname in direct_text:
        _has_text_recursive(bname, set())

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

            # Expand block contents only when the block carries semantic
            # text. recursive_decompose handles nested INSERTs, BYLAYER /
            # BYBLOCK inheritance, and OCS→WCS transform automatically.
            if block_should_expand.get(bname, False):
                parent_layer = getattr(e.dxf, "layer", "")
                try:
                    for child in recursive_decompose([e]):
                        ct = child.dxftype()
                        # Skip the INSERT itself (already emitted).
                        if ct == "INSERT":
                            continue
                        # Only emit the meaningful children. Decomposed
                        # geometric junk (LINE/ARC etc. from the block
                        # frame) would just bloat the panel — drop it.
                        if ct not in EXPANDABLE_INNER:
                            continue
                        cl = getattr(child.dxf, "layer", "") or ""
                        override = parent_layer if cl == "0" else None
                        _push(child, override_layer=override,
                              parent_handle=parent_handle)
                        # Capture the value back onto the INSERT row.
                        if ct in ("TEXT", "ATTRIB", "ATTDEF"):
                            val = (getattr(child.dxf, "text", "") or "").strip()
                        elif ct == "MTEXT":
                            try:
                                val = (child.plain_text() or "").strip()
                            except Exception:
                                val = ""
                        else:
                            val = ""
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
