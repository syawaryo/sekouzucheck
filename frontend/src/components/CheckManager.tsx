import { useEffect, useState } from "react";
import type { CheckDef } from "../types";
import { listChecks, createCheck, updateCheck, deleteCheck } from "../api";

interface Props {
  onClose: () => void;
  onChanged?: () => void; // called after any create/update/delete
}

interface Draft {
  name: string;
  description: string;
}

const EMPTY_DRAFT: Draft = { name: "", description: "" };

export default function CheckManager({ onClose, onChanged }: Props) {
  const [checks, setChecks] = useState<CheckDef[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<number | "new" | null>(null);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editDraft, setEditDraft] = useState<Draft>(EMPTY_DRAFT);
  const [adding, setAdding] = useState(false);
  const [newDraft, setNewDraft] = useState<Draft>(EMPTY_DRAFT);

  const refresh = async () => {
    setLoading(true);
    try {
      setChecks(await listChecks());
    } catch {
      setError("チェック一覧の取得に失敗しました");
    }
    setLoading(false);
  };

  useEffect(() => { refresh(); }, []);

  const notifyChanged = () => onChanged?.();

  const startEdit = (c: CheckDef) => {
    setEditingId(c.id);
    setEditDraft({ name: c.name, description: c.description });
    setError(null);
  };

  const saveEdit = async (id: number) => {
    setBusyId(id);
    setError(null);
    try {
      const updated = await updateCheck(id, {
        name: editDraft.name,
        description: editDraft.description,
      });
      setChecks((prev) => prev.map((x) => (x.id === id ? updated : x)));
      setEditingId(null);
      notifyChanged();
    } catch (e: any) {
      setError(e?.response?.data?.detail ?? "保存（再生成）に失敗しました");
    }
    setBusyId(null);
  };

  const handleDelete = async (c: CheckDef) => {
    if (!window.confirm(`「${c.name}」を削除しますか?`)) return;
    setBusyId(c.id);
    setError(null);
    try {
      await deleteCheck(c.id);
      setChecks((prev) => prev.filter((x) => x.id !== c.id));
      notifyChanged();
    } catch {
      setError("削除に失敗しました");
    }
    setBusyId(null);
  };

  const handleAdd = async () => {
    if (!newDraft.name.trim() || !newDraft.description.trim()) {
      setError("名前と基準文を入力してください");
      return;
    }
    setBusyId("new");
    setError(null);
    try {
      const created = await createCheck({ ...newDraft, category: "その他" });
      setChecks((prev) => [...prev, created]);
      setAdding(false);
      setNewDraft(EMPTY_DRAFT);
      notifyChanged();
    } catch (e: any) {
      setError(e?.response?.data?.detail ?? "チェック生成に失敗しました");
    }
    setBusyId(null);
  };

  const rows = [...checks].sort((a, b) => a.order - b.order);

  return (
    <div
      onClick={onClose}
      style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.35)", zIndex: 200, display: "flex", alignItems: "center", justifyContent: "center" }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{ background: "#fff", borderRadius: 10, width: 720, maxWidth: "92vw", maxHeight: "88vh", display: "flex", flexDirection: "column", boxShadow: "0 10px 40px rgba(0,0,0,0.18)" }}
      >
        {/* Header */}
        <div style={{ padding: "16px 20px", borderBottom: "1px solid #e5e7eb", display: "flex", alignItems: "center", gap: 12 }}>
          <span style={{ fontSize: 16, fontWeight: 700, color: "#111827" }}>チェック観点</span>
          <span style={{ fontSize: 11, color: "#9ca3af" }}>{checks.length} 項目</span>
          <div style={{ flex: 1 }} />
          <button onClick={() => { setAdding((v) => !v); setError(null); }}
            style={{ padding: "5px 14px", fontSize: 12, fontWeight: 500, border: "none", borderRadius: 6, background: adding ? "#6b7280" : "#ff4b4b", color: "#fff", cursor: "pointer" }}>
            {adding ? "閉じる" : "＋ 追加"}
          </button>
          <button onClick={onClose} style={{ background: "none", border: "none", fontSize: 18, color: "#9ca3af", cursor: "pointer", lineHeight: 1 }}>×</button>
        </div>

        {/* Note */}
        <div style={{ padding: "8px 20px", background: "#fffbeb", borderBottom: "1px solid #fde68a", fontSize: 11, color: "#92400e", lineHeight: 1.5 }}>
          基準文を編集・追加すると AI がチェックのロジックを自動生成します。編集後は「再チェック」で結果に反映されます。
        </div>

        {error && (
          <div style={{ padding: "8px 20px", background: "#fef2f2", borderBottom: "1px solid #fecaca", fontSize: 12, color: "#dc2626" }}>{error}</div>
        )}

        {/* Add form */}
        {adding && (
          <div style={{ padding: "14px 20px", borderBottom: "1px solid #e5e7eb", background: "#f9fafb" }}>
            <input value={newDraft.name} onChange={(e) => setNewDraft({ ...newDraft, name: e.target.value })}
              placeholder="名前（例: 防火区画貫通の確認）"
              style={{ width: "100%", padding: "6px 10px", fontSize: 12, border: "1px solid #d1d5db", borderRadius: 6, outline: "none", boxSizing: "border-box", marginBottom: 8 }} />
            <textarea value={newDraft.description} onChange={(e) => setNewDraft({ ...newDraft, description: e.target.value })}
              placeholder="チェック基準を自由文で（例: 排水スリーブには FL 値が記載されていること）"
              rows={2}
              style={{ width: "100%", padding: "6px 10px", fontSize: 12, border: "1px solid #d1d5db", borderRadius: 6, outline: "none", resize: "vertical", fontFamily: "inherit", boxSizing: "border-box" }} />
            <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 8 }}>
              <button onClick={handleAdd} disabled={busyId === "new"}
                style={{ padding: "6px 16px", fontSize: 12, fontWeight: 500, border: "none", borderRadius: 6, background: busyId === "new" ? "#d1d5db" : "#ff4b4b", color: "#fff", cursor: busyId === "new" ? "default" : "pointer" }}>
                {busyId === "new" ? "生成中..." : "追加"}
              </button>
            </div>
          </div>
        )}

        {/* Flat list of rows */}
        <div style={{ overflow: "auto" }}>
          {loading ? (
            <div style={{ padding: 30, textAlign: "center", color: "#9ca3af", fontSize: 13 }}>読み込み中...</div>
          ) : rows.map((c) => {
            const isEditing = editingId === c.id;
            const busy = busyId === c.id;
            return (
              <div key={c.id} style={{ padding: "10px 20px", borderBottom: "1px solid #f3f4f6" }}>
                {isEditing ? (
                  <div>
                    <input value={editDraft.name} onChange={(e) => setEditDraft({ ...editDraft, name: e.target.value })}
                      style={{ width: "100%", padding: "5px 8px", fontSize: 12, border: "1px solid #d1d5db", borderRadius: 6, boxSizing: "border-box", marginBottom: 6 }} />
                    <textarea value={editDraft.description} onChange={(e) => setEditDraft({ ...editDraft, description: e.target.value })}
                      rows={2}
                      style={{ width: "100%", padding: "5px 8px", fontSize: 12, border: "1px solid #d1d5db", borderRadius: 6, resize: "vertical", fontFamily: "inherit", boxSizing: "border-box" }} />
                    <div style={{ display: "flex", gap: 6, justifyContent: "flex-end", marginTop: 6 }}>
                      <button onClick={() => setEditingId(null)} disabled={busy}
                        style={{ padding: "4px 12px", fontSize: 11, border: "1px solid #d1d5db", borderRadius: 6, background: "#fff", color: "#6b7280", cursor: "pointer" }}>キャンセル</button>
                      <button onClick={() => saveEdit(c.id)} disabled={busy}
                        style={{ padding: "4px 12px", fontSize: 11, fontWeight: 500, border: "none", borderRadius: 6, background: busy ? "#d1d5db" : "#ff4b4b", color: "#fff", cursor: busy ? "default" : "pointer" }}>
                        {busy ? "再生成中..." : "保存"}
                      </button>
                    </div>
                  </div>
                ) : (
                  <div style={{ display: "flex", alignItems: "start", gap: 10 }}>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                        <span style={{ fontSize: 13, fontWeight: 600, color: "#111827" }}>{c.name}</span>
                        <span style={{ fontSize: 9, fontWeight: 600, padding: "1px 6px", borderRadius: 3, background: c.source === "builtin" ? "#f3f4f6" : "#ede9fe", color: c.source === "builtin" ? "#6b7280" : "#6d28d9" }}>
                          {c.source === "builtin" ? "組込み" : "AI生成"}
                        </span>
                      </div>
                      <div style={{ fontSize: 12, color: "#6b7280", marginTop: 3, lineHeight: 1.5 }}>{c.description}</div>
                    </div>
                    <div style={{ display: "flex", gap: 6, flexShrink: 0 }}>
                      <button onClick={() => startEdit(c)} disabled={busy}
                        style={{ padding: "3px 10px", fontSize: 11, border: "1px solid #d1d5db", borderRadius: 6, background: "#fff", color: "#374151", cursor: "pointer" }}>編集</button>
                      <button onClick={() => handleDelete(c)} disabled={busy}
                        style={{ padding: "3px 10px", fontSize: 11, border: "1px solid #fecaca", borderRadius: 6, background: "#fff", color: "#dc2626", cursor: "pointer" }}>
                        {busy ? "..." : "削除"}
                      </button>
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
