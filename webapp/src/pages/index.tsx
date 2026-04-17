// src/pages/Positions.tsx
import { format } from "date-fns";
import type { LiveState } from "../hooks/useLive";
import {
  DirectionBadge, ConvictionDots, EmptyState,
  SectionHead, Table, Td,
} from "../components/ui";

export function Positions({ live }: { live: LiveState }) {
  const positions = Object.values(live.positions);

  return (
    <div className="animate-fade-in space-y-6">
      <div>
        <h1 className="font-display text-2xl font-bold text-white">Open Positions</h1>
        <p className="text-xs font-mono text-white/30 mt-0.5">{positions.length} active positions</p>
      </div>

      {positions.length === 0 ? (
        <div className="card py-20"><EmptyState message="no open positions" /></div>
      ) : (
        <div className="space-y-4">
          {positions.map(pos => (
            <div key={pos.symbol} className="card p-5 space-y-4">
              {/* Header */}
              <div className="flex items-start justify-between">
                <div className="flex items-center gap-3">
                  <span className="font-display text-2xl font-bold text-white">{pos.symbol}</span>
                  <DirectionBadge dir={pos.direction} />
                  <span className="text-xs font-mono text-white/30">{pos.setup_type}</span>
                </div>
                <ConvictionDots score={pos.conviction} />
              </div>

              {/* Level bar */}
              <div className="relative h-1.5 bg-surface-3 rounded-full overflow-hidden">
                {(() => {
                  const range  = pos.target_1 - pos.stop_loss;
                  const cur    = pos.entry_price - pos.stop_loss;
                  const pct    = Math.min(100, Math.max(0, (cur / range) * 100));
                  return (
                    <div
                      className="absolute left-0 top-0 h-full bg-brand-500 rounded-full"
                      style={{ width: `${pct}%` }}
                    />
                  );
                })()}
              </div>

              {/* Key levels */}
              <div className="grid grid-cols-5 gap-2 text-center">
                {[
                  { label: "SL",     val: pos.stop_loss,   cls: "text-down" },
                  { label: "Entry",  val: pos.entry_price, cls: "text-white" },
                  { label: "T1",     val: pos.target_1,    cls: "text-up" },
                  { label: "T2",     val: pos.target_2,    cls: "text-up/60" },
                  { label: "Qty",    val: pos.quantity,    cls: "text-white/60", noRupee: true },
                ].map(({ label, val, cls, noRupee }) => val != null && (
                  <div key={label} className="bg-surface-3 rounded-lg p-2">
                    <p className="text-[10px] font-mono uppercase tracking-wider text-white/25">{label}</p>
                    <p className={`font-mono text-sm font-medium ${cls}`}>
                      {noRupee ? val : `₹${Number(val).toLocaleString("en-IN")}`}
                    </p>
                  </div>
                ))}
              </div>

              {/* Reasoning */}
              {pos.reasoning && (
                <p className="text-xs text-white/40 font-mono leading-relaxed border-l-2 border-surface-border pl-3">
                  {pos.reasoning}
                </p>
              )}

              {/* Footer */}
              <p className="text-[10px] font-mono text-white/20">
                opened {pos.opened_at ? format(new Date(pos.opened_at), "HH:mm:ss") : "—"}
                {" · "}{pos.status}
              </p>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}


// ─────────────────────────────────────────────────────────────────────────────
// src/pages/Trades.tsx
// ─────────────────────────────────────────────────────────────────────────────
import { api } from "../lib/api";
import { useQuery } from "../hooks/useQuery";
import { PnlChip, Spinner, EmptyState, SectionHead, Table, Td } from "../components/ui";

export function Trades() {
  const { data, status } = useQuery(() => api.trades(100), [], 30_000);

  return (
    <div className="animate-fade-in space-y-6">
      <div>
        <h1 className="font-display text-2xl font-bold text-white">Trade History</h1>
        <p className="text-xs font-mono text-white/30 mt-0.5">all closed trades</p>
      </div>

      <div className="card">
        <div className="p-5 pb-0">
          <SectionHead title="All trades" sub={`${data?.trades.length ?? 0} records`} />
        </div>
        {status === "loading" && !data ? (
          <div className="py-12 flex justify-center"><Spinner /></div>
        ) : !data || data.trades.length === 0 ? (
          <EmptyState message="no trades yet" />
        ) : (
          <Table headers={["Symbol", "Dir", "Setup", "Entry", "Exit", "P&L", "Conv", "Status", "Date"]}>
            {data.trades.map(t => (
              <tr key={t.id} className="hover:bg-surface-3/50 transition-colors">
                <Td><span className="font-mono font-medium">{t.symbol}</span></Td>
                <Td><DirectionBadge dir={t.direction} /></Td>
                <Td><span className="text-xs text-white/40 font-mono">{t.setup_type}</span></Td>
                <Td><span className="font-mono text-sm">₹{t.entry?.toLocaleString("en-IN") ?? "—"}</span></Td>
                <Td><span className="font-mono text-sm">₹{t.exit?.toLocaleString("en-IN") ?? "—"}</span></Td>
                <Td><PnlChip value={t.pnl_pct} /></Td>
                <Td><span className="font-mono text-xs text-white/50">{t.conviction}/10</span></Td>
                <Td><span className={`text-xs font-mono ${
                  t.status === "CLOSED" ? "text-white/40" :
                  t.status === "ARMED"  ? "text-up"      : "text-white/25"
                }`}>{t.status}</span></Td>
                <Td><span className="text-xs font-mono text-white/30">
                  {t.opened_at ? format(new Date(t.opened_at), "d MMM HH:mm") : "—"}
                </span></Td>
              </tr>
            ))}
          </Table>
        )}
      </div>
    </div>
  );
}


// ─────────────────────────────────────────────────────────────────────────────
// src/pages/Journal.tsx
// ─────────────────────────────────────────────────────────────────────────────

export function Journal() {
  const { data, status } = useQuery(() => api.journal(30), [], 60_000);

  return (
    <div className="animate-fade-in space-y-6">
      <div>
        <h1 className="font-display text-2xl font-bold text-white">Arjun's Journal</h1>
        <p className="text-xs font-mono text-white/30 mt-0.5">post-trade reflections written by Claude</p>
      </div>

      {status === "loading" && !data ? (
        <div className="py-16 flex justify-center"><Spinner /></div>
      ) : !data || data.entries.length === 0 ? (
        <div className="card py-20"><EmptyState message="journal is empty — trades will appear here after close" /></div>
      ) : (
        <div className="space-y-4">
          {data.entries.map(e => (
            <div key={e.id} className="card p-5 space-y-3">
              <div className="flex items-center justify-between flex-wrap gap-2">
                <div className="flex items-center gap-2">
                  <span className="font-display font-bold text-white text-lg">{e.symbol}</span>
                  <DirectionBadge dir={e.direction} />
                  <span className="text-xs font-mono text-white/30">{e.setup_type}</span>
                </div>
                <div className="flex items-center gap-3">
                  <PnlChip value={e.pnl_pct} />
                  <span className="text-xs font-mono text-white/25">
                    {e.created_at ? format(new Date(e.created_at), "d MMM, HH:mm") : "—"}
                  </span>
                </div>
              </div>

              {/* Journal text — split into numbered sentences */}
              <div className="space-y-2">
                {e.entry_text.split(/(?<=\.)\s+/).filter(Boolean).map((sentence, i) => (
                  <div key={i} className="flex gap-2.5">
                    <span className="text-[10px] font-mono text-white/20 mt-0.5 w-4 shrink-0">{i + 1}.</span>
                    <p className="text-sm text-white/65 leading-relaxed">{sentence}</p>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}


// ─────────────────────────────────────────────────────────────────────────────
// src/pages/Control.tsx
// ─────────────────────────────────────────────────────────────────────────────
import { useState } from "react";
import { clsx } from "clsx";
import type { LiveState } from "../hooks/useLive";

export function Control({ live }: { live: LiveState }) {
  const [busy, setBusy] = useState<string | null>(null);
  const [msg,  setMsg]  = useState<string | null>(null);

  const exec = async (key: string, fn: () => Promise<unknown>, label: string) => {
    if (!confirm(`Confirm: ${label}?`)) return;
    setBusy(key);
    setMsg(null);
    try {
      await fn();
      setMsg(`✓ ${label} executed`);
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : "Error"}`);
    } finally {
      setBusy(null);
    }
  };

  const { perf } = live;

  return (
    <div className="animate-fade-in space-y-8 max-w-2xl">
      <div>
        <h1 className="font-display text-2xl font-bold text-white">Control Panel</h1>
        <p className="text-xs font-mono text-white/30 mt-0.5">agent controls and emergency actions</p>
      </div>

      {msg && (
        <div className={clsx(
          "rounded-lg border px-4 py-3 text-sm font-mono",
          msg.startsWith("✓") ? "bg-up/10 border-up/20 text-up" : "bg-down/10 border-down/20 text-down"
        )}>
          {msg}
        </div>
      )}

      {/* Agent status */}
      <div className="card p-5 space-y-4">
        <SectionHead title="Agent status" />
        <div className="grid grid-cols-2 gap-3">
          {[
            { label: "Mode",           val: "paper" },
            { label: "Daily P&L",      val: `${(perf?.daily_pnl_pct ?? 0).toFixed(2)}%` },
            { label: "Trades today",   val: perf?.trades_today ?? 0 },
            { label: "Loss streak",    val: perf?.consecutive_losses ?? 0 },
            { label: "Win rate (L10)", val: `${((perf?.win_rate_last_10 ?? 0.5) * 100).toFixed(0)}%` },
            { label: "Status",         val: live.halted ? "HALTED" : "ACTIVE" },
          ].map(({ label, val }) => (
            <div key={label} className="bg-surface-3 rounded-lg px-3 py-2 flex justify-between items-center">
              <span className="text-xs font-mono text-white/40">{label}</span>
              <span className="text-sm font-mono font-medium text-white">{val}</span>
            </div>
          ))}
        </div>
      </div>

      {/* Kill switch */}
      <div className="card p-5 space-y-4">
        <SectionHead title="Kill switch" sub="stop all trading instantly" />
        <div className="flex gap-3">
          <button
            disabled={!!busy}
            onClick={() => exec("halt", api.halt, "Halt all trading")}
            className="btn-danger flex-1 text-center"
          >
            {busy === "halt" ? "halting…" : "⚠ Halt trading"}
          </button>
          <button
            disabled={!!busy}
            onClick={() => exec("resume", api.resume, "Resume trading")}
            className="btn-primary flex-1 text-center"
          >
            {busy === "resume" ? "resuming…" : "Resume trading"}
          </button>
        </div>
      </div>

      {/* Emergency squareoff */}
      <div className="card p-5 space-y-4 border-down/20">
        <SectionHead title="Emergency square off" sub="closes all open positions at market" />
        <p className="text-xs text-white/40 font-mono leading-relaxed">
          This will halt the agent AND immediately place market orders to close all open positions.
          Use only in emergencies — fills will be at prevailing market price.
        </p>
        <button
          disabled={!!busy}
          onClick={() => exec("squareoff", api.squareoff, "Square off all positions")}
          className="btn-danger w-full text-center font-semibold"
        >
          {busy === "squareoff" ? "executing…" : "🛑 Square off all positions"}
        </button>
      </div>
    </div>
  );
}
