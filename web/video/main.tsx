// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

// Dev-only entry for the system-map film (/video/index.html on the Vite dev
// server). `?repo=<id>` picks a captured map, `?cut=story` the long cut, and
// `?play` loops it in real time. scripts/render-repo-video.mjs drives
// window.__video to pick a call site and seek frame by frame.
import { flushSync } from "react-dom";
import { createRoot } from "react-dom/client";

import type { CapturedMap } from "@/landing-hero/LandingMap";
import captured from "@/landing-hero/maps";

import RepoVideo, { planVideo, type CallSite, type Cut, type Excerpt } from "./RepoVideo";

declare global {
  interface Window {
    __video?: {
      duration: number;
      repo: CapturedMap["repo"];
      sites: CallSite[];
      /** Show candidate `index` with its source; false when the map does not draw its edge. */
      choose: (index: number, excerpt: Excerpt) => Promise<boolean>;
      seek: (t: number) => Promise<void>;
    };
  }
}

const params = new URLSearchParams(window.location.search);
const id = params.get("repo") || "psf__requests";
const cut: Cut = params.get("cut") === "story" ? "story" : "short";
const scene = (captured as CapturedMap[]).find((item) => item.id === id);
const plan = scene ? planVideo(scene, cut) : null;
const mount = document.getElementById("video")!;

if (!scene || !plan) {
  mount.textContent = `No ${cut} film for ${id}: it needs a drawn map with an anchored reference${
    cut === "story" ? " and a traced path" : ""
  }.`;
} else {
  const root = createRoot(mount);
  let t = Number(params.get("t") || 0);
  let site = plan.sites[0];
  let excerpt: Excerpt | null = null;
  const host = params.get("host") || "demo.codenib.ai";
  const render = () =>
    flushSync(() =>
      root.render(<RepoVideo captured={scene} plan={plan} site={site} t={t} excerpt={excerpt} host={host} />),
    );
  const frame = () => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  render();
  window.__video = {
    duration: plan.cues.total,
    repo: scene.repo,
    sites: plan.sites,
    async choose(index, next) {
      site = plan.sites[index];
      excerpt = next;
      render();
      await frame();
      return !!document.querySelector(
        `.poster-edge[data-source="${site.source}"][data-target="${site.target}"]`,
      );
    },
    async seek(next) {
      t = next;
      render();
      await frame();
      // A measured layout change re-renders once more; let it paint.
      await frame();
    },
  };
  if (params.has("play")) {
    const start = performance.now();
    const loop = (now: number) => {
      t = (now - start) % plan.cues.total;
      render();
      requestAnimationFrame(loop);
    };
    requestAnimationFrame(loop);
  }
}
