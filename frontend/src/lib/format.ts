import type { Category, ChangedFile } from "./types";

export function relativeTime(iso: string): string {
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000;
  if (!Number.isFinite(seconds)) return "";
  const units: [number, string][] = [
    [60, "second"],
    [60, "minute"],
    [24, "hour"],
    [30, "day"],
    [12, "month"],
    [Infinity, "year"],
  ];
  let value = Math.max(0, seconds);
  for (const [size, unit] of units) {
    if (value < size) {
      const n = Math.floor(value);
      return n <= 1 && unit === "second" ? "just now" : `${n} ${unit}${n === 1 ? "" : "s"} ago`;
    }
    value /= size;
  }
  return "";
}

export const STATUS_LETTER: Record<ChangedFile["status"], string> = {
  added: "A",
  modified: "M",
  deleted: "D",
  renamed: "R",
  copied: "C",
  typechange: "T",
};

export const CATEGORY_LABEL: Record<Category, string> = {
  source: "Logic",
  test: "Tests",
  template: "Templates",
  style: "Styles",
  config: "Config & build",
  migration: "Migrations",
  docs: "Docs",
  generated: "Generated & locks",
  asset: "Assets",
  other: "Other",
};

export const KIND_LABEL: Record<string, string> = {
  function: "function",
  method: "method",
  class: "class",
  interface: "interface",
  type: "type",
  enum: "enum",
  object: "object",
  module: "module code",
};

export function basename(path: string): string {
  return path.slice(path.lastIndexOf("/") + 1);
}

export function dirname(path: string): string {
  const i = path.lastIndexOf("/");
  return i === -1 ? "" : path.slice(0, i);
}

export const HLJS_LANGUAGE: Record<string, string> = {
  python: "python",
  javascript: "javascript",
  jsx: "javascript",
  typescript: "typescript",
  tsx: "typescript",
  html: "django",
  vue: "xml",
  svelte: "xml",
  css: "css",
  scss: "scss",
  less: "less",
  json: "json",
  yaml: "yaml",
  toml: "ini",
  markdown: "markdown",
  sql: "sql",
  shell: "bash",
  go: "go",
  java: "java",
  kotlin: "kotlin",
  ruby: "ruby",
  php: "php",
  rust: "rust",
};
