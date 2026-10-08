import { useEffect, useMemo, useRef, useState } from "react";
import { STATUS_LETTER, CATEGORY_LABEL } from "../lib/format";
import type { ChangedFile } from "../lib/types";

interface Props {
  files: ChangedFile[];
  selected: string | null;
  reviewed: Set<string>;
  onSelect: (path: string) => void;
  onToggleReviewed: (path: string) => void;
  totalFiles: number;
  onClearFilter: () => void;
}

interface DirNode {
  name: string;
  path: string;
  dirs: Map<string, DirNode>;
  files: ChangedFile[];
}

function buildTree(files: ChangedFile[]): DirNode {
  const root: DirNode = { name: "", path: "", dirs: new Map(), files: [] };
  for (const file of files) {
    const parts = file.path.split("/");
    let node = root;
    for (const part of parts.slice(0, -1)) {
      const path = node.path ? `${node.path}/${part}` : part;
      if (!node.dirs.has(part)) node.dirs.set(part, { name: part, path, dirs: new Map(), files: [] });
      node = node.dirs.get(part)!;
    }
    node.files.push(file);
  }
  // Collapse chains of single-child directories: src/app/views
  const compress = (node: DirNode): DirNode => {
    for (const [key, child] of node.dirs) node.dirs.set(key, compress(child));
    if (node.path && node.files.length === 0 && node.dirs.size === 1) {
      const only = [...node.dirs.values()][0];
      return { ...only, name: `${node.name}/${only.name}` };
    }
    return node;
  };
  return compress(root);
}

/** Files in the order the tree displays them (directories first), for j/k navigation. */
export function treeOrder(files: ChangedFile[]): ChangedFile[] {
  const out: ChangedFile[] = [];
  const walk = (node: DirNode) => {
    for (const dir of [...node.dirs.values()].sort((a, b) => a.name.localeCompare(b.name))) walk(dir);
    out.push(...[...node.files].sort((a, b) => a.path.localeCompare(b.path)));
  };
  walk(buildTree(files));
  return out;
}

export function FileTree({ files, selected, reviewed, onSelect, onToggleReviewed, totalFiles, onClearFilter }: Props) {
  const tree = useMemo(() => buildTree(files), [files]);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const selectedRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    selectedRef.current?.scrollIntoView({ block: "nearest" });
  }, [selected]);

  if (!files.length) {
    return (
      <div className="tree-empty">
        <p>No files match these filters.</p>
        {totalFiles > 0 && (
          <button className="btn btn-small" onClick={onClearFilter}>
            Show all {totalFiles} files
          </button>
        )}
      </div>
    );
  }

  const toggle = (path: string) =>
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });

  const renderDir = (node: DirNode, depth: number): JSX.Element[] => {
    const rows: JSX.Element[] = [];
    const dirs = [...node.dirs.values()].sort((a, b) => a.name.localeCompare(b.name));
    for (const dir of dirs) {
      const isCollapsed = collapsed.has(dir.path);
      rows.push(
        <li key={`d:${dir.path}`}>
          <button className="tree-dir" style={{ paddingLeft: 8 + depth * 12 }} onClick={() => toggle(dir.path)} aria-expanded={!isCollapsed}>
            <svg className={`caret ${isCollapsed ? "" : "open"}`} width="8" height="8" viewBox="0 0 8 8" aria-hidden="true">
              <path d="M2 1 L6 4 L2 7 Z" fill="currentColor" />
            </svg>
            {dir.name}
          </button>
        </li>,
      );
      if (!isCollapsed) rows.push(...renderDir(dir, depth + 1));
    }
    for (const file of [...node.files].sort((a, b) => a.path.localeCompare(b.path))) {
      const name = file.path.slice(file.path.lastIndexOf("/") + 1);
      const isSelected = file.path === selected;
      const isReviewed = reviewed.has(file.path);
      rows.push(
        <li key={file.path} className={`tree-file-row ${isReviewed ? "reviewed" : ""}`}>
          <input
            type="checkbox"
            className="tree-check"
            style={{ marginLeft: 8 + depth * 12 }}
            checked={isReviewed}
            onChange={() => onToggleReviewed(file.path)}
            aria-label={`Mark ${file.path} reviewed`}
            title="Reviewed (r)"
          />
          <button
            ref={isSelected ? selectedRef : undefined}
            className={`tree-file ${isSelected ? "selected" : ""}`}
            onClick={() => onSelect(file.path)}
            title={`${file.path}\n${CATEGORY_LABEL[file.category]}${file.old_path ? `\nrenamed from ${file.old_path}` : ""}`}
          >
            <span className={`status status-${file.status}`}>{STATUS_LETTER[file.status]}</span>
            <span className="swatch" style={{ background: `var(--cat-${file.category})` }} />
            <span className="tree-name">{name}</span>
            <span className="tree-stat">
              {file.binary ? (
                <span className="muted">bin</span>
              ) : (
                <>
                  {file.additions > 0 && <span className="add">+{file.additions}</span>}
                  {file.deletions > 0 && <span className="del">−{file.deletions}</span>}
                </>
              )}
            </span>
          </button>
        </li>,
      );
    }
    return rows;
  };

  return (
    <nav className="tree" aria-label="Changed files">
      <ul>{renderDir(tree, 0)}</ul>
      <p className="tree-help muted small">
        <kbd className="kbd">j</kbd> <kbd className="kbd">k</kbd> move between files, <kbd className="kbd">r</kbd> marks reviewed and moves on, <kbd className="kbd">p</kbd> plan
      </p>
    </nav>
  );
}
