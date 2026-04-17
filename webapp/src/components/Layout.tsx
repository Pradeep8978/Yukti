// src/components/Layout.tsx
import { NavLink } from "react-router-dom";
import { clsx } from "clsx";
import { LiveDot } from "./ui";
import type { ReactNode } from "react";

const NAV = [
  { to: "/",         label: "Dashboard",  icon: GridIcon },
  { to: "/positions",label: "Positions",  icon: LayersIcon },
  { to: "/trades",   label: "Trades",     icon: ActivityIcon },
  { to: "/journal",  label: "Journal",    icon: BookIcon },
  { to: "/control",  label: "Control",    icon: ShieldIcon },
];

interface LayoutProps {
  children: ReactNode;
  connected: boolean;
  halted:    boolean;
}

export function Layout({ children, connected, halted }: LayoutProps) {
  return (
    <div className="flex min-h-screen bg-surface-0">
      {/* ── Sidebar ─────────────────────────────────────────────── */}
      <aside className="w-56 shrink-0 border-r border-surface-border flex flex-col py-6 px-3 sticky top-0 h-screen">
        {/* Logo */}
        <div className="px-3 mb-8">
          <p className="font-display text-xl font-bold text-white tracking-tight">
            Yukti
          </p>
          <p className="text-xs font-mono text-white/30 mt-0.5">युक्ति · trading agent</p>
        </div>

        {/* Status pill */}
        <div className="px-3 mb-6">
          <div className={clsx(
            "rounded-lg px-3 py-2 flex items-center justify-between",
            halted ? "bg-down/10 border border-down/20" : "bg-surface-2 border border-surface-border"
          )}>
            <span className={clsx("text-xs font-mono font-medium", halted ? "text-down" : "text-white/50")}>
              {halted ? "⚠ HALTED" : "ACTIVE"}
            </span>
            <LiveDot connected={connected} />
          </div>
        </div>

        {/* Nav links */}
        <nav className="flex flex-col gap-0.5 flex-1">
          {NAV.map(({ to, label, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              end={to === "/"}
              className={({ isActive }) => clsx(
                "flex items-center gap-2.5 px-3 py-2 rounded-lg text-sm transition-all",
                isActive
                  ? "bg-brand-600/15 text-brand-400 font-medium border border-brand-600/25"
                  : "text-white/45 hover:text-white/80 hover:bg-surface-2"
              )}
            >
              <Icon size={15} />
              {label}
            </NavLink>
          ))}
        </nav>

        {/* Footer */}
        <div className="px-3 mt-4">
          <p className="text-[10px] font-mono text-white/20">v0.1.0 · NSE/BSE</p>
        </div>
      </aside>

      {/* ── Main content ─────────────────────────────────────────── */}
      <main className="flex-1 min-w-0 p-8 overflow-y-auto">
        {children}
      </main>
    </div>
  );
}

// ── Inline icon components ────────────────────────────────────────────────────

function GridIcon({ size = 16 }: { size?: number }) {
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round">
    <rect x={3} y={3} width={7} height={7} rx={1} /><rect x={14} y={3} width={7} height={7} rx={1} />
    <rect x={3} y={14} width={7} height={7} rx={1} /><rect x={14} y={14} width={7} height={7} rx={1} />
  </svg>;
}

function LayersIcon({ size = 16 }: { size?: number }) {
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round">
    <polygon points="12 2 22 8.5 12 15 2 8.5 12 2" /><polyline points="2 15.5 12 22 22 15.5" /><polyline points="2 12 12 18.5 22 12" />
  </svg>;
}

function ActivityIcon({ size = 16 }: { size?: number }) {
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round">
    <polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />
  </svg>;
}

function BookIcon({ size = 16 }: { size?: number }) {
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round">
    <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" /><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" />
  </svg>;
}

function ShieldIcon({ size = 16 }: { size?: number }) {
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round">
    <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
  </svg>;
}
