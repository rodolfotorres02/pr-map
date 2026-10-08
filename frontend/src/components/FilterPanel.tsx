import type { RefObject } from "react";
import { PRESETS, activePreset, facetCounts, toggleChip, type FileFilter } from "../lib/filters";
import { CATEGORY_LABEL } from "../lib/format";
import type { Category, ChangedFile } from "../lib/types";

interface Props {
  files: ChangedFile[];
  filter: FileFilter;
  onChange: (filter: FileFilter) => void;
  reviewed: Set<string>;
  visibleCount: number;
  inputRef: RefObject<HTMLInputElement>;
}

const CATEGORY_ORDER: Category[] = [
  "source", "test", "template", "style", "config", "migration", "docs", "generated", "asset", "other",
];

export function FilterPanel({ files, filter, onChange, reviewed, visibleCount, inputRef }: Props) {
  const presentCategories = CATEGORY_ORDER.filter((c) => files.some((f) => f.category === c));
  const presentExts = [...new Set(files.map((f) => f.ext))].sort((a, b) => {
    const count = (e: string) => files.filter((f) => f.ext === e).length;
    return count(b) - count(a) || a.localeCompare(b);
  });
  const counts = facetCounts(files, filter, reviewed);
  const preset = activePreset(filter);
  const restricted = filter.categories || filter.extensions || filter.patterns.trim() || filter.hideReviewed;

  return (
    <section className="filters">
      <div className="presets" role="radiogroup" aria-label="Review focus">
        {PRESETS.map((p) => (
          <button
            key={p.id}
            role="radio"
            aria-checked={preset === p.id}
            className={`preset ${preset === p.id ? "on" : ""}`}
            title={p.hint}
            onClick={() => onChange({ ...filter, ...p.filter, patterns: "" })}
          >
            {p.label}
          </button>
        ))}
      </div>

      <div className="facet">
        <div className="facet-head">
          <span>Role</span>
          <span className="facet-hint" title="Click narrows to a role and further clicks add more. Alt-click shows everything except that role.">
            alt-click to exclude
          </span>
        </div>
        <div className="chips">
          {presentCategories.map((c) => {
            const on = !filter.categories || filter.categories.includes(c);
            return (
              <button
                key={c}
                className={`chip ${on ? "on" : "off"}`}
                aria-pressed={on}
                onClick={(e) => onChange({ ...filter, categories: toggleChip(filter.categories, c, presentCategories, e.altKey) })}
              >
                <span className="swatch" style={{ background: `var(--cat-${c})` }} />
                {CATEGORY_LABEL[c]}
                <span className="chip-count">{counts.categories.get(c) ?? 0}</span>
              </button>
            );
          })}
        </div>
      </div>

      <div className="facet">
        <div className="facet-head">
          <span>File type</span>
        </div>
        <div className="chips">
          {presentExts.map((ext) => {
            const on = !filter.extensions || filter.extensions.includes(ext);
            return (
              <button
                key={ext}
                className={`chip chip-mono ${on ? "on" : "off"}`}
                aria-pressed={on}
                onClick={(e) => onChange({ ...filter, extensions: toggleChip(filter.extensions, ext, presentExts, e.altKey) })}
              >
                {ext}
                <span className="chip-count">{counts.extensions.get(ext) ?? 0}</span>
              </button>
            );
          })}
        </div>
      </div>

      <div className="facet">
        <label className="facet-head" htmlFor="path-filter">
          <span>Path</span>
          <span className="facet-hint">
            press <kbd className="kbd">/</kbd>
          </span>
        </label>
        <input
          id="path-filter"
          ref={inputRef}
          className="input input-small mono"
          placeholder="views  src/**  !**/migrations/**"
          value={filter.patterns}
          onChange={(e) => onChange({ ...filter, patterns: e.target.value })}
          onKeyDown={(e) => e.key === "Escape" && (e.target as HTMLInputElement).blur()}
          spellCheck={false}
        />
      </div>

      <div className="filter-foot">
        <label className="check check-small">
          <input
            type="checkbox"
            checked={filter.hideReviewed}
            onChange={(e) => onChange({ ...filter, hideReviewed: e.target.checked })}
          />
          Hide reviewed
        </label>
        <span className="muted small">
          {visibleCount} of {files.length}
        </span>
        {restricted && (
          <button
            className="btn btn-link small"
            onClick={() => onChange({ categories: null, extensions: null, patterns: "", hideReviewed: false })}
          >
            Reset
          </button>
        )}
      </div>
    </section>
  );
}
