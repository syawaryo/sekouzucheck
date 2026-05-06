import { useMemo, useState } from "react";
import type { FloorData } from "../types";
import type { UniversalEntity } from "../api";
import { useUniversalEntities } from "../useUniversalEntities";

interface Props {
  floorData: FloorData;
  floorId: string | null;
  onNavigate: (coords: [number, number], sleeveId?: string | null) => void;
}

interface EntityRow {
  id: string;
  type: string;       // raw DXF/IFC type
  rawLayer: string;
  groupName: string;  // normalized category
  label: string;      // primary display value
  properties: string; // secondary display
  navigate: [number, number] | null;
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
  "外壁",
  "内壁",
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
  "耐火壁・防火区画": "耐火壁・防火区画",
};

function displayName(group: string): string {
  return DISPLAY_LABEL[group] ?? group;
}

// ---------------------------------------------------------------------------
// Build rows from the universal /api/all_entities payload + FloorData
// (FloorData is used to enrich Sleeve rows with discipline, FL, P-N etc.)
// ---------------------------------------------------------------------------

function rowFromEntity(
  e: UniversalEntity,
  category: string,
  sleeveById: Map<string, ReturnType<typeof sleeveSummary>>,
): EntityRow {
  // Try to attach to a sleeve when this row is the INSERT/CIRCLE that
  // represents one. We match on rounded coordinate so identifiers don't
  // need to be stable across parses.
  const sleeveKey = e.pos ? `${Math.round(e.pos[0])},${Math.round(e.pos[1])}` : "";
  const matchedSleeve = sleeveKey ? sleeveById.get(sleeveKey) : undefined;

  let label = e.subtype || e.type;
  let properties = "";

  if (matchedSleeve) {
    label = matchedSleeve.label;
    properties = matchedSleeve.props;
  } else if (e.type === "TEXT" || e.type === "MTEXT") {
    label = e.subtype || "(空)";
    properties = e.props.height ? `H=${Math.round(e.props.height)}` : "";
  } else if (e.type === "INSERT") {
    // Prefer the INSERT's "inner_texts" (ATTRIB values + decomposed in-block
    // TEXT/MTEXT) for the row label, so a symbol+value composite like
    // an FL block shows up as "FL ブロック → -565" instead of just the
    // block name with the value floating off as a separate child row.
    const innerTexts = e.props.inner_texts;
    const blockName = e.subtype || "(無名ブロック)";
    if (Array.isArray(innerTexts) && innerTexts.length > 0) {
      const joined = innerTexts.slice(0, 4).join(" / ");
      const more = innerTexts.length > 4 ? ` …+${innerTexts.length - 4}` : "";
      label = `${blockName} → ${joined}${more}`;
    } else {
      label = blockName;
    }
    const inner = e.props.block_inner;
    if (inner && typeof inner === "object") {
      const total = Object.values(inner).reduce<number>((s, v) => s + Number(v), 0);
      properties = `中身 ${total} 件 (${Object.entries(inner).map(([k, v]) => `${k}:${v}`).join(", ")})`;
    }
  } else if (e.type === "LINE") {
    const start = e.props.start;
    const end = e.props.end;
    if (Array.isArray(start) && Array.isArray(end)) {
      const len = Math.round(Math.hypot(end[0] - start[0], end[1] - start[1]));
      properties = `長さ ${len}`;
    }
  } else if (e.type === "CIRCLE") {
    properties = `r=${Math.round(e.props.radius || 0)}`;
  } else if (e.type === "DIMENSION") {
    label = `寸法 ${Math.round(e.props.measurement || 0)}`;
    properties = e.props.text || "";
  }

  return {
    id: `${e.type}-${e.handle}`,
    type: e.type,
    rawLayer: e.layer,
    groupName: category,
    label,
    properties,
    navigate: e.pos,
    sleeveId: matchedSleeve?.id,
  };
}

function sleeveSummary(s: FloorData["sleeves"][number]) {
  const isHorizontal = s.orientation === "horizontal";
  const size = isHorizontal
    ? `横φ${Math.round(s.diameter)}`
    : s.shape === "rect"
      ? `□${Math.round(s.width ?? 0)}×${Math.round(s.height ?? 0)}`
      : `φ${Math.round(s.diameter)}`;
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
// Component
// ---------------------------------------------------------------------------

export default function DataExplorer({ floorData, floorId, onNavigate }: Props) {
  const { data: universal, loading } = useUniversalEntities(floorId);
  const [query, setQuery] = useState("");
  const [openGroups, setOpenGroups] = useState<Set<string>>(new Set());
  const [showHidden, setShowHidden] = useState(false);

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

  const rows = useMemo<EntityRow[]>(() => {
    if (!universal) return [];
    const cats = universal.layer_categories;
    const out: EntityRow[] = [];

    // Synthetic header rows from $EXTMIN/EXTMAX, units, version
    const hdr = universal.summary.header || {};
    if (hdr.version) {
      out.push({
        id: "hdr-version", type: "HEADER", rawLayer: "",
        groupName: "図面ヘッダー", label: "DXF バージョン",
        properties: String(hdr.version), navigate: null,
      });
    }
    if (hdr.insunits !== undefined && hdr.insunits !== null) {
      out.push({
        id: "hdr-units", type: "HEADER", rawLayer: "",
        groupName: "図面ヘッダー", label: "単位",
        properties: hdr.insunits === 4 ? "mm" : `INSUNITS=${hdr.insunits}`, navigate: null,
      });
    }
    if (Array.isArray(hdr.extmin) && Array.isArray(hdr.extmax)) {
      out.push({
        id: "hdr-bbox", type: "HEADER", rawLayer: "",
        groupName: "図面ヘッダー", label: "図面範囲",
        properties: `${hdr.extmin.slice(0, 2).map((n: number) => Math.round(n)).join(", ")} 〜 ${hdr.extmax.slice(0, 2).map((n: number) => Math.round(n)).join(", ")}`,
        navigate: null,
      });
    }
    if (hdr.saved_by) {
      out.push({
        id: "hdr-savedby", type: "HEADER", rawLayer: "",
        groupName: "図面ヘッダー", label: "最終保存者",
        properties: String(hdr.saved_by), navigate: null,
      });
    }
    if (universal.summary.entity_count !== undefined) {
      out.push({
        id: "hdr-count", type: "HEADER", rawLayer: "",
        groupName: "図面ヘッダー", label: "総エンティティ数",
        properties: `${universal.summary.entity_count} 個 (${universal.summary.layer_count} レイヤー)`,
        navigate: null,
      });
    }

    // Entity rows
    for (const e of universal.entities) {
      // Skip children that have been folded into a parent INSERT row.
      // The parent's `inner_texts` already carries the value (e.g.
      // "FL ブロック → -565"), so emitting the child text/ATTRIB as
      // an extra row would just duplicate it.
      if (e.props && e.props.parent_handle) continue;

      const baseCat = cats[e.layer] || "不要";

      // Sleeve override: if this is the INSERT/CIRCLE at a sleeve's
      // position, route to the discipline category and use the rich label.
      const posKey = e.pos ? `${Math.round(e.pos[0])},${Math.round(e.pos[1])}` : "";
      const sleeveCat = sleeveDisciplineByPos.get(posKey);
      const cat = sleeveCat && (e.type === "INSERT" || e.type === "CIRCLE") ? sleeveCat : baseCat;

      out.push(rowFromEntity(e, cat, sleeveByPos));
    }
    return out;
  }, [universal, sleeveByPos, sleeveDisciplineByPos]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    let pool = rows;
    if (!showHidden) {
      pool = pool.filter(r => r.groupName !== "不要");
    }
    if (!q) return pool;
    return pool.filter(r =>
      r.label.toLowerCase().includes(q) ||
      r.rawLayer.toLowerCase().includes(q) ||
      r.groupName.toLowerCase().includes(q) ||
      r.properties.toLowerCase().includes(q) ||
      r.type.toLowerCase().includes(q)
    );
  }, [rows, query, showHidden]);

  // Count hidden so the toggle shows how many are stashed.
  const hiddenCount = useMemo(
    () => rows.filter(r => r.groupName === "不要").length,
    [rows],
  );

  const grouped = useMemo(() => {
    type G = { name: string; rawLayers: string[]; entities: EntityRow[] };
    const m = new Map<string, G>();
    for (const r of filtered) {
      let g = m.get(r.groupName);
      if (!g) {
        g = { name: r.groupName, rawLayers: [], entities: [] };
        m.set(r.groupName, g);
      }
      g.entities.push(r);
      if (r.rawLayer && !g.rawLayers.includes(r.rawLayer)) g.rawLayers.push(r.rawLayer);
    }
    return [...m.values()].sort((a, b) => {
      const d = groupOrderIdx(a.name) - groupOrderIdx(b.name);
      if (d !== 0) return d;
      return a.name.localeCompare(b.name, "ja");
    });
  }, [filtered]);

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
        <span style={{ width: 70, flexShrink: 0 }}>タイプ</span>
        <span style={{ minWidth: 140, flexShrink: 0 }}>名前</span>
        <span style={{ width: 200, flexShrink: 0 }}>レイヤー</span>
        <span style={{ flex: 1 }}>プロパティ</span>
      </div>

      <div style={{ flex: 1, overflow: "auto", marginLeft: -4 }}>
        {!universal && !loading && (
          <div style={{ color: "#9ca3af", textAlign: "center", marginTop: 60, fontSize: 13 }}>
            図面を選択してください
          </div>
        )}
        {universal && grouped.length === 0 && (
          <div style={{ color: "#9ca3af", textAlign: "center", marginTop: 60, fontSize: 13 }}>
            該当するデータがありません
          </div>
        )}
        {grouped.map((g, gi) => {
          const isOpen = openGroups.has(g.name) || autoExpand;
          // Useful (= not 不要) groups always render every entity — these
          // are the ones the inspector actually cares about, so we never
          // hide rows behind a "+N more". Only the noisy "不要" bucket is
          // capped, since it can hold thousands of decorative shapes that
          // would otherwise freeze the browser.
          const isUnneeded = g.name === "不要";
          const MAX_RENDER = isUnneeded ? 500 : Number.POSITIVE_INFINITY;
          const visible = isUnneeded
            ? g.entities.slice(0, MAX_RENDER)
            : g.entities;
          const truncated = isUnneeded
            ? Math.max(0, g.entities.length - visible.length)
            : 0;
          return (
            <div key={g.name} style={gi > 0 ? { borderTop: "1px solid #f3f4f6" } : undefined}>
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
                  {g.rawLayers.length > 0 && (
                    <span style={{
                      fontSize: 10.5,
                      color: "#9ca3af",
                      letterSpacing: 0.1,
                      whiteSpace: "pre-wrap",
                      wordBreak: "break-all",
                      overflowWrap: "anywhere",
                      minWidth: 0,
                    }}>
                      {g.rawLayers.join(" · ")}
                    </span>
                  )}
                </div>
                <span style={{
                  color: "#9ca3af",
                  fontSize: 12,
                  fontVariantNumeric: "tabular-nums",
                  marginTop: 2,
                }}>
                  {g.entities.length}
                </span>
              </div>
              {isOpen && (
                <div style={{ paddingBottom: 6 }}>
                  {visible.map(r => (
                    <div
                      key={r.id}
                      onClick={() => r.navigate && onNavigate(r.navigate, r.sleeveId)}
                      style={{
                        padding: "5px 4px 5px 32px",
                        cursor: r.navigate ? "pointer" : "default",
                        display: "flex",
                        alignItems: "baseline",
                        gap: 14,
                        fontSize: 12,
                        borderRadius: 4,
                        transition: "background 80ms",
                      }}
                      onMouseEnter={(e) => (e.currentTarget.style.background = "#fafafa")}
                      onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}
                    >
                      <span style={{ color: "#9ca3af", width: 70, flexShrink: 0, fontSize: 11 }}>
                        {r.type}
                      </span>
                      <span style={{ color: "#374151", fontWeight: 500, minWidth: 140, flexShrink: 0 }}>
                        {r.label}
                      </span>
                      <span style={{
                        color: r.rawLayer ? "#6b7280" : "#d1d5db",
                        fontSize: 10.5,
                        letterSpacing: 0.1,
                        width: 200,
                        flexShrink: 0,
                        whiteSpace: "pre-wrap",
                        wordBreak: "break-all",
                        overflowWrap: "anywhere",
                        fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
                      }}>
                        {r.rawLayer || "—"}
                      </span>
                      <span style={{
                        color: "#9ca3af",
                        flex: 1,
                        whiteSpace: "nowrap",
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                      }}>
                        {r.properties}
                      </span>
                    </div>
                  ))}
                  {truncated > 0 && (
                    <div style={{
                      padding: "6px 4px 6px 32px",
                      color: "#9ca3af",
                      fontSize: 11,
                      fontStyle: "italic",
                    }}>
                      …他 {truncated} 件は検索で絞り込んでください
                    </div>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
