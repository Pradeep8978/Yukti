import { clsx } from "clsx";
import type { ReactNode } from "react";

export const Icons = {
  TrendingUp: (props: any) => (
    <svg {...props} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
      <polyline points="23 6 13.5 15.5 8.5 10.5 1 18" />
      <polyline points="17 6 23 6 23 12" />
    </svg>
  ),
  TrendingDown: (props: any) => (
    <svg {...props} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
      <polyline points="23 18 13.5 8.5 8.5 13.5 1 6" />
      <polyline points="17 18 23 18 23 12" />
    </svg>
  ),
  Shield: (props: any) => (
    <svg {...props} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
    </svg>
  ),
  Activity: (props: any) => (
    <svg {...props} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
      <polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />
    </svg>
  ),
  AlertTriangle: (props: any) => (
    <svg {...props} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
      <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
      <line x1="12" y1="9" x2="12" y2="13" />
      <line x1="12" y1="17" x2="12.01" y2="17" />
    </svg>
  ),
};

interface StatCardProps {
  label:    string;
  value:    ReactNode;
  sub?:     ReactNode;
  accent?:  "up" | "down" | "warn" | "info" | "default";
  animate?: boolean;
  icon?:    ReactNode;
}

export function StatCard({ label, value, sub, accent = "default", animate, icon }: StatCardProps) {
  const accentColor = {
    up:      "border-up/30 bg-up/5",
    down:    "border-down/30 bg-down/5",
    warn:    "border-warn/30 bg-warn/5",
    info:    "border-info/30 bg-info/5",
    default: "border-surface-border",
  }[accent];

  const iconColor = {
    up:      "text-up",
    down:    "text-down",
    warn:    "text-warn",
    info:    "text-info",
    default: "text-white/20",
  }[accent];

  return (
    <div className={clsx("card p-5 flex flex-col gap-1 border transition-all duration-300 hover:border-white/20", accentColor, animate && "animate-slide-up")}>
      <div className="flex items-start justify-between">
        <span className="stat-label">{label}</span>
        {icon && <div className={clsx("p-1.5 rounded-lg bg-white/5", iconColor)}>{icon}</div>}
      </div>
      <span className="stat-value mt-1">{value}</span>
      {sub && <span className="text-[11px] text-white/30 font-mono mt-1 flex items-center gap-1">
        {sub}
      </span>}
    </div>
  );
}

export function PnlChip({ value }: { value: number | null }) {
  if (value == null) return <span className="text-white/30 font-mono text-sm">—</span>;
  const isUp = value > 0;
  const isDown = value < 0;
  const cls = isUp ? "badge-up" : isDown ? "badge-down" : "text-white/40 text-xs font-mono";
  const sign = isUp ? "+" : "";
  return (
    <span className={clsx(cls, "flex items-center gap-1 w-fit")}>
      {isUp && <Icons.TrendingUp className="w-3 h-3" />}
      {isDown && <Icons.TrendingDown className="w-3 h-3" />}
      {sign}{value.toFixed(2)}%
    </span>
  );
}

export function DirectionBadge({ dir }: { dir: "LONG" | "SHORT" }) {
  return dir === "LONG"
    ? <span className="badge-up">LONG</span>
    : <span className="badge-down">SHORT</span>;
}

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

export function EmptyState({ message }: { message: string }) {
  return (
    <div className="flex flex-col items-center justify-center py-16 text-white/20">
      <div className="p-4 rounded-full bg-white/5 border border-white/5 mb-4 animate-pulse-slow">
        <Icons.AlertTriangle className="w-8 h-8 opacity-50" />
      </div>
      <p className="text-sm font-mono tracking-tight">{message}</p>
    </div>
  );
}

export function SectionHead({ title, sub }: { title: string; sub?: string }) {
  return (
    <div className="mb-6">
      <h2 className="font-display text-xl font-bold text-white tracking-tight">{title}</h2>
      {sub && <p className="text-[11px] text-white/30 font-mono uppercase tracking-widest mt-1.5">{sub}</p>}
    </div>
  );
}

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
