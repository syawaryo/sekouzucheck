import type { Sleeve, CheckResult, SlabLabel } from "../types";

interface Props {
  sleeve: Sleeve;
  results: CheckResult[];
  slabLabels?: SlabLabel[];
}

// Parse sleeve FL annotation like "FL+40", "FL-360", "FL±0" into an integer mm.
function parseFlValue(flText: string | null | undefined): number | null {
  if (!flText) return null;
  const m = flText.match(/FL\s*([±+\-])?\s*(\d+)/i);
  if (!m) return null;
  const sign = m[1] || "+";
  const n = parseInt(m[2], 10);
  if (sign === "-") return -n;
  if (sign === "±") return 0;
  return n;
}

// Parse SlabLabel.level. Returns [low, high] mm range — single-value labels
// like "-60" return [-60, -60]; sloped labels like "-545～-600" return the
// proper range. null means unparseable (e.g. empty).
function parseSlabLevel(level: string): [number, number] | null {
  if (!level) return null;
  const range = level.match(/^([+\-])?\s*(\d+)\s*[～~〜]\s*([+\-])?\s*(\d+)$/);
  if (range) {
    const a = (range[1] === "-" ? -1 : 1) * parseInt(range[2], 10);
    const b = (range[3] === "-" ? -1 : 1) * parseInt(range[4], 10);
    return [Math.min(a, b), Math.max(a, b)];
  }
  const single = level.match(/^([+\-])?\s*(\d+)$/);
  if (single) {
    const v = (single[1] === "-" ? -1 : 1) * parseInt(single[2], 10);
    return [v, v];
  }
  return null;
}

function nearestSlabLabel(sleeve: Sleeve, slabLabels: SlabLabel[]): SlabLabel | null {
  if (!slabLabels.length) return null;
  const [sx, sy] = sleeve.center;
  let best: SlabLabel | null = null;
  let bestD = Infinity;
  for (const sl of slabLabels) {
    const dx = sl.x - sx;
    const dy = sl.y - sy;
    const d = dx * dx + dy * dy;
    if (d < bestD) { bestD = d; best = sl; }
  }
  return best;
}

function formatOffset(prefix: string, value: number): string {
  if (value === 0) return `${prefix}±0`;
  return `${prefix}${value > 0 ? "+" : ""}${value}`;
}

// Compute the sleeve height relative to the nearest slab's top surface.
// Returns the display string ("SL+100", "SL+625～+685") or null if it can't
// be computed (missing FL on the sleeve, or no parseable slab nearby).
function computeSlabTopText(sleeve: Sleeve, slabLabels: SlabLabel[]): string | null {
  const flValue = parseFlValue(sleeve.fl_text);
  if (flValue === null) return null;
  const slab = nearestSlabLabel(sleeve, slabLabels);
  if (!slab) return null;
  const lvl = parseSlabLevel(slab.level);
  if (!lvl) return null;
  const [lo, hi] = lvl;
  if (lo === hi) return formatOffset("SL", flValue - lo);
  // Sloped slab: present as a range. The smaller slab-top level (more
  // negative) yields the larger sleeve-above-slab offset, so swap ends.
  const a = flValue - hi;
  const b = flValue - lo;
  return `SL${a >= 0 ? "+" : ""}${a}～${b >= 0 ? "+" : ""}${b}`;
}

function renderResultCard(r: CheckResult, prefix: string, i: number) {
  const isNg = r.severity === "NG";
  const palette = isNg
    ? { bg: "#fef2f2", border: "#ef4444", id: "#dc2626", title: "#991b1b" }
    : { bg: "#fffbeb", border: "#fbbf24", id: "#d97706", title: "#92400e" };
  const hasStructured = !!(r.target || r.rule || r.expected || r.found || r.fix_hint);

  const Row = ({ label, value, accent }: { label: string; value: string; accent?: boolean }) => (
    <>
      <div style={{ color: "#9ca3af", fontSize: 10, fontWeight: 500 }}>{label}</div>
      <div style={{ color: accent ? palette.id : "#374151", fontSize: 11, fontWeight: accent ? 600 : 400, lineHeight: 1.45 }}>{value}</div>
    </>
  );

  return (
    <div key={`${prefix}${i}`} style={{ background: palette.bg, borderLeft: `3px solid ${palette.border}`, padding: "8px 10px", borderRadius: "0 6px 6px 0", marginBottom: 6 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: hasStructured ? 6 : 3 }}>
        <span style={{ color: palette.id, fontWeight: 700, fontSize: 11 }}>#{r.check_id}</span>
        <span style={{ color: palette.title, fontSize: 12, fontWeight: 600 }}>{r.check_name}</span>
      </div>
      {hasStructured ? (
        <div style={{ display: "grid", gridTemplateColumns: "60px 1fr", rowGap: 3, columnGap: 8 }}>
          {r.target && <Row label="対象" value={r.target} />}
          {r.rule && <Row label="基準" value={r.rule} />}
          {r.expected && <Row label="期待" value={r.expected} />}
          {r.found && <Row label="検出" value={r.found} accent />}
          {r.fix_hint && <Row label="対応" value={r.fix_hint} />}
        </div>
      ) : (
        <div style={{ color: "#6b7280", fontSize: 11 }}>{r.message}</div>
      )}
    </div>
  );
}

export default function SleeveInfo({ sleeve, results, slabLabels = [] }: Props) {
  const sleeveResults = results.filter((r) => r.sleeve_id === sleeve.id);
  const ngResults = sleeveResults.filter((r) => r.severity === "NG");
  const warnResults = sleeveResults.filter((r) => r.severity === "WARNING");
  const okCount = sleeveResults.filter((r) => r.severity === "OK").length;

  // Extract only the leading alphanumeric discipline code from label_text (e.g. "G(低) 225φ" → "G")
  const disciplineCode = sleeve.label_text?.match(/^[A-Za-z0-9]+/)?.[0] || null;

  const SLEEVE_TYPE_LABEL: Record<string, string> = {
    duct: "ダクト",
    pipe: "配管",
    cable: "電気",
  };
  const typeLabel = sleeve.sleeve_type ? SLEEVE_TYPE_LABEL[sleeve.sleeve_type] : null;
  const isHorizontal = sleeve.orientation === "horizontal";
  const isRect = sleeve.shape === "rect";
  // Display rule:
  //   round-pipe label (φXXX / XXXA / 外径XXX) → size as φ<diameter>
  //   no round-pipe label                       → size as W×H
  //   "横" prefix for horizontal, else "角" for rect / "丸" for round
  // Distinguishing the two rect cases by LABEL, not by shape:
  //   - horizontal round pipe drawn as long rect with φ label → "横 φX"
  //   - cable-rack / box opening drawn as rect, no φ label    → "横 W×H"
  const labelCombined = `${sleeve.label_text || ""} ${sleeve.diameter_text || ""}`;
  const hasRoundPipeLabel = /(?:[φΦ]\s*\d+|\d+\s*[φΦ]|\d+\s*A\b|外径\s*\d+)/i.test(labelCombined);
  const shapeLabel = isHorizontal ? "横" : (isRect ? "角" : "丸");
  const sizeText = hasRoundPipeLabel
    ? `${shapeLabel} φ${Math.round(sleeve.diameter)}mm`
    : (isRect && sleeve.width && sleeve.height
        ? `${shapeLabel} ${Math.round(sleeve.width)}×${Math.round(sleeve.height)}mm`
        : `${shapeLabel} φ${Math.round(sleeve.diameter)}mm`);

  const slabTopText = computeSlabTopText(sleeve, slabLabels);

  const worst = ngResults.length > 0 ? "NG" : warnResults.length > 0 ? "WARNING" : "OK";
  const badgeStyle: Record<string, { bg: string; color: string }> = {
    NG: { bg: "#fef2f2", color: "#dc2626" },
    WARNING: { bg: "#fffbeb", color: "#d97706" },
    OK: { bg: "#f0fdf4", color: "#16a34a" },
  };
  const badge = badgeStyle[worst];

  return (
    <div style={{ fontSize: 13 }}>
      {/* Header */}
      <div style={{ padding: "14px 16px", borderBottom: "1px solid #f3f4f6" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ background: badge.bg, color: badge.color, padding: "2px 8px", borderRadius: 4, fontSize: 11, fontWeight: 600 }}>{worst}</span>
          <span style={{ fontWeight: 700, color: "#111827", fontSize: 15 }}>{sleeve.pn_number || sleeve.id}</span>
        </div>
        <div style={{ color: "#9ca3af", fontSize: 11, marginTop: 4 }}>{sleeve.layer}</div>
      </div>

      {/* Properties */}
      <div style={{ padding: "12px 16px", borderBottom: "1px solid #f3f4f6" }}>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "10px 16px" }}>
          <div><div style={{ color: "#9ca3af", fontSize: 10, marginBottom: 2 }}>形状/寸法</div><div style={{ color: "#111827", fontWeight: 600 }}>
            {sizeText}
          </div></div>
          <div><div style={{ color: "#9ca3af", fontSize: 10, marginBottom: 2 }}>スラブ天端</div><div style={{ color: "#111827", fontWeight: 600 }}>{slabTopText || sleeve.fl_text || "-"}</div></div>
          <div><div style={{ color: "#9ca3af", fontSize: 10, marginBottom: 2 }}>種別</div><div style={{ color: "#374151" }}>
            {typeLabel ? `${typeLabel} (${disciplineCode})` : (disciplineCode || "-")}
          </div></div>
        </div>
      </div>

      {/* Check results */}
      <div style={{ padding: "12px 16px" }}>
        <div style={{ color: "#6b7280", fontSize: 10, marginBottom: 8, textTransform: "uppercase", letterSpacing: 0.5 }}>
          チェック結果
        </div>

        {ngResults.map((r, i) => renderResultCard(r, "ng", i))}
        {warnResults.map((r, i) => renderResultCard(r, "w", i))}

        {okCount > 0 && (
          <div style={{ color: "#9ca3af", fontSize: 11, marginTop: 8 }}>
            + {okCount} 件 OK
          </div>
        )}
      </div>
    </div>
  );
}
