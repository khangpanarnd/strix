/**
 * Pixel ground textures for the landing page scenery (the grass, dirt and stone tiles).
 *
 * Drawn once into small canvases and handed to CSS as `url(...)` values. The mascot is
 * an image (see Penguin.tsx), not generated here.
 */

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
