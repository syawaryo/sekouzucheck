import { useMemo, useState, useRef, useEffect } from "react";
import type { FloorData } from "../types";
import type { UniversalEntity } from "../api";
import { useUniversalEntities } from "../useUniversalEntities";

interface Props {
  floorData: FloorData;
  floorId: string | null;
  // Manual category overrides for this floor: { [layerName]: category }.
  // The map is merged on top of the API-supplied auto classification so
  // the user can correct misclassifications without re-running anything.
  layerOverrides: Record<string, string>;
  onCategoryChange: (layer: string, category: string | null) => void;
  onNavigate: (coords: [number, number], sleeveId?: string | null) => void;
}

// One row per layer (not per entity).  Per-entity rows produced 18k+
// rows on a typical floor — overwhelmingly duplicate text values that
// drowned out the actually-actionable lines (sleeves, P-N, room names).
// Layer-level summary keeps the data tab navigable while still exposing
// every entity counted in the type breakdown.
interface LayerRow {
  id: string;             // unique key for React
  rawLayer: string;       // DXF layer name
  groupName: string;      // effective category (auto or overridden)
  overridden: boolean;    // user manually moved this layer's category
  totalCount: number;     // entities on this layer
  typeCounts: Record<string, number>;  // {LINE: 263, CIRCLE: 3}
  uniqueTexts: string[];  // up to 8 unique TEXT/MTEXT values for the preview
  uniqueTextTotal: number; // total unique text count (for "+N 種" suffix)
  sampleSleeve: ReturnType<typeof sleeveSummary> | null;  // first sleeve hit, if any
  navigate: [number, number] | null;  // first entity position
  sleeveId?: string;
}

// Display order for the useful groups — these are the categories that
// matter for sleeve checking + plan readability. Anything else lands in
// "不要" and is hidden by default behind a toggle.
//
// Must stay in sync with sleeve_checker/layer_classifier.py
// USEFUL_CATEGORIES (24 entries here = 23 backend + "図面ヘッダー" UI-only).
const GROUP_ORDER = [
  "図面ヘッダー",
  // 躯体・建築
  "通り芯",
  "躯体壁",
  "乾式壁",
  "耐火壁・防火区画",
  "柱・仕上線",
  "梁",
  "スラブ外形",
  "スラブラベル",
  "スラブFL",
  "段差線",
  "床ヌスミ",
  // 記号・テキスト
  "FL表記",
  "寸法線",
  "P-N番号",
  "部屋名",
  "水勾配",
  "機器コード_衛生",
  "機器コード_空調",
  "機器コード_電気",
  // スリーブ本体
  "スリーブ_衛生",
  "スリーブ_空調",
  "スリーブ_電気",
  "スリーブ_その他",
  "不要",
];

function groupOrderIdx(name: string): number {
  const i = GROUP_ORDER.indexOf(name);
  return i < 0 ? GROUP_ORDER.length + 1 : i;
}

const DISPLAY_LABEL: Record<string, string> = {
  スリーブ_衛生: "衛生スリーブ",
  スリーブ_空調: "空調スリーブ",
  スリーブ_電気: "電気スリーブ",
  スリーブ_その他: "その他スリーブ",
  機器コード_衛生: "機器コード（衛生）",
  機器コード_空調: "機器コード（空調）",
  機器コード_電気: "機器コード（電気）",
  "躯体壁": "躯体壁 (RC/PCa)",
  "乾式壁": "乾式壁 (ALC/LGS/CB等)",
  "耐火壁・防火区画": "耐火壁・防火区画",
};

function displayName(group: string): string {
  return DISPLAY_LABEL[group] ?? group;
}

// ---------------------------------------------------------------------------
// Build rows from the universal /api/all_entities payload + FloorData
// (FloorData is used to enrich Sleeve rows with discipline, FL, P-N etc.)
// ---------------------------------------------------------------------------

// Aggregate every entity belonging to one layer into a single LayerRow.
// `entities` is the slice of universal.entities that lives on this layer
// (post-discipline-override; the caller is responsible for routing each
// entity to the right category before grouping).
function buildLayerRow(
  layer: string,
  category: string,
  overridden: boolean,
  entities: UniversalEntity[],
  sleeveByPos: Map<string, ReturnType<typeof sleeveSummary>>,
): LayerRow {
  const typeCounts: Record<string, number> = {};
  const seenText = new Set<string>();
  const uniqueTexts: string[] = [];
  let firstNavigate: [number, number] | null = null;
  let sampleSleeve: ReturnType<typeof sleeveSummary> | null = null;
  let sleeveId: string | undefined;

  for (const e of entities) {
    typeCounts[e.type] = (typeCounts[e.type] ?? 0) + 1;

    // First entity with a position seeds the navigate target.
    if (!firstNavigate && e.pos) {
      firstNavigate = e.pos;
    }
    // Sleeve hit on this layer? Pull the rich summary for the preview.
    if (e.pos && !sampleSleeve) {
      const key = `${Math.round(e.pos[0])},${Math.round(e.pos[1])}`;
      const matched = sleeveByPos.get(key);
      if (matched) {
        sampleSleeve = matched;
        sleeveId = matched.id;
        firstNavigate = e.pos;
      }
    }
    // Collect unique TEXT/MTEXT contents and INSERT block names so the
    // user has a sense of what the layer carries without expanding it.
    if (e.type === "TEXT" || e.type === "MTEXT") {
      const txt = e.subtype?.trim();
      if (txt && !seenText.has(txt)) {
        seenText.add(txt);
        uniqueTexts.push(txt);
      }
    } else if (e.type === "INSERT" && e.subtype) {
      const bname = e.subtype.trim();
      if (bname && !seenText.has(bname)) {
        seenText.add(bname);
        uniqueTexts.push(bname);
      }
    } else if (e.type === "DIMENSION") {
      const m = e.props?.measurement;
      if (typeof m === "number" && m > 0) {
        const tag = `寸法 ${Math.round(m)}`;
        if (!seenText.has(tag)) {
          seenText.add(tag);
          uniqueTexts.push(tag);
        }
      }
    }
  }

  // 8 件まで表示、残りはサマリの末尾で件数表示。
  return {
    id: `layer:${layer}`,
    rawLayer: layer,
    groupName: category,
    overridden,
    totalCount: entities.length,
    typeCounts,
    uniqueTexts: uniqueTexts.slice(0, 8),
    uniqueTextTotal: uniqueTexts.length,
    sampleSleeve,
    navigate: firstNavigate,
    sleeveId,
  };
}

function sleeveSummary(s: FloorData["sleeves"][number]) {
  // Format follows the LABEL, not the drawn shape:
  //   round-pipe label (φXXX / XXXA / 外径XXX) → φ<diameter>
  //   no round-pipe label                       → □<width>×<height>
  //   horizontal sleeves prepend "横".
  // A rect-drawn horizontal sleeve with a φ label is a round pipe through
  // a wall (drawn as side-view rect) — show φ. A rect-drawn sleeve without
  // a φ label is a real rectangular opening (cable rack / box) — show □W×H.
  const isHorizontal = s.orientation === "horizontal";
  const isRect = s.shape === "rect";
  const labelCombined = `${s.label_text || ""} ${s.diameter_text || ""}`;
  const hasRoundPipeLabel = /(?:[φΦ]\s*\d+|\d+\s*[φΦ]|\d+\s*A\b|外径\s*\d+)/i.test(labelCombined);
  const dims = hasRoundPipeLabel
    ? `φ${Math.round(s.diameter)}`
    : (isRect && s.width && s.height
        ? `□${Math.round(s.width)}×${Math.round(s.height)}`
        : `φ${Math.round(s.diameter)}`);
  const size = isHorizontal ? `横${dims}` : dims;
  const props = [
    size,
    s.fl_text,
    s.discipline,
    s.pn_number ? `P-N${s.pn_number}` : null,
    s.label_text,
  ].filter(Boolean).join("  ");
  return { id: s.id, label: s.id, props };
}

// Override category for a sleeve INSERT/CIRCLE so it lands in the
// discipline-specific group regardless of where the layer-classifier put it.
function sleeveDisciplineCategory(discipline: string, layer: string): string {
  if (discipline === "衛生") return "スリーブ_衛生";
  if (discipline === "空調") return "スリーブ_空調";
  if (discipline === "電気") return "スリーブ_電気";
  if (/衛生/.test(layer)) return "スリーブ_衛生";
  if (/空調/.test(layer)) return "スリーブ_空調";
  if (/電気/.test(layer)) return "スリーブ_電気";
  return "スリーブ_その他";
}

// ---------------------------------------------------------------------------
// Category picker popover — opens from the "移動 ▾" button on each layer
// row. Lists every GROUP_ORDER bucket (including 不要) plus an "自動分類"
// reset entry when the row currently carries a manual override.
// ---------------------------------------------------------------------------

function CategoryPicker({
  currentCategory, overridden, onPick, onReset, onClose,
}: {
  currentCategory: string;
  overridden: boolean;
  onPick: (category: string) => void;
  onReset: () => void;
  onClose: () => void;
}) {
  // Close on outside click — attach once and tear down on unmount.
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const handler = (ev: MouseEvent) => {
      if (ref.current && !ref.current.contains(ev.target as Node)) {
        onClose();
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [onClose]);

  // 図面ヘッダー is a UI-only pseudo-category — not a valid bucket.
  const options = GROUP_ORDER.filter((c) => c !== "図面ヘッダー");

  return (
    <div
      ref={ref}
      style={{
        position: "absolute",
        top: "calc(100% + 4px)",
        right: 0,
        zIndex: 50,
        background: "#fff",
        border: "1px solid #e5e7eb",
        borderRadius: 6,
        boxShadow: "0 6px 24px rgba(0,0,0,0.12)",
        minWidth: 180,
        maxHeight: 320,
        overflowY: "auto",
        padding: 4,
      }}
    >
      {overridden && (
        <button
          onClick={(e) => { e.stopPropagation(); onReset(); }}
          style={{
            display: "block",
            width: "100%",
            textAlign: "left",
            padding: "5px 10px",
            fontSize: 11,
            border: "none",
            background: "transparent",
            color: "#b45309",
            cursor: "pointer",
            borderRadius: 4,
            borderBottom: "1px solid #f3f4f6",
            marginBottom: 2,
          }}
          onMouseEnter={(e) => (e.currentTarget.style.background = "#fef3c7")}
          onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}
        >
          ← 自動分類に戻す
        </button>
      )}
      {options.map((cat) => {
        const isCurrent = cat === currentCategory;
        return (
          <button
            key={cat}
            onClick={(e) => { e.stopPropagation(); onPick(cat); }}
            style={{
              display: "block",
              width: "100%",
              textAlign: "left",
              padding: "5px 10px",
              fontSize: 11,
              border: "none",
              background: isCurrent ? "#f3f4f6" : "transparent",
              color: isCurrent ? "#111827" : "#374151",
              cursor: "pointer",
              borderRadius: 4,
              fontWeight: isCurrent ? 600 : 400,
            }}
            onMouseEnter={(e) => {
              if (!isCurrent) e.currentTarget.style.background = "#fafafa";
            }}
            onMouseLeave={(e) => {
              if (!isCurrent) e.currentTarget.style.background = "transparent";
            }}
          >
            {displayName(cat)}{isCurrent ? "  ✓" : ""}
          </button>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function DataExplorer({
  floorData, floorId, layerOverrides, onCategoryChange, onNavigate,
}: Props) {
  const { data: universal, loading } = useUniversalEntities(floorId);
  const [query, setQuery] = useState("");
  const [openGroups, setOpenGroups] = useState<Set<string>>(new Set());
  const [showHidden, setShowHidden] = useState(false);
  // Which layer's category-picker popover is open. `null` = none.
  const [pickerLayer, setPickerLayer] = useState<string | null>(null);

  // Sleeve lookup by rounded coordinate so we can swap raw labels for
  // the discipline-aware sleeve summary.
  const sleeveByPos = useMemo(() => {
    const m = new Map<string, ReturnType<typeof sleeveSummary>>();
    for (const s of floorData.sleeves) {
      const key = `${Math.round(s.center[0])},${Math.round(s.center[1])}`;
      m.set(key, sleeveSummary(s));
    }
    return m;
  }, [floorData]);

  // Sleeve handle → discipline category, used to override layer-based
  // categorisation for sleeve entities.
  const sleeveDisciplineByPos = useMemo(() => {
    const m = new Map<string, string>();
    for (const s of floorData.sleeves) {
      const key = `${Math.round(s.center[0])},${Math.round(s.center[1])}`;
      m.set(key, sleeveDisciplineCategory(s.discipline, s.layer));
    }
    return m;
  }, [floorData]);

  // Header pseudo-rows for the 図面ヘッダー card.  These don't represent
  // a real layer — they surface header metadata (version, units, bbox,
  // saved by) at the top of the data tab.
  type HeaderRow = { kind: "header"; id: string; label: string; value: string };
  const headerRows = useMemo<HeaderRow[]>(() => {
    if (!universal) return [];
    const hdr = universal.summary.header || {};
    const rows: HeaderRow[] = [];
    if (hdr.version) rows.push({ kind: "header", id: "hdr-version", label: "DXF バージョン", value: String(hdr.version) });
    if (hdr.insunits !== undefined && hdr.insunits !== null) {
      rows.push({ kind: "header", id: "hdr-units", label: "単位", value: hdr.insunits === 4 ? "mm" : `INSUNITS=${hdr.insunits}` });
    }
    if (Array.isArray(hdr.extmin) && Array.isArray(hdr.extmax)) {
      rows.push({
        kind: "header", id: "hdr-bbox", label: "図面範囲",
        value: `${hdr.extmin.slice(0, 2).map((n: number) => Math.round(n)).join(", ")} 〜 ${hdr.extmax.slice(0, 2).map((n: number) => Math.round(n)).join(", ")}`,
      });
    }
    if (hdr.saved_by) rows.push({ kind: "header", id: "hdr-savedby", label: "最終保存者", value: String(hdr.saved_by) });
    if (universal.summary.entity_count !== undefined) {
      rows.push({
        kind: "header", id: "hdr-count", label: "総エンティティ数",
        value: `${universal.summary.entity_count} 個 (${universal.summary.layer_count} レイヤー)`,
      });
    }
    return rows;
  }, [universal]);

  const layerRows = useMemo<LayerRow[]>(() => {
    if (!universal) return [];
    const cats = universal.layer_categories;

    // Group entities by (category, layer).  The category is per-entity,
    // not per-layer, because sleeve INSERTs/CIRCLEs can override into
    // their discipline-specific category even when the layer LLM-classified
    // differently. A user-supplied override outranks both — when present,
    // every entity on that layer lands in the chosen bucket regardless of
    // its type / sleeve-discipline routing.
    const grouped = new Map<string, UniversalEntity[]>();   // key = "<cat>\x1f<layer>"
    const SEP = "\x1f";
    for (const e of universal.entities) {
      // Skip children folded into a parent INSERT row.
      if (e.props && e.props.parent_handle) continue;
      const manual = layerOverrides[e.layer];
      let cat: string;
      if (manual) {
        cat = manual;
      } else {
        const baseCat = cats[e.layer] || "不要";
        const posKey = e.pos ? `${Math.round(e.pos[0])},${Math.round(e.pos[1])}` : "";
        const sleeveCat = sleeveDisciplineByPos.get(posKey);
        cat = sleeveCat && (e.type === "INSERT" || e.type === "CIRCLE") ? sleeveCat : baseCat;
      }
      const key = `${cat}${SEP}${e.layer}`;
      let bucket = grouped.get(key);
      if (!bucket) { bucket = []; grouped.set(key, bucket); }
      bucket.push(e);
    }

    const rows: LayerRow[] = [];
    for (const [key, ents] of grouped) {
      const sep = key.indexOf(SEP);
      const cat = key.slice(0, sep);
      const layer = key.slice(sep + 1);
      const overridden = layerOverrides[layer] !== undefined;
      rows.push(buildLayerRow(layer, cat, overridden, ents, sleeveByPos));
    }
    return rows;
  }, [universal, sleeveByPos, sleeveDisciplineByPos, layerOverrides]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    let pool = layerRows;
    if (!showHidden) {
      pool = pool.filter(r => r.groupName !== "不要");
    }
    if (!q) return pool;
    return pool.filter(r =>
      r.rawLayer.toLowerCase().includes(q) ||
      r.groupName.toLowerCase().includes(q) ||
      r.uniqueTexts.some(t => t.toLowerCase().includes(q)) ||
      Object.keys(r.typeCounts).some(t => t.toLowerCase().includes(q))
    );
  }, [layerRows, query, showHidden]);

  // Count hidden ENTITIES (sum, not layer count) so the toggle shows how
  // many actual entities are tucked into 不要.
  const hiddenCount = useMemo(
    () => layerRows.filter(r => r.groupName === "不要").reduce((s, r) => s + r.totalCount, 0),
    [layerRows],
  );

  const grouped = useMemo(() => {
    type G = { name: string; layers: LayerRow[]; entityTotal: number };
    const m = new Map<string, G>();
    // Pre-seed every category so empty buckets (e.g. 耐火壁・防火区画 on a
    // drawing without firewall layers) still appear as a card.
    if (!query.trim()) {
      for (const name of GROUP_ORDER) {
        if (name === "不要" && !showHidden) continue;
        if (name === "図面ヘッダー") continue; // header card uses a separate path below
        m.set(name, { name, layers: [], entityTotal: 0 });
      }
    }
    for (const r of filtered) {
      let g = m.get(r.groupName);
      if (!g) {
        g = { name: r.groupName, layers: [], entityTotal: 0 };
        m.set(r.groupName, g);
      }
      g.layers.push(r);
      g.entityTotal += r.totalCount;
    }
    // Layers within each group: descending by entity count.
    for (const g of m.values()) {
      g.layers.sort((a, b) => b.totalCount - a.totalCount);
    }
    return [...m.values()].sort((a, b) => {
      const d = groupOrderIdx(a.name) - groupOrderIdx(b.name);
      if (d !== 0) return d;
      return a.name.localeCompare(b.name, "ja");
    });
  }, [filtered, query, showHidden]);

  const autoExpand = query.trim().length > 0;

  const toggleGroup = (name: string) => {
    setOpenGroups(p => {
      const next = new Set(p);
      if (next.has(name)) next.delete(name); else next.add(name);
      return next;
    });
  };

  return (
    <div style={{
      padding: "24px 28px 0",
      background: "#fff",
      height: "100%",
      display: "flex",
      flexDirection: "column",
      fontSize: 13,
      color: "#111827",
    }}>
      <div style={{ display: "flex", gap: 10, marginBottom: 18, alignItems: "center" }}>
        <input
          value={query}
          onChange={e => setQuery(e.target.value)}
          placeholder="検索 (レイヤー名・テキスト・タイプ)"
          style={{
            flex: 1,
            padding: "7px 14px",
            fontSize: 13,
            border: "none",
            borderRadius: 6,
            background: "#f5f5f7",
            outline: "none",
            color: "#111827",
          }}
        />
        {hiddenCount > 0 && (
          <button
            onClick={() => setShowHidden(s => !s)}
            style={{
              padding: "7px 12px",
              fontSize: 11,
              border: "none",
              borderRadius: 6,
              background: showHidden ? "#374151" : "#f5f5f7",
              color: showHidden ? "#fff" : "#6b7280",
              cursor: "pointer",
              whiteSpace: "nowrap",
            }}
          >
            {showHidden ? "不要を隠す" : `不要を表示 (${hiddenCount})`}
          </button>
        )}
        {loading && <span style={{ fontSize: 11, color: "#9ca3af" }}>読込中…</span>}
        {universal && (
          <span style={{ fontSize: 11, color: "#9ca3af", fontVariantNumeric: "tabular-nums" }}>
            {universal.summary.entity_count.toLocaleString()} 件
          </span>
        )}
      </div>

      <div style={{
        display: "flex",
        alignItems: "baseline",
        gap: 14,
        padding: "8px 4px 8px 46px",
        fontSize: 10,
        color: "#9ca3af",
        textTransform: "uppercase",
        letterSpacing: 0.6,
        borderBottom: "1px solid #f3f4f6",
        background: "#fafafa",
      }}>
        <span style={{ width: 220, flexShrink: 0 }}>レイヤー</span>
        <span style={{ width: 70, flexShrink: 0, textAlign: "right" }}>件数</span>
        <span style={{ flex: 1, paddingLeft: 14 }}>内訳・主要値</span>
      </div>

      <div style={{ flex: 1, overflow: "auto", marginLeft: -4 }}>
        {!universal && !loading && (
          <div style={{ color: "#9ca3af", textAlign: "center", marginTop: 60, fontSize: 13 }}>
            図面を選択してください
          </div>
        )}
        {universal && grouped.length === 0 && headerRows.length === 0 && (
          <div style={{ color: "#9ca3af", textAlign: "center", marginTop: 60, fontSize: 13 }}>
            該当するデータがありません
          </div>
        )}

        {/* 図面ヘッダーカード */}
        {!query.trim() && headerRows.length > 0 && (
          <div style={{ borderBottom: "1px solid #f3f4f6" }}>
            <div
              onClick={() => toggleGroup("図面ヘッダー")}
              style={{
                padding: "10px 4px",
                cursor: "pointer",
                display: "flex",
                alignItems: "flex-start",
                gap: 10,
                userSelect: "none",
              }}
            >
              <span style={{ color: "#c4c4c8", fontSize: 9, width: 10, textAlign: "center", marginTop: 4 }}>
                {openGroups.has("図面ヘッダー") || autoExpand ? "⌄" : "›"}
              </span>
              <span style={{ flex: 1, fontWeight: 500, color: "#111827", letterSpacing: -0.1 }}>
                図面ヘッダー
              </span>
              <span style={{ color: "#9ca3af", fontSize: 12, fontVariantNumeric: "tabular-nums" }}>
                {headerRows.length}
              </span>
            </div>
            {(openGroups.has("図面ヘッダー") || autoExpand) && (
              <div style={{ paddingBottom: 6 }}>
                {headerRows.map(h => (
                  <div key={h.id} style={{
                    padding: "5px 4px 5px 32px",
                    display: "flex",
                    alignItems: "baseline",
                    gap: 14,
                    fontSize: 12,
                  }}>
                    <span style={{ color: "#374151", fontWeight: 500, minWidth: 220, flexShrink: 0 }}>
                      {h.label}
                    </span>
                    <span style={{ color: "#9ca3af", flex: 1 }}>{h.value}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {grouped.map((g, gi) => {
          const isOpen = openGroups.has(g.name) || autoExpand;
          return (
            <div key={g.name} style={gi > 0 || headerRows.length > 0 ? { borderTop: "1px solid #f3f4f6" } : undefined}>
              <div
                onClick={() => toggleGroup(g.name)}
                style={{
                  padding: "10px 4px",
                  cursor: "pointer",
                  display: "flex",
                  alignItems: "flex-start",
                  gap: 10,
                  borderRadius: 4,
                  userSelect: "none",
                  transition: "background 80ms",
                }}
                onMouseEnter={(e) => (e.currentTarget.style.background = "#fafafa")}
                onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}
              >
                <span style={{
                  color: "#c4c4c8",
                  fontSize: 9,
                  width: 10,
                  display: "inline-block",
                  textAlign: "center",
                  marginTop: 4,
                }}>
                  {isOpen ? "⌄" : "›"}
                </span>
                <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", gap: 2 }}>
                  <span style={{ fontWeight: 500, color: "#111827", letterSpacing: -0.1 }}>
                    {displayName(g.name)}
                  </span>
                  <span style={{ fontSize: 10.5, color: "#9ca3af" }}>
                    {g.layers.length} レイヤー / {g.entityTotal.toLocaleString()} 件
                  </span>
                </div>
              </div>
              {isOpen && g.layers.length > 0 && (
                <div style={{ paddingBottom: 6 }}>
                  {g.layers.map(r => {
                    const types = Object.entries(r.typeCounts).sort((a, b) => b[1] - a[1]);
                    const typesStr = types.map(([t, c]) => `${t}:${c}`).join(", ");
                    const sampleStr = r.uniqueTexts.length > 0
                      ? r.uniqueTexts.join(" / ")
                          + (r.uniqueTextTotal > r.uniqueTexts.length ? ` …+${r.uniqueTextTotal - r.uniqueTexts.length}種` : "")
                      : "";
                    const isPickerOpen = pickerLayer === r.rawLayer;
                    return (
                      <div
                        key={r.id}
                        onClick={() => r.navigate && onNavigate(r.navigate, r.sleeveId)}
                        style={{
                          padding: "6px 4px 6px 32px",
                          cursor: r.navigate ? "pointer" : "default",
                          display: "flex",
                          alignItems: "flex-start",
                          gap: 14,
                          fontSize: 12,
                          borderRadius: 4,
                          transition: "background 80ms",
                          position: "relative",
                        }}
                        onMouseEnter={(e) => (e.currentTarget.style.background = "#fafafa")}
                        onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}
                      >
                        <span style={{
                          color: "#374151",
                          width: 220,
                          flexShrink: 0,
                          whiteSpace: "pre-wrap",
                          wordBreak: "break-all",
                          overflowWrap: "anywhere",
                          fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
                          fontSize: 11,
                          display: "flex",
                          flexDirection: "column",
                          gap: 2,
                        }}>
                          <span>{r.rawLayer}</span>
                          {r.overridden && (
                            <span
                              onClick={(e) => {
                                e.stopPropagation();
                                onCategoryChange(r.rawLayer, null);
                              }}
                              style={{
                                fontSize: 9,
                                color: "#b45309",
                                background: "#fef3c7",
                                padding: "1px 6px",
                                borderRadius: 3,
                                fontFamily: "'Inter','Noto Sans JP',sans-serif",
                                fontWeight: 500,
                                cursor: "pointer",
                                alignSelf: "flex-start",
                              }}
                              title="自動分類に戻す"
                            >
                              手動 ✕
                            </span>
                          )}
                        </span>
                        <span style={{
                          color: "#111827",
                          width: 70,
                          flexShrink: 0,
                          textAlign: "right",
                          fontVariantNumeric: "tabular-nums",
                          fontWeight: 500,
                        }}>
                          {r.totalCount.toLocaleString()}
                        </span>
                        <div style={{
                          flex: 1,
                          minWidth: 0,
                          paddingLeft: 14,
                          display: "flex",
                          flexDirection: "column",
                          gap: 2,
                        }}>
                          <span style={{ color: "#6b7280", fontSize: 11 }}>
                            {typesStr}
                          </span>
                          {sampleStr && (
                            <span style={{
                              color: "#9ca3af",
                              fontSize: 11,
                              whiteSpace: "pre-wrap",
                              wordBreak: "break-all",
                              overflowWrap: "anywhere",
                            }}>
                              {sampleStr}
                            </span>
                          )}
                        </div>
                        <div
                          onClick={(e) => e.stopPropagation()}
                          style={{ position: "relative", flexShrink: 0 }}
                        >
                          <button
                            onClick={(e) => {
                              e.stopPropagation();
                              setPickerLayer(isPickerOpen ? null : r.rawLayer);
                            }}
                            style={{
                              border: "1px solid #e5e7eb",
                              background: isPickerOpen ? "#f3f4f6" : "#fff",
                              color: "#6b7280",
                              borderRadius: 4,
                              padding: "2px 8px",
                              fontSize: 10,
                              cursor: "pointer",
                              whiteSpace: "nowrap",
                            }}
                            title="カテゴリーを変更"
                          >
                            移動 ▾
                          </button>
                          {isPickerOpen && (
                            <CategoryPicker
                              currentCategory={r.groupName}
                              overridden={r.overridden}
                              onPick={(cat) => {
                                onCategoryChange(r.rawLayer, cat);
                                setPickerLayer(null);
                              }}
                              onReset={() => {
                                onCategoryChange(r.rawLayer, null);
                                setPickerLayer(null);
                              }}
                              onClose={() => setPickerLayer(null)}
                            />
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
              {isOpen && g.layers.length === 0 && (
                <div style={{
                  padding: "6px 4px 10px 32px",
                  color: "#9ca3af",
                  fontSize: 11,
                  fontStyle: "italic",
                }}>
                  該当レイヤーなし
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
