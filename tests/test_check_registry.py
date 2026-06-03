"""tests/test_check_registry.py — registry parity, memoisation, codegen safety.

The headline guarantee: running the seeded registry (builtin dispatch) must
produce *byte-for-byte the same* CheckResults as the original
``run_all_checks`` — so wrapping the 14 checks in a registry changes nothing.
"""

from __future__ import annotations

import os
from collections import Counter

import pytest

from sleeve_checker.checks import run_all_checks
from sleeve_checker.check_registry import (
    CheckContext,
    CheckDef,
    default_defs,
    run_registry_checks,
)
from sleeve_checker.codegen import (
    CodegenError,
    run_generated_check,
    validate_check_code,
)
from sleeve_checker.models import FloorData, Sleeve, WallLine

DXF_2F = os.path.join(os.path.dirname(__file__), "..", "dxf_output", "2階床スリーブ図.dxf")
DXF_1F = os.path.join(os.path.dirname(__file__), "..", "dxf_output", "1階床スリーブ図.dxf")


def _result_key(cr):
    """A stable, fully-comparable signature for one CheckResult."""
    return (
        cr.check_id, cr.check_name, cr.severity,
        cr.sleeve.id if cr.sleeve is not None else None,
        cr.message, cr.target, cr.rule, cr.expected, cr.found, cr.fix_hint,
    )


# ---------------------------------------------------------------------------
# Seed
# ---------------------------------------------------------------------------

def test_seed_builtins():
    # The checklist has 14 line items but #1 (根拠図との整合) is a human-only
    # review, so 13 auto-checks (#2..#14) are implemented.
    defs = default_defs()
    assert len(defs) == 13
    assert all(d.builtin_key for d in defs)
    assert all(d.code is None for d in defs)
    assert {d.id for d in defs} == {2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14}


# ---------------------------------------------------------------------------
# Parity — mock data
# ---------------------------------------------------------------------------

def _mock_floors():
    s1 = Sleeve(id="s1", center=(0, 0), diameter=100, label_text="SD φ100 外径116φ",
                pn_number="P-N-1", discipline="衛生", fl_text="1FL+1750",
                orientation="vertical")
    s2 = Sleeve(id="s2", center=(2000, 500), diameter=200, label_text="",
                discipline="空調", orientation="horizontal")
    wall = WallLine(start=(20, -500), end=(20, 500), wall_type="LGS", material="LGS")
    floor_2f = FloorData(sleeves=[s1, s2])
    floor_1f = FloorData(wall_lines=[wall])
    return floor_2f, floor_1f


def test_parity_mock():
    floor_2f, floor_1f = _mock_floors()
    legacy = run_all_checks(floor_2f, floor_1f)
    registry = run_registry_checks(floor_2f, floor_1f, registry=default_defs())
    assert Counter(_result_key(r) for r in legacy) == \
        Counter(_result_key(r) for r in registry)


@pytest.mark.skipif(not os.path.exists(DXF_2F), reason="sample DXF not present")
def test_parity_real_dxf():
    from sleeve_checker.parser import parse_dxf
    floor_2f = parse_dxf(DXF_2F)
    floor_1f = parse_dxf(DXF_1F) if os.path.exists(DXF_1F) else None
    legacy = run_all_checks(floor_2f, floor_1f)
    registry = run_registry_checks(floor_2f, floor_1f, registry=default_defs())
    assert Counter(_result_key(r) for r in legacy) == \
        Counter(_result_key(r) for r in registry)


def test_disabled_check_is_skipped():
    floor_2f, floor_1f = _mock_floors()
    defs = default_defs()
    for d in defs:
        if d.id == 14:
            d.enabled = False
    results = run_registry_checks(floor_2f, floor_1f, registry=defs)
    assert all(r.check_id != 14 for r in results)


# ---------------------------------------------------------------------------
# position_determinacy memoisation
# ---------------------------------------------------------------------------

def test_position_determinacy_memoised():
    floor_2f, floor_1f = _mock_floors()
    ctx = CheckContext(floor_2f, floor_1f, {})
    a = ctx.position_determinacy()
    b = ctx.position_determinacy()
    assert a is b  # same object → computed once


# ---------------------------------------------------------------------------
# codegen validation
# ---------------------------------------------------------------------------

_GOOD = '''\
def check(floor, ctx):
    results = []
    for s in floor.sleeves:
        if s.pn_number:
            results.append(ctx.result(severity="OK", sleeve=s, message="ok"))
        else:
            results.append(ctx.result(severity="NG", sleeve=s, message="番号なし"))
    return results'''


def test_validate_good_code():
    validate_check_code(_GOOD)  # no raise


@pytest.mark.parametrize("bad,reason", [
    ("import os\ndef check(floor, ctx):\n    return []", "import"),
    ("def check(floor, ctx):\n    return open('/etc/passwd')", "open"),
    ("def check(floor, ctx):\n    return [].__class__", "dunder"),
    ("def nope(floor, ctx):\n    return []", "no check"),
    ("def check(a, b):\n    return []", "wrong args"),
    ("def check(floor, ctx):\n    eval('1+1')\n    return []", "eval"),
    ("def check(floor, ctx)\n    return []", "syntax"),
])
def test_validate_rejects(bad, reason):
    with pytest.raises(CodegenError):
        validate_check_code(bad)


def test_run_generated_good():
    floor_2f, floor_1f = _mock_floors()
    ctx = CheckContext(floor_2f, floor_1f, {})
    out = run_generated_check(_GOOD, floor_2f, ctx)
    assert len(out) == 2
    sevs = {r.severity for r in out}
    assert sevs <= {"OK", "NG"}


def test_generated_runtime_error_isolated():
    """A check that throws at runtime → registry surfaces one NG, no crash."""
    boom = "def check(floor, ctx):\n    return floor.sleeves[999].center"
    defs = [CheckDef(id=99, name="爆発", category="その他",
                     description="x", builtin_key=None, code=boom,
                     enabled=True, order=0)]
    floor_2f, floor_1f = _mock_floors()
    results = run_registry_checks(floor_2f, floor_1f, registry=defs)
    assert len(results) == 1
    assert results[0].severity == "NG"
    assert "エラー" in results[0].message
    assert results[0].check_id == 99
