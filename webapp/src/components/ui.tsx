// src/components/ui.tsx
// Shared UI primitives used across all pages.

import { clsx } from "clsx";
import type { ReactNode } from "react";

// ── StatCard ──────────────────────────────────────────────────────────────────

interface StatCardProps {
  label:    string;
  value:    ReactNode;
  sub?:     ReactNode;
  accent?:  "up" | "down" | "warn" | "info" | "default";
  animate?: boolean;
}

export function StatCard({ label, value, sub, accent = "default", animate }: StatCardProps) {
  const accentColor = {
    up:      "border-up/30",
    down:    "border-down/30",
    warn:    "border-warn/30",
    info:    "border-info/30",
    default: "border-surface-border",
  }[accent];

  return (
    <div className={clsx("card p-4 flex flex-col gap-1 border", accentColor, animate && "animate-slide-up")}>
      <span className="stat-label">{label}</span>
      <span className="stat-value">{value}</span>
      {sub && <span className="text-xs text-white/40 font-mono mt-0.5">{sub}</span>}
    </div>
  );
}

// ── PnlChip ───────────────────────────────────────────────────────────────────

export function PnlChip({ value }: { value: number | null }) {
  if (value == null) return <span className="text-white/30 font-mono text-sm">—</span>;
  const cls = value > 0 ? "badge-up" : value < 0 ? "badge-down" : "text-white/40 text-xs font-mono";
  const sign = value > 0 ? "+" : "";
  return <span className={cls}>{sign}{value.toFixed(2)}%</span>;
}

// ── DirectionBadge ────────────────────────────────────────────────────────────

export function DirectionBadge({ dir }: { dir: "LONG" | "SHORT" }) {
  return dir === "LONG"
    ? <span className="badge-up">LONG</span>
    : <span className="badge-down">SHORT</span>;
}

// ── ConvictionDots ────────────────────────────────────────────────────────────

export function ConvictionDots({ score }: { score: number }) {
  return (
    <span className="flex gap-[3px] items-center">
      {Array.from({ length: 10 }, (_, i) => (
        <span
          key={i}
          className={clsx(
            "inline-block w-1.5 h-1.5 rounded-full",
            i < score
              ? score >= 8 ? "bg-up" : score >= 6 ? "bg-warn" : "bg-down"
              : "bg-white/10"
          )}
        />
      ))}
      <span className="ml-1 text-xs font-mono text-white/40">{score}/10</span>
    </span>
  );
}

// ── Spinner ───────────────────────────────────────────────────────────────────

export function Spinner({ size = 20 }: { size?: number }) {
  return (
    <svg
      width={size} height={size} viewBox="0 0 24 24"
      className="animate-spin text-brand-400"
      fill="none" stroke="currentColor" strokeWidth={2}
    >
      <circle cx={12} cy={12} r={9} strokeOpacity={0.2} />
      <path d="M21 12a9 9 0 0 0-9-9" strokeLinecap="round" />
    </svg>
  );
}

// ── EmptyState ────────────────────────────────────────────────────────────────

export function EmptyState({ message }: { message: string }) {
  return (
    <div className="flex flex-col items-center justify-center py-16 text-white/25">
      <svg width={40} height={40} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1}>
        <circle cx={12} cy={12} r={9} />
        <path d="M12 8v4m0 4h.01" strokeLinecap="round" />
      </svg>
      <p className="mt-3 text-sm font-mono">{message}</p>
    </div>
  );
}

// ── Section heading ───────────────────────────────────────────────────────────

export function SectionHead({ title, sub }: { title: string; sub?: string }) {
  return (
    <div className="mb-4">
      <h2 className="font-display text-lg font-semibold text-white">{title}</h2>
      {sub && <p className="text-xs text-white/40 font-mono mt-0.5">{sub}</p>}
    </div>
  );
}

// ── Live dot ──────────────────────────────────────────────────────────────────

export function LiveDot({ connected }: { connected: boolean }) {
  return (
    <span className="flex items-center gap-1.5 text-xs font-mono">
      <span className={clsx(
        "inline-block w-1.5 h-1.5 rounded-full",
        connected ? "bg-up animate-pulse-slow" : "bg-down"
      )} />
      <span className={connected ? "text-up" : "text-down/60"}>
        {connected ? "live" : "offline"}
      </span>
    </span>
  );
}

// ── Table ─────────────────────────────────────────────────────────────────────

export function Table({ headers, children }: { headers: string[]; children: ReactNode }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-surface-border">
            {headers.map(h => (
              <th key={h} className="text-left py-2.5 px-3 text-xs font-mono uppercase tracking-widest text-white/30 font-normal">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-surface-border/50">
          {children}
        </tbody>
      </table>
    </div>
  );
}

export function Td({ children, className }: { children: ReactNode; className?: string }) {
  return <td className={clsx("py-2.5 px-3 text-white/80", className)}>{children}</td>;
}
