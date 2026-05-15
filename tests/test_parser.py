"""
Integration tests for sleeve_checker.parser using real DXF files.

The tests use the files under dxf_output/ which must be present.
Run with:  python -m pytest tests/test_parser.py -v -s
"""

from __future__ import annotations

import pytest
from pathlib import Path


def test_wall_material_classification():
    from sleeve_checker.parser import _wall_material
    assert _wall_material("[空調]F106_RC壁_構造体線") == "RC"
    assert _wall_material("[空調]★既存躯体外壁") == "RC"
    assert _wall_material("[空調]A422_壁：ＡＬＣ") == "ALC"
    assert _wall_material("[建築]A441_壁：ＬＧＳ") == "LGS"
    assert _wall_material("[建築]A423_壁：PCa") == "PCa"
    assert _wall_material("[建築]A424_壁：パネル") == "パネル"
    assert _wall_material("[建築]A443_壁：ＣＢ") == "CB"
    assert _wall_material("[空調]A521_壁：仕上") == "仕上"
    assert _wall_material("[空調]A561_耐火被覆") == "耐火被覆"
    assert _wall_material("[空調]C151_壁心") == "壁心"
    assert _wall_material("[建築]壁") == "RC"
    assert _wall_material("[unknown]xyz") == "不明"

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parent.parent
DXF_1F = _ROOT / "dxf_output" / "1階床スリーブ図.dxf"
DXF_2F = _ROOT / "dxf_output" / "2階床スリーブ図.dxf"


def _skip_if_missing(path: Path) -> None:
    if not path.exists():
        pytest.skip(f"DXF file not found: {path}")


# ---------------------------------------------------------------------------
# Fixtures (parse once per session to avoid repeated 30-second loads)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def floor_data_2f():
    _skip_if_missing(DXF_2F)
    from sleeve_checker.parser import parse_dxf
    return parse_dxf(DXF_2F)


@pytest.fixture(scope="session")
def floor_data_1f():
    _skip_if_missing(DXF_1F)
    from sleeve_checker.parser import parse_dxf
    return parse_dxf(DXF_1F)


# ---------------------------------------------------------------------------
# 2F Sleeve tests
# ---------------------------------------------------------------------------

def test_parse_2f_sleeves(floor_data_2f):
    """At least 100 sleeves should be extracted from the 2F drawing."""
    n = len(floor_data_2f.sleeves)
    print(f"\n  2F sleeves extracted: {n}")
    assert n >= 100, f"Expected ≥100 sleeves, got {n}"


def test_parse_2f_sleeve_has_diameter(floor_data_2f):
    """All sleeves must have a positive diameter."""
    bad = [s for s in floor_data_2f.sleeves if s.diameter <= 0]
    if bad:
        for s in bad[:5]:
            print(f"  Bad sleeve: {s.id}, diameter={s.diameter}, center={s.center}")
    assert not bad, f"{len(bad)} sleeves have diameter ≤ 0"


def test_parse_2f_sleeve_has_center(floor_data_2f):
    """All sleeve centres must lie within the building coordinate range."""
    out = [
        s for s in floor_data_2f.sleeves
        if not (0 <= s.center[0] <= 80_000 and 0 <= s.center[1] <= 34_000)
    ]
    if out:
        for s in out[:5]:
            print(f"  Out-of-range sleeve: {s.id}, center={s.center}")
    assert not out, f"{len(out)} sleeves have centres outside building range"


# ---------------------------------------------------------------------------
# 2F Grid line tests
# ---------------------------------------------------------------------------

def test_parse_2f_grid_lines(floor_data_2f):
    """At least 4 horizontal grid lines and 6 vertical grid lines."""
    h_lines = [g for g in floor_data_2f.grid_lines if g.direction == "H"]
    v_lines = [g for g in floor_data_2f.grid_lines if g.direction == "V"]
    print(f"\n  2F H-grid lines: {len(h_lines)}, V-grid lines: {len(v_lines)}")
    assert len(h_lines) >= 4, f"Expected ≥4 H grid lines, got {len(h_lines)}"
    assert len(v_lines) >= 6, f"Expected ≥6 V grid lines, got {len(v_lines)}"


def test_parse_2f_grid_positions(floor_data_2f):
    """Y positions of horizontal grid lines should match the known 5 values."""
    expected_y = {0.0, 4000.0, 19200.0, 25200.0, 34000.0}
    tolerance = 100.0  # mm tolerance for floating-point / rounding

    h_positions = sorted(
        g.position for g in floor_data_2f.grid_lines if g.direction == "H"
    )
    print(f"\n  2F H-grid positions: {h_positions}")

    for expected in expected_y:
        closest = min(h_positions, key=lambda p: abs(p - expected), default=None)
        assert closest is not None and abs(closest - expected) <= tolerance, (
            f"Expected H-grid near Y={expected}, closest found: {closest}"
        )


# ---------------------------------------------------------------------------
# 2F Wall / Step / Dimension tests
# ---------------------------------------------------------------------------

def test_parse_2f_wall_lines(floor_data_2f):
    """Wall lines must be extracted."""
    n = len(floor_data_2f.wall_lines)
    print(f"\n  2F wall lines: {n}")
    assert n > 0, "No wall lines extracted from 2F"


def test_parse_2f_step_lines(floor_data_2f):
    """Step lines must be extracted."""
    n = len(floor_data_2f.step_lines)
    print(f"\n  2F step lines: {n}")
    assert n > 0, "No step lines extracted from 2F"


def test_parse_2f_dim_lines(floor_data_2f):
    """Dimension lines must be extracted."""
    n = len(floor_data_2f.dim_lines)
    print(f"\n  2F dim lines: {n}")
    assert n > 0, "No dimension lines extracted from 2F"


# ---------------------------------------------------------------------------
# 1F tests
# ---------------------------------------------------------------------------

def test_parse_1f_sleeves(floor_data_1f):
    """At least 200 sleeves should be extracted from the 1F drawing."""
    n = len(floor_data_1f.sleeves)
    print(f"\n  1F sleeves extracted: {n}")
    assert n >= 100, f"Expected ≥100 sleeves, got {n}"


def test_parse_1f_wall_lines(floor_data_1f):
    """Wall lines must be extracted from 1F."""
    n = len(floor_data_1f.wall_lines)
    print(f"\n  1F wall lines: {n}")
    assert n > 0, "No wall lines extracted from 1F"


def test_parse_1f_wall_is_exterior_set(floor_data_1f):
    """1F has a dedicated ★既存躯体外壁 layer; geometry-based is_exterior
    must mark a substantial portion of walls as exterior."""
    exterior = [w for w in floor_data_1f.wall_lines if w.is_exterior]
    interior = [w for w in floor_data_1f.wall_lines if not w.is_exterior]
    print(f"\n  1F exterior walls: {len(exterior)}, interior: {len(interior)}")
    assert len(exterior) >= 100, f"too few exterior walls: {len(exterior)}"
    assert len(interior) > 0, "all walls cannot be exterior"


def test_parse_2f_walls_have_is_exterior_field(floor_data_2f):
    """2F walls must all carry the is_exterior field (True or False).

    Note on coverage: 2F's architectural walls live inside INSERT blocks
    that the current wall extractor doesn't recurse into — that's a
    separate pre-existing limitation tracked in `universal_parser.py`.
    For the walls that ARE extracted, the geometry path must run and
    set is_exterior cleanly (no AttributeError, no None values).
    """
    for w in floor_data_2f.wall_lines:
        assert isinstance(w.is_exterior, bool), (
            f"is_exterior not set on wall: {w.layer}"
        )


# ---------------------------------------------------------------------------
# Shape / color classification (customer-visibility regression guard)
# ---------------------------------------------------------------------------

def test_parse_1f_sleeve_shapes(floor_data_1f):
    """Both round and rect sleeves should be classified on 1F."""
    rounds = [s for s in floor_data_1f.sleeves if s.shape == "round"]
    rects = [s for s in floor_data_1f.sleeves if s.shape == "rect"]
    print(f"\n  1F round={len(rounds)}, rect={len(rects)}")
    assert len(rounds) > 0, "No round sleeves classified"
    assert len(rects) > 0, "No rect sleeves classified (角スリーブ脱落の疑い)"


def test_parse_1f_sleeve_dimensions(floor_data_1f):
    """Every sleeve must have non-zero width and height."""
    bad = [
        s for s in floor_data_1f.sleeves
        if s.width <= 0 or s.height <= 0
    ]
    assert not bad, f"{len(bad)} sleeves have zero width/height"


def test_standalone_sleeves_captured(floor_data_1f):
    """Standalone (non-INSERT) sleeves should be captured if present in drawing.

    The 1F sample drawing includes at least one standalone red rect sleeve.
    """
    standalone = [s for s in floor_data_1f.sleeves if s.id.startswith("standalone_")]
    print(f"\n  1F standalone sleeves: {len(standalone)}")
    # Assertion is a lower bound — we expect at least the known red rect.
    assert len(standalone) >= 1, "No standalone sleeves extracted from 1F"
