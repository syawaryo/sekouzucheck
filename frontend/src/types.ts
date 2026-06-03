export interface Sleeve {
  id: string;
  center: [number, number];
  diameter: number;
  label_text: string | null;
  diameter_text: string | null;
  fl_text: string | null;
  pn_number: string | null;
  layer: string;
  discipline: string;
  shape?: "round" | "rect";
  width?: number;
  height?: number;
  color?: number | null;  // ACI color index (1 = red)
  sleeve_type?: "duct" | "pipe" | "cable" | "";
  orientation?: "vertical" | "horizontal" | "";
}

export interface GridLine {
  axis_label: string;
  direction: "H" | "V";
  position: number;
}

export interface WallLine {
  start: [number, number];
  end: [number, number];
  layer: string;
  wall_type: string;                // legacy — keep until backend drops it
  material?: string;                // RC/ALC/LGS/PCa/CB/木軸/パネル/仕上/耐火被覆/壁心/不明
  is_exterior?: boolean;            // building perimeter? (geometric)
}

export interface StepLine {
  start: [number, number];
  end: [number, number];
  layer: string;
  side_a_fl?: number | null;
  side_b_fl?: number | null;
  fl_status?: "real" | "spurious" | "unknown";
}

export interface RecessPolygon {
  vertices: [number, number][];
  layer: string;
}

export interface ColumnLine {
  start: [number, number];
  end: [number, number];
  layer: string;
}

export interface BeamLine {
  start: [number, number];
  end: [number, number];
  layer: string;
  beam_type: string;  // "RC梁" / "S梁" / "付帯梁" / "不明"
}

export interface DimLine {
  layer: string;
  measurement: number;
  defpoint1: [number, number];  // dimension line position
  defpoint2: [number, number];  // 1st extension line origin
  defpoint3: [number, number];  // 2nd extension line origin
  angle: number | null;         // rotation angle (degrees), null = auto
  text_override: string | null;
}

export interface SlabZone {
  x: number;
  y: number;
  fl_text: string;
  fl_value: number;
}

export interface SlabLabel {
  x: number;
  y: number;
  slab_no: string;      // "S16", "DS16"
  level: string;        // "-60" or "-545～-600"
  thickness: string;    // "165"
}

export interface PnLabel {
  x: number;
  y: number;
  text: string;  // "P-N-1"
  number: number;
  arrow_verts: [number, number][];  // triangle vertices, empty if no arrow
}

export interface SlabOutline {
  start: [number, number];
  end: [number, number];
}

export interface RawLine {
  points: [number, number][];
  layer: string;
  color: number | null;
}

export interface RawText {
  x: number;
  y: number;
  text: string;
  layer: string;
  height: number;
  rotation: number;
  color: number | null;
}

export interface RoomLabel {
  x: number;
  y: number;
  text: string;
  height: number;
  rotation: number;
}

export interface FloorData {
  sleeves: Sleeve[];
  grid_lines: GridLine[];
  wall_lines: WallLine[];
  step_lines: StepLine[];
  column_lines: ColumnLine[];
  beam_lines?: BeamLine[];
  dim_lines: DimLine[];
  slab_zones: SlabZone[];
  slab_outlines: SlabOutline[];
  recess_polygons: RecessPolygon[];
  slab_labels: SlabLabel[];
  pn_labels: PnLabel[];
  raw_lines?: RawLine[];
  raw_texts?: RawText[];
  room_labels?: RoomLabel[];
  slab_level: string | null;
}

export interface CheckResult {
  check_id: number;
  check_name: string;
  severity: "NG" | "WARNING" | "OK";
  sleeve_id: string | null;
  message: string;
  related_coords: [number, number][];
  // Structured explanation (optional — empty string if not populated).
  target?: string;
  rule?: string;
  expected?: string;
  found?: string;
  fix_hint?: string;
}

export interface CheckResponse {
  results: CheckResult[];
  summary: { ng: number; warning: number; ok: number };
}

export interface CheckDef {
  id: number;
  name: string;
  category: string;
  description: string;
  source: "builtin" | "generated";
  enabled: boolean;
  order: number;
}
