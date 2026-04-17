// src/hooks/useQuery.ts
// Lightweight data-fetching hook. No external deps.

import { useEffect, useRef, useState } from "react";

type Status = "idle" | "loading" | "success" | "error";

export interface QueryResult<T> {
  data:    T | null;
  status:  Status;
  error:   string | null;
  refetch: () => void;
}

export function useQuery<T>(
  fetcher:       () => Promise<T>,
  deps:          unknown[] = [],
  pollMs:        number    = 0,   // 0 = no polling
): QueryResult<T> {
  const [data,   setData]   = useState<T | null>(null);
  const [status, setStatus] = useState<Status>("idle");
  const [error,  setError]  = useState<string | null>(null);
  const timerRef            = useRef<ReturnType<typeof setTimeout> | null>(null);

  const load = async () => {
    setStatus("loading");
    try {
      const result = await fetcher();
      setData(result);
      setStatus("success");
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setStatus("error");
    }
  };

  useEffect(() => {
    load();
    if (pollMs > 0) {
      timerRef.current = setInterval(load, pollMs);
    }
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return { data, status, error, refetch: load };
}
