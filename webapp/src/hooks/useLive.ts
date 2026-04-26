/// <reference types="vite/client" />
// src/hooks/useLive.ts
// WebSocket connection to FastAPI /ws/live — pushes state every 5 seconds.

import { useEffect, useRef, useState, useCallback } from "react";
import type { Perf, Position } from "../lib/api";

const WS_URL = import.meta.env.VITE_WS_URL ?? `ws://${window.location.host}/ws/live`;

export interface LiveState {
  halted:    boolean;
  perf:      Perf | null;
  positions: Record<string, Position>;
  connected: boolean;
  lastUpdate: Date | null;
}

const INITIAL: LiveState = {
  halted:     false,
  perf:       null,
  positions:  {},
  connected:  false,
  lastUpdate: null,
};

export function useLive() {
  const [state, setState]  = useState<LiveState>(INITIAL);
  const wsRef              = useRef<WebSocket | null>(null);
  const reconnectTimer     = useRef<ReturnType<typeof setTimeout> | null>(null);

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return;

    const ws = new WebSocket(WS_URL);
    wsRef.current = ws;

    ws.onopen = () => {
      setState(s => ({ ...s, connected: true }));
      // Keep-alive ping every 20s
      const ping = setInterval(() => {
        if (ws.readyState === WebSocket.OPEN)
          ws.send(JSON.stringify({ type: "ping" }));
      }, 20_000);
      ws.onclose = () => clearInterval(ping);
    };

    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data as string);
        if (msg.type === "state_update") {
          setState({
            halted:     msg.halted    ?? false,
            perf:       msg.perf      ?? null,
            positions:  msg.positions ?? {},
            connected:  true,
            lastUpdate: new Date(),
          });
        }
      } catch {
        // ignore malformed messages
      }
    };

    ws.onerror = () => setState(s => ({ ...s, connected: false }));

    ws.onclose = () => {
      setState(s => ({ ...s, connected: false }));
      reconnectTimer.current = setTimeout(connect, 3_000);
    };
  }, []);

  useEffect(() => {
    connect();
    return () => {
      reconnectTimer.current && clearTimeout(reconnectTimer.current);
      wsRef.current?.close();
    };
  }, [connect]);

  const sendHalt = useCallback(() => {
    wsRef.current?.send(JSON.stringify({ type: "halt" }));
  }, []);

  const sendResume = useCallback(() => {
    wsRef.current?.send(JSON.stringify({ type: "resume" }));
  }, []);

  return { ...state, sendHalt, sendResume };
}
