import { useEffect, useState } from "react";

/** Light or dark, following the system as the app always does. It lands on
 *  <html> as .light or .dark so portalled menus and dialogs follow it too. */
export function useSystemMode(): "light" | "dark" {
  const media = window.matchMedia("(prefers-color-scheme: dark)");
  const [mode, setMode] = useState<"light" | "dark">(media.matches ? "dark" : "light");
  useEffect(() => {
    const update = () => setMode(media.matches ? "dark" : "light");
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, [media]);
  useEffect(() => {
    const root = document.documentElement;
    root.classList.toggle("dark", mode === "dark");
    root.classList.toggle("light", mode === "light");
  }, [mode]);
  return mode;
}
