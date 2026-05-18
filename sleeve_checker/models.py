from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class Sleeve:
    id: str
    center: tuple[float, float]
    diameter: float
    label_text: str | None = None
    diameter_text: str | None = None
    fl_text: str | None = None
    pn_number: str | None = None
    layer: str = ""
    discipline: str = ""
    shape: str = "round"       # "round" or "rect"
    width: float = 0.0         # rect: full width (mm); round: equals diameter
    height: float = 0.0        # rect: full height (mm); round: equals diameter
    color: int | None = None   # ACI color index (1 = red). None = unknown / BYLAYER.
    sleeve_type: str = ""      # "duct" / "pipe" / "cable" / "" (from equipment code)
    orientation: str = ""      # "vertical" (縦管) / "horizontal" (横管) / ""


@dataclass
class GridLine:
    axis_label: str
    direction: str  # "H" or "V"
    position: float


@dataclass
class DimLine:
    layer: str
    measurement: float
    defpoint1: tuple[float, float]   # defpoint (10): dimension line position
    defpoint2: tuple[float, float]   # defpoint2 (13): 1st extension line origin
    defpoint3: tuple[float, float] = (0.0, 0.0)  # defpoint3 (14): 2nd extension line origin
    angle: float | None = None       # rotation angle (group 50), None = auto
    text_override: str | None = None


@dataclass
class WallLine:
    start: tuple[float, float]
    end: tuple[float, float]
    layer: str = ""
    wall_type: str = "不明"   # legacy field — kept during migration; remove in Task 12
    material: str = "不明"    # RC / ALC / LGS / PCa / 木軸 / CB / パネル / 仕上 / 耐火被覆 / 壁心 / 不明
    is_exterior: bool = False # True when segment lies near slab outline (=外周壁)


@dataclass
class StepLine:
    start: tuple[float, float]
    end: tuple[float, float]
    layer: str = ""
    # FL classification (filled by sleeve_checker.regions).
    # "real" = both sides resolved to different FL, "spurious" = same FL on both sides,
    # "unknown" = at least one side could not be resolved.
    side_a_fl: int | None = None
    side_b_fl: int | None = None
    fl_status: str = "unknown"


@dataclass
class RecessPolygon:
    """A closed floor-recess outline (床ヌスミ)."""
    vertices: list[tuple[float, float]]
    layer: str = ""


@dataclass
class ColumnLine:
    start: tuple[float, float]
    end: tuple[float, float]
    layer: str = ""


@dataclass
class BeamLine:
    """A beam (梁) outline segment — RC梁 (F103) or 鉄骨梁 (F202)."""
    start: tuple[float, float]
    end: tuple[float, float]
    layer: str = ""
    beam_type: str = ""  # "RC梁" / "S梁" / "付帯梁" / "不明"


@dataclass
class SlabZone:
    x: float
    y: float
    fl_text: str  # e.g. "FL+40", "FL-360"
    fl_value: int  # numeric mm value, e.g. +40, -360


@dataclass
class PnLabel:
    x: float
    y: float
    text: str     # "P-N-1"
    number: int
    arrow_verts: list[tuple[float, float]] = field(default_factory=list)  # triangle vertices, empty if no arrow


@dataclass
class SlabLabel:
    x: float
    y: float
    slab_no: str       # "S16", "DS16"
    level: str         # "-60" or "-545～-600"
    thickness: str     # "165"


@dataclass
class SlabOutline:
    start: tuple[float, float]
    end: tuple[float, float]


@dataclass
class WaterGradient:
    x: float
    y: float
    direction: str = ""  # "→", "←", "↑", "↓" or empty


@dataclass
class RawLine:
    """Generic polyline extracted for "raw DXF passthrough" rendering.

    Used to surface every structural / annotation layer the typed
    extractors don't cover (room-name frames, beam outlines, revision
    clouds, etc.) so the UI can render the complete drawing.
    """
    points: list[tuple[float, float]] = field(default_factory=list)
    layer: str = ""
    color: int | None = None


@dataclass
class RawText:
    """Generic text extracted for "raw DXF passthrough" rendering."""
    x: float = 0.0
    y: float = 0.0
    text: str = ""
    layer: str = ""
    height: float = 0.0
    rotation: float = 0.0
    color: int | None = None


@dataclass
class RoomLabel:
    """A room-name text placed on A211_室名 — e.g. '店舗１', 'エントランス'."""
    x: float
    y: float
    text: str
    height: float = 0.0
    rotation: float = 0.0


@dataclass
class FloorData:
    sleeves: list[Sleeve] = field(default_factory=list)
    grid_lines: list[GridLine] = field(default_factory=list)
    dim_lines: list[DimLine] = field(default_factory=list)
    wall_lines: list[WallLine] = field(default_factory=list)
    step_lines: list[StepLine] = field(default_factory=list)
    column_lines: list[ColumnLine] = field(default_factory=list)
    beam_lines: list[BeamLine] = field(default_factory=list)
    slab_zones: list[SlabZone] = field(default_factory=list)
    slab_outlines: list[SlabOutline] = field(default_factory=list)
    recess_polygons: list[RecessPolygon] = field(default_factory=list)
    slab_labels: list[SlabLabel] = field(default_factory=list)
    pn_labels: list[PnLabel] = field(default_factory=list)
    water_gradients: list[WaterGradient] = field(default_factory=list)
    raw_lines: list[RawLine] = field(default_factory=list)
    raw_texts: list[RawText] = field(default_factory=list)
    room_labels: list[RoomLabel] = field(default_factory=list)
    slab_level: str | None = None
    has_base_level_def: bool = False


@dataclass
class CheckResult:
    check_id: int
    check_name: str
    severity: str  # "NG" / "WARNING" / "OK"
    sleeve: Sleeve | None = None
    message: str = ""
    related_coords: list[tuple[float, float]] = field(default_factory=list)
    # Structured explanation — populated for NG / WARNING cases so the UI can
    # render a 5-field card without having to re-parse `message`. Empty strings
    # mean "not applicable / not yet populated".
    target: str = ""     # 何を検査したか e.g. "スリーブ P-N-12 / [空調]F141_..."
    rule: str = ""       # 判定基準
    expected: str = ""   # 期待値
    found: str = ""      # 実検出
    fix_hint: str = ""   # 推奨対応
