import type { ReactNode } from "react";
import { CATEGORY_LABEL } from "../lib/format";
import type { Theme } from "../lib/theme";
import type { Category, ChangeStatus } from "../lib/types";

export function BrandMark({ size = 22 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden="true" className="brand-mark">
      <path d="M4 24 L14 10 L22 18 L28 8" fill="none" stroke="var(--cat-source)" strokeWidth="4" strokeLinecap="round" strokeLinejoin="round" />
      <circle cx="14" cy="10" r="3.5" fill="var(--canvas)" stroke="var(--ink)" strokeWidth="2.5" />
      <circle cx="22" cy="18" r="3.5" fill="var(--canvas)" stroke="var(--ink)" strokeWidth="2.5" />
    </svg>
  );
}

export function ThemeButton({ theme }: { theme: Theme }) {
  const label = theme.choice === "system" ? "Theme: system" : theme.choice === "light" ? "Theme: light" : "Theme: dark";
  return (
    <button className="btn btn-ghost btn-icon" onClick={theme.cycle} title={`${label} (click to change)`} aria-label={label}>
      <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true">
        <circle cx="8" cy="8" r="6.25" fill="none" stroke="currentColor" strokeWidth="1.5" />
        <path d="M8 1.75 A6.25 6.25 0 0 1 8 14.25 Z" fill="currentColor" />
      </svg>
    </button>
  );
}

export function CategorySwatch({ category }: { category: Category }) {
  return <span className="swatch" style={{ background: `var(--cat-${category})` }} title={CATEGORY_LABEL[category]} />;
}

export function ChangeBadge({ change }: { change: ChangeStatus | null }) {
  if (!change) return null;
  return <span className={`change change-${change}`}>{change}</span>;
}

export function Kbd({ children }: { children: ReactNode }) {
  return <kbd className="kbd">{children}</kbd>;
}

export function Spinner() {
  return <span className="spinner" aria-hidden="true" />;
}
