import { useEffect, useState } from "react";
import { storage } from "./storage";

export type ThemeChoice = "system" | "light" | "dark";

export interface Theme {
  choice: ThemeChoice;
  resolved: "light" | "dark";
  cycle: () => void;
}

const media = () => window.matchMedia("(prefers-color-scheme: dark)");

export function useTheme(): Theme {
  const [choice, setChoice] = useState<ThemeChoice>(() => storage.get<ThemeChoice>("prmap:theme", "system"));
  const [systemDark, setSystemDark] = useState(() => media().matches);

  useEffect(() => {
    const m = media();
    const onChange = () => setSystemDark(m.matches);
    m.addEventListener("change", onChange);
    return () => m.removeEventListener("change", onChange);
  }, []);

  const resolved = choice === "system" ? (systemDark ? "dark" : "light") : choice;

  useEffect(() => {
    document.documentElement.dataset.theme = resolved;
    storage.set("prmap:theme", choice);
  }, [resolved, choice]);

  const cycle = () => setChoice((c) => (c === "system" ? "light" : c === "light" ? "dark" : "system"));
  return { choice, resolved, cycle };
}

/** Read a CSS custom property (used to feed theme colours into the canvas graph). */
export function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
