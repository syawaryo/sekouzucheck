"""check_registry.py — editable / freely-addable check definitions.

The 14 hard-coded checks in ``checks.py`` become *seed* entries in a
system-wide registry stored as JSON. Each entry is a :class:`CheckDef`.

- **builtin** entries (``builtin_key`` set) keep the original, tested Python:
  they dispatch to the real function in ``checks.py`` — zero accuracy loss.
- **generated** entries (``code`` set) carry LLM-generated Python that is
  executed in the sandbox (see ``codegen.py``).

Both kinds run through the uniform contract ``check(floor, ctx) -> list``.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from . import checks as _checks
from . import geometry as _geom
from .models import CheckResult, FloorData

# ---------------------------------------------------------------------------
# Storage location
# ---------------------------------------------------------------------------

def _data_dir() -> Path:
    return Path(os.getenv("DATA_DIR", "."))


def _registry_path() -> Path:
    p = _data_dir() / "checks"
    p.mkdir(parents=True, exist_ok=True)
    return p / "registry.json"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class CheckDef:
    id: int
    name: str
    category: str
    description: str
    builtin_key: str | None = None
    code: str | None = None
    enabled: bool = True
    order: int = 0

    @property
    def source(self) -> str:
        return "builtin" if self.builtin_key else "generated"

    def to_public_dict(self) -> dict:
        """Shape sent to the frontend (omits raw code)."""
        return {
            "id": self.id,
            "name": self.name,
            "category": self.category,
            "description": self.description,
            "source": self.source,
            "enabled": self.enabled,
            "order": self.order,
        }


# ---------------------------------------------------------------------------
# Execution context shared by every check
# ---------------------------------------------------------------------------

class CheckContext:
    """Shared state + helpers handed to every check function.

    Generated checks receive this as ``ctx``; builtin adapters read the same
    fields. ``position_determinacy()`` is memoised so checks #9 and #11 share
    one graph solve.
    """

    def __init__(
        self,
        floor: FloorData,
        lower_floor: FloorData | None,
        wall_thickness: dict[str, float],
    ) -> None:
        self.floor = floor
        self.lower_floor = lower_floor
        self.wall_thickness = wall_thickness
        self._pd_cache: tuple | None = None

        # Geometry / stdlib helpers exposed to generated code.
        import math as _math
        import re as _re
        self.math = _math
        self.re = _re
        self.point_to_segment_distance = _geom.point_to_segment_distance
        self.points_match = _geom.points_match
        self.point_in_polygon = _geom.point_in_polygon
        self.point_on_any_segment = _geom.point_on_any_segment
        self.segments_intersect = _geom.segments_intersect

    @property
    def lower_walls(self):
        return self.lower_floor.wall_lines if self.lower_floor is not None else []

    def position_determinacy(self):
        """Memoised (results, x_resolved, y_resolved) for the active floor."""
        if self._pd_cache is None:
            self._pd_cache = _checks.check_position_determinacy(
                self.floor.sleeves, self.floor.dim_lines, self.floor.grid_lines,
            )
        return self._pd_cache

    def result(self, *, severity: str, sleeve=None, message: str = "",
               related_coords=None, target: str = "", rule: str = "",
               expected: str = "", found: str = "", fix_hint: str = "") -> CheckResult:
        """Build a CheckResult. check_id/check_name are stamped by the runner."""
        return CheckResult(
            check_id=0, check_name="", severity=severity, sleeve=sleeve,
            message=message, related_coords=list(related_coords or []),
            target=target, rule=rule, expected=expected, found=found,
            fix_hint=fix_hint,
        )


# ---------------------------------------------------------------------------
# Builtin adapters — wrap existing checks.py functions to (floor, ctx) -> list
# ---------------------------------------------------------------------------

def _a_discipline(floor, ctx):
    out = []
    for s in floor.sleeves:
        out.extend(_checks.check_discipline(s))
    return out


def _a_diameter_label(floor, ctx):
    out = []
    for s in floor.sleeves:
        out.extend(_checks.check_diameter_label(s))
    return out


def _a_gradient(floor, ctx):
    out = []
    for s in floor.sleeves:
        out.extend(_checks.check_gradient(
            s, floor.pn_labels, floor.slab_zones, floor.slab_labels,
            floor.water_gradients,
        ))
    return out


def _a_sleeve_number(floor, ctx):
    out = []
    for s in floor.sleeves:
        out.extend(_checks.check_sleeve_number(s))
    return out


def _a_step_slab(floor, ctx):
    out = []
    for s in floor.sleeves:
        out.extend(_checks.check_step_slab(s, floor.step_lines, floor.recess_polygons))
    return out


def _a_lower_wall(floor, ctx):
    out = []
    lower_walls = ctx.lower_walls
    if not lower_walls:
        return out
    for s in floor.sleeves:
        if (s.orientation or "").lower() == "horizontal":
            continue
        out.extend(_checks.check_lower_wall(s, lower_walls, ctx.wall_thickness))
    return out


def _a_base_level(floor, ctx):
    return _checks.check_base_level(floor.sleeves)


def _a_dim_sum(floor, ctx):
    return _checks.check_dim_sum(floor.dim_lines, floor.grid_lines)


def _a_dim_notation(floor, ctx):
    return _checks.check_dim_notation(floor.dim_lines)


def _a_step_dim(floor, ctx):
    out = []
    for s in floor.sleeves:
        out.extend(_checks.check_step_dim(s, floor.dim_lines, floor.step_lines))
    return out


def _a_column_wall_dim(floor, ctx):
    out = []
    for s in floor.sleeves:
        out.extend(_checks.check_column_wall_dim(s, floor.dim_lines, floor.column_lines))
    return out


def _a_position_determinacy(floor, ctx):
    det_results, _x, _y = ctx.position_determinacy()
    return det_results


def _a_sleeve_center_dim(floor, ctx):
    _det, x_resolved, y_resolved = ctx.position_determinacy()
    return _checks.check_sleeve_center_dim(floor.sleeves, x_resolved, y_resolved)


BUILTIN_ADAPTERS: dict[str, Callable[[FloorData, CheckContext], list[CheckResult]]] = {
    "discipline": _a_discipline,
    "diameter_label": _a_diameter_label,
    "gradient": _a_gradient,
    "sleeve_number": _a_sleeve_number,
    "step_slab": _a_step_slab,
    "lower_wall": _a_lower_wall,
    "base_level": _a_base_level,
    "dim_sum": _a_dim_sum,
    "dim_notation": _a_dim_notation,
    "step_dim": _a_step_dim,
    "column_wall_dim": _a_column_wall_dim,
    "position_determinacy": _a_position_determinacy,
    "sleeve_center_dim": _a_sleeve_center_dim,
}


# ---------------------------------------------------------------------------
# Seed definitions (the original 14 checks)
# ---------------------------------------------------------------------------

_INTEGRITY = "整合性"
_DRAFTING = "施工図表現"

# (id, name, category, builtin_key, description)
_SEED: list[tuple[int, str, str, str, str]] = [
    (2, "設備種別記載", _INTEGRITY, "discipline",
     "スリーブ近傍のラベルテキストに設備種別コード（空調 EA/OA/SA/RA、衛生 CW/RD/SD/HW、電気 XS/KD/KV 等）が含まれること。"),
    (3, "口径・外径記載", _INTEGRITY, "diameter_label",
     "ラベルに呼び口径（例 200φ）と外径（例 外径216φ）の両方が記載されていること。角スリーブは矩形ジオメトリから W×H 寸法が取得できること。"),
    (4, "寸法合計", _INTEGRITY, "dim_sum",
     "連続する寸法チェーンの合計が、通り芯〜芯の距離と一致すること（許容±5mm）。チェーン端点は通り芯にスナップしていること。"),
    (5, "勾配記載", _INTEGRITY, "gradient",
     "排水スリーブには勾配情報（FL値、または近傍の水勾配記号・スラブ勾配ラベル）が記載されていること。"),
    (6, "下階壁干渉", _INTEGRITY, "lower_wall",
     "縦スリーブが直下階の壁・複合耐火壁と干渉していないこと。RC壁は芯=表面とみなし、乾式壁は壁厚の半分を見込んで判定する。"),
    (7, "段差近接", _INTEGRITY, "step_slab",
     "スリーブ円が段差線に重ならず、床ヌスミ（凹み）ポリゴン内に芯が入っていないこと（残コンクリート厚不足の防止）。"),
    (8, "基準レベル記載", _DRAFTING, "base_level",
     "横スリーブ（壁貫通）は基準レベルからの寸法（例 1FL+1750、2FL-550）を持つこと。"),
    (9, "位置特定寸法", _DRAFTING, "position_determinacy",
     "各スリーブの位置が X 方向・Y 方向とも寸法チェーンから特定でき、最終的に通り芯へ帰着すること。"),
    (10, "段差基準寸法", _DRAFTING, "step_dim",
     "スリーブ位置寸法の参照点が段差線・ヌスミ線上にないこと（段差はずれる可能性があり基準として不適切）。"),
    (11, "スリーブ芯寸法", _DRAFTING, "sleeve_center_dim",
     "スリーブ芯から発する寸法チェーンが、他スリーブ経由を含め最終的に通り芯まで到達すること。"),
    (12, "柱・壁仕上寸法", _DRAFTING, "column_wall_dim",
     "スリーブ位置寸法の参照点が柱外周線・壁仕上線上にないこと（通り芯または躯体線基準が望ましい）。"),
    (13, "寸法表記統一", _DRAFTING, "dim_notation",
     "寸法テキストの表記フォーマット（カンマ区切り / 単位 mm / 小数桁）が図面内で統一されていること。"),
    (14, "スリーブ番号記載", _DRAFTING, "sleeve_number",
     "各スリーブに P-N-{数字} 形式の番号が振られていること。"),
]


def default_defs() -> list[CheckDef]:
    return [
        CheckDef(id=i, name=name, category=cat, description=desc,
                 builtin_key=key, code=None, enabled=True, order=order)
        for order, (i, name, cat, key, desc) in enumerate(_SEED)
    ]


# ---------------------------------------------------------------------------
# Load / save / seed
# ---------------------------------------------------------------------------

def load_registry() -> list[CheckDef]:
    """Load the registry, seeding it from the builtins on first use."""
    path = _registry_path()
    if not path.exists():
        defs = default_defs()
        save_registry(defs)
        return defs
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        defs = [CheckDef(**d) for d in raw]
        if not defs:
            defs = default_defs()
            save_registry(defs)
        return defs
    except Exception:
        # Corrupt file → reseed rather than 500.
        defs = default_defs()
        save_registry(defs)
        return defs


def save_registry(defs: list[CheckDef]) -> None:
    path = _registry_path()
    path.write_text(
        json.dumps([asdict(d) for d in defs], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def next_id(defs: list[CheckDef]) -> int:
    return (max((d.id for d in defs), default=0) + 1)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_registry_checks(
    floor_2f: FloorData,
    floor_1f: FloorData | None = None,
    wall_thickness: dict[str, float] | None = None,
    registry: list[CheckDef] | None = None,
) -> list[CheckResult]:
    """Execute every enabled check in the registry, in ``order``.

    builtin entries dispatch to the original functions; generated entries are
    executed in the sandbox. Each entry is isolated so one bad check cannot
    crash the run. Results are stamped with the def's id/name for grouping.
    """
    from .codegen import run_generated_check  # local import: avoids cycle

    if wall_thickness is None:
        wall_thickness = dict(_checks._DEFAULT_WALL_THICKNESS)
    if registry is None:
        registry = load_registry()

    ctx = CheckContext(floor_2f, floor_1f, wall_thickness)
    results: list[CheckResult] = []

    for d in sorted(registry, key=lambda x: x.order):
        if not d.enabled:
            continue
        try:
            if d.builtin_key:
                adapter = BUILTIN_ADAPTERS.get(d.builtin_key)
                if adapter is None:
                    raise ValueError(f"未知の組込みキー: {d.builtin_key}")
                part = adapter(floor_2f, ctx)
            else:
                part = run_generated_check(d.code or "", floor_2f, ctx)
        except Exception as e:  # isolate failures per check
            part = [ctx.result(
                severity="NG",
                message=f"チェック実行エラー: {type(e).__name__}: {e}",
                rule=d.description,
            )]
        for cr in part:
            cr.check_id = d.id
            cr.check_name = d.name
            results.append(cr)

    return results
