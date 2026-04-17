// src/pages/Dashboard.tsx
import { useMemo } from "react";
import {
  AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, ReferenceLine,
} from "recharts";
import { format } from "date-fns";
import { api } from "../lib/api";
import { useQuery } from "../hooks/useQuery";
import type { LiveState } from "../hooks/useLive";
import {
  StatCard, PnlChip, DirectionBadge, ConvictionDots,
  Spinner, EmptyState, SectionHead, Table, Td,
} from "../components/ui";

interface Props { live: LiveState }

export function Dashboard({ live }: Props) {
  const { data: history } = useQuery(() => api.pnlHistory(14), [], 60_000);
  const { data: trades }  = useQuery(() => api.trades(5),      [], 30_000);

  const chartData = useMemo(() =>
    (history?.history ?? [])
      .slice()
      .reverse()
      .map(d => ({
        date:   format(new Date(d.date), "dd MMM"),
        pnl:    +d.gross_pnl.toFixed(0),
        wr:     +(d.win_rate * 100).toFixed(1),
      })),
  [history]);

  const positions   = Object.values(live.positions);
  const perf        = live.perf;
  const totalPnl    = perf?.daily_pnl_pct ?? 0;
  const pnlAccent   = totalPnl > 0 ? "up" : totalPnl < 0 ? "down" : "default";

  return (
    <div className="animate-fade-in space-y-8">
      {/* ── Page header ──────────────────────────────────────────── */}
      <div className="flex items-end justify-between">
        <div>
          <h1 className="font-display text-2xl font-bold text-white">Dashboard</h1>
          <p className="text-xs font-mono text-white/30 mt-0.5">
            {format(new Date(), "EEEE, d MMMM yyyy")}
          </p>
        </div>
        <p className="text-xs font-mono text-white/25">
          last update {live.lastUpdate ? format(live.lastUpdate, "HH:mm:ss") : "—"}
        </p>
      </div>

      {/* ── Top stats ────────────────────────────────────────────── */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard
          label="today P&L"
          value={<PnlChip value={totalPnl} />}
          sub={`${perf?.trades_today ?? 0} trades`}
          accent={pnlAccent}
          animate
        />
        <StatCard
          label="win rate (L10)"
          value={`${((perf?.win_rate_last_10 ?? 0.5) * 100).toFixed(0)}%`}
          sub="last 10 trades"
          accent={(perf?.win_rate_last_10 ?? 0.5) >= 0.5 ? "up" : "down"}
          animate
        />
        <StatCard
          label="open positions"
          value={positions.length}
          sub={`of 5 max`}
          animate
        />
        <StatCard
          label="loss streak"
          value={perf?.consecutive_losses ?? 0}
          sub={
            (perf?.consecutive_losses ?? 0) >= 3
              ? "⚠ reduced size mode"
              : "within normal range"
          }
          accent={(perf?.consecutive_losses ?? 0) >= 3 ? "warn" : "default"}
          animate
        />
      </div>

      {/* ── P&L equity chart ─────────────────────────────────────── */}
      <div className="card p-5">
        <SectionHead title="14-day P&L (₹)" sub="realised gross profit/loss per day" />
        {chartData.length === 0 ? (
          <EmptyState message="no history yet" />
        ) : (
          <ResponsiveContainer width="100%" height={200}>
            <AreaChart data={chartData} margin={{ top: 4, right: 4, bottom: 0, left: -10 }}>
              <defs>
                <linearGradient id="pnlGrad" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%"  stopColor="#22c55e" stopOpacity={0.25} />
                  <stop offset="95%" stopColor="#22c55e" stopOpacity={0} />
                </linearGradient>
                <linearGradient id="pnlGradNeg" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%"  stopColor="#ef4444" stopOpacity={0.25} />
                  <stop offset="95%" stopColor="#ef4444" stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis dataKey="date" tick={{ fontSize: 11 }} />
              <YAxis tick={{ fontSize: 11 }} tickFormatter={v => `₹${(v/1000).toFixed(0)}k`} />
              <ReferenceLine y={0} stroke="rgba(255,255,255,0.15)" />
              <Tooltip
                contentStyle={{ background: "#1c2429", border: "1px solid #2a353d", borderRadius: 8, fontSize: 12 }}
                labelStyle={{ color: "rgba(255,255,255,0.5)" }}
                formatter={(v: number) => [`₹${v.toLocaleString("en-IN")}`, "P&L"]}
              />
              <Area
                type="monotone" dataKey="pnl"
                stroke="#22c55e" strokeWidth={2}
                fill="url(#pnlGrad)"
                dot={{ r: 3, fill: "#22c55e" }}
                activeDot={{ r: 5 }}
              />
            </AreaChart>
          </ResponsiveContainer>
        )}
      </div>

      {/* ── Open positions ────────────────────────────────────────── */}
      <div className="card">
        <div className="p-5 pb-0">
          <SectionHead
            title="Open positions"
            sub={`${positions.length} active`}
          />
        </div>
        {positions.length === 0 ? (
          <EmptyState message="no open positions" />
        ) : (
          <Table headers={["Symbol", "Dir", "Qty", "Entry", "SL", "Target", "Conv", "Setup"]}>
            {positions.map(pos => (
              <tr key={pos.symbol} className="hover:bg-surface-3/50 transition-colors">
                <Td><span className="font-mono font-medium text-white">{pos.symbol}</span></Td>
                <Td><DirectionBadge dir={pos.direction} /></Td>
                <Td><span className="font-mono">{pos.quantity}</span></Td>
                <Td><span className="font-mono">₹{pos.entry_price.toLocaleString("en-IN")}</span></Td>
                <Td><span className="font-mono text-down">₹{pos.stop_loss.toLocaleString("en-IN")}</span></Td>
                <Td><span className="font-mono text-up">₹{pos.target_1.toLocaleString("en-IN")}</span></Td>
                <Td><ConvictionDots score={pos.conviction} /></Td>
                <Td><span className="text-xs text-white/40 font-mono">{pos.setup_type}</span></Td>
              </tr>
            ))}
          </Table>
        )}
      </div>

      {/* ── Recent trades ─────────────────────────────────────────── */}
      <div className="card">
        <div className="p-5 pb-0">
          <SectionHead title="Recent trades" sub="last 5 closed" />
        </div>
        {!trades ? (
          <div className="py-8 flex justify-center"><Spinner /></div>
        ) : trades.trades.length === 0 ? (
          <EmptyState message="no trades yet" />
        ) : (
          <Table headers={["Symbol", "Dir", "Entry", "Exit", "P&L", "Status", "Closed"]}>
            {trades.trades.map(t => (
              <tr key={t.id} className="hover:bg-surface-3/50 transition-colors">
                <Td><span className="font-mono font-medium text-white">{t.symbol}</span></Td>
                <Td><DirectionBadge dir={t.direction} /></Td>
                <Td><span className="font-mono">₹{t.entry?.toLocaleString("en-IN") ?? "—"}</span></Td>
                <Td><span className="font-mono">₹{t.exit?.toLocaleString("en-IN") ?? "—"}</span></Td>
                <Td><PnlChip value={t.pnl_pct} /></Td>
                <Td><span className="text-xs font-mono text-white/40">{t.status}</span></Td>
                <Td><span className="text-xs font-mono text-white/30">
                  {t.closed_at ? format(new Date(t.closed_at), "HH:mm") : "—"}
                </span></Td>
              </tr>
            ))}
          </Table>
        )}
      </div>
    </div>
  );
}
