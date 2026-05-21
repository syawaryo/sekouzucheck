import { useEffect, useState } from "react";
import { getAllEntities, type AllEntitiesResponse } from "./api";

// Module-level cache shared by every component that needs the universal
// entity dump. Keying by floorId means switching tabs / floors reuses
// already-fetched payloads instantly.
const _cache = new Map<string, AllEntitiesResponse>();
const _inflight = new Map<string, Promise<AllEntitiesResponse>>();

export function useUniversalEntities(floorId: string | null) {
  const [data, setData] = useState<AllEntitiesResponse | null>(
    floorId ? _cache.get(floorId) ?? null : null,
  );
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!floorId) {
      setData(null);
      setLoading(false);
      return;
    }
    const cached = _cache.get(floorId);
    if (cached) {
      setData(cached);
      setLoading(false);
      return;
    }

    // Clear stale data from the previous floor immediately. Without this,
    // the data tab keeps rendering the old floor's entities while the new
    // floor's fetch is in flight — which makes users wonder whether the
    // displayed content actually matches the active tab.
    setData(null);

    let cancelled = false;
    setLoading(true);
    let promise = _inflight.get(floorId);
    if (!promise) {
      promise = getAllEntities(floorId)
        .then((u) => {
          _cache.set(floorId, u);
          return u;
        })
        .finally(() => {
          _inflight.delete(floorId);
        });
      _inflight.set(floorId, promise);
    }
    promise
      .then((u) => { if (!cancelled) setData(u); })
      .catch((err) => { console.error("all_entities failed:", err); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [floorId]);

  return { data, loading };
}
