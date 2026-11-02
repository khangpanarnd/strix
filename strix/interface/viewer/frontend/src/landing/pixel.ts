/**
 * Pixel characters and ground textures for the landing page.
 *
 * Ported from the DE slide template (assets/de-template.html). Pure functions, no
 * React: a character is an SVG string built only from the constants below (never
 * from user input), so it is safe to inject with dangerouslySetInnerHTML.
 */

const OFFSET = 4;

function px(x: number, y: number, w: number, h: number, fill: string): string {
  return `<rect x="${x + OFFSET}" y="${y + OFFSET}" width="${w}" height="${h}" fill="${fill}"/>`;
}

type HatBuilder = (main: string, dark: string) => string;

const HATS = {
  cap: (c, d) => px(3, 0, 10, 3, c) + px(3, 2, 10, 1, d) + px(10, 3, 4, 1, d),
  hard: (c, d) => px(4, -1, 8, 3, c) + px(2, 2, 12, 1, d) + px(7, -1, 2, 3, d),
  hood: (c, d) =>
    px(3, -1, 10, 3, c) + px(3, 2, 1, 6, c) + px(12, 2, 1, 6, c) + px(4, 2, 8, 1, d),
  helmet: (c, d) =>
    px(3, -1, 10, 4, c) +
    px(3, 3, 1, 4, c) +
    px(12, 3, 1, 4, c) +
    px(3, 2, 10, 1, d) +
    px(7, -1, 2, 3, d),
  hair: (c, d) =>
    px(4, 0, 8, 2, c) + px(4, 2, 1, 2, c) + px(11, 2, 1, 2, c) + px(5, 2, 3, 1, d),
  headset: (c, d) =>
    px(4, 0, 8, 2, c) +
    px(3, 1, 10, 1, "#1f2937") +
    px(3, 2, 1, 4, "#1f2937") +
    px(12, 2, 1, 4, "#1f2937") +
    px(12, 6, 2, 1, "#1f2937") +
    px(13, 6, 1, 1, d),
} satisfies Record<string, HatBuilder>;

const PROPS = {
  scroll:
    px(12, 11, 1, 5, "#c8a96a") +
    px(13, 11, 5, 5, "#f4ecd2") +
    px(18, 11, 1, 5, "#c8a96a") +
    px(14, 13, 3, 1, "#9a8a6a"),
  ruler:
    px(13, 9, 7, 6, "#3b82f6") +
    px(14, 11, 5, 1, "#dbeafe") +
    px(14, 13, 3, 1, "#dbeafe") +
    px(13, 9, 7, 1, "#1d4ed8"),
  pickaxe:
    px(13, 15, 1, 1, "#7a4f25") +
    px(14, 14, 1, 1, "#7a4f25") +
    px(15, 13, 1, 1, "#7a4f25") +
    px(15, 12, 1, 1, "#7a4f25") +
    px(16, 11, 1, 1, "#7a4f25") +
    px(16, 10, 1, 1, "#7a4f25") +
    px(17, 9, 1, 1, "#7a4f25") +
    px(14, 8, 7, 1, "#b8bec8") +
    px(13, 9, 1, 1, "#b8bec8") +
    px(21, 9, 1, 1, "#b8bec8") +
    px(17, 8, 1, 1, "#e5e7eb"),
  magnifier:
    px(13, 15, 1, 1, "#6b4a2b") +
    px(14, 14, 1, 1, "#6b4a2b") +
    px(15, 8, 3, 1, "#374151") +
    px(15, 12, 3, 1, "#374151") +
    px(14, 9, 1, 3, "#374151") +
    px(18, 9, 1, 3, "#374151") +
    px(15, 9, 3, 3, "#bfe9ff") +
    px(15, 9, 1, 1, "#ffffff"),
  flask:
    px(15, 9, 2, 3, "#cbd5e1") +
    px(14, 12, 4, 4, "#d1fae5") +
    px(14, 14, 4, 2, "#16a34a") +
    px(15, 8, 2, 1, "#94a3b8"),
  book: px(13, 10, 5, 6, "#7c3aed") + px(17, 10, 1, 6, "#efe7ff") + px(14, 12, 2, 1, "#c4b5fd"),
  flag:
    px(14, 3, 1, 13, "#6b4a2b") + px(15, 3, 5, 4, "#7c3aed") + px(16, 4, 2, 2, "#ddd6fe"),
  megaphone: px(13, 12, 2, 3, "#065f46") + px(15, 11, 2, 5, "#10b981") + px(17, 10, 2, 7, "#34d399"),
  clipboard:
    px(13, 9, 6, 7, "#a16207") +
    px(14, 10, 4, 5, "#ffffff") +
    px(15, 8, 2, 2, "#9ca3af") +
    px(14, 13, 1, 2, "#e5484d") +
    px(16, 12, 1, 3, "#16a34a"),
  shield:
    px(-3, 9, 6, 8, "#9aa4b5") +
    px(-3, 9, 6, 1, "#5b6474") +
    px(-3, 16, 6, 1, "#5b6474") +
    px(-1, 11, 2, 4, "#2563eb") +
    px(-2, 12, 4, 2, "#2563eb"),
} as const;

interface Spec {
  hat: keyof typeof HATS;
  hc: string;
  hd: string;
  skin: string;
  shirt: string;
  sd: string;
  pants: string;
  prop?: keyof typeof PROPS;
  propL?: keyof typeof PROPS;
}

const CHARACTERS = {
  planner: { hat: "cap", hc: "#2563eb", hd: "#1e40af", skin: "#f1c7a5", shirt: "#60a5fa", sd: "#3b82f6", pants: "#1e3a8a", prop: "scroll" },
  architect: { hat: "hard", hc: "#f59e0b", hd: "#b45309", skin: "#d9a27b", shirt: "#a78bfa", sd: "#7c3aed", pants: "#4c1d95", prop: "ruler" },
  coder: { hat: "hood", hc: "#16a34a", hd: "#166534", skin: "#f1c7a5", shirt: "#22c55e", sd: "#15803d", pants: "#1f2937", prop: "pickaxe" },
  reviewer: { hat: "hair", hc: "#5b3a1e", hd: "#3f2712", skin: "#b97a56", shirt: "#fb923c", sd: "#ea580c", pants: "#374151", prop: "magnifier" },
  tester: { hat: "cap", hc: "#eab308", hd: "#a16207", skin: "#8d5a3b", shirt: "#facc15", sd: "#ca8a04", pants: "#334155", prop: "flask" },
  writer: { hat: "hair", hc: "#1f2937", hd: "#111827", skin: "#f1c7a5", shirt: "#f472b6", sd: "#db2777", pants: "#475569", prop: "book" },
  guard: { hat: "helmet", hc: "#9ca3af", hd: "#6b7280", skin: "#d9a27b", shirt: "#cbd5e1", sd: "#94a3b8", pants: "#64748b", propL: "shield" },
  coach: { hat: "cap", hc: "#e5484d", hd: "#b91c1c", skin: "#d9a27b", shirt: "#f87171", sd: "#dc2626", pants: "#1f2937", prop: "clipboard" },
  captain: { hat: "headset", hc: "#3f2712", hd: "#7c3aed", skin: "#f1c7a5", shirt: "#22d3ee", sd: "#0891b2", pants: "#164e63", prop: "flag" },
  orchestrator: { hat: "hair", hc: "#7c2d12", hd: "#431407", skin: "#8d5a3b", shirt: "#34d399", sd: "#059669", pants: "#064e3b", prop: "megaphone" },
} satisfies Record<string, Spec>;

export type CharacterName = keyof typeof CHARACTERS;
export type CharacterAnim = "walk" | "work" | "wave" | "read" | "guard" | "think";

/** One character as an SVG string. `anim` picks a CSS-driven pose (see lp.css). */
export function characterSvg(name: CharacterName, anim?: CharacterAnim): string {
  const c: Spec = CHARACTERS[name];
  let s = "";
  s += `<g class="leg leg-l">${px(4, 16, 4, 6, c.pants)}${px(4, 22, 4, 2, "#3b2a1a")}</g>`;
  s += `<g class="leg leg-r">${px(8, 16, 4, 6, c.pants)}${px(8, 22, 4, 2, "#3b2a1a")}</g>`;
  s += '<g class="body-all">';
  s += `<g class="arm arm-l">${px(2, 8, 2, 6, c.sd)}${px(2, 14, 2, 2, c.skin)}${c.propL ? PROPS[c.propL] : ""}</g>`;
  s +=
    px(4, 8, 8, 8, c.shirt) +
    px(4, 8, 8, 1, c.sd) +
    px(4, 14, 8, 1, c.sd) +
    px(9, 10, 2, 2, "rgba(255,255,255,.55)");
  s += px(4, 0, 8, 8, c.skin) + px(4, 7, 8, 1, "rgba(0,0,0,.08)");
  s += `<g class="eyes">${px(6, 4, 1, 2, "#1f2937")}${px(9, 4, 1, 2, "#1f2937")}</g>`;
  s += px(7, 6, 2, 1, "#9a5a45");
  s += HATS[c.hat](c.hc, c.hd);
  s += `<g class="arm arm-r">${px(12, 8, 2, 6, c.sd)}${px(12, 14, 2, 2, c.skin)}${c.prop ? PROPS[c.prop] : ""}</g>`;
  s += "</g>";
  const cls = anim ? `ch anim-${anim}` : "ch";
  return `<svg class="${cls}" viewBox="0 0 30 32" aria-hidden="true" focusable="false">${s}</svg>`;
}

export interface GroundTextures {
  ground: string;
  grassSide: string;
  stone: string;
}

/** Small seeded generator so the textures look the same on every load. */
function rng(seed: number): () => number {
  let s = seed >>> 0;
  return () => {
    s = (Math.imul(s, 1664525) + 1013904223) >>> 0;
    return s / 4294967296;
  };
}

function texture(palette: string[], seed: number, grassTop = false): string {
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = 16;
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("2d canvas unavailable");
  const r = rng(seed);
  for (let y = 0; y < 16; y++) {
    for (let x = 0; x < 16; x++) {
      ctx.fillStyle = palette[Math.floor(r() * palette.length)];
      ctx.fillRect(x, y, 1, 1);
    }
  }
  if (grassTop) {
    const greens = ["#5d9c3b", "#6aad45", "#4f8a31", "#74b84c"];
    for (let x = 0; x < 16; x++) {
      const h = 4 + Math.floor(r() * 3);
      for (let y = 0; y < h; y++) {
        ctx.fillStyle = greens[Math.floor(r() * 4)];
        ctx.fillRect(x, y, 1, 1);
      }
    }
  }
  return `url(${canvas.toDataURL()})`;
}

/** Ground, grass-edge and stone tiles as CSS `url(...)` values, or null without a canvas. */
export function makeGroundTextures(): GroundTextures | null {
  try {
    const dirt = ["#866043", "#79553a", "#96704f", "#6c4b30", "#8f6847"];
    return {
      ground: texture(dirt, 7),
      grassSide: texture(dirt, 5, true),
      stone: texture(["#7f7f7f", "#747474", "#888888", "#6b6b6b", "#8f8f8f"], 11),
    };
  } catch {
    return null;
  }
}
