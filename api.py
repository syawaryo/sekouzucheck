"""
api.py - FastAPI backend for the sleeve checker project.

Run with:
    uvicorn api:app --reload --port 8000
"""

from __future__ import annotations

import dataclasses
import os
import re
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from sleeve_checker.checks import run_all_checks
from sleeve_checker.models import (
    FloorData, Sleeve, GridLine, DimLine, WallLine, StepLine,
    ColumnLine, BeamLine, SlabZone, SlabLabel, SlabOutline, PnLabel, RecessPolygon,
    RawLine, RawText, RoomLabel,
)
from sleeve_checker.parser import parse_dxf
from sleeve_checker.ifc_parser import parse_ifc
from sleeve_checker.universal_parser import flatten, to_dict as udump_to_dict
from sleeve_checker.layer_classifier import classify_layers
from sleeve_checker.dwg_to_dxf import convert_dwg_to_dxf, DwgConversionError

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(title="Sleeve Checker API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:5174",
        "http://localhost:5176",
        "http://localhost:5175",
        os.getenv("FRONTEND_URL", ""),
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Where mutable state lives. On Render this is a Persistent Disk mounted at
# /app/data so uploads + caches survive container restarts. Defaults to the
# repo root for local dev (preserves existing dxf_output/ behaviour).
DATA_DIR = Path(os.getenv("DATA_DIR", "."))

DXF_DIR = DATA_DIR / "dxf_output"
IFC_DIR = DATA_DIR / "ifc_output"

# Seed dir baked into the Docker image (COPY dxf_output/ ./dxf_output/). Used
# to populate a fresh Persistent Disk with the default 1F/2F samples on first
# boot — Path(__file__).parent is /app in the deployed image.
_SEED_DXF_DIR = Path(__file__).resolve().parent / "dxf_output"


def _seed_data_dir() -> None:
    """Copy baked-in seed DXFs into DXF_DIR if the disk is empty."""
    if DATA_DIR.resolve() == Path(".").resolve():
        return  # local dev: DXF_DIR already points at the seed dir
    if not _SEED_DXF_DIR.is_dir():
        return
    DXF_DIR.mkdir(parents=True, exist_ok=True)
    for src in _SEED_DXF_DIR.glob("*.dxf"):
        dst = DXF_DIR / src.name
        if not dst.exists():
            import shutil
            shutil.copy2(src, dst)


_seed_data_dir()

# Map filename stems to short IDs — built dynamically via _stem_to_floor_id()
_FLOOR_ID_MAP: dict[str, str] = {}
# Reverse map: id -> stem
_ID_TO_STEM: dict[str, str] = {}
# floor_id -> list of IFC paths (auto-classified by parser)
_IFC_SOURCES: dict[str, list[Path]] = {}

_RE_FLOOR = re.compile(r"(B?\d+)階")


def _stem_to_floor_id(stem: str) -> str:
    """Extract floor ID from filename stem (e.g. '2階床スリーブ図' → '2f')."""
    m = _RE_FLOOR.search(stem)
    if m:
        raw = m.group(1)  # "B1", "1", "2", etc.
        return raw.lower() + "f"
    return stem


# Pre-populate maps from existing DXF files on startup
if DXF_DIR.exists():
    for _f in DXF_DIR.glob("*.dxf"):
        _s = _f.stem
        _fid = _stem_to_floor_id(_s)
        _FLOOR_ID_MAP[_s] = _fid
        _ID_TO_STEM[_fid] = _s

# Pre-populate IFC sources by collecting every *.ifc in each IFC_DIR/<floor_id>/.
if IFC_DIR.exists():
    for _sub in IFC_DIR.iterdir():
        if not _sub.is_dir():
            continue
        _ifcs = sorted(_sub.glob("*.ifc"))
        if not _ifcs:
            continue
        _fid = _sub.name
        _IFC_SOURCES[_fid] = _ifcs
        _ID_TO_STEM.setdefault(_fid, _fid)
        _FLOOR_ID_MAP.setdefault(_fid, _fid)

# ---------------------------------------------------------------------------
# In-memory parse cache  {filepath_str: FloorData}
# ---------------------------------------------------------------------------

_parse_cache: dict[str, FloorData] = {}


def _get_floor_path(floor_id: str | None, path: str | None) -> Path:
    """Resolve a floor path from either a floor_id or an explicit path string."""
    if path:
        p = Path(path)
        if not p.exists():
            raise HTTPException(status_code=404, detail=f"File not found: {path}")
        return p

    if floor_id:
        stem = _ID_TO_STEM.get(floor_id)
        if stem is None:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown floor_id '{floor_id}'. Valid ids: {list(_ID_TO_STEM)}",
            )
        p = DXF_DIR / f"{stem}.dxf"
        if not p.exists():
            raise HTTPException(status_code=404, detail=f"DXF file not found: {p}")
        return p

    raise HTTPException(
        status_code=422, detail="Provide either 'floor_id' or 'path'."
    )


def _resolve_floor_data(floor_id: str | None, path: str | None) -> FloorData:
    """Unified resolver: returns FloorData for either IFC or DXF sources."""
    # IFC path: floor_id maps to a pair of IFC files
    if floor_id and floor_id in _IFC_SOURCES:
        cache_key = f"ifc::{floor_id}"
        if cache_key in _parse_cache:
            return _parse_cache[cache_key]
        paths = _IFC_SOURCES[floor_id]
        fd = parse_ifc(paths)
        _parse_cache[cache_key] = fd
        return fd
    # DXF path (existing behaviour)
    filepath = _get_floor_path(floor_id, path)
    return _get_or_parse(filepath)


CACHE_DIR = DATA_DIR / ".parse_cache"


def _dict_to_floor_data(d: dict) -> FloorData:
    """Reconstruct FloorData from a JSON-parsed dict."""
    return FloorData(
        sleeves=[Sleeve(
            id=s["id"], center=tuple(s["center"]), diameter=s["diameter"],
            label_text=s.get("label_text"), diameter_text=s.get("diameter_text"),
            fl_text=s.get("fl_text"),
            pn_number=s.get("pn_number"), layer=s.get("layer", ""),
            discipline=s.get("discipline", ""),
            shape=s.get("shape", "round"),
            width=s.get("width", s["diameter"]),
            height=s.get("height", s["diameter"]),
            color=s.get("color"),
            sleeve_type=s.get("sleeve_type", ""),
            orientation=s.get("orientation", ""),
        ) for s in d.get("sleeves", [])],
        grid_lines=[GridLine(
            axis_label=g["axis_label"], direction=g["direction"], position=g["position"],
        ) for g in d.get("grid_lines", [])],
        dim_lines=[DimLine(
            layer=dl["layer"], measurement=dl["measurement"],
            defpoint1=tuple(dl["defpoint1"]), defpoint2=tuple(dl["defpoint2"]),
            defpoint3=tuple(dl.get("defpoint3", [0, 0])),
            angle=dl.get("angle"), text_override=dl.get("text_override"),
        ) for dl in d.get("dim_lines", [])],
        wall_lines=[WallLine(
            start=tuple(w["start"]), end=tuple(w["end"]),
            layer=w.get("layer", ""),
            wall_type=w.get("wall_type", w.get("material", "不明")),
            material=w.get("material", w.get("wall_type", "不明")),
            is_exterior=bool(w.get("is_exterior", False)),
        ) for w in d.get("wall_lines", [])],
        step_lines=[StepLine(
            start=tuple(s["start"]), end=tuple(s["end"]), layer=s.get("layer", ""),
            side_a_fl=s.get("side_a_fl"), side_b_fl=s.get("side_b_fl"),
            fl_status=s.get("fl_status", "unknown"),
        ) for s in d.get("step_lines", [])],
        column_lines=[ColumnLine(
            start=tuple(c["start"]), end=tuple(c["end"]), layer=c.get("layer", ""),
        ) for c in d.get("column_lines", [])],
        beam_lines=[BeamLine(
            start=tuple(b["start"]), end=tuple(b["end"]),
            layer=b.get("layer", ""), beam_type=b.get("beam_type", ""),
        ) for b in d.get("beam_lines", [])],
        slab_zones=[SlabZone(
            x=z["x"], y=z["y"], fl_text=z["fl_text"], fl_value=z["fl_value"],
        ) for z in d.get("slab_zones", [])],
        slab_outlines=[SlabOutline(
            start=tuple(o["start"]), end=tuple(o["end"]),
        ) for o in d.get("slab_outlines", [])],
        recess_polygons=[RecessPolygon(
            vertices=[tuple(v) for v in rp.get("vertices", [])],
            layer=rp.get("layer", ""),
        ) for rp in d.get("recess_polygons", [])],
        slab_labels=[SlabLabel(
            x=sl["x"], y=sl["y"], slab_no=sl["slab_no"],
            level=sl["level"], thickness=sl["thickness"],
        ) for sl in d.get("slab_labels", [])],
        pn_labels=[PnLabel(
            x=p["x"], y=p["y"], text=p["text"], number=p["number"],
            arrow_verts=[tuple(v) for v in p.get("arrow_verts", [])],
        ) for p in d.get("pn_labels", [])],
        raw_lines=[RawLine(
            points=[tuple(p) for p in rl.get("points", [])],
            layer=rl.get("layer", ""), color=rl.get("color"),
        ) for rl in d.get("raw_lines", [])],
        raw_texts=[RawText(
            x=rt.get("x", 0.0), y=rt.get("y", 0.0),
            text=rt.get("text", ""), layer=rt.get("layer", ""),
            height=rt.get("height", 0.0), rotation=rt.get("rotation", 0.0),
            color=rt.get("color"),
        ) for rt in d.get("raw_texts", [])],
        room_labels=[RoomLabel(
            x=r.get("x", 0.0), y=r.get("y", 0.0),
            text=r.get("text", ""),
            height=r.get("height", 0.0), rotation=r.get("rotation", 0.0),
        ) for r in d.get("room_labels", [])],
        slab_level=d.get("slab_level"),
        has_base_level_def=d.get("has_base_level_def", False),
    )


def _cache_path(filepath: Path) -> Path:
    """Return the JSON cache file path for a given DXF file."""
    CACHE_DIR.mkdir(exist_ok=True)
    return CACHE_DIR / (filepath.stem + ".json")


def _get_or_parse(filepath: Path) -> FloorData:
    """Return cached FloorData or parse and cache it.

    Two-tier cache:
    1. In-memory dict (fastest, lost on restart)
    2. JSON file on disk (fast, survives restarts)
    Falls back to full DXF parse if neither cache is valid.
    """
    import json

    key = str(filepath.resolve())

    # Tier 1: in-memory
    if key in _parse_cache:
        return _parse_cache[key]

    # Tier 2: JSON file cache (check DXF mtime)
    cache_file = _cache_path(filepath)
    dxf_mtime = filepath.stat().st_mtime if filepath.exists() else 0

    if cache_file.exists():
        try:
            cache_mtime = cache_file.stat().st_mtime
            if cache_mtime >= dxf_mtime:
                raw = json.loads(cache_file.read_text(encoding="utf-8"))
                fd = _dict_to_floor_data(raw)
                _parse_cache[key] = fd
                return fd
        except Exception:
            pass  # cache corrupted, re-parse

    # Tier 3: full parse
    fd = parse_dxf(str(filepath))
    _parse_cache[key] = fd

    # Write cache file
    try:
        cache_file.write_text(
            json.dumps(_convert(dataclasses.asdict(fd)), ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass  # non-fatal

    return fd


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _convert(obj: Any) -> Any:
    """Recursively convert tuples to lists for JSON-safe output."""
    if isinstance(obj, tuple):
        return [_convert(v) for v in obj]
    if isinstance(obj, list):
        return [_convert(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _convert(v) for k, v in obj.items()}
    return obj


def _floor_data_to_dict(fd: FloorData) -> dict:
    """Convert FloorData dataclass to a JSON-serialisable dict."""
    raw = dataclasses.asdict(fd)
    return _convert(raw)


def _check_result_to_dict(cr) -> dict:
    """Convert a single CheckResult to a JSON-serialisable dict.

    The CheckResult.sleeve field is a Sleeve dataclass; we extract sleeve_id
    and drop the nested object to keep the response flat and frontend-friendly.
    """
    sleeve = cr.sleeve
    return {
        "check_id": cr.check_id,
        "check_name": cr.check_name,
        "severity": cr.severity,
        "sleeve_id": sleeve.id if sleeve is not None else None,
        "message": cr.message,
        "related_coords": _convert(cr.related_coords),
        "target": getattr(cr, "target", ""),
        "rule": getattr(cr, "rule", ""),
        "expected": getattr(cr, "expected", ""),
        "found": getattr(cr, "found", ""),
        "fix_hint": getattr(cr, "fix_hint", ""),
    }


# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------

class ParseRequest(BaseModel):
    floor_id: str | None = None
    path: str | None = None


class CheckRequest(BaseModel):
    floor_2f_id: str | None = None
    floor_1f_id: str | None = None
    floor_2f_path: str | None = None
    floor_1f_path: str | None = None
    wall_thickness: dict[str, float] | None = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/floors")
def list_floors() -> list[dict]:
    """Return all available floors (DXF + IFC)."""
    floors: list[dict] = []

    if DXF_DIR.exists():
        for dxf_file in sorted(DXF_DIR.glob("*.dxf")):
            stem = dxf_file.stem
            floor_id = _FLOOR_ID_MAP.get(stem) or _stem_to_floor_id(stem)
            _FLOOR_ID_MAP[stem] = floor_id
            _ID_TO_STEM[floor_id] = stem
            floors.append({
                "id": floor_id,
                "name": stem,
                "path": str(dxf_file).replace("\\", "/"),
                "source": "dxf",
            })

    for fid, paths in _IFC_SOURCES.items():
        floors.append({
            "id": fid,
            "name": _ID_TO_STEM.get(fid, fid),
            "path": str(paths[0]).replace("\\", "/") if paths else "",
            "source": "ifc",
        })

    return floors


@app.delete("/api/floors/{floor_id}")
def delete_floor(floor_id: str) -> dict:
    """Remove a floor: its source file(s) and every derived cache entry.

    Lets the UI prune unwanted entries (test uploads, accidental DWG → tmp
    artefacts, etc.) from the Persistent Disk so they stop appearing in tabs.
    """
    import shutil as _shutil

    removed: list[str] = []

    # IFC: floor_id maps to a directory of .ifc files
    if floor_id in _IFC_SOURCES:
        folder = IFC_DIR / floor_id
        if folder.is_dir():
            _shutil.rmtree(folder, ignore_errors=True)
            removed.append(str(folder))
        _IFC_SOURCES.pop(floor_id, None)
        _parse_cache.pop(f"ifc::{floor_id}", None)

    # DXF: floor_id maps to a single .dxf
    stem = _ID_TO_STEM.get(floor_id)
    if stem:
        dxf_path = DXF_DIR / f"{stem}.dxf"
        if dxf_path.is_file():
            try:
                dxf_path.unlink()
                removed.append(str(dxf_path))
            except OSError:
                pass
        _parse_cache.pop(str(dxf_path.resolve()), None)

        # On-disk parse cache (.parse_cache/<stem>.json)
        pc = _cache_path(dxf_path)
        if pc.is_file():
            pc.unlink(missing_ok=True)

    # Universal-entity caches keyed by floor_id
    _universal_cache.pop(floor_id, None)
    ae_cache = _all_entities_cache_path(floor_id)
    if ae_cache.is_file():
        ae_cache.unlink(missing_ok=True)

    # Classifier cache keyed by floor_id (created in /api/all_entities)
    classifier_cache = DATA_DIR / ".classifier_cache" / f"{floor_id}.json"
    if classifier_cache.is_file():
        classifier_cache.unlink(missing_ok=True)

    # Strip from the id maps last so the lookups above could still resolve
    if stem:
        _FLOOR_ID_MAP.pop(stem, None)
    _ID_TO_STEM.pop(floor_id, None)

    if not removed and stem is None and floor_id not in _IFC_SOURCES:
        raise HTTPException(status_code=404, detail=f"Unknown floor_id '{floor_id}'")

    return {"id": floor_id, "removed": removed}


@app.post("/api/upload")
async def upload_dxf(file: UploadFile = File(...), label: str = Form("")):
    """Upload a DXF file and return its floor entry."""
    if not file.filename or not file.filename.lower().endswith(".dxf"):
        raise HTTPException(status_code=400, detail="DXF file required")

    DXF_DIR.mkdir(exist_ok=True)
    stem = Path(file.filename).stem
    dest = DXF_DIR / file.filename
    content = await file.read()
    dest.write_bytes(content)

    floor_id = _FLOOR_ID_MAP.get(stem) or _stem_to_floor_id(stem)
    _FLOOR_ID_MAP[stem] = floor_id
    _ID_TO_STEM[floor_id] = stem

    # Clear cache for this file if it was previously parsed
    key = str(dest.resolve())
    _parse_cache.pop(key, None)

    return {
        "id": floor_id,
        "name": stem,
        "label": label or stem,
        "path": str(dest).replace("\\", "/"),
    }


@app.post("/api/upload_dwg")
async def upload_dwg(file: UploadFile = File(...), label: str = Form("")):
    """Upload a DWG, convert to DXF via ODA File Converter, register as a floor."""
    import tempfile

    if not file.filename or not file.filename.lower().endswith(".dwg"):
        raise HTTPException(status_code=400, detail="DWG file required")

    DXF_DIR.mkdir(exist_ok=True)
    stem = Path(file.filename).stem

    # Stage with original filename so the converter's output DXF inherits the
    # original stem (it derives the output name from the input stem).
    with tempfile.TemporaryDirectory() as tmpdir:
        dwg_tmp = Path(tmpdir) / f"{stem}.dwg"
        dwg_tmp.write_bytes(await file.read())
        try:
            dxf_path = convert_dwg_to_dxf(dwg_tmp, DXF_DIR)
        except DwgConversionError as e:
            raise HTTPException(
                status_code=500,
                detail=(
                    f"DWG→DXF conversion failed: {e} — "
                    "try converting locally with ODA File Converter and upload the DXF."
                ),
            )

    floor_id = _FLOOR_ID_MAP.get(stem) or _stem_to_floor_id(stem)
    _FLOOR_ID_MAP[stem] = floor_id
    _ID_TO_STEM[floor_id] = stem
    _parse_cache.pop(str(dxf_path.resolve()), None)

    return {
        "id": floor_id,
        "name": stem,
        "label": label or stem,
        "path": str(dxf_path).replace("\\", "/"),
        "source": "dwg",
    }


@app.post("/api/upload_ifc")
async def upload_ifc(
    files: list[UploadFile] = File(...),
    label: str = Form(""),
):
    """Upload one or more IFC files. The parser figures out which provides
    sleeves / grids / architecture — the UI doesn't need to pre-classify.
    """
    if not files:
        raise HTTPException(status_code=400, detail="At least one IFC file is required")
    for f in files:
        if not f.filename or not f.filename.lower().endswith(".ifc"):
            raise HTTPException(status_code=400, detail=f"Non-IFC file rejected: {f.filename}")

    # Derive a floor id from the first filename (stable enough for demos).
    stem = Path(files[0].filename or "ifc").stem
    base_id = _stem_to_floor_id(stem)
    # Disambiguate from DXF namespace so the same stem doesn't clobber an existing DXF
    floor_id = f"{base_id}-ifc"

    IFC_DIR.mkdir(exist_ok=True)
    folder = IFC_DIR / floor_id
    folder.mkdir(exist_ok=True)

    # Clear any previous IFCs in this folder so re-upload doesn't leave stale ones.
    for old in folder.glob("*.ifc"):
        try:
            old.unlink()
        except OSError:
            pass

    saved: list[Path] = []
    for f in files:
        name = Path(f.filename or "file.ifc").name
        dest = folder / name
        dest.write_bytes(await f.read())
        saved.append(dest)

    _IFC_SOURCES[floor_id] = saved
    _ID_TO_STEM[floor_id] = stem
    _parse_cache.pop(f"ifc::{floor_id}", None)

    return {
        "id": floor_id,
        "name": stem,
        "label": label or stem,
        "path": str(saved[0]).replace("\\", "/"),
        "source": "ifc",
        "file_count": len(saved),
    }


@app.post("/api/parse")
def parse_floor(request: ParseRequest) -> dict:
    """Parse a floor (DXF or IFC) and return FloorData as JSON (result is cached)."""
    floor_data = _resolve_floor_data(request.floor_id, request.path)
    return _floor_data_to_dict(floor_data)


# ---------------------------------------------------------------------------
# /api/all_entities — flat enumeration of EVERY DXF/IFC element with the
# rule/LLM-based UI category attached. Used by DataExplorer to show every
# layer the file contains, not just the 14 typed extractors in parser.py.
# ---------------------------------------------------------------------------

_universal_cache: dict[str, dict] = {}
_ALL_ENTITIES_CACHE_DIR = DATA_DIR / ".all_entities_cache"


def _all_entities_cache_path(cache_key: str) -> Path:
    _ALL_ENTITIES_CACHE_DIR.mkdir(exist_ok=True)
    safe = cache_key.replace("/", "_").replace("\\", "_")
    return _ALL_ENTITIES_CACHE_DIR / f"{safe}.json"


@app.post("/api/all_entities")
def all_entities(request: ParseRequest) -> dict:
    """Return every entity in the file plus a per-layer UI category mapping."""
    import json as _json

    cache_key = request.floor_id or request.path or ""

    # In-memory cache
    if cache_key and cache_key in _universal_cache:
        return _universal_cache[cache_key]

    # Disk cache (survives restarts)
    if cache_key:
        cache_file = _all_entities_cache_path(cache_key)
        if cache_file.exists():
            try:
                cached = _json.loads(cache_file.read_text(encoding="utf-8"))
                # Invalidate when the cached layer_categories reference a
                # category that no longer exists (renames / splits). The
                # next call rebuilds the cache from scratch.
                from sleeve_checker.layer_classifier import CATEGORIES
                cat_set = set(CATEGORIES)
                cat_values = (cached.get("layer_categories") or {}).values()
                if all(v in cat_set for v in cat_values):
                    _universal_cache[cache_key] = cached
                    return cached
            except Exception:
                pass

    # Source: IFC if registered, else resolve as DXF
    if request.floor_id and request.floor_id in _IFC_SOURCES:
        paths = [str(p) for p in _IFC_SOURCES[request.floor_id]]
        dump = flatten(paths)
    else:
        filepath = _get_floor_path(request.floor_id, request.path)
        dump = flatten(filepath)

    # Build per-layer summary for the classifier
    from collections import defaultdict
    layer_types: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    layer_texts: dict[str, list[str]] = defaultdict(list)
    layer_text_seen: dict[str, set[str]] = defaultdict(set)
    for e in dump.entities:
        layer_types[e.layer][e.type] = layer_types[e.layer].get(e.type, 0) + 1
        if e.subtype and e.type in ("TEXT", "MTEXT"):
            if e.subtype not in layer_text_seen[e.layer] and len(layer_texts[e.layer]) < 8:
                layer_text_seen[e.layer].add(e.subtype)
                layer_texts[e.layer].append(e.subtype)

    layer_inputs = [
        {
            "name": name,
            "sample_texts": layer_texts[name],
            "type_count": dict(layer_types[name]),
        }
        for name in sorted(layer_types.keys())
    ]

    # Classify via LLM (cached on disk). Each unique layer is sent to the
    # LLM exactly once per project — subsequent parses read the cache.
    cache_dir = DATA_DIR / ".classifier_cache"
    cache_file = cache_dir / f"{cache_key or 'default'}.json"
    classifications = classify_layers(
        layer_inputs,
        use_llm=True,
        cache_path=cache_file,
    )

    response = {
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
        "layer_categories": {
            name: cls.get("category", "その他")
            for name, cls in classifications.items()
        },
    }

    if cache_key:
        _universal_cache[cache_key] = response
        try:
            _all_entities_cache_path(cache_key).write_text(
                _json.dumps(response, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass

    return response


@app.post("/api/check")
def run_checks(request: CheckRequest) -> dict:
    """Run all sleeve checks and return results with a summary."""
    floor_2f = _resolve_floor_data(request.floor_2f_id, request.floor_2f_path)

    floor_1f: FloorData | None = None
    if request.floor_1f_id is not None or request.floor_1f_path is not None:
        floor_1f = _resolve_floor_data(request.floor_1f_id, request.floor_1f_path)

    check_results = run_all_checks(
        floor_2f=floor_2f,
        floor_1f=floor_1f,
        wall_thickness=request.wall_thickness,
    )

    results_list = [_check_result_to_dict(cr) for cr in check_results]

    summary = {"ng": 0, "warning": 0, "ok": 0}
    for cr in check_results:
        key = cr.severity.lower()
        if key in summary:
            summary[key] += 1

    return {"results": results_list, "summary": summary}


# ---------------------------------------------------------------------------
# Serve frontend static files in production
# ---------------------------------------------------------------------------

_static_dir = Path(__file__).parent / "static"

if _static_dir.is_dir():
    app.mount("/assets", StaticFiles(directory=str(_static_dir / "assets")), name="assets")

    @app.get("/{full_path:path}")
    def serve_spa(full_path: str):
        """Fallback: serve index.html for any non-API route (SPA routing)."""
        file = _static_dir / full_path
        if file.is_file():
            return FileResponse(str(file))
        return FileResponse(str(_static_dir / "index.html"))
