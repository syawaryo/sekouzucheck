"""
checks.py - All 14 sleeve check functions for the sleeve checker project.

Each function returns a list[CheckResult].  The integration function
run_all_checks() wires them all together given FloorData objects.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict

from .geometry import point_to_segment_distance, points_match, point_on_any_segment, point_in_polygon
from .models import (
    CheckResult,
    ColumnLine,
    DimLine,
    FloorData,
    GridLine,
    RecessPolygon,
    SlabLabel,
    Sleeve,
    StepLine,
    WallLine,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DRAIN_CODES = ["SD", "RD", "WD", "KD", "CDW", "SPD", "D:", "排水", "汚水", "雨水"]

_DEFAULT_WALL_THICKNESS: dict[str, float] = {
    "RC": 0,            # RC = 躯体線 (中心 ≒ 表面) のためオフセット 0
    "LGS": 150,
    "ALC": 150,
    "PCa": 200,
    "パネル": 100,
    "木軸": 100,
    "CB": 150,
    "耐火被覆": 50,
    "壁心": 0,           # 中心参照線 — オフセットなし
    "仕上": 0,           # 仕上線 — 表面なのでオフセットなし
    "不明": 200,
}


def _sleeve_target(sleeve: Sleeve) -> str:
    """Human-readable 'what was inspected' label for a sleeve.

    Format: "スリーブ P-N-12 (空調) / [空調]F141_..."
    Falls back gracefully when pn_number / discipline / layer are empty.
    """
    pn = (sleeve.pn_number or "").strip() or "(番号なし)"
    disc = (sleeve.discipline or "").strip()
    layer = (sleeve.layer or "").strip()
    head = f"スリーブ {pn}"
    if disc:
        head += f" ({disc})"
    if layer:
        return f"{head} / {layer}"
    return head

# ---------------------------------------------------------------------------
# #2 check_discipline
# ---------------------------------------------------------------------------

def check_discipline(sleeve: Sleeve) -> list[CheckResult]:
    """Check #2: discipline label exists (label_text non-empty)."""
    if sleeve.label_text and sleeve.label_text.strip():
        return [CheckResult(
            check_id=2,
            check_name="設備種別記載",
            severity="OK",
            sleeve=sleeve,
            message="設備種別記載あり",
        )]
    return [CheckResult(
        check_id=2,
        check_name="設備種別記載",
        severity="NG",
        sleeve=sleeve,
        message="設備種別ラベルなし",
        target=_sleeve_target(sleeve),
        rule="スリーブ近傍のラベルテキストに設備種別コードが含まれること",
        expected="EA / OA / SA / RA / KEA（空調）, CW / RD / SD / HW（衛生）, XS / KD / KV（電気）等",
        found="ラベルテキスト なし",
        fix_hint="スリーブに設備種別コードを含むラベルを追記する",
    )]


# ---------------------------------------------------------------------------
# #3 check_diameter_label
# ---------------------------------------------------------------------------

_RE_OUTER = re.compile(r"外径\s*\d+\s*[φΦø]?")
# Match φ+number, number+φ, or number+A (pipe size like 150A)
_RE_PHI_NUM = re.compile(r"(\d+)\s*[φΦø]|[φΦø]\s*(\d+)|(\d+)A\b")
# Match W×H / W x H (box-sleeve dimension, e.g. "500×300", "1000x500", "500 X 600")
_RE_RECT_DIM = re.compile(r"\d+\s*[x×X]\s*\d+")


def check_diameter_label(sleeve: Sleeve) -> list[CheckResult]:
    """Check #3: sleeve size annotation is present on the drawing.

    Shape-aware rule:
    - **Vertical 角スリーブ (rectangular box through slab)**: the drafting
      convention is to write a W×H outer dimension (e.g. "500×300"). The
      concept of 内径/外径 does not apply here — one dimension pair is
      sufficient.
    - **Round slab penetrations AND horizontal wall penetrations**: the
      conventional rule applies (呼び口径φ and 外径φ both recorded). Even
      when a horizontal round sleeve is drawn as a long rectangle on the
      plan, the underlying object is a circular pipe so the round rule
      is what matters for 施工 + 構造.
    """
    txt = sleeve.diameter_text or ""
    label = sleeve.label_text or ""
    combined = f"{txt} {label}"

    is_horizontal = (sleeve.orientation or "").lower() == "horizontal"
    is_rect_box = sleeve.shape == "rect" and not is_horizontal

    if is_rect_box:
        # For box sleeves the W×H dimension lives in the rectangle geometry
        # itself (the drafter draws the box at scale; there's no text W×H).
        # We consider the dimension "recorded" whenever both sides exist.
        w = float(getattr(sleeve, "width", 0.0) or 0.0)
        h = float(getattr(sleeve, "height", 0.0) or 0.0)
        if w > 0 and h > 0:
            return [CheckResult(
                check_id=3,
                check_name="口径・外径記載",
                severity="OK",
                sleeve=sleeve,
                message=f"角スリーブ寸法 {int(round(w))}×{int(round(h))}",
            )]
        return [CheckResult(
            check_id=3,
            check_name="口径・外径記載",
            severity="NG",
            sleeve=sleeve,
            message="角スリーブ: W×H寸法が取得できない",
            related_coords=[sleeve.center],
            target=_sleeve_target(sleeve),
            rule="角スリーブは矩形ジオメトリから W×H 寸法が取得できること",
            expected="width > 0 かつ height > 0",
            found=f"width={w:.0f}, height={h:.0f}",
            fix_hint="角スリーブの矩形ジオメトリを修正し W×H を入れる",
        )]

    # Round / horizontal-wall-penetration: need nominal + outer both recorded
    has_outer = bool(_RE_OUTER.search(combined))
    stripped = _RE_OUTER.sub("", combined)
    has_nominal = bool(_RE_PHI_NUM.search(stripped))

    if has_nominal and has_outer:
        return [CheckResult(
            check_id=3,
            check_name="口径・外径記載",
            severity="OK",
            sleeve=sleeve,
            message="呼び口径・外径記載あり",
        )]

    missing = []
    if not has_nominal:
        missing.append("呼び口径")
    if not has_outer:
        missing.append("外径")

    return [CheckResult(
        check_id=3,
        check_name="口径・外径記載",
        severity="NG",
        sleeve=sleeve,
        message=f"{'・'.join(missing)}の記載なし",
        related_coords=[sleeve.center],
        target=_sleeve_target(sleeve),
        rule="ラベルに呼び口径 (例: 200φ) と外径 (例: 外径216φ) の両方が記載されていること",
        expected='例: "200φ 外径216φ"',
        found=f'label="{label.strip() or "-"}" / diameter_text="{txt.strip() or "-"}"（{"・".join(missing)} 未検出）',
        fix_hint=f"{'・'.join(missing)}をラベルに追記する",
    )]


# ---------------------------------------------------------------------------
# #5 check_gradient
# ---------------------------------------------------------------------------

_RE_GRADIENT = re.compile(r"FL|1/\d+", re.IGNORECASE)

_GRADIENT_SEARCH_RADIUS = 1500.0  # mm — search radius for nearby gradient info


def check_gradient(
    sleeve: Sleeve,
    pn_labels: list | None = None,
    slab_zones: list | None = None,
    slab_labels: list | None = None,
    water_gradients: list | None = None,
) -> list[CheckResult]:
    """Check #5: drain sleeves — collect gradient/flow info and flag for review.

    For drain sleeves, report:
    - FL value on sleeve itself
    - P-N number (pipe route)
    - Nearby water gradient text (水勾配)
    - Nearby slab gradient (range slab label like 350～300)
    """
    label = sleeve.label_text or ""
    is_drain = any(code in label for code in DRAIN_CODES)

    if not is_drain:
        return [CheckResult(
            check_id=5,
            check_name="勾配記載",
            severity="OK",
            sleeve=sleeve,
            message="排水スリーブではないためスキップ",
        )]

    pn_labels = pn_labels or []
    slab_zones = slab_zones or []
    slab_labels = slab_labels or []
    water_gradients = water_gradients or []
    sx, sy = sleeve.center
    r = _GRADIENT_SEARCH_RADIUS

    # --- FL value on sleeve itself ---
    fl = sleeve.fl_text or ""
    has_fl = bool(_RE_GRADIENT.search(fl) or _RE_GRADIENT.search(label))

    # --- P-N number (use pre-assigned first, fallback to nearest) ---
    pn_str = sleeve.pn_number or ""
    if not pn_str:
        for pn in pn_labels:
            dist = math.hypot(sx - pn.x, sy - pn.y)
            if dist < r:
                pn_str = pn.text
                break

    # --- Nearby water gradient text (水勾配) ---
    nearest_wg_dist = float("inf")
    nearest_wg_direction = ""
    for wg in water_gradients:
        dist = math.hypot(sx - wg.x, sy - wg.y)
        if dist < nearest_wg_dist:
            nearest_wg_dist = dist
            nearest_wg_direction = wg.direction
    has_water_gradient = nearest_wg_dist <= r

    # --- Nearby slab gradient (range label like 350～300) ---
    nearest_slab_gradient: str | None = None
    nearest_sg_dist = float("inf")
    for sl in slab_labels:
        if "～" not in sl.level and "~" not in sl.level:
            continue
        dist = math.hypot(sx - sl.x, sy - sl.y)
        if dist < r and dist < nearest_sg_dist:
            nearest_slab_gradient = sl.level
            nearest_sg_dist = dist

    # --- Build detail message ---
    details: list[str] = []
    details.append(f"FL記載: {'あり (' + fl + ')' if has_fl else 'なし'}")
    if pn_str:
        details.append(f"配管番号: {pn_str}")
    if has_water_gradient:
        dir_str = f" {nearest_wg_direction}" if nearest_wg_direction else ""
        details.append(f"水勾配: あり{dir_str} ({nearest_wg_dist:.0f}mm)")
    if nearest_slab_gradient:
        details.append(f"スラブ勾配: {nearest_slab_gradient} ({nearest_sg_dist:.0f}mm)")

    detail_str = " | ".join(details)

    # Drain sleeve with nearby gradient info → OK (the info is recorded;
    # the drafter has done their job). Any integrity doubt is left to
    # human review elsewhere, not surfaced as a check NG/WARNING here.
    if has_water_gradient or nearest_slab_gradient:
        return [CheckResult(
            check_id=5,
            check_name="勾配記載",
            severity="OK",
            sleeve=sleeve,
            message=f"排水スリーブ 勾配記載あり | {detail_str}",
        )]

    if not has_fl:
        return [CheckResult(
            check_id=5,
            check_name="勾配記載",
            severity="NG",
            sleeve=sleeve,
            message=f"排水スリーブ FL記載なし | {detail_str}",
            related_coords=[sleeve.center],
            target=_sleeve_target(sleeve),
            rule="排水スリーブには勾配情報（FL値または水勾配記号）が必要",
            expected="FL±値の記載 または 水勾配矢印 (↓↑→←)",
            found=detail_str,
            fix_hint="FL値を追記、もしくは近傍に水勾配記号を配置する",
        )]

    return [CheckResult(
        check_id=5,
        check_name="勾配記載",
        severity="OK",
        sleeve=sleeve,
        message=f"排水スリーブ | {detail_str}",
    )]


# ---------------------------------------------------------------------------
# #8 check_base_level
# ---------------------------------------------------------------------------

_RE_BASE_LEVEL = re.compile(r"\dFL\s*[＝=]", re.IGNORECASE)


_RE_FL_NOTATION = re.compile(r"\d?\s*FL\s*[＋－+\-]?\s*\d+", re.IGNORECASE)


def check_base_level(
    sleeves: list[Sleeve],
) -> list[CheckResult]:
    """Check #8: every horizontal sleeve (壁貫通) must carry a FL reference.

    Rationale: a horizontal sleeve goes through a wall at a specific height.
    Without an explicit FL value on the drawing, the installer cannot know
    where to cut the opening. Vertical sleeves (slab penetrations) do not
    need this check — their elevation is the slab itself.

    Sources searched for the FL pattern: fl_text, label_text, diameter_text.
    Per-sleeve NG when no source matches; OK otherwise.
    """
    results: list[CheckResult] = []
    for s in sleeves:
        if (s.orientation or "").lower() != "horizontal":
            continue
        sources = [s.fl_text or "", s.label_text or "", s.diameter_text or ""]
        if any(_RE_FL_NOTATION.search(src) for src in sources):
            results.append(CheckResult(
                check_id=8,
                check_name="基準レベル記載",
                severity="OK",
                sleeve=s,
                message=f"基準レベル記載あり ({(s.fl_text or '').strip()})",
            ))
        else:
            results.append(CheckResult(
                check_id=8,
                check_name="基準レベル記載",
                severity="NG",
                sleeve=s,
                message="横スリーブに基準レベル（1FL+1750等）の記載なし",
                related_coords=[s.center],
                target=_sleeve_target(s),
                rule="横スリーブ（壁貫通）は fl_text / label_text / diameter_text のいずれかに FL±値 を持つこと",
                expected='例: "1FL+1750", "2FL-55"',
                found=f'fl_text="{(s.fl_text or "-").strip()}" / label="{(s.label_text or "-").strip()}" / diameter_text="{(s.diameter_text or "-").strip()}"',
                fix_hint="基準レベル（1FL+1750 等）をラベルに追記する",
            ))
    return results


# ---------------------------------------------------------------------------
# #14 check_sleeve_number
# ---------------------------------------------------------------------------

_RE_PN = re.compile(r"P-N-\d+")


def check_sleeve_number(sleeve: Sleeve) -> list[CheckResult]:
    """Check #14: pn_number matches P-N-{digits}."""
    pn = sleeve.pn_number or ""
    if _RE_PN.fullmatch(pn.strip()):
        return [CheckResult(
            check_id=14,
            check_name="スリーブ番号記載",
            severity="OK",
            sleeve=sleeve,
            message="番号記載あり",
        )]
    return [CheckResult(
        check_id=14,
        check_name="スリーブ番号記載",
        severity="NG",
        sleeve=sleeve,
        message="P-N-番号記載なし・形式不正",
        related_coords=[sleeve.center],
        target=_sleeve_target(sleeve),
        rule="各スリーブに P-N-{数字} 形式の番号が振られていること",
        expected='例: "P-N-1", "P-N-23"',
        found=f'pn_number="{(sleeve.pn_number or "").strip() or "-"}"',
        fix_hint="スリーブに P-N 引出線と番号を追記する",
    )]


# ---------------------------------------------------------------------------
# #6 check_lower_wall
# ---------------------------------------------------------------------------

def check_lower_wall(
    sleeve: Sleeve,
    lower_walls: list[WallLine],
    wall_thickness: dict[str, float] | None = None,
) -> list[CheckResult]:
    """
    Check #6: sleeve must not overlap the wall below it.

    For RC walls: threshold = sleeve.diameter / 2
    For other walls: threshold = sleeve.diameter / 2 + wall_thickness[wall_type] / 2
    """
    if wall_thickness is None:
        wall_thickness = _DEFAULT_WALL_THICKNESS

    results: list[CheckResult] = []

    for wall in lower_walls:
        # Prefer .material; fall back to legacy .wall_type for paths that
        # still construct WallLine without setting material.
        mat = getattr(wall, "material", None) or wall.wall_type

        # Determine threshold
        if mat in ("RC", "RC壁", "仕上"):
            # Surface lines (RC structure / wall finish) — sleeve edge to surface
            threshold = sleeve.diameter / 2.0
        else:
            tk = mat if mat in wall_thickness else "不明"
            thickness = wall_thickness.get(tk, wall_thickness.get("不明", 200))
            threshold = sleeve.diameter / 2.0 + thickness / 2.0

        dist = point_to_segment_distance(sleeve.center, wall.start, wall.end)
        if dist < threshold:
            results.append(CheckResult(
                check_id=6,
                check_name="下階壁干渉",
                severity="NG",
                sleeve=sleeve,
                message=f"下階壁（{mat}）との距離 {dist:.1f}mm < しきい値 {threshold:.1f}mm",
                related_coords=[sleeve.center, wall.start, wall.end],
                target=_sleeve_target(sleeve),
                rule=f"スリーブ中心と下階壁（{mat}）中心線の距離 ≥ スリーブ半径 + 壁厚/2",
                expected=f"距離 ≥ {threshold:.1f}mm",
                found=f"距離 {dist:.1f}mm（{wall.layer or '下階壁'}）",
                fix_hint="スリーブ位置を下階壁から離すか、下階の壁配置を確認する",
            ))

    if not results:
        results.append(CheckResult(
            check_id=6,
            check_name="下階壁干渉",
            severity="OK",
            sleeve=sleeve,
            message="下階壁との干渉なし",
        ))

    return results


# ---------------------------------------------------------------------------
# #7 check_step_slab
# ---------------------------------------------------------------------------

def check_step_slab(
    sleeve: Sleeve,
    step_lines: list[StepLine],
    recess_polygons: list[RecessPolygon] | None = None,
) -> list[CheckResult]:
    """Check #7: sleeve must not overlap slab step lines or sit inside a floor recess.

    Two independent geometric conditions produce NG under this check:
    - スリーブ端が段差線に重なる (point-to-segment distance ≤ radius)
    - スリーブ芯が床ヌスミ内にある (point-in-polygon)
    """
    results: list[CheckResult] = []
    radius = sleeve.diameter / 2.0

    for step in step_lines:
        dist = point_to_segment_distance(sleeve.center, step.start, step.end)
        edge_dist = dist - radius
        if edge_dist <= 0:
            results.append(CheckResult(
                check_id=7,
                check_name="段差近接",
                severity="NG",
                sleeve=sleeve,
                message=f"スリーブ端が段差線に重なっている（端から段差線まで {edge_dist:.1f}mm）",
                related_coords=[sleeve.center, step.start, step.end],
                target=_sleeve_target(sleeve),
                rule="スリーブ円が段差線に重ならないこと",
                expected="スリーブ端から段差線まで > 0mm",
                found=f"端から段差線まで {edge_dist:.1f}mm（{step.layer or 'スラブ段差'}）",
                fix_hint="スリーブを段差線から離す",
            ))

    if recess_polygons:
        for recess in recess_polygons:
            if point_in_polygon(sleeve.center, recess.vertices):
                results.append(CheckResult(
                    check_id=7,
                    check_name="段差近接",
                    severity="NG",
                    sleeve=sleeve,
                    message="床ヌスミ内にスリーブがあり、残コンクリート厚不足のおそれ",
                    related_coords=[sleeve.center, *recess.vertices],
                    target=_sleeve_target(sleeve),
                    rule="スリーブ中心が床ヌスミ（凹み）ポリゴン内にないこと",
                    expected="ヌスミ外",
                    found=f"ヌスミ内（{recess.layer or '床ヌスミ'}）",
                    fix_hint="スリーブをヌスミ領域外へ移動する。残コンクリート厚を再確認する",
                ))

    if not results:
        results.append(CheckResult(
            check_id=7,
            check_name="段差近接",
            severity="OK",
            sleeve=sleeve,
            message="段差線・床ヌスミとの干渉なし",
        ))

    return results


# ---------------------------------------------------------------------------
# #10 check_step_dim
# ---------------------------------------------------------------------------

def check_step_dim(
    sleeve: Sleeve,
    dims: list[DimLine],
    step_lines: list[StepLine],
    sleeve_tolerance: float = 50.0,
    step_tolerance: float = 5.0,
) -> list[CheckResult]:
    """Check #10: sleeve-related dims must not originate from a step line.

    For each dimension whose defpoint2 or defpoint3 is near the sleeve
    centre, check whether the *other* (reference-side) defpoint sits on
    a step line.  If so, the dimension is measuring FROM a step/recess
    rather than from a reliable reference — flag as NG.
    """
    if not step_lines:
        return [CheckResult(
            check_id=10,
            check_name="段差基準寸法",
            severity="OK",
            sleeve=sleeve,
            message="段差線なし（スキップ）",
        )]

    step_segs = [(s.start, s.end) for s in step_lines]
    results: list[CheckResult] = []

    for dim in dims:
        # Find which defpoint(s) match the sleeve centre
        dp2_match = points_match(dim.defpoint2, sleeve.center, sleeve_tolerance)
        dp3_match = points_match(dim.defpoint3, sleeve.center, sleeve_tolerance)

        if not dp2_match and not dp3_match:
            continue

        # Check the reference-side (non-sleeve) defpoint against step lines
        ref_pts = []
        if dp2_match:
            ref_pts.append(dim.defpoint3)  # dp2 is sleeve-side → dp3 is reference
        if dp3_match:
            ref_pts.append(dim.defpoint2)  # dp3 is sleeve-side → dp2 is reference

        for pt in ref_pts:
            if point_on_any_segment(pt, step_segs, step_tolerance):
                results.append(CheckResult(
                    check_id=10,
                    check_name="段差基準寸法",
                    severity="NG",
                    sleeve=sleeve,
                    message=f"寸法の参照点が段差線上: {pt}",
                    related_coords=[pt, sleeve.center],
                    target=_sleeve_target(sleeve),
                    rule="寸法チェーンの参照点は段差線・ヌスミ線上にあってはならない（段差はずれる可能性があり基準として不適切）",
                    expected="寸法参照点は通り芯または躯体線上",
                    found=f"参照点 ({pt[0]:.0f}, {pt[1]:.0f}) が段差線上",
                    fix_hint="寸法の基準を通り芯側に変更する",
                ))

    if not results:
        results.append(CheckResult(
            check_id=10,
            check_name="段差基準寸法",
            severity="OK",
            sleeve=sleeve,
            message="段差線上の寸法基点なし",
        ))

    return results


# ---------------------------------------------------------------------------
# #11 check_sleeve_center_dim
# ---------------------------------------------------------------------------

def check_sleeve_center_dim(
    sleeves: list[Sleeve],
    x_resolved: dict[str, bool] | None,
    y_resolved: dict[str, bool] | None,
) -> list[CheckResult]:
    """Check #11: sleeve dims must trace back to a grid line, not only to other sleeves."""
    results: list[CheckResult] = []
    for s in sleeves:
        x_ok = x_resolved[s.id] if x_resolved is not None else True
        y_ok = y_resolved[s.id] if y_resolved is not None else True

        if x_ok and y_ok:
            results.append(CheckResult(
                check_id=11,
                check_name="スリーブ芯寸法",
                severity="OK",
                sleeve=s,
                message="寸法チェーンが通り芯に帰着",
            ))
        else:
            missing = []
            if not x_ok:
                missing.append("X")
            if not y_ok:
                missing.append("Y")
            results.append(CheckResult(
                check_id=11,
                check_name="スリーブ芯寸法",
                severity="NG",
                sleeve=s,
                message=f"{'・'.join(missing)}方向の寸法が通り芯に帰着しない（スリーブ芯のみ）",
                related_coords=[s.center],
                target=_sleeve_target(s),
                rule="スリーブ芯から発する寸法チェーンが、他スリーブ経由を含め、最終的に通り芯まで到達すること",
                expected="X方向・Y方向とも通り芯まで到達",
                found=f"{'・'.join(missing)}方向: 通り芯に到達せずスリーブ間のみで完結",
                fix_hint=f"{'・'.join(missing)}方向に通り芯と結ぶ寸法を追加（直接 or 既に通り芯に接続済みの別スリーブ経由）",
            ))
    return results


# ---------------------------------------------------------------------------
# #12 check_column_wall_dim
# ---------------------------------------------------------------------------

def check_column_wall_dim(
    sleeve: Sleeve,
    dims: list[DimLine],
    column_lines: list[ColumnLine],
    sleeve_tolerance: float = 50.0,
    col_tolerance: float = 5.0,
) -> list[CheckResult]:
    """Check #12: sleeve offset dims must not reference a column/wall-finish line.

    For each dimension whose defpoint2 or defpoint3 is near the sleeve
    centre, check whether the *other* (reference-side) defpoint sits on
    a column or wall-finish line.  If so, the dimension is measuring
    FROM a column/wall face rather than from a grid line — flag as NG.
    """
    if not column_lines:
        return [CheckResult(
            check_id=12,
            check_name="柱・壁仕上寸法",
            severity="OK",
            sleeve=sleeve,
            message="柱・壁仕上線なし（スキップ）",
        )]

    segments = [(c.start, c.end) for c in column_lines]
    results: list[CheckResult] = []

    for dim in dims:
        dp2_match = points_match(dim.defpoint2, sleeve.center, sleeve_tolerance)
        dp3_match = points_match(dim.defpoint3, sleeve.center, sleeve_tolerance)

        if not dp2_match and not dp3_match:
            continue

        ref_pts = []
        if dp2_match:
            ref_pts.append(dim.defpoint3)
        if dp3_match:
            ref_pts.append(dim.defpoint2)

        for pt in ref_pts:
            if point_on_any_segment(pt, segments, col_tolerance):
                results.append(CheckResult(
                    check_id=12,
                    check_name="柱・壁仕上寸法",
                    severity="NG",
                    sleeve=sleeve,
                    message=f"寸法の参照点が柱・壁仕上線上: {pt}",
                    related_coords=[pt, sleeve.center],
                    target=_sleeve_target(sleeve),
                    rule="スリーブ位置寸法の参照点は柱外周線・壁仕上線上にあってはならない（通り芯基準が望ましい）",
                    expected="寸法参照点は通り芯または躯体線",
                    found=f"参照点 ({pt[0]:.0f}, {pt[1]:.0f}) が柱外周線または壁仕上線上",
                    fix_hint="寸法の基準を通り芯または躯体線に変更する",
                ))

    if not results:
        results.append(CheckResult(
            check_id=12,
            check_name="柱・壁仕上寸法",
            severity="OK",
            sleeve=sleeve,
            message="柱・壁仕上線上の寸法基点なし",
        ))

    return results


# ---------------------------------------------------------------------------
# #4 check_dim_sum
# ---------------------------------------------------------------------------

def check_dim_sum(
    dims: list[DimLine],
    grids: list[GridLine],
) -> list[CheckResult]:
    """
    Check #4: Detect continuous dimension chains and verify their sum
    equals the grid-to-grid span.

    Algorithm:
    1. Classify dims as horizontal or vertical by comparing defpoint2/3 spread.
    2. Group by dimension line position (defpoint1 Y for horiz, X for vert).
    3. Within each group, find continuous chains (endpoint gap < 50mm).
    4. If chain endpoints are near grid lines (< 200mm), compare
       chain sum vs grid span.
    """
    results: list[CheckResult] = []

    v_grids = sorted([g for g in grids if g.direction == "V"], key=lambda g: g.position)
    h_grids = sorted([g for g in grids if g.direction == "H"], key=lambda g: g.position)

    CHAIN_GAP = 50.0      # max gap between adjacent dims in a chain
    GRID_SNAP_STRICT = 5.0   # chain endpoint snapped to grid (Case A)
    GRID_SNAP_LOOSE = 300.0  # chain near grid but not snapped (Case B)
    SUM_TOLERANCE = 5.0   # mm
    GROUP_BIN = 50.0      # bin size for grouping dim line positions

    def _find_nearest_grid(
        pos: float, grid_list: list[GridLine]
    ) -> tuple[GridLine | None, float]:
        if not grid_list:
            return None, float("inf")
        best = min(grid_list, key=lambda g: abs(g.position - pos))
        return best, abs(best.position - pos)

    def _split_chain_at_grids(
        chain: list[DimLine],
        get_start: callable,
        get_end: callable,
        grid_list: list[GridLine],
    ) -> list[list[DimLine]]:
        """Split a continuous chain at grid line positions."""
        if not grid_list or len(chain) < 2:
            return [chain]

        grid_positions = sorted(g.position for g in grid_list)
        sub_chains: list[list[DimLine]] = []
        current: list[DimLine] = [chain[0]]

        for i in range(1, len(chain)):
            prev_end = get_end(chain[i - 1])
            curr_start = get_start(chain[i])
            mid = (prev_end + curr_start) / 2.0

            # Check if a grid line falls between prev dim end and curr dim start
            crossed = any(
                abs(gp - mid) < GRID_SNAP_LOOSE
                and min(prev_end, curr_start) - CHAIN_GAP < gp < max(prev_end, curr_start) + CHAIN_GAP
                for gp in grid_positions
            )

            if crossed:
                if len(current) >= 2:
                    sub_chains.append(current)
                current = [chain[i]]
            else:
                current.append(chain[i])

        if len(current) >= 2:
            sub_chains.append(current)

        return sub_chains

    def _detect_chains(
        dim_group: list[DimLine],
        get_start: callable,
        get_end: callable,
        grid_list: list[GridLine],
        axis: str,
    ) -> None:
        if len(dim_group) < 2:
            return

        dim_group.sort(key=lambda d: get_start(d))

        # Build continuous chains (gap < 50mm)
        raw_chains: list[list[DimLine]] = []
        chain: list[DimLine] = [dim_group[0]]

        for i in range(1, len(dim_group)):
            prev_end = get_end(chain[-1])
            curr_start = get_start(dim_group[i])
            if abs(curr_start - prev_end) < CHAIN_GAP:
                chain.append(dim_group[i])
            else:
                if len(chain) >= 2:
                    raw_chains.append(chain)
                chain = [dim_group[i]]

        if len(chain) >= 2:
            raw_chains.append(chain)

        # Split chains at grid line positions
        chains: list[list[DimLine]] = []
        for raw in raw_chains:
            chains.extend(_split_chain_at_grids(raw, get_start, get_end, grid_list))

        # Check each chain against grid span
        for ch in chains:
            chain_start = get_start(ch[0])
            chain_end = get_end(ch[-1])
            chain_sum = sum(d.measurement for d in ch)

            g_start, d_start = _find_nearest_grid(chain_start, grid_list)
            g_end, d_end = _find_nearest_grid(chain_end, grid_list)

            if (g_start is None or g_end is None
                    or g_start.axis_label == g_end.axis_label):
                continue

            # Skip chains too far from any grid
            if d_start > GRID_SNAP_LOOSE or d_end > GRID_SNAP_LOOSE:
                continue

            grid_span = abs(g_end.position - g_start.position)

            if axis == "X":
                coords = [(chain_start, ch[0].defpoint1[1]),
                           (chain_end, ch[-1].defpoint1[1])]
            else:
                coords = [(ch[0].defpoint1[0], chain_start),
                           (ch[-1].defpoint1[0], chain_end)]

            dim_vals = " + ".join(f"{d.measurement:.0f}" for d in ch)

            # Case B: chain near grid but not snapped
            if d_start > GRID_SNAP_STRICT or d_end > GRID_SNAP_STRICT:
                results.append(CheckResult(
                    check_id=4,
                    check_name="寸法合計",
                    severity="WARNING",
                    sleeve=None,
                    message=(
                        f"通り芯 {g_start.axis_label}–{g_end.axis_label} ({axis}) | "
                        f"寸法チェーンが通り芯にスナップしていない "
                        f"(始点差: {d_start:.0f}mm, 終点差: {d_end:.0f}mm)"
                    ),
                    related_coords=coords,
                    target=f"通り芯 {g_start.axis_label}–{g_end.axis_label} ({axis}方向) 寸法チェーン",
                    rule=f"寸法チェーンの始点・終点が通り芯にスナップしている（許容 {GRID_SNAP_STRICT:.0f}mm 以内）",
                    expected=f"始点差 ≤ {GRID_SNAP_STRICT:.0f}mm, 終点差 ≤ {GRID_SNAP_STRICT:.0f}mm",
                    found=f"始点差: {d_start:.0f}mm, 終点差: {d_end:.0f}mm",
                    fix_hint="寸法チェーンの端点を通り芯にスナップし直す",
                ))
                continue

            # Case A: properly snapped — compare sum vs grid span
            if abs(chain_sum - grid_span) <= SUM_TOLERANCE:
                results.append(CheckResult(
                    check_id=4,
                    check_name="寸法合計",
                    severity="OK",
                    sleeve=None,
                    message=(
                        f"通り芯 {g_start.axis_label}–{g_end.axis_label} ({axis}) | "
                        f"寸法: {dim_vals} | "
                        f"合計: {chain_sum:.0f} | "
                        f"通り芯間: {grid_span:.0f}"
                    ),
                    related_coords=coords,
                ))
            else:
                diff = chain_sum - grid_span
                results.append(CheckResult(
                    check_id=4,
                    check_name="寸法合計",
                    severity="NG",
                    sleeve=None,
                    message=(
                        f"通り芯 {g_start.axis_label}–{g_end.axis_label} ({axis}) | "
                        f"寸法: {dim_vals} | "
                        f"合計: {chain_sum:.0f} | "
                        f"通り芯間: {grid_span:.0f} | "
                        f"差: {diff:+.0f}mm"
                    ),
                    related_coords=coords,
                    target=f"通り芯 {g_start.axis_label}–{g_end.axis_label} ({axis}方向) 寸法チェーン",
                    rule=f"寸法チェーンの合計が通り芯間距離と一致する（許容 {SUM_TOLERANCE:.0f}mm 以内）",
                    expected=f"合計 = 通り芯間 {grid_span:.0f}mm (±{SUM_TOLERANCE:.0f}mm)",
                    found=f"寸法: {dim_vals} → 合計 {chain_sum:.0f}mm（差 {diff:+.0f}mm）",
                    fix_hint="寸法値を見直して合計を通り芯間距離と一致させる",
                ))

    def _is_horizontal(d: DimLine) -> bool | None:
        """Determine if dim is horizontal, vertical, or unknown."""
        if d.angle is not None:
            norm = d.angle % 360
            if norm < 45 or norm > 315 or (135 < norm < 225):
                return True
            return False
        dx = abs(d.defpoint2[0] - d.defpoint3[0])
        dy = abs(d.defpoint2[1] - d.defpoint3[1])
        if dx == 0 and dy == 0:
            return None
        return dx > dy

    # --- Horizontal dims (measure X span, grouped by dim-line Y position) ---
    h_dims: dict[float, list[DimLine]] = defaultdict(list)
    for d in dims:
        if d.measurement <= 10:
            continue
        if _is_horizontal(d) is True:
            y_bin = round(d.defpoint1[1] / GROUP_BIN) * GROUP_BIN
            h_dims[y_bin].append(d)

    for y_bin, group in h_dims.items():
        _detect_chains(
            group,
            get_start=lambda d: min(d.defpoint2[0], d.defpoint3[0]),
            get_end=lambda d: max(d.defpoint2[0], d.defpoint3[0]),
            grid_list=v_grids,
            axis="X",
        )

    # --- Vertical dims (measure Y span, grouped by dim-line X position) ---
    v_dims: dict[float, list[DimLine]] = defaultdict(list)
    for d in dims:
        if d.measurement <= 10:
            continue
        if _is_horizontal(d) is False:
            x_bin = round(d.defpoint1[0] / GROUP_BIN) * GROUP_BIN
            v_dims[x_bin].append(d)

    for x_bin, group in v_dims.items():
        _detect_chains(
            group,
            get_start=lambda d: min(d.defpoint2[1], d.defpoint3[1]),
            get_end=lambda d: max(d.defpoint2[1], d.defpoint3[1]),
            grid_list=h_grids,
            axis="Y",
        )

    if not results:
        results.append(CheckResult(
            check_id=4,
            check_name="寸法合計",
            severity="OK",
            sleeve=None,
            message="連続寸法チェーンなし",
        ))

    return results


# ---------------------------------------------------------------------------
# #9 check_position_determinacy
# ---------------------------------------------------------------------------


def check_position_determinacy(
    sleeves: list[Sleeve],
    dims: list[DimLine],
    grids: list[GridLine],
    sleeve_margin: float = 30.0,
    grid_tolerance: float = 0.0,
) -> tuple[list[CheckResult], dict[str, bool] | None, dict[str, bool] | None]:
    """Check #9: each sleeve's position must be determinable from dimensions.

    For each axis (X via V-grids, Y via H-grids), a sleeve is "resolved" if:
      - a dimension directly links it to a grid line, OR
      - a dimension links it to another sleeve that is already resolved.

    Algorithm (per axis):
    1. Build edges: dim connects sleeve↔grid or sleeve↔sleeve.
    2. Mark sleeves with a direct grid dim as resolved.
    3. Propagate through sleeve↔sleeve dims (BFS).
    4. Unresolved sleeves → NG.

    The sleeve match tolerance is ``sleeve.diameter / 2 + sleeve_margin``
    because dimension extension lines often originate from the sleeve edge
    rather than the centre.
    """
    if not sleeves:
        return [], {}, {}

    v_grid_positions = [g.position for g in grids if g.direction == "V"]
    h_grid_positions = [g.position for g in grids if g.direction == "H"]

    def _near_any_grid(val: float, grid_positions: list[float]) -> bool:
        return any(abs(val - gp) <= grid_tolerance for gp in grid_positions)

    def _match_sleeves(pt: tuple[float, float], axis: str) -> list[Sleeve]:
        """Return *all* sleeves whose coordinate on *axis* matches *pt*.

        For a vertical dim (axis="Y"), any sleeve whose Y is within
        ``diameter/2 + margin`` of ``pt[1]`` is a match — regardless of X.
        This correctly handles drawings where the dim extension-line origin
        is horizontally offset from the sleeve centre.
        """
        idx = 0 if axis == "X" else 1
        matched: list[Sleeve] = []
        for s in sleeves:
            tol = s.diameter / 2.0 + sleeve_margin
            if abs(pt[idx] - s.center[idx]) <= tol:
                matched.append(s)
        return matched

    def _is_horizontal(d: DimLine) -> bool | None:
        if d.angle is not None:
            norm = d.angle % 360
            if norm < 45 or norm > 315 or (135 < norm < 225):
                return True
            return False
        dx = abs(d.defpoint2[0] - d.defpoint3[0])
        dy = abs(d.defpoint2[1] - d.defpoint3[1])
        if dx == 0 and dy == 0:
            return None
        return dx > dy

    def _resolve_axis(
        axis: str,
        grid_positions: list[float],
    ) -> dict[str, bool]:
        """Return {sleeve.id: resolved} for one axis.

        Resolution propagates through:
        1. Same-axis dims (horizontal dims for X, vertical dims for Y):
           sleeve↔grid → resolved; sleeve↔sleeve → BFS edge.
        2. Cross-axis dims (vertical dims for X, horizontal dims for Y):
           sleeve↔sleeve edges only.  A vertical dim chain proves the
           connected sleeves share the same X coordinate, so if one has
           X resolved the others do too (and vice-versa for Y).
        """
        resolved: dict[str, bool] = {s.id: False for s in sleeves}
        sleeve_edges: dict[str, list[str]] = {s.id: [] for s in sleeves}

        is_x = axis == "X"
        cross_axis = "Y" if is_x else "X"

        for d in dims:
            h = _is_horizontal(d)
            dp2, dp3 = d.defpoint2, d.defpoint3

            same_axis = (is_x and h is True) or (not is_x and h is False)
            cross = (is_x and h is False) or (not is_x and h is True)

            if same_axis:
                # Same-axis: sleeve↔grid resolves, sleeve↔sleeve builds edge
                ss2 = _match_sleeves(dp2, axis)
                ss3 = _match_sleeves(dp3, axis)

                g2 = _near_any_grid(dp2[0] if is_x else dp2[1], grid_positions)
                g3 = _near_any_grid(dp3[0] if is_x else dp3[1], grid_positions)

                if g3:
                    for s in ss2:
                        resolved[s.id] = True
                if g2:
                    for s in ss3:
                        resolved[s.id] = True

                # sleeve ↔ sleeve edges between all pairs
                for s2 in ss2:
                    for s3 in ss3:
                        if s2.id != s3.id:
                            sleeve_edges[s2.id].append(s3.id)
                            sleeve_edges[s3.id].append(s2.id)

            elif cross:
                # Cross-axis: a horizontal dim connects sleeves that share Y;
                # a vertical dim connects sleeves that share X.
                # Match using the dim's own measurement axis (horizontal→X,
                # vertical→Y) so that the perpendicular offset doesn't break
                # the match.
                dim_axis = "X" if h else "Y"
                ss2 = _match_sleeves(dp2, dim_axis)
                ss3 = _match_sleeves(dp3, dim_axis)

                for s2 in ss2:
                    for s3 in ss3:
                        if s2.id != s3.id:
                            sleeve_edges[s2.id].append(s3.id)
                            sleeve_edges[s3.id].append(s2.id)

        # BFS propagation
        queue = [sid for sid, r in resolved.items() if r]
        visited = set(queue)
        while queue:
            current = queue.pop(0)
            for neighbor in sleeve_edges[current]:
                if neighbor not in visited:
                    visited.add(neighbor)
                    resolved[neighbor] = True
                    queue.append(neighbor)

        return resolved

    x_resolved = _resolve_axis("X", v_grid_positions) if v_grid_positions else None
    y_resolved = _resolve_axis("Y", h_grid_positions) if h_grid_positions else None

    results: list[CheckResult] = []

    for s in sleeves:
        x_ok = x_resolved[s.id] if x_resolved is not None else True
        y_ok = y_resolved[s.id] if y_resolved is not None else True

        if x_ok and y_ok:
            results.append(CheckResult(
                check_id=9,
                check_name="位置特定寸法",
                severity="OK",
                sleeve=s,
                message="X・Y方向とも位置特定可能",
            ))
        else:
            missing = []
            if not x_ok:
                missing.append("X")
            if not y_ok:
                missing.append("Y")
            results.append(CheckResult(
                check_id=9,
                check_name="位置特定寸法",
                severity="NG",
                sleeve=s,
                message=f"{'・'.join(missing)}方向の位置が寸法から特定できません",
                related_coords=[s.center],
                target=_sleeve_target(s),
                rule="スリーブ位置がX方向・Y方向ともに寸法チェーンから特定できること（通り芯への帰着経路が存在）",
                expected="X方向・Y方向とも位置特定可能",
                found=f"{'・'.join(missing)}方向: 位置特定不可（通り芯へのチェーン経路なし）",
                fix_hint=f"{'・'.join(missing)}方向にスリーブ芯を参照する寸法を追加する",
            ))

    return results, x_resolved, y_resolved


# ---------------------------------------------------------------------------
# #13 check_dim_notation
# ---------------------------------------------------------------------------

_RE_MM_SUFFIX = re.compile(r"\d+mm", re.IGNORECASE)
_RE_COMMA_SEP = re.compile(r"\d+,\d+")
_RE_DECIMAL = re.compile(r"\d+\.\d+")


def check_dim_notation(dims: list[DimLine]) -> list[CheckResult]:
    """
    Check #13: All dimension text overrides should follow a consistent notation.

    Patterns detected:
    - mm_suffix: "150mm"
    - comma_sep: "1,500"
    - decimal:   "1.5"
    - plain:     plain integer, no special format

    If multiple patterns are present → WARNING.
    """
    overrides = [d.text_override for d in dims if d.text_override is not None]

    if not overrides:
        return [CheckResult(
            check_id=13,
            check_name="寸法表記統一",
            severity="OK",
            sleeve=None,
            message="テキストオーバーライドなし",
        )]

    patterns_found: set[str] = set()

    for txt in overrides:
        if _RE_MM_SUFFIX.search(txt):
            patterns_found.add("mm_suffix")
        elif _RE_COMMA_SEP.search(txt):
            patterns_found.add("comma_sep")
        elif _RE_DECIMAL.search(txt):
            patterns_found.add("decimal")
        else:
            patterns_found.add("plain")

    if len(patterns_found) > 1:
        return [CheckResult(
            check_id=13,
            check_name="寸法表記統一",
            severity="NG",
            sleeve=None,
            message=f"寸法表記が混在しています: {sorted(patterns_found)}",
            target="図面全体の寸法テキスト",
            rule="寸法表記フォーマット（カンマ区切り / 単位 / 小数桁）は図面内で統一されていること",
            expected="単一の表記パターン",
            found=f"混在パターン: {sorted(patterns_found)}",
            fix_hint="寸法表記を一つのパターンに統一する（例: 全て カンマ区切りなし・整数 mm）",
        )]

    return [CheckResult(
        check_id=13,
        check_name="寸法表記統一",
        severity="OK",
        sleeve=None,
        message=f"寸法表記統一: {sorted(patterns_found)}",
    )]


# ---------------------------------------------------------------------------
# Integration: run_all_checks
# ---------------------------------------------------------------------------

def run_all_checks(
    floor_2f: FloorData,
    floor_1f: FloorData | None = None,
    wall_thickness: dict[str, float] | None = None,
) -> list[CheckResult]:
    """
    Run all checks against the provided floor data.

    Parameters
    ----------
    floor_2f:
        Primary floor (2F sleeve drawing) — the floor being checked.
    floor_1f:
        Lower floor (1F) providing wall data for check #6.
    wall_thickness:
        Dict mapping wall type → thickness in mm.  Defaults to
        ``_DEFAULT_WALL_THICKNESS``.

    Returns
    -------
    list[CheckResult]
    """
    if wall_thickness is None:
        wall_thickness = dict(_DEFAULT_WALL_THICKNESS)

    results: list[CheckResult] = []

    lower_walls = floor_1f.wall_lines if floor_1f is not None else []

    # --- Per-sleeve text/geometry checks ---
    for sleeve in floor_2f.sleeves:
        results.extend(check_discipline(sleeve))          # #2
        results.extend(check_diameter_label(sleeve))       # #3
        results.extend(check_gradient(sleeve, floor_2f.pn_labels, floor_2f.slab_zones, floor_2f.slab_labels, floor_2f.water_gradients))  # #5
        # #8 is now a global check (check_base_level), not per-sleeve
        results.extend(check_sleeve_number(sleeve))        # #14
        results.extend(check_step_slab(sleeve, floor_2f.step_lines, floor_2f.recess_polygons))  # #7

        if lower_walls and (sleeve.orientation or "").lower() != "horizontal":
            # Check #6 is about vertical (slab-penetration) sleeves not sitting
            # over a lower-floor wall. Horizontal sleeves *are* wall penetrations
            # by definition, so the interference concept doesn't apply.
            results.extend(check_lower_wall(sleeve, lower_walls, wall_thickness))  # #6

    # --- Global level check ---
    results.extend(check_base_level(floor_2f.sleeves))  # #8 — horizontal sleeves only

    # --- Global dim checks ---
    results.extend(check_dim_sum(floor_2f.dim_lines, floor_2f.grid_lines))   # #4
    results.extend(check_dim_notation(floor_2f.dim_lines))                    # #13

    # --- Per-sleeve dim checks ---
    for sleeve in floor_2f.sleeves:
        results.extend(check_step_dim(sleeve, floor_2f.dim_lines, floor_2f.step_lines))  # #10

    # --- Per-sleeve dim checks (column/wall) ---
    for sleeve in floor_2f.sleeves:
        results.extend(check_column_wall_dim(sleeve, floor_2f.dim_lines, floor_2f.column_lines))  # #12

    # --- Position determinacy (graph-based) ---
    det_results, x_resolved, y_resolved = check_position_determinacy(
        floor_2f.sleeves, floor_2f.dim_lines, floor_2f.grid_lines,
    )
    results.extend(det_results)  # #9
    results.extend(check_sleeve_center_dim(
        floor_2f.sleeves, x_resolved, y_resolved,
    ))  # #11

    return results
