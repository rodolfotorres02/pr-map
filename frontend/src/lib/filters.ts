import picomatch from "picomatch";
import type { Category, ChangedFile } from "./types";

/**
 * A file passes the filter when it matches every active dimension:
 * category (its role) AND extension AND path patterns AND review state.
 * `null` sets mean "no restriction" on that dimension.
 */
export interface FileFilter {
  categories: Category[] | null;
  extensions: string[] | null;
  patterns: string; // space/comma separated globs; prefix with ! to exclude
  hideReviewed: boolean;
}

export const EMPTY_FILTER: FileFilter = { categories: null, extensions: null, patterns: "", hideReviewed: false };

export interface Preset {
  id: string;
  label: string;
  hint: string;
  filter: Partial<FileFilter>;
}

export const PRESETS: Preset[] = [
  { id: "all", label: "All files", hint: "Everything in the change", filter: { categories: null, extensions: null } },
  {
    id: "logic",
    label: "Logic only",
    hint: "Application code without tests, templates, config or generated files",
    filter: { categories: ["source"], extensions: null },
  },
  { id: "tests", label: "Tests only", hint: "Test files, fixtures and snapshots", filter: { categories: ["test"], extensions: null } },
  {
    id: "templates",
    label: "Templates & styles",
    hint: "HTML/Jinja/Django templates and stylesheets",
    filter: { categories: ["template", "style"], extensions: null },
  },
  {
    id: "no-noise",
    label: "Skip tests & noise",
    hint: "Hide tests, lockfiles, generated code, assets and docs",
    filter: { categories: ["source", "template", "style", "config", "migration", "other"], extensions: null },
  },
];

export function activePreset(filter: FileFilter): string | null {
  const sameList = (a: string[] | null | undefined, b: string[] | null | undefined) =>
    (a ?? null) === null ? (b ?? null) === null : !!b && a!.length === b.length && a!.every((x) => b.includes(x));
  const preset = PRESETS.find(
    (p) => sameList(p.filter.categories, filter.categories) && sameList(p.filter.extensions, filter.extensions),
  );
  return preset && !filter.patterns.trim() ? preset.id : null;
}

export function compilePatterns(patterns: string): ((path: string) => boolean) | null {
  const parts = patterns
    .split(/[\s,]+/)
    .map((p) => p.trim())
    .filter(Boolean);
  if (!parts.length) return null;
  const toGlob = (p: string) => (/[*?[\]{}]/.test(p) ? p : `**/*${p}*`); // bare words are substring matches
  const include = parts.filter((p) => !p.startsWith("!")).map(toGlob);
  const exclude = parts.filter((p) => p.startsWith("!")).map((p) => toGlob(p.slice(1)));
  const inc = include.length ? picomatch(include, { dot: true, nocase: true }) : null;
  const exc = exclude.length ? picomatch(exclude, { dot: true, nocase: true }) : null;
  return (path: string) => (!inc || inc(path)) && (!exc || !exc(path));
}

export function applyFilter(files: ChangedFile[], filter: FileFilter, reviewed: Set<string>): ChangedFile[] {
  const match = compilePatterns(filter.patterns);
  return files.filter(
    (f) =>
      (!filter.categories || filter.categories.includes(f.category)) &&
      (!filter.extensions || filter.extensions.includes(f.ext)) &&
      (!match || match(f.path)) &&
      (!filter.hideReviewed || !reviewed.has(f.path)),
  );
}

/** Counts per category / extension among files that pass the *other* dimensions. */
export function facetCounts(files: ChangedFile[], filter: FileFilter, reviewed: Set<string>) {
  const categories = new Map<string, number>();
  const extensions = new Map<string, number>();
  for (const f of applyFilter(files, { ...filter, categories: null }, reviewed)) {
    categories.set(f.category, (categories.get(f.category) ?? 0) + 1);
  }
  for (const f of applyFilter(files, { ...filter, extensions: null }, reviewed)) {
    extensions.set(f.ext, (extensions.get(f.ext) ?? 0) + 1);
  }
  return { categories, extensions };
}

/**
 * Chip toggling. With no restriction, a click narrows to that value; afterwards
 * clicks add/remove values. `exclusive` (alt/option-click) means "everything but this".
 */
export function toggleChip<T>(list: T[] | null, value: T, universe: T[], exclusive = false): T[] | null {
  if (exclusive) {
    const next = universe.filter((x) => x !== value);
    return next.length ? next : null;
  }
  if (list === null) return [value];
  const next = list.includes(value) ? list.filter((x) => x !== value) : [...list, value];
  if (next.length === 0 || universe.every((u) => next.includes(u))) return null;
  return next;
}
