import { useState, useEffect, useCallback } from "react";
import type { FloorData, Sleeve, CheckResult } from "./types";
import { getFloors, parseFloor, runChecks, uploadDxf, uploadDwg, uploadIfc } from "./api";
import { useUniversalEntities } from "./useUniversalEntities";
import DrawingView from "./components/DrawingView";
import SleeveInfo from "./components/SleeveInfo";
import ListView from "./components/ListView";
import DataExplorer from "./components/DataExplorer";
import * as pdfjs from "pdfjs-dist";

// Vite bundles the worker URL for us; pdf.js requires this or it falls back
// to main-thread rendering that often fails in production builds.
pdfjs.GlobalWorkerOptions.workerSrc = new URL(
  "pdfjs-dist/build/pdf.worker.min.mjs",
  import.meta.url,
).href;

async function pdfFileToDataUrl(file: File): Promise<string> {
  const buffer = await file.arrayBuffer();
  const pdf = await pdfjs.getDocument({ data: buffer }).promise;
  const page = await pdf.getPage(1);
  const viewport = page.getViewport({ scale: 2 });
  const canvas = document.createElement("canvas");
  canvas.width = viewport.width;
  canvas.height = viewport.height;
  const ctx = canvas.getContext("2d")!;
  await page.render({ canvas, canvasContext: ctx, viewport } as any).promise;
  return canvas.toDataURL("image/png");
}

type ViewMode = "drawing" | "list" | "data";

interface FloorEntry {
  id: string;
  label: string;
  source: "dxf" | "ifc" | "dwg";
  data: FloorData | null;
  results: CheckResult[];
}

// "b1f" → -1, "1f" → 1, "2f" → 2, "3f" → 3, ...
// The "-ifc" suffix is stripped so DXF and IFC of the same storey share a level.
function floorLevel(id: string): number | null {
  const m = id.replace(/-ifc$/, "").match(/^(b)?(\d+)f$/i);
  if (!m) return null;
  const n = parseInt(m[2], 10);
  return m[1] ? -n : n;
}

function findLowerFloor(floors: FloorEntry[], currentId: string): FloorEntry | null {
  const level = floorLevel(currentId);
  if (level === null) return null;
  const current = floors.find((f) => f.id === currentId);
  if (!current) return null;
  const candidates = floors.filter((f) => floorLevel(f.id) === level - 1);
  if (candidates.length === 0) return null;
  // Prefer the same source (IFC↔IFC / DXF↔DXF) so mixed sets still pair cleanly.
  return candidates.find((f) => f.source === current.source) ?? candidates[0];
}

function floorLevelLabel(id: string): string {
  const lv = floorLevel(id);
  if (lv === null) return id;
  return lv < 0 ? `B${-lv}F` : `${lv}F`;
}

function App() {
  const [floors, setFloors] = useState<FloorEntry[]>([]);
  const [activeFloorIdx, setActiveFloorIdx] = useState(0);
  const [viewMode, setViewMode] = useState<ViewMode>("list");
  const [loading, setLoading] = useState(false);
  const [hoveredSleeve, setHoveredSleeve] = useState<Sleeve | null>(null);
  const [selectedSleeve, setSelectedSleeve] = useState<Sleeve | null>(null);
  const [filter, setFilter] = useState<"all" | "NG" | "WARNING" | "OK">("all");
  const [navigateTarget, setNavigateTarget] = useState<[number, number] | null>(null);
  const [highlightCoords, setHighlightCoords] = useState<[number, number][]>([]);
  const [openChecks, setOpenChecks] = useState<Set<number>>(new Set());
  // Layer toggles. The first block (1-to-1 with data tab GROUP_ORDER) is
  // category-driven — each toggle corresponds to one of the 23 useful
  // categories. The second block (overlays) is typed-rendering modes that
  // are conceptually orthogonal to the categories (computed visualisations).
  const [layers, setLayers] = useState({
    // categories
    grid: true,           // 通り芯
    outerWall: true,      // 外壁
    wall: true,           // 内壁
    firewall: true,       // 耐火壁・防火区画
    column: true,         // 柱・仕上線
    beam: false,          // 梁
    slabOutline: true,    // スラブ外形
    slabLabel: false,     // スラブラベル (F308 / S番号・厚み)
    slabFL: false,        // スラブFL (F155 / 面レベル)
    step: true,           // 段差線
    recess: true,         // 床ヌスミ
    flLabel: false,       // FL表記
    dim: false,           // 寸法線
    pn: true,             // P-N番号
    room: true,           // 部屋名
    waterGradient: false, // 水勾配
    equipSanitary: false, // 機器コード_衛生
    equipHvac: false,     // 機器コード_空調
    equipElectric: false, // 機器コード_電気
    sleeve: true,         // スリーブ_* (4 sub via sleeveFilters)
    // overlays — typed/computed visualisations (orthogonal)
    lowerWall: false,
    slabLevel: false,
    flZone: false,
    raw: false,
  });
  const [sleeveFilters, setSleeveFilters] = useState({
    衛生: true, 空調: true, 電気: true, その他: true,
  });
  const toggleSleeveFilter = (key: keyof typeof sleeveFilters) =>
    setSleeveFilters((p) => ({ ...p, [key]: !p[key] }));
  // Unified upload modal (.dxf / .dwg / .ifc).
  // Extension-based routing decides which backend endpoint each file goes to.
  const [uploadModalOpen, setUploadModalOpen] = useState(false);
  const [uploadFiles, setUploadFiles] = useState<File[]>([]);
  const [dwgConverting, setDwgConverting] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [pdfOverlayUrl, setPdfOverlayUrl] = useState<string | null>(null);
  const [pdfOverlayOpacity, setPdfOverlayOpacity] = useState(0.4);
  const handleUploadPdf = async (files: FileList | null) => {
    if (!files || files.length === 0) return;
    try {
      const url = await pdfFileToDataUrl(files[0]);
      setPdfOverlayUrl(url);
    } catch (e) {
      console.error("PDF overlay failed:", e);
    }
  };

  const toggleLayer = (key: keyof typeof layers) =>
    setLayers((p) => ({ ...p, [key]: !p[key] }));

  // Stable callback so DrawingView's React.memo stays effective
  const handleNavigated = useCallback(() => setNavigateTarget(null), []);

  const activeFloor: FloorEntry = floors[activeFloorIdx] || { id: "", label: "", source: "dxf", data: null, results: [] };
  const floorData = activeFloor.data;
  const results = activeFloor.results;
  const displaySleeve = hoveredSleeve || selectedSleeve;

  // Fetch the universal entity dump for whichever floor is active. The
  // DrawingView and DataExplorer both consume it; the hook caches the
  // result so we only pay once per floor per session.
  const { data: universalEntities } = useUniversalEntities(
    activeFloor.id || null,
  );

  // Lower floor (level − 1) for wall-interference overlay and inter-floor checks.
  const lowerFloor = findLowerFloor(floors, activeFloor.id);
  const lowerFloorData = lowerFloor?.data ?? null;

  // Sleeve counts per discipline (for filter badges)
  const sleeveCounts = (() => {
    const counts = { 衛生: 0, 空調: 0, 電気: 0, その他: 0 };
    if (!floorData) return counts;
    for (const s of floorData.sleeves) {
      const d = s.discipline as keyof typeof counts;
      if (d in counts) counts[d]++;
      else counts["その他"]++;
    }
    return counts;
  })();

  // Load existing floors on mount
  useEffect(() => {
    getFloors().then((serverFloors) => {
      if (serverFloors.length > 0) {
        setFloors(serverFloors.map((f) => ({
          id: f.id,
          label: f.name,
          source: (f.source === "ifc" ? "ifc" : "dxf") as "dxf" | "ifc",
          data: null,
          results: [],
        })));
      }
    }).catch(() => {});
  }, []);

  const handleUploadSubmit = async () => {
    if (uploadFiles.length === 0) return;
    setUploadError(null);
    setLoading(true);
    try {
      // Group by extension. IFC is batched together (multiple files can be
      // one logical upload). DXF/DWG each become their own floor entry.
      const ifc: File[] = [];
      const dxfs: File[] = [];
      const dwgs: File[] = [];
      for (const f of uploadFiles) {
        const ext = f.name.split(".").pop()?.toLowerCase();
        if (ext === "ifc") ifc.push(f);
        else if (ext === "dxf") dxfs.push(f);
        else if (ext === "dwg") dwgs.push(f);
        else throw new Error(`対応していない拡張子: ${f.name}`);
      }

      const addEntry = (id: string, name: string, source: FloorEntry["source"]) => {
        setFloors((prev) => {
          const entry: FloorEntry = { id, label: name, source, data: null, results: [] };
          const exists = prev.findIndex((f) => f.id === id);
          if (exists >= 0) {
            const next = [...prev];
            next[exists] = entry;
            return next;
          }
          return [...prev, entry];
        });
      };

      if (ifc.length > 0) {
        const res = await uploadIfc(ifc, "");
        addEntry(res.id, res.name, "ifc");
      }
      for (const f of dxfs) {
        const res = await uploadDxf(f, "");
        addEntry(res.id, res.name, "dxf");
      }
      for (const f of dwgs) {
        setDwgConverting(true);
        const res = await uploadDwg(f, "");
        setDwgConverting(false);
        addEntry(res.id, res.name, "dwg");
      }

      setUploadModalOpen(false);
      setUploadFiles([]);
    } catch (e: any) {
      const detail = e?.response?.data?.detail ?? e?.message ?? "アップロードに失敗しました";
      setUploadError(String(detail));
      setDwgConverting(false);
      console.error(e);
    }
    setLoading(false);
  };

  const handleRun = async () => {
    if (floors.length === 0) return;
    setLoading(true);
    try {
      // Parse all floors
      const parsed = await Promise.all(floors.map((f) => parseFloor(f.id)));

      // Pair each floor with the floor one level below (level − 1). Same-source
      // (IFC↔IFC / DXF↔DXF) pairs win over cross-source; floors with no
      // lower-level counterpart get checked standalone.
      const checked = await Promise.all(
        floors.map((f) => {
          const lower = findLowerFloor(floors, f.id);
          return runChecks(f.id, lower?.id);
        })
      );

      setFloors(
        floors.map((f, i) => ({
          ...f,
          data: parsed[i],
          results: checked[i].results,
        }))
      );
    } catch (e) {
      console.error(e);
    }
    setLoading(false);
  };

  const handleRemoveFloor = (idx: number) => {
    setFloors((prev) => prev.filter((_, i) => i !== idx));
    if (activeFloorIdx >= floors.length - 1) {
      setActiveFloorIdx(Math.max(0, floors.length - 2));
    }
  };

  const hasData = floors.some((f) => f.data !== null);

  // Sleeve number search — jump to the sleeve whose P-N number matches the
  // query. Accepts "12", "P-N-12", "P-N12", "pn-12" etc.
  const [sleeveSearch, setSleeveSearch] = useState("");
  const [sleeveSearchError, setSleeveSearchError] = useState(false);
  const handleSleeveSearch = (raw: string) => {
    if (!floorData) return;
    const q = raw.trim();
    if (!q) return;
    // Extract trailing digits
    const m = q.match(/(\d+)\s*$/);
    if (!m) {
      setSleeveSearchError(true);
      return;
    }
    const wanted = `P-N-${parseInt(m[1], 10)}`;
    const sleeve = floorData.sleeves.find(
      (s) => (s.pn_number || "").trim().toUpperCase() === wanted.toUpperCase()
    );
    if (!sleeve) {
      setSleeveSearchError(true);
      return;
    }
    setSleeveSearchError(false);
    setViewMode("drawing");
    setNavigateTarget([sleeve.center[0], sleeve.center[1]]);
    setHighlightCoords([]);
    setSelectedSleeve(sleeve);
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100vh", background: "#f8fafc", color: "#111827", fontFamily: "'Inter', 'Noto Sans JP', -apple-system, sans-serif" }}>
      {/* Row 1: Header */}
      <div className="no-print" style={{ background: "#fff", borderBottom: "1px solid #e5e7eb", padding: "10px 20px", display: "flex", alignItems: "center", gap: 12 }}>
        <span style={{ fontSize: 16, fontWeight: 700, color: "#111827", letterSpacing: -0.3 }}>
          スリーブチェッカー
        </span>

        {/* View toggle */}
        <div style={{ display: "inline-flex", background: "#f3f4f6", borderRadius: 7, padding: 2, gap: 2, fontSize: 12, marginLeft: 12 }}>
          {([["list", "一覧"], ["drawing", "図面"], ["data", "データ"]] as const).map(([mode, label]) => (
            <button key={mode} onClick={() => setViewMode(mode)}
              style={{
                padding: "4px 14px", border: "none", borderRadius: 5, cursor: "pointer", fontSize: 12, fontWeight: viewMode === mode ? 500 : 400,
                background: viewMode === mode ? "#111827" : "transparent",
                color: viewMode === mode ? "#fff" : "#9ca3af",
              }}>{label}</button>
          ))}
        </div>

        {floors.length > 0 && (
          <button onClick={handleRun} disabled={loading}
            style={{
              padding: "5px 16px", background: loading ? "#d1d5db" : "#ff4b4b",
              color: "#fff", border: "none", borderRadius: 6, cursor: loading ? "default" : "pointer",
              fontSize: 12, fontWeight: 500, marginLeft: 8,
            }}>
            {loading ? "解析中..." : hasData ? "再チェック" : "チェック実行"}
          </button>
        )}

        {hasData && floorData && (
          <div style={{ display: "inline-flex", alignItems: "center", gap: 6, marginLeft: 12 }}>
            <input
              type="text"
              value={sleeveSearch}
              onChange={(e) => { setSleeveSearch(e.target.value); setSleeveSearchError(false); }}
              onKeyDown={(e) => { if (e.key === "Enter") handleSleeveSearch(sleeveSearch); }}
              placeholder="P-N番号で検索 (例 12)"
              style={{
                padding: "5px 10px", fontSize: 12, width: 160,
                border: `1px solid ${sleeveSearchError ? "#dc2626" : "#d1d5db"}`,
                borderRadius: 6, outline: "none",
                background: sleeveSearchError ? "#fef2f2" : "#fff",
              }}
            />
            <button onClick={() => handleSleeveSearch(sleeveSearch)}
              style={{
                padding: "5px 10px", fontSize: 12, background: "#fff", color: "#374151",
                border: "1px solid #d1d5db", borderRadius: 6, cursor: "pointer",
              }}>飛ぶ</button>
          </div>
        )}

        {hasData && (
          <button onClick={() => window.print()} className="no-print"
            style={{
              padding: "5px 12px", background: "#fff", color: "#374151",
              border: "1px solid #d1d5db", borderRadius: 6, cursor: "pointer",
              fontSize: 12, fontWeight: 500,
            }}>
            PDF出力
          </button>
        )}

      </div>

      {/* Row 2: Floor tabs + upload buttons */}
      <div className="no-print" style={{ background: "#fff", borderBottom: "1px solid #e5e7eb", padding: "6px 20px", display: "flex", alignItems: "center", gap: 10 }}>
        {/* Floor segment */}
        {floors.length > 0 && (
          <div style={{ display: "inline-flex", background: "#f3f4f6", borderRadius: 7, padding: 2, gap: 2, fontSize: 12 }}>
            {floors.map((f, i) => (
              <div key={f.id} style={{ display: "inline-flex", alignItems: "center" }}>
                <button
                  onClick={() => { setActiveFloorIdx(i); setSelectedSleeve(null); setHoveredSleeve(null); }}
                  style={{
                    padding: "4px 16px", border: "none", borderRadius: 5, cursor: "pointer", fontSize: 12,
                    fontWeight: activeFloorIdx === i ? 500 : 400,
                    background: activeFloorIdx === i ? "#fff" : "transparent",
                    color: activeFloorIdx === i ? "#111827" : "#9ca3af",
                    boxShadow: activeFloorIdx === i ? "0 1px 2px rgba(0,0,0,0.06)" : "none",
                    display: "inline-flex", alignItems: "center", gap: 6,
                  }}>
                  {f.label}
                  <span style={{
                    fontSize: 9, fontWeight: 600, padding: "1px 5px", borderRadius: 3,
                    background: f.source === "ifc" ? "#dbeafe" : f.source === "dwg" ? "#fef3c7" : "#f3f4f6",
                    color: f.source === "ifc" ? "#1e40af" : f.source === "dwg" ? "#92400e" : "#6b7280",
                    letterSpacing: 0.3,
                  }}>{f.source.toUpperCase()}</span>
                </button>
                {!f.data && (
                  <button onClick={(e) => { e.stopPropagation(); handleRemoveFloor(i); }}
                    style={{ background: "none", border: "none", color: "#d1d5db", cursor: "pointer", fontSize: 10, padding: "0 4px", lineHeight: 1 }}
                    title="削除">x</button>
                )}
              </div>
            ))}
          </div>
        )}

        <button onClick={() => setUploadModalOpen(true)} disabled={loading}
          style={{ padding: "3px 12px", fontSize: 11, background: "#fff", border: "1px dashed #d1d5db", borderRadius: 6, color: "#6b7280", cursor: "pointer" }}>
          {dwgConverting ? "DWG変換中..." : "+ ファイルを追加"}
        </button>

      </div>

      {/* Upload error toast */}
      {uploadError && (
        <div style={{
          position: "fixed", bottom: 24, right: 24, zIndex: 120, maxWidth: 420,
          background: "#fef2f2", border: "1px solid #fecaca", borderRadius: 8,
          padding: "12px 16px", boxShadow: "0 4px 16px rgba(0,0,0,0.08)",
        }}>
          <div style={{ display: "flex", alignItems: "start", gap: 10 }}>
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: 12, fontWeight: 600, color: "#dc2626", marginBottom: 4 }}>
                アップロードエラー
              </div>
              <div style={{ fontSize: 11, color: "#7f1d1d", lineHeight: 1.5 }}>{uploadError}</div>
            </div>
            <button onClick={() => setUploadError(null)}
              style={{ background: "none", border: "none", color: "#dc2626", cursor: "pointer", fontSize: 14, padding: 0, lineHeight: 1 }}>x</button>
          </div>
        </div>
      )}

      {/* Unified upload modal — .dxf / .dwg / .ifc */}
      {uploadModalOpen && (
        <div
          onClick={() => !loading && setUploadModalOpen(false)}
          style={{
            position: "fixed", inset: 0, background: "rgba(0,0,0,0.35)",
            zIndex: 100, display: "flex", alignItems: "center", justifyContent: "center",
          }}
        >
          <div
            onClick={(e) => e.stopPropagation()}
            style={{ background: "#fff", borderRadius: 10, padding: 24, width: 480, boxShadow: "0 10px 40px rgba(0,0,0,0.15)" }}
          >
            <div style={{ fontSize: 15, fontWeight: 600, marginBottom: 14 }}>
              ファイルをアップロード（DWG / IFC / DXF 対応）
            </div>

            <div style={{ marginBottom: 20 }}>
              <input type="file" accept=".dxf,.dwg,.ifc" multiple
                onChange={(e) => setUploadFiles(Array.from(e.target.files || []))}
                style={{ fontSize: 12, width: "100%" }} />
              {uploadFiles.length > 0 && (
                <ul style={{ fontSize: 11, color: "#6b7280", marginTop: 8, paddingLeft: 16, listStyle: "disc" }}>
                  {uploadFiles.map((f, i) => (
                    <li key={i}>{f.name}</li>
                  ))}
                </ul>
              )}
            </div>

            <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
              <button
                onClick={() => { if (!loading) { setUploadModalOpen(false); setUploadFiles([]); } }}
                disabled={loading}
                style={{ padding: "6px 16px", fontSize: 12, background: "#fff", border: "1px solid #d1d5db", borderRadius: 6, color: "#6b7280", cursor: "pointer" }}
              >キャンセル</button>
              <button
                onClick={handleUploadSubmit}
                disabled={uploadFiles.length === 0 || loading}
                style={{
                  padding: "6px 16px", fontSize: 12, border: "none", borderRadius: 6,
                  background: (uploadFiles.length === 0 || loading) ? "#d1d5db" : "#ff4b4b",
                  color: "#fff", cursor: (uploadFiles.length === 0 || loading) ? "default" : "pointer",
                  fontWeight: 500,
                }}
              >{dwgConverting ? "DWG変換中..." : loading ? "アップロード中..." : "登録"}</button>
            </div>
          </div>
        </div>
      )}

      {/* Main content */}
      <div style={{ flex: 1, overflow: "hidden", display: "flex" }}>
        {floors.length === 0 ? (
          <div style={{ flex: 1, display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: 16, color: "#9ca3af" }}>
            <div style={{ fontSize: 14 }}>DXF / DWG / IFC ファイルを追加してください</div>
            <button onClick={() => setUploadModalOpen(true)}
              style={{
                padding: "10px 24px", fontSize: 13, background: "#fff", border: "2px dashed #d1d5db",
                borderRadius: 8, color: "#6b7280", cursor: "pointer",
              }}>
              + ファイルを追加
            </button>
          </div>
        ) : viewMode === "drawing" ? (
          <>
            {/* Drawing area */}
            <div style={{ flex: 1, overflow: "hidden" }}>
              {floorData ? (
                <DrawingView
                  floorData={floorData}
                  lowerFloorData={layers.lowerWall ? lowerFloorData : null}
                  universalEntities={universalEntities?.entities ?? null}
                  layerCategories={universalEntities?.layer_categories ?? null}
                  results={results}
                  onSleeveHover={setHoveredSleeve}
                  onSleeveClick={setSelectedSleeve}
                  selectedSleeveId={selectedSleeve?.id || null}
                  layers={layers}
                  sleeveFilters={sleeveFilters}
                  navigateTarget={navigateTarget}
                  onNavigated={handleNavigated}
                  highlightCoords={highlightCoords}
                  pdfOverlayUrl={pdfOverlayUrl}
                  pdfOverlayOpacity={pdfOverlayOpacity}
                  onToggleLayer={(key) => toggleLayer(key as keyof typeof layers)}
                  onToggleSleeveFilter={(key) => toggleSleeveFilter(key)}
                  sleeveCounts={sleeveCounts}
                  showLowerWallToggle={lowerFloor !== null}
                  lowerFloorLabel={lowerFloor ? floorLevelLabel(lowerFloor.id) : ""}
                  onPdfFilesSelected={handleUploadPdf}
                  onPdfClear={() => setPdfOverlayUrl(null)}
                  onPdfOpacityChange={setPdfOverlayOpacity}
                />
              ) : (
                <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100%", color: "#9ca3af" }}>
                  「チェック実行」を押してください
                </div>
              )}
            </div>
            {/* Right sidebar */}
            <div className="no-print" style={{ width: 300, borderLeft: "1px solid #e5e7eb", background: "#fff", overflow: "auto", flexShrink: 0 }}>
              {displaySleeve ? (
                <SleeveInfo sleeve={displaySleeve} results={results} />
              ) : (
                <div style={{ padding: 20, color: "#9ca3af", fontSize: 13, textAlign: "center", marginTop: 40 }}>
                  スリーブにカーソルを合わせると<br />詳細が表示されます
                </div>
              )}
            </div>
          </>
        ) : viewMode === "data" ? (
          <div style={{ flex: 1, overflow: "hidden" }}>
            {floorData ? (
              <DataExplorer
                floorData={floorData}
                floorId={activeFloor.id}
                onNavigate={(coords, sleeveId) => {
                  setViewMode("drawing");
                  setNavigateTarget(coords);
                  setHighlightCoords([]);
                  if (sleeveId) {
                    const sleeve = floorData?.sleeves.find(s => s.id === sleeveId);
                    if (sleeve) setSelectedSleeve(sleeve);
                  } else {
                    setSelectedSleeve(null);
                  }
                }}
              />
            ) : (
              <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100%", color: "#9ca3af" }}>
                「チェック実行」を押してください
              </div>
            )}
          </div>
        ) : (
          <div style={{ flex: 1, overflow: "auto" }}>
            {floorData ? (
              <ListView
                floorData={floorData}
                results={results}
                filter={filter}
                onFilterChange={setFilter}
                openChecks={openChecks}
                onOpenChecksChange={setOpenChecks}
                onNavigate={(coords, sleeveId, relatedCoords) => {
                  setViewMode("drawing");
                  // Compute bounding center of all related coords so both base points are visible
                  const allPts = relatedCoords && relatedCoords.length > 1 ? relatedCoords : [coords];
                  const cx = allPts.reduce((s, p) => s + p[0], 0) / allPts.length;
                  const cy = allPts.reduce((s, p) => s + p[1], 0) / allPts.length;
                  setNavigateTarget([cx, cy]);
                  setHighlightCoords(relatedCoords || []);
                  if (sleeveId) {
                    const sleeve = floorData?.sleeves.find(s => s.id === sleeveId);
                    if (sleeve) setSelectedSleeve(sleeve);
                  } else {
                    setSelectedSleeve(null);
                  }
                }}
              />
            ) : (
              <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100%", color: "#9ca3af" }}>
                「チェック実行」を押してください
              </div>
            )}
          </div>
        )}
      </div>

      {/* Print-only: results table appended on separate page(s) */}
      {hasData && (
        <div className="print-results" style={{ display: "none" }}>
          <style>{`@media print { .print-results { display: block !important; page-break-before: always; padding: 10mm; } }`}</style>
          <h2 style={{ fontSize: 14, marginBottom: 8 }}>
            {activeFloor.label} — チェック結果一覧
          </h2>
          <table>
            <thead>
              <tr>
                <th style={{ width: "8%" }}>No.</th>
                <th style={{ width: "10%" }}>重要度</th>
                <th style={{ width: "20%" }}>チェック項目</th>
                <th style={{ width: "12%" }}>P-N</th>
                <th>メッセージ</th>
              </tr>
            </thead>
            <tbody>
              {results
                .filter((r) => r.severity !== "OK")
                .map((r, i) => {
                  const sleeve = r.sleeve_id
                    ? floorData?.sleeves.find((s) => s.id === r.sleeve_id)
                    : null;
                  return (
                    <tr key={i}>
                      <td>{r.check_id}</td>
                      <td style={{
                        color: r.severity === "NG" ? "#dc2626" : "#d97706",
                        fontWeight: 600,
                      }}>{r.severity}</td>
                      <td>{r.check_name}</td>
                      <td>{sleeve?.pn_number || r.sleeve_id || "-"}</td>
                      <td>{r.message}</td>
                    </tr>
                  );
                })}
            </tbody>
          </table>
          <div style={{ marginTop: 10, fontSize: 10, color: "#6b7280" }}>
            出力日時: {new Date().toLocaleString("ja-JP")}
          </div>
        </div>
      )}
    </div>
  );
}

export default App;
