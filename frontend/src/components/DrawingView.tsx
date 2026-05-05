import { useMemo, useState, useRef, useCallback, useEffect, memo } from "react";
import type { ReactElement } from "react";
import type { FloorData, Sleeve, CheckResult } from "../types";
import type { UniversalEntity } from "../api";

// Category toggles (1-to-1 with backend USEFUL_CATEGORIES, 23 entries).
// Plus a second band of "overlay" modes that are typed/computed
// visualisations (slabLevel = formatted slab labels, flZone = FL tiling,
// raw = raw DXF passthrough, lowerWall = lower-floor wall overlay).
type LayerKey =
  // categories
  | "grid"           // 通り芯
  | "outerWall"      // 外壁
  | "wall"           // 内壁
  | "firewall"       // 耐火壁・防火区画
  | "column"         // 柱・仕上線
  | "beam"           // 梁
  | "slabOutline"    // スラブ外形
  | "slabLabel"      // スラブラベル
  | "slabFL"         // スラブFL
  | "step"           // 段差線
  | "recess"         // 床ヌスミ
  | "flLabel"        // FL表記
  | "dim"            // 寸法線
  | "pn"             // P-N番号
  | "room"           // 部屋名
  | "waterGradient"  // 水勾配
  | "equipSanitary"  // 機器コード_衛生
  | "equipHvac"      // 機器コード_空調
  | "equipElectric"  // 機器コード_電気
  | "sleeve"         // スリーブ_* (4 sub via sleeveFilters)
  // overlays
  | "lowerWall"
  | "slabLevel"
  | "flZone"
  | "raw";
type DisciplineKey = "衛生" | "空調" | "電気" | "その他";

interface Props {
  floorData: FloorData;
  lowerFloorData: FloorData | null;
  // Optional: every DXF/IFC entity flat-listed, plus per-layer category
  // mapping. Used for "show every shape on this layer" rendering paths
  // that bypass the strict typed extractors in parser.py.
  universalEntities?: UniversalEntity[] | null;
  layerCategories?: Record<string, string> | null;
  results: CheckResult[];
  onSleeveHover: (sleeve: Sleeve | null) => void;
  onSleeveClick: (sleeve: Sleeve | null) => void;
  selectedSleeveId: string | null;
  layers: Record<LayerKey, boolean>;
  sleeveFilters: Record<DisciplineKey, boolean>;
  pdfOverlayUrl?: string | null;
  pdfOverlayOpacity?: number;
  navigateTarget?: [number, number] | null;
  onNavigated?: () => void;
  highlightCoords?: [number, number][];
  // Toolbar callbacks (moved out of the App header)
  onToggleLayer: (key: LayerKey) => void;
  onToggleSleeveFilter: (key: DisciplineKey) => void;
  sleeveCounts: Record<DisciplineKey, number>;
  showLowerWallToggle: boolean;
  lowerFloorLabel?: string;
  onPdfFilesSelected: (files: FileList | null) => void;
  onPdfClear: () => void;
  onPdfOpacityChange: (v: number) => void;
}

const SEVERITY_COLORS: Record<string, { stroke: string; fill: string }> = {
  NG: { stroke: "#dc2626", fill: "#fef2f2" },
  WARNING: { stroke: "#d97706", fill: "#fffbeb" },
  OK: { stroke: "#16a34a", fill: "#dcfce7" },
};

const FL_COLORS: Record<string, string> = {
  "FL-225": "#3b82f6",
  "FL-175": "#8b5cf6",
  "FL-750": "#ec4899",
  "FL-765": "#d946ef",
  "FL+40": "#f59e0b",
  "FL+0": "#84cc16",
  "FL-60": "#06b6d4",
};

// Palette for FL values not present in FL_COLORS — lets the FL legend
// distinguish site-specific levels without lying about the color.
const FL_FALLBACK_PALETTE = ["#10b981", "#6366f1", "#eab308", "#f97316", "#0ea5e9", "#a855f7", "#14b8a6", "#f43f5e"];
const FL_UNKNOWN_COLOR = "#9ca3af";


const INITIAL_VB = { x: -5000, y: -40000, w: 90000, h: 45000 };
const MIN_ZOOM_W = 5000;
const MAX_ZOOM_W = 500000;

type ViewBox = { x: number; y: number; w: number; h: number };
type DataBounds = { minX: number; maxX: number; minY: number; maxY: number };
type LayersState = Props["layers"];

interface RawStyle {
  stroke?: string;
  strokeWidth?: number;
  strokeOpacity?: number;
  strokeDasharray?: string;
  fill?: string;
  fillOpacity?: number;
}

/**
 * Render every universal entity whose layer is classified into the given
 * category. Used by drawing toggles whose semantic meaning is "show every
 * shape on this layer", so we paint the raw geometry — closed polygons
 * become filled paths, open polylines / lines become strokes.
 */
function renderRawEntitiesByCategory(
  entities: UniversalEntity[],
  layerCategories: Record<string, string>,
  category: string,
  style: RawStyle,
  keyPrefix: string,
): ReactElement[] {
  const out: ReactElement[] = [];
  for (let i = 0; i < entities.length; i++) {
    const e = entities[i];
    if (layerCategories[e.layer] !== category) continue;

    const t = e.type;
    if (t === "LINE") {
      const s = e.props?.start;
      const en = e.props?.end;
      if (!Array.isArray(s) || !Array.isArray(en)) continue;
      out.push(
        <line
          key={`${keyPrefix}-${i}`}
          x1={s[0]} y1={s[1]} x2={en[0]} y2={en[1]}
          stroke={style.stroke}
          strokeWidth={style.strokeWidth}
          strokeOpacity={style.strokeOpacity}
          strokeDasharray={style.strokeDasharray}
        />,
      );
    } else if (t === "LWPOLYLINE" || t === "POLYLINE") {
      const verts: number[][] | undefined = e.props?.vertices;
      if (!verts || verts.length === 0) continue;
      const closed = !!e.props?.closed;
      const d = "M " + verts.map(([x, y]) => `${x} ${y}`).join(" L ") + (closed ? " Z" : "");
      out.push(
        <path
          key={`${keyPrefix}-${i}`}
          d={d}
          stroke={style.stroke}
          strokeWidth={style.strokeWidth}
          strokeOpacity={style.strokeOpacity}
          strokeDasharray={style.strokeDasharray}
          fill={closed ? (style.fill ?? "none") : "none"}
          fillOpacity={closed ? style.fillOpacity : undefined}
        />,
      );
    } else if (t === "CIRCLE") {
      const cx = e.pos?.[0];
      const cy = e.pos?.[1];
      const r = e.props?.radius;
      if (cx == null || cy == null || !r) continue;
      out.push(
        <circle
          key={`${keyPrefix}-${i}`}
          cx={cx} cy={cy} r={r}
          stroke={style.stroke}
          strokeWidth={style.strokeWidth}
          strokeOpacity={style.strokeOpacity}
          fill={style.fill ?? "none"}
          fillOpacity={style.fillOpacity}
        />,
      );
    } else if (t === "HATCH") {
      // Hatch boundaries — we don't reproduce the actual diagonal lines,
      // just outline + fill the region so it reads as "this area is
      // hatched in the source DXF".
      const loops: number[][][] | undefined = e.props?.loops;
      if (!Array.isArray(loops) || loops.length === 0) continue;
      const d = loops
        .filter(l => l.length >= 3)
        .map(loop => "M " + loop.map(([x, y]) => `${x} ${y}`).join(" L ") + " Z")
        .join(" ");
      if (!d) continue;
      out.push(
        <path
          key={`${keyPrefix}-${i}`}
          d={d}
          stroke={style.stroke}
          strokeWidth={(style.strokeWidth ?? 10) * 0.5}
          strokeOpacity={style.strokeOpacity ?? 0.5}
          fill={style.fill ?? style.stroke ?? "#94a3b8"}
          fillOpacity={style.fillOpacity ?? 0.15}
          fillRule="evenodd"
        />,
      );
    }
    // ARC / TEXT / DIMENSION are intentionally ignored — they're handled
    // elsewhere or aren't useful as raw SVG lines.
  }
  return out;
}
type SleeveFilters = Props["sleeveFilters"];
type SeverityMap = Map<string, "NG" | "WARNING" | "OK">;

// ---------------------------------------------------------------------------
// Static layers — grid, walls, dims, slab, P-N labels.
// Only re-renders when floorData / lowerFloorData / layers / dataBounds change.
// ---------------------------------------------------------------------------

interface StaticLayersProps {
  floorData: FloorData;
  lowerFloorData: FloorData | null;
  layers: LayersState;
  dataBounds: DataBounds | null;
  flColorMap: Map<string, string>;
  universalEntities: UniversalEntity[] | null;
  layerCategories: Record<string, string> | null;
}

const StaticLayers = memo(function StaticLayers({
  floorData, lowerFloorData, layers, dataBounds, flColorMap,
  universalEntities, layerCategories,
}: StaticLayersProps) {
  // Pre-compute outer-wall classification once (regex in render loop is wasteful).
  const wallIsOuter = useMemo(
    () => floorData.wall_lines.map(w => w.wall_type === "外壁" || /外壁/.test(w.layer)),
    [floorData.wall_lines]
  );

  // Grid envelope from the axes themselves — walls/dims can extend past the
  // building, so deriving from dataBounds would stretch axes and bubble
  // positions far beyond the actual grid.
  const gridFrame = useMemo(() => {
    const vPos = floorData.grid_lines.filter(g => g.direction === "V").map(g => g.position);
    const hPos = floorData.grid_lines.filter(g => g.direction === "H").map(g => g.position);
    if (vPos.length === 0 || hPos.length === 0) return null;
    const overhang = 2000;  // 2m overshoot past the outermost grid
    return {
      x1: Math.min(...vPos) - overhang,
      x2: Math.max(...vPos) + overhang,
      y1: Math.min(...hPos) - overhang,
      y2: Math.max(...hPos) + overhang,
      bubbleR: 900,
    };
  }, [floorData.grid_lines]);

  // Raw passthrough rendering — every LINE / LWPOLYLINE / ARC / CIRCLE that
  // didn't get picked up by a typed extractor. One <path> per layer keeps the
  // SVG tree compact (~100 elements) even when the underlying DXF has tens of
  // thousands of segments.
  const rawPathsByLayer = useMemo(() => {
    const raw = floorData.raw_lines || [];
    if (raw.length === 0) return [] as { layer: string; d: string }[];
    const groups = new Map<string, string[]>();
    for (const r of raw) {
      if (r.points.length < 2) continue;
      const [x0, y0] = r.points[0];
      const parts = [`M${x0.toFixed(1)} ${y0.toFixed(1)}`];
      for (let i = 1; i < r.points.length; i++) {
        const [x, y] = r.points[i];
        parts.push(`L${x.toFixed(1)} ${y.toFixed(1)}`);
      }
      const list = groups.get(r.layer) || [];
      list.push(parts.join(" "));
      groups.set(r.layer, list);
    }
    return Array.from(groups.entries()).map(([layer, ds]) => ({
      layer, d: ds.join(" "),
    }));
  }, [floorData.raw_lines]);

  // FL-zone tiling — Voronoi-style color fill seeded by slab_zones positions.
  // Each cell finds its nearest slab_zone and inherits that FL's color; runs
  // of same-color cells in a row are merged into single rects to keep the SVG
  // compact (~20–80 rects instead of thousands).
  const flZoneTiles = useMemo(() => {
    const zones = floorData.slab_zones || [];
    if (!dataBounds || zones.length === 0) return [] as { x: number; y: number; w: number; h: number; color: string }[];
    const { minX, maxX, minY, maxY } = dataBounds;
    const N = 48;
    const cellW = (maxX - minX) / N;
    const cellH = (maxY - minY) / N;
    const tiles: { x: number; y: number; w: number; h: number; color: string }[] = [];

    const nearestColor = (cx: number, cy: number): string => {
      let bestI = 0;
      let bestD = Infinity;
      for (let k = 0; k < zones.length; k++) {
        const z = zones[k];
        const d = (z.x - cx) * (z.x - cx) + (z.y - cy) * (z.y - cy);
        if (d < bestD) { bestD = d; bestI = k; }
      }
      const fl = zones[bestI].fl_text || "不明";
      return flColorMap.get(fl) || FL_UNKNOWN_COLOR;
    };

    for (let j = 0; j < N; j++) {
      const y = minY + j * cellH;
      const cy = y + cellH / 2;
      let runStartI = 0;
      let runColor = nearestColor(minX + cellW / 2, cy);
      for (let i = 1; i <= N; i++) {
        const cellColor = i < N ? nearestColor(minX + (i + 0.5) * cellW, cy) : null;
        if (cellColor !== runColor || i === N) {
          tiles.push({
            x: minX + runStartI * cellW,
            y,
            w: cellW * (i - runStartI),
            h: cellH,
            color: runColor,
          });
          if (cellColor) {
            runColor = cellColor;
            runStartI = i;
          }
        }
      }
    }
    return tiles;
  }, [dataBounds, floorData.slab_zones, flColorMap]);

  return (
    <>
      {/* FL zone tiling — rendered first so every other layer sits on top */}
      {layers.flZone && flZoneTiles.map((t, i) => (
        <rect key={`fz${i}`} x={t.x} y={t.y} width={t.w} height={t.h}
          fill={t.color} opacity={0.18} stroke="none" />
      ))}
      {/* Raw DXF passthrough — behind everything else, faint grey */}
      {layers.raw && rawPathsByLayer.map(({ layer, d }) => (
        <path key={`raw-${layer}`} d={d}
          fill="none" stroke="#cbd5e1" strokeWidth={8} opacity={0.55} />
      ))}
      {layers.raw && (floorData.raw_texts || []).map((t, i) => (
        <g key={`rawt-${i}`} transform={`translate(${t.x},${t.y}) scale(1,-1)${t.rotation ? ` rotate(${-t.rotation})` : ""}`}>
          <text x={0} y={0} fontSize={Math.max(t.height || 250, 250)}
                fill="#475569" fontFamily="'Noto Sans JP',sans-serif">{t.text}</text>
        </g>
      ))}

      {/* Room labels (部屋名) — typed extraction from A211_室名 layer only */}
      {layers.room && (floorData.room_labels || []).map((r, i) => (
        <g key={`room-${i}`} transform={`translate(${r.x},${r.y}) scale(1,-1)${r.rotation ? ` rotate(${-r.rotation})` : ""}`}>
          <text x={0} y={0}
                fontSize={Math.max(r.height || 500, 500)}
                fill="#1e293b" fontWeight={600}
                fontFamily="'Noto Sans JP', 'Yu Gothic', 'Hiragino Sans', sans-serif"
                style={{ paintOrder: "stroke", stroke: "#fff", strokeWidth: 60, strokeLinejoin: "round" }}>
            {r.text}
          </text>
        </g>
      ))}

      {/* Raw "grid line" geometry — every line on a 通り芯-classified
          layer, regardless of length. Renders behind the typed grid
          frame so the bubbled axis labels stay legible. While the
          universal payload is still loading, the typed grid frame
          (below) is enough on its own. */}
      {layers.grid && universalEntities && layerCategories &&
        renderRawEntitiesByCategory(
          universalEntities, layerCategories, "通り芯",
          { stroke: "#1f2937", strokeWidth: 8, strokeOpacity: 0.35 },
          "g-raw",
        )
      }

      {/* Grid lines + axis-label bubbles — solid thin line, darker than
          walls so they still read as the drawing's skeleton. */}
      {layers.grid && gridFrame && floorData.grid_lines.flatMap((g, i) => {
        const { x1, x2, y1, y2, bubbleR } = gridFrame;
        const endpoints: [number, number][] = g.direction === "H"
          ? [[x1 - bubbleR, g.position], [x2 + bubbleR, g.position]]
          : [[g.position, y1 - bubbleR], [g.position, y2 + bubbleR]];
        const line = g.direction === "H" ? (
          <line key={`gh${i}`} x1={x1} y1={g.position} x2={x2} y2={g.position}
            stroke="#1f2937" strokeWidth={18} opacity={0.75} />
        ) : (
          <line key={`gv${i}`} x1={g.position} y1={y1} x2={g.position} y2={y2}
            stroke="#1f2937" strokeWidth={18} opacity={0.75} />
        );
        const bubbles = endpoints.map(([lx, ly], pi) => (
          <g key={`gl${i}-${pi}`} transform={`translate(${lx} ${ly})`}>
            <circle cx={0} cy={0} r={bubbleR} fill="#fff" stroke="#111827" strokeWidth={35} />
            <text x={0} y={0} textAnchor="middle" dominantBaseline="central"
                  fontSize={bubbleR * 1.2} fill="#111827" fontWeight={700}
                  fontFamily="'Inter','Noto Sans JP',sans-serif"
                  transform="scale(1,-1)">{g.axis_label}</text>
          </g>
        ));
        return [line, ...bubbles];
      })}

      {/* Lower floor walls (1F wall overlay on 2F) */}
      {layers.lowerWall && lowerFloorData?.wall_lines.map((w, i) => (
        <line key={`lw${i}`} x1={w.start[0]} y1={w.start[1]} x2={w.end[0]} y2={w.end[1]}
          stroke="#8b5cf6" strokeWidth={40} opacity={0.4} />
      ))}

      {/* Walls — raw render when the universal payload is available,
          otherwise fall back to the typed FloorData walls so the drawing
          isn't blank during the initial fetch. */}
      {universalEntities && layerCategories ? (
        <>
          {layers.outerWall && renderRawEntitiesByCategory(
            universalEntities, layerCategories, "外壁",
            { stroke: "#111827", strokeWidth: 55, strokeOpacity: 1.0 },
            "ow-raw",
          )}
          {layers.wall && renderRawEntitiesByCategory(
            universalEntities, layerCategories, "内壁",
            { stroke: "#64748b", strokeWidth: 25, strokeOpacity: 1.0 },
            "iw-raw",
          )}
          {/* 耐火壁・防火区画 — 個別トグル (firewall)。視覚的に
              区画線として識別できるよう赤系破線で重ねる。 */}
          {layers.firewall && renderRawEntitiesByCategory(
            universalEntities, layerCategories, "耐火壁・防火区画",
            { stroke: "#b91c1c", strokeWidth: 30, strokeOpacity: 0.9, strokeDasharray: "300 150" },
            "fw-raw",
          )}
        </>
      ) : (
        floorData.wall_lines.map((w, i) => {
          const isOuter = wallIsOuter[i];
          const visible = isOuter ? layers.outerWall : layers.wall;
          if (!visible) return null;
          return (
            <line key={`w${i}`} x1={w.start[0]} y1={w.start[1]} x2={w.end[0]} y2={w.end[1]}
              stroke={isOuter ? "#111827" : "#64748b"}
              strokeWidth={isOuter ? 55 : 25}
              strokeLinecap={isOuter ? "round" : undefined} />
          );
        })
      )}

      {/* Recess (床ヌスミ) — raw if available, typed fallback otherwise. */}
      {layers.recess && (
        universalEntities && layerCategories
          ? renderRawEntitiesByCategory(
              universalEntities, layerCategories, "床ヌスミ",
              { stroke: "#0369a1", strokeWidth: 15, fill: "#0ea5e9", fillOpacity: 0.22, strokeOpacity: 0.8, strokeDasharray: "60 30" },
              "r-raw",
            )
          : (floorData.recess_polygons || []).map((rp, i) => {
              const d = rp.vertices.length
                ? "M " + rp.vertices.map(([x, y]) => `${x} ${y}`).join(" L ") + " Z"
                : "";
              if (!d) return null;
              return (
                <path key={`r${i}`} d={d}
                  fill="#0ea5e9" fillOpacity={0.22}
                  stroke="#0369a1" strokeWidth={15} strokeOpacity={0.7}
                  strokeDasharray="60 30" />
              );
            })
      )}

      {/* Step lines — raw with typed fallback. */}
      {layers.step && (
        universalEntities && layerCategories
          ? renderRawEntitiesByCategory(
              universalEntities, layerCategories, "段差線",
              { stroke: "#d97706", strokeWidth: 25, strokeOpacity: 0.9 },
              "s-raw",
            )
          : floorData.step_lines.map((s, i) => {
              if (s.fl_status === "spurious") return null;
              const opacity = s.fl_status === "real" ? 0.9 : 0.5;
              return (
                <line key={`s${i}`} x1={s.start[0]} y1={s.start[1]} x2={s.end[0]} y2={s.end[1]}
                  stroke="#d97706" strokeWidth={25} opacity={opacity} />
              );
            })
      )}

      {/* Columns / slab categories — each toggled independently to match
          the data tab 1-to-1. Typed fallback applies only to columns since
          the legacy FloorData doesn't carry slab outlines as lines. */}
      {universalEntities && layerCategories ? (
        <>
          {layers.column && renderRawEntitiesByCategory(
            universalEntities, layerCategories, "柱・仕上線",
            { stroke: "#7c3aed", strokeWidth: 20, strokeOpacity: 0.6 },
            "col-raw",
          )}
          {layers.slabOutline && renderRawEntitiesByCategory(
            universalEntities, layerCategories, "スラブ外形",
            { stroke: "#7c3aed", strokeWidth: 18, strokeOpacity: 0.4 },
            "so-raw",
          )}
          {layers.slabLabel && renderRawEntitiesByCategory(
            universalEntities, layerCategories, "スラブラベル",
            { stroke: "#94a3b8", strokeWidth: 8, strokeOpacity: 0.4,
              fill: "#cbd5e1", fillOpacity: 0.18 },
            "sl-raw",
          )}
          {layers.slabFL && renderRawEntitiesByCategory(
            universalEntities, layerCategories, "スラブFL",
            { stroke: "#94a3b8", strokeWidth: 8, strokeOpacity: 0.4,
              fill: "#cbd5e1", fillOpacity: 0.18 },
            "sf-raw",
          )}
        </>
      ) : (
        layers.column && floorData.column_lines.map((c, i) => (
          <line key={`col${i}`} x1={c.start[0]} y1={c.start[1]} x2={c.end[0]} y2={c.end[1]}
            stroke="#7c3aed" strokeWidth={20} opacity={0.6} />
        ))
      )}

      {/* Beams — raw with typed fallback. */}
      {layers.beam && (
        universalEntities && layerCategories
          ? renderRawEntitiesByCategory(
              universalEntities, layerCategories, "梁",
              { stroke: "#a855f7", strokeWidth: 10, strokeDasharray: "200 100", strokeOpacity: 0.7 },
              "bm-raw",
            )
          : (floorData.beam_lines || []).map((b, i) => (
              <line key={`bm${i}`} x1={b.start[0]} y1={b.start[1]} x2={b.end[0]} y2={b.end[1]}
                stroke="#a855f7" strokeWidth={10} strokeDasharray="200 100" opacity={0.7} />
            ))
      )}

      {/* Dimension lines */}
      {layers.dim && floorData.dim_lines.map((d, i) => {
        const val = Math.round(d.measurement);
        if (val < 1) return null;

        const x1 = d.defpoint2[0], y1 = d.defpoint2[1];
        const x2 = d.defpoint3[0], y2 = d.defpoint3[1];

        let isHorizontal: boolean;
        if (d.angle !== null && d.angle !== undefined) {
          const normAngle = ((d.angle % 360) + 360) % 360;
          isHorizontal = normAngle < 45 || normAngle > 315 || (normAngle > 135 && normAngle < 225);
        } else {
          const dx = Math.abs(x2 - x1);
          const dy = Math.abs(y2 - y1);
          isHorizontal = dx >= dy;
        }

        let dlx1: number, dly1: number, dlx2: number, dly2: number;
        if (isHorizontal) {
          dly1 = d.defpoint1[1]; dly2 = d.defpoint1[1];
          dlx1 = x1; dlx2 = x2;
        } else {
          dlx1 = d.defpoint1[0]; dlx2 = d.defpoint1[0];
          dly1 = y1; dly2 = y2;
        }

        const mx = (dlx1 + dlx2) / 2;
        const my = (dly1 + dly2) / 2;

        return (
          <g key={`dm${i}`}>
            <line x1={x1} y1={y1} x2={dlx1} y2={dly1}
                  stroke="#06b6d4" strokeWidth={5} opacity={0.3} />
            <line x1={x2} y1={y2} x2={dlx2} y2={dly2}
                  stroke="#06b6d4" strokeWidth={5} opacity={0.3} />
            <line x1={dlx1} y1={dly1} x2={dlx2} y2={dly2}
                  stroke="#06b6d4" strokeWidth={10} opacity={0.5} />
            <circle cx={dlx1} cy={dly1} r={20} fill="#06b6d4" opacity={0.5} />
            <circle cx={dlx2} cy={dly2} r={20} fill="#06b6d4" opacity={0.5} />
            <g transform={`translate(${mx},${my})`}>
              <g transform="scale(1,-1)">
                <text x={0} y={0} fontSize={120} fill="#06b6d4" fontWeight={600}
                  textAnchor="middle" opacity={0.7}>
                  {val}
                </text>
              </g>
            </g>
          </g>
        );
      })}

      {/* Slab outlines */}
      {layers.slabLevel && floorData.slab_outlines?.map((s, i) => (
        <line key={`so${i}`} x1={s.start[0]} y1={s.start[1]} x2={s.end[0]} y2={s.end[1]}
          stroke="#4338ca" strokeWidth={18} opacity={0.5} />
      ))}

      {/* Slab info labels */}
      {layers.slabLevel && floorData.slab_labels?.map((sl, i) => (
        <g key={`slab${i}`} transform={`translate(${sl.x},${sl.y})`}>
          <g transform="scale(1,-1)">
            <text x={0} y={-120} fontSize={200} fill="#4338ca" fontWeight={700} textAnchor="middle" opacity={0.8}>
              {sl.slab_no}
            </text>
            <text x={0} y={100} fontSize={150} fill="#6366f1" fontWeight={500} textAnchor="middle" opacity={0.7}>
              FL{sl.level}
            </text>
            {sl.thickness && (
              <text x={0} y={300} fontSize={130} fill="#818cf8" fontWeight={400} textAnchor="middle" opacity={0.6}>
                t={sl.thickness}
              </text>
            )}
          </g>
        </g>
      ))}

      {/* Step level labels */}
      {layers.slabLevel && floorData.slab_zones?.map((z, i) => (
        <g key={`sl${i}`} transform={`translate(${z.x},${z.y})`}>
          <g transform="scale(1,-1)">
            <text x={0} y={0} fontSize={160} fill="#d97706" fontWeight={600} textAnchor="middle" opacity={0.7}>
              FL{z.fl_value >= 0 ? "+" : ""}{z.fl_value}
            </text>
          </g>
        </g>
      ))}

      {/* P-N number labels + arrow lines (own toggle, no longer slaved to sleeve) */}
      {layers.pn && floorData.pn_labels?.map((pn, i) => (
        <g key={`pn${i}`}>
          {pn.arrow_verts && pn.arrow_verts.length === 2 && (
            <line
              x1={pn.arrow_verts[0][0]} y1={pn.arrow_verts[0][1]}
              x2={pn.arrow_verts[1][0]} y2={pn.arrow_verts[1][1]}
              stroke="#e11d48" strokeWidth={12} opacity={0.6}
            />
          )}
          <g transform={`translate(${pn.x},${pn.y})`}>
            <g transform="scale(1,-1)">
              <text x={0} y={0} fontSize={150} fill="#e11d48" fontWeight={700} textAnchor="middle" opacity={0.8}>
                {pn.text}
              </text>
            </g>
          </g>
        </g>
      ))}

      {/* New category renderings (1-to-1 with data tab). All routed through
          the universal-entity payload — typed extractors haven't been added
          for these yet, so we just paint whatever sits on the relevant
          layers using a generic style. */}
      {universalEntities && layerCategories && (
        <>
          {layers.flLabel && renderRawEntitiesByCategory(
            universalEntities, layerCategories, "FL表記",
            { stroke: "#0891b2", strokeWidth: 6, strokeOpacity: 0.7 },
            "fll-raw",
          )}
          {layers.waterGradient && renderRawEntitiesByCategory(
            universalEntities, layerCategories, "水勾配",
            { stroke: "#0ea5e9", strokeWidth: 14, strokeOpacity: 0.8 },
            "wg-raw",
          )}
          {layers.equipSanitary && renderRawEntitiesByCategory(
            universalEntities, layerCategories, "機器コード_衛生",
            { stroke: "#3b82f6", strokeWidth: 6, strokeOpacity: 0.55 },
            "eq-san-raw",
          )}
          {layers.equipHvac && renderRawEntitiesByCategory(
            universalEntities, layerCategories, "機器コード_空調",
            { stroke: "#f59e0b", strokeWidth: 6, strokeOpacity: 0.55 },
            "eq-hvac-raw",
          )}
          {layers.equipElectric && renderRawEntitiesByCategory(
            universalEntities, layerCategories, "機器コード_電気",
            { stroke: "#ef4444", strokeWidth: 6, strokeOpacity: 0.55 },
            "eq-elec-raw",
          )}
        </>
      )}
    </>
  );
});

// ---------------------------------------------------------------------------
// Sleeve layer — interactive sleeves. Re-renders on selection/results/mode change,
// NOT on hover (because hover state lives in the parent, never passed down here).
// ---------------------------------------------------------------------------

function getSleeveColors(
  s: Sleeve,
  severityMap: SeverityMap,
): { stroke: string; fill: string } {
  return SEVERITY_COLORS[severityMap.get(s.id) || "OK"];
}

interface SleeveLayerProps {
  sleeves: Sleeve[];
  sleeveFilters: SleeveFilters;
  severityMap: SeverityMap;
  selectedSleeveId: string | null;
  onSleeveHover: (s: Sleeve | null) => void;
  onSleeveClick: (s: Sleeve | null) => void;
  visible: boolean;
}

const SleeveLayer = memo(function SleeveLayer({
  sleeves, sleeveFilters, severityMap, selectedSleeveId, onSleeveHover, onSleeveClick, visible,
}: SleeveLayerProps) {
  const filtered = useMemo(() => {
    return sleeves.filter(s => {
      const disc = s.discipline as keyof SleeveFilters;
      if (disc in sleeveFilters) return sleeveFilters[disc];
      return sleeveFilters["その他"];
    });
  }, [sleeves, sleeveFilters]);

  if (!visible) return null;

  return (
    <>
      {filtered.map((s) => {
        const colors = getSleeveColors(s, severityMap);
        const isSelected = s.id === selectedSleeveId;
        const isRect = s.shape === "rect";
        // Render at the drawing's true dimensions. Previously we clamped
        // everything to 200 mm half-size for click ergonomics, which blew
        // up small φ60 / φ124 pipes into giant blobs. The hit area below
        // is still padded so small sleeves remain clickable.
        const halfW = (s.width ?? s.diameter) / 2;
        const halfH = (s.height ?? s.diameter) / 2;
        const r = s.diameter / 2;
        // Hit area: always at least 300 mm half-size so even φ60 stays
        // clickable. Drawn transparently, does not affect the visible glyph.
        const hitR = Math.max(Math.max(halfW, halfH, r) * 1.8, 300);
        const cx = s.center[0];
        const cy = s.center[1];
        return (
          <g key={s.id} style={{ cursor: "pointer" }}
            onMouseEnter={(e) => { e.stopPropagation(); onSleeveHover(s); }}
            onClick={(e) => { e.stopPropagation(); onSleeveClick(s); }}>
            <circle cx={cx} cy={cy} r={hitR} fill="transparent" stroke="none" />
            {isSelected && (
              <>
                <circle cx={cx} cy={cy} r={r + 200}
                  fill="none" stroke="#ef4444" strokeWidth={25} opacity={0.8}>
                  <animate attributeName="r" from={r + 100} to={r + 400} dur="1s" repeatCount="indefinite" />
                  <animate attributeName="opacity" from="0.8" to="0" dur="1s" repeatCount="indefinite" />
                </circle>
                <circle cx={cx} cy={cy} r={r + 80}
                  fill="none" stroke="#ef4444" strokeWidth={20} strokeDasharray="60,30" opacity={0.7} />
              </>
            )}
            {isRect ? (
              <rect
                x={cx - halfW} y={cy - halfH}
                width={halfW * 2} height={halfH * 2}
                fill={colors.fill} stroke={colors.stroke}
                strokeWidth={isSelected ? 35 : 20}
              />
            ) : (
              <circle cx={cx} cy={cy} r={r}
                fill={colors.fill} stroke={colors.stroke} strokeWidth={isSelected ? 35 : 20} />
            )}
            <circle cx={cx} cy={cy} r={35} fill={colors.stroke} />
          </g>
        );
      })}
    </>
  );
});

// ---------------------------------------------------------------------------
// Highlight layer — pulsing markers from list navigation. Tiny.
// ---------------------------------------------------------------------------

interface HighlightLayerProps {
  coords: [number, number][];
}

const HighlightLayer = memo(function HighlightLayer({ coords }: HighlightLayerProps) {
  if (!coords || coords.length === 0) return null;
  return (
    <>
      {coords.map((c, i) => (
        <g key={`hl${i}`}>
          <circle cx={c[0]} cy={c[1]} r={300}
            fill="none" stroke="#ef4444" strokeWidth={30} opacity={0.9}>
            <animate attributeName="r" from="200" to="500" dur="1s" repeatCount="indefinite" />
            <animate attributeName="opacity" from="0.9" to="0" dur="1s" repeatCount="indefinite" />
          </circle>
          <circle cx={c[0]} cy={c[1]} r={150}
            fill="#ef4444" opacity={0.4} />
        </g>
      ))}
    </>
  );
});

// ---------------------------------------------------------------------------
// Main DrawingView — owns viewport state; delegates rendering to memo'd children.
// ---------------------------------------------------------------------------

function DrawingViewInner({
  floorData, lowerFloorData, universalEntities, layerCategories,
  results, onSleeveHover, onSleeveClick,
  selectedSleeveId, layers, sleeveFilters, navigateTarget, onNavigated, highlightCoords,
  pdfOverlayUrl, pdfOverlayOpacity = 0.4,
  onToggleLayer, onToggleSleeveFilter, sleeveCounts,
  showLowerWallToggle, lowerFloorLabel, onPdfFilesSelected, onPdfClear, onPdfOpacityChange,
}: Props) {
  const svgRef = useRef<SVGSVGElement>(null);
  const pdfInputRef = useRef<HTMLInputElement>(null);
  const [vb, setVb] = useState<ViewBox>(INITIAL_VB);
  const [isPanning, setIsPanning] = useState(false);
  const panStart = useRef<{ x: number; y: number; vb: ViewBox } | null>(null);

  // Bounds of all drawable entities (loop — spread on 10k+ args overflows the stack).
  const dataBounds = useMemo<DataBounds | null>(() => {
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    let has = false;
    const addX = (x: number) => {
      if (x < minX) minX = x;
      if (x > maxX) maxX = x;
      has = true;
    };
    const addY = (y: number) => {
      if (y < minY) minY = y;
      if (y > maxY) maxY = y;
      has = true;
    };
    for (const s of floorData.sleeves) { addX(s.center[0]); addY(s.center[1]); }
    for (const g of floorData.grid_lines) {
      if (g.direction === "H") addY(g.position);
      else addX(g.position);
    }
    for (const w of floorData.wall_lines) {
      addX(w.start[0]); addX(w.end[0]);
      addY(w.start[1]); addY(w.end[1]);
    }
    for (const c of floorData.column_lines) {
      addX(c.start[0]); addX(c.end[0]);
      addY(c.start[1]); addY(c.end[1]);
    }
    for (const o of floorData.slab_outlines || []) {
      addX(o.start[0]); addX(o.end[0]);
      addY(o.start[1]); addY(o.end[1]);
    }
    if (!has) return null;
    return { minX, maxX, minY, maxY };
  }, [floorData]);

  const fitVb = useCallback((): ViewBox => {
    if (!dataBounds) return INITIAL_VB;
    const { minX, maxX, minY, maxY } = dataBounds;
    const pad = 0.08;
    const spanX = (maxX - minX) || MIN_ZOOM_W;
    const spanY = (maxY - minY) || MIN_ZOOM_W;
    const w = Math.min(MAX_ZOOM_W, Math.max(MIN_ZOOM_W, spanX * (1 + pad * 2)));
    const h = Math.min(MAX_ZOOM_W * 0.6, Math.max(MIN_ZOOM_W * 0.5, spanY * (1 + pad * 2)));
    const cx = (minX + maxX) / 2;
    const cy = (minY + maxY) / 2;
    return { x: cx - w / 2, y: -cy - h / 2, w, h };
  }, [dataBounds]);

  const fittedFloorRef = useRef<unknown>(null);
  useEffect(() => {
    if (floorData !== fittedFloorRef.current && dataBounds) {
      fittedFloorRef.current = floorData;
      setVb(fitVb());
    }
  }, [floorData, dataBounds, fitVb]);

  const vbRef = useRef(vb);
  vbRef.current = vb;

  useEffect(() => {
    const svg = svgRef.current;
    if (!svg) return;
    const handler = (e: WheelEvent) => {
      e.preventDefault();
      e.stopPropagation();
      const rect = svg.getBoundingClientRect();
      const cur = vbRef.current;
      const sx = (e.clientX - rect.left) / rect.width;
      const sy = (e.clientY - rect.top) / rect.height;
      const ptX = cur.x + sx * cur.w;
      const ptY = cur.y + sy * cur.h;
      const delta = Math.sign(e.deltaY) * Math.min(Math.abs(e.deltaY), 100);
      const factor = 1 + delta * 0.003;
      const newW = Math.min(Math.max(cur.w * factor, MIN_ZOOM_W), MAX_ZOOM_W);
      const newH = Math.min(Math.max(cur.h * factor, MIN_ZOOM_W * 0.5), MAX_ZOOM_W * 0.5);
      const ratio = newW / cur.w;
      setVb({
        x: ptX - (ptX - cur.x) * ratio,
        y: ptY - (ptY - cur.y) * ratio,
        w: newW,
        h: newH,
      });
    };
    svg.addEventListener("wheel", handler, { passive: false });
    return () => svg.removeEventListener("wheel", handler);
  }, []);

  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    if (e.button !== 0) return;
    setIsPanning(true);
    panStart.current = { x: e.clientX, y: e.clientY, vb: { ...vb } };
  }, [vb]);

  const handleMouseMove = useCallback((e: React.MouseEvent) => {
    if (!isPanning || !panStart.current || !svgRef.current) return;
    const rect = svgRef.current.getBoundingClientRect();
    const dx = (e.clientX - panStart.current.x) / rect.width * panStart.current.vb.w;
    const dy = (e.clientY - panStart.current.y) / rect.height * panStart.current.vb.h;
    setVb({ ...panStart.current.vb, x: panStart.current.vb.x - dx, y: panStart.current.vb.y - dy });
  }, [isPanning]);

  const handleMouseUp = useCallback(() => {
    setIsPanning(false);
    panStart.current = null;
  }, []);

  const handleDoubleClick = useCallback(() => setVb(fitVb()), [fitVb]);

  // Navigate to target coordinates (from list view click)
  useEffect(() => {
    if (!navigateTarget) return;
    const pts = highlightCoords && highlightCoords.length > 1 ? highlightCoords : [navigateTarget];
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    for (const [x, y] of pts) {
      if (x < minX) minX = x;
      if (x > maxX) maxX = x;
      if (y < minY) minY = y;
      if (y > maxY) maxY = y;
    }
    const spanX = maxX - minX;
    const spanY = maxY - minY;
    const cx = (minX + maxX) / 2;
    const cy = (minY + maxY) / 2;
    const zoomW = Math.max(15000, spanX + 10000);
    const zoomH = Math.max(7500, spanY + 5000);
    setVb({ x: cx - zoomW / 2, y: -cy - zoomH / 2, w: zoomW, h: zoomH });
    onNavigated?.();
  }, [navigateTarget, onNavigated, highlightCoords]);

  // severity lookup for sleeve coloring
  const severityMap = useMemo<SeverityMap>(() => {
    const map: SeverityMap = new Map();
    for (const r of results) {
      if (r.sleeve_id) {
        const cur = map.get(r.sleeve_id);
        if (!cur || r.severity === "NG" || (r.severity === "WARNING" && cur === "OK")) {
          map.set(r.sleeve_id, r.severity);
        }
      }
    }
    return map;
  }, [results]);

  // FL → color map. Known FLs use the curated palette; everything else
  // gets a deterministic fallback color so the legend doesn't collapse
  // site-specific levels into a single grey dot.
  const flColorMap = useMemo(() => {
    const counts = new Map<string, number>();
    for (const s of floorData.sleeves) {
      const key = s.fl_text || "不明";
      counts.set(key, (counts.get(key) || 0) + 1);
    }
    // Sort by count desc so the dominant FL lands the top palette slot.
    const sorted = [...counts.entries()].sort((a, b) => b[1] - a[1]);
    const map = new Map<string, string>();
    let fallbackIdx = 0;
    for (const [fl] of sorted) {
      if (fl === "不明") { map.set(fl, FL_UNKNOWN_COLOR); continue; }
      const preset = FL_COLORS[fl];
      if (preset) { map.set(fl, preset); continue; }
      map.set(fl, FL_FALLBACK_PALETTE[fallbackIdx % FL_FALLBACK_PALETTE.length]);
      fallbackIdx++;
    }
    return map;
  }, [floorData.sleeves]);

  const flLegend = useMemo(() => {
    return [...flColorMap.entries()].map(([label, color]) => ({ label, color }));
  }, [flColorMap]);

  const viewBox = `${vb.x} ${vb.y} ${vb.w} ${vb.h}`;

  // 1-to-1 with USEFUL_CATEGORIES (23: 19 explicit + sleeve aggregate of 4).
  // Order mirrors GROUP_ORDER on the data tab so the two toolbars stay
  // mentally aligned.
  const LAYER_ITEMS: [LayerKey, string][] = [
    ["grid", "通り芯"],
    ["outerWall", "外壁"],
    ["wall", "内壁"],
    ["firewall", "耐火壁・区画"],
    ["column", "柱・仕上線"],
    ["beam", "梁"],
    ["slabOutline", "スラブ外形"],
    ["slabLabel", "スラブラベル"],
    ["slabFL", "スラブFL"],
    ["step", "段差線"],
    ["recess", "床ヌスミ"],
    ["flLabel", "FL表記"],
    ["dim", "寸法線"],
    ["pn", "P-N番号"],
    ["room", "部屋名"],
    ["waterGradient", "水勾配"],
    ["equipSanitary", "機器(衛生)"],
    ["equipHvac", "機器(空調)"],
    ["equipElectric", "機器(電気)"],
    ["sleeve", "スリーブ"],
  ];
  const discColor = (d: DisciplineKey) =>
    d === "衛生" ? "#3b82f6" : d === "空調" ? "#f59e0b" : d === "電気" ? "#ef4444" : "#6b7280";

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", background: "#fdfdfe" }}>
      {/* Toolbar */}
      <div className="no-print" style={{
        display: "flex", flexWrap: "wrap", alignItems: "center", gap: 6,
        padding: "6px 12px", background: "#fff",
        borderBottom: "1px solid #f3f4f6",
      }}>
        <span style={{ color: "#9ca3af", fontSize: 10 }}>レイヤー</span>
        {LAYER_ITEMS.map(([key, label]) => {
          const on = layers[key];
          return (
            <button key={key} onClick={() => onToggleLayer(key)}
              style={{
                padding: "2px 10px", borderRadius: 4, cursor: "pointer", fontSize: 10,
                border: `1px solid ${on ? "#9ca3af" : "#e5e7eb"}`,
                background: on ? "#f3f4f6" : "#fff",
                color: on ? "#374151" : "#d1d5db",
              }}>{label}</button>
          );
        })}
        {showLowerWallToggle && (
          <button onClick={() => onToggleLayer("lowerWall")}
            style={{
              padding: "2px 10px", borderRadius: 4, cursor: "pointer", fontSize: 10,
              border: `1px solid ${layers.lowerWall ? "#9ca3af" : "#e5e7eb"}`,
              background: layers.lowerWall ? "#f3f4f6" : "#fff",
              color: layers.lowerWall ? "#374151" : "#d1d5db",
            }}>{lowerFloorLabel ? `${lowerFloorLabel}壁` : "下階壁"}</button>
        )}

        {layers.sleeve && (
          <>
            <span style={{ color: "#d1d5db", margin: "0 2px" }}>›</span>
            {(["衛生", "空調", "電気", "その他"] as const).map((disc) => {
              const on = sleeveFilters[disc];
              const cnt = sleeveCounts[disc];
              const color = discColor(disc);
              return (
                <button key={disc} onClick={() => onToggleSleeveFilter(disc)} disabled={cnt === 0}
                  style={{
                    padding: "2px 8px", borderRadius: 4, cursor: cnt === 0 ? "default" : "pointer", fontSize: 10,
                    border: `1px solid ${on && cnt > 0 ? color : "#e5e7eb"}`,
                    background: on && cnt > 0 ? `${color}15` : "#fff",
                    color: on && cnt > 0 ? color : "#d1d5db",
                    opacity: cnt === 0 ? 0.4 : 1,
                  }}>
                  {disc} {cnt > 0 && <span style={{ opacity: 0.7 }}>({cnt})</span>}
                </button>
              );
            })}
          </>
        )}

        {/* Judgement legend — always shown when the sleeve layer is on */}
        {layers.sleeve && (
          <>
            <span style={{ color: "#d1d5db", margin: "0 4px" }}>|</span>
            <div style={{ display: "flex", gap: 8, fontSize: 10, alignItems: "center" }}>
              <span style={{ display: "flex", alignItems: "center", gap: 3 }}><span style={{ width: 8, height: 8, borderRadius: "50%", background: "#dc2626", display: "inline-block" }} />NG</span>
              <span style={{ display: "flex", alignItems: "center", gap: 3 }}><span style={{ width: 8, height: 8, borderRadius: "50%", background: "#d97706", display: "inline-block" }} />WARN</span>
              <span style={{ display: "flex", alignItems: "center", gap: 3 }}><span style={{ width: 8, height: 8, borderRadius: "50%", background: "#16a34a", display: "inline-block" }} />OK</span>
            </div>
          </>
        )}

        {/* FL-zone legend — shown when the FL-zone tiling layer is on */}
        {layers.flZone && flLegend.length > 0 && (
          <>
            <span style={{ color: "#d1d5db", margin: "0 4px" }}>|</span>
            <span style={{ color: "#9ca3af", fontSize: 10 }}>FL</span>
            <div style={{ display: "flex", gap: 8, fontSize: 10, alignItems: "center", flexWrap: "wrap" }}>
              {flLegend.map(({ label, color }) => (
                <span key={label} style={{ display: "flex", alignItems: "center", gap: 3 }}>
                  <span style={{ width: 8, height: 8, borderRadius: "50%", background: color, display: "inline-block" }} />
                  {label}
                </span>
              ))}
            </div>
          </>
        )}

        <span style={{ color: "#d1d5db", margin: "0 4px" }}>|</span>
        <span style={{ color: "#9ca3af", fontSize: 10 }}>PDF重ね</span>
        <input ref={pdfInputRef} type="file" accept=".pdf" style={{ display: "none" }}
          onChange={(e) => onPdfFilesSelected(e.target.files)} />
        <button onClick={() => pdfInputRef.current?.click()}
          style={{
            padding: "2px 10px", borderRadius: 4, cursor: "pointer", fontSize: 10,
            border: "1px solid #d1d5db", background: pdfOverlayUrl ? "#eff6ff" : "#fff",
            color: pdfOverlayUrl ? "#1d4ed8" : "#374151",
          }}>{pdfOverlayUrl ? "PDF読込済" : "PDF選択"}</button>
        {pdfOverlayUrl && (
          <>
            <input type="range" min={0} max={100}
              value={Math.round(pdfOverlayOpacity * 100)}
              onChange={(e) => onPdfOpacityChange(Number(e.target.value) / 100)}
              style={{ width: 80 }} />
            <span style={{ fontSize: 10, color: "#6b7280", minWidth: 28 }}>
              {Math.round(pdfOverlayOpacity * 100)}%
            </span>
            <button onClick={onPdfClear}
              style={{
                padding: "2px 6px", borderRadius: 4, cursor: "pointer", fontSize: 10,
                border: "1px solid #e5e7eb", background: "#fff", color: "#6b7280",
              }}>×</button>
          </>
        )}
      </div>

      {/* SVG */}
      <svg
        ref={svgRef}
        viewBox={viewBox}
        style={{ flex: 1, width: "100%", background: "#fdfdfe", cursor: isPanning ? "grabbing" : "grab" }}
        xmlns="http://www.w3.org/2000/svg"
        onMouseDown={handleMouseDown}
        onMouseMove={handleMouseMove}
        onMouseUp={handleMouseUp}
        onMouseLeave={handleMouseUp}
        onDoubleClick={handleDoubleClick}
      >
      <g transform="scale(1,-1)">
        {/* PDF overlay (optional) — drawn behind everything.
            Nested scale(1,-1) cancels the outer Y-flip so the PDF
            renders upright in world space. */}
        {pdfOverlayUrl && dataBounds && (() => {
          const w = dataBounds.maxX - dataBounds.minX;
          const h = dataBounds.maxY - dataBounds.minY;
          return (
            <g transform={`translate(0 ${dataBounds.minY + dataBounds.maxY}) scale(1,-1)`}>
              <image
                href={pdfOverlayUrl}
                x={dataBounds.minX}
                y={dataBounds.minY}
                width={w}
                height={h}
                opacity={pdfOverlayOpacity}
                preserveAspectRatio="xMidYMid meet"
              />
            </g>
          );
        })()}
        <StaticLayers
          floorData={floorData}
          lowerFloorData={lowerFloorData}
          layers={layers}
          dataBounds={dataBounds}
          flColorMap={flColorMap}
          universalEntities={universalEntities ?? null}
          layerCategories={layerCategories ?? null}
        />
        <SleeveLayer
          sleeves={floorData.sleeves}
          sleeveFilters={sleeveFilters}
          severityMap={severityMap}
          selectedSleeveId={selectedSleeveId}
          onSleeveHover={onSleeveHover}
          onSleeveClick={onSleeveClick}
          visible={layers.sleeve}
        />
        <HighlightLayer coords={highlightCoords || []} />
      </g>
      </svg>
    </div>
  );
}

export default memo(DrawingViewInner);
