// Cutting wide (or tall) emotes into square pieces, like the desktop app does:
//   KEKW (4:1)  ->  :KEKW1: :KEKW2: :KEKW3: :KEKW4:
// The Worker can't decode GIFs itself (10 ms CPU on the free plan), so the cropping is done by the
// Cloudflare Images binding (env.IMAGES). Only the numbers are worked out here.

export const MAX_PIECES = 8;                 // Discord shows up to ~8 emojis side by side on one line
export const PIECE_SIZES = [128, 96, 64];    // tried in this order until every piece fits in 256 KB

// Same rule as auto_grid() in the desktop app: each piece is one full-height square.
// 384x112 (3.4:1) -> 4 pieces, 256x128 (2:1) -> 2, 160x128 -> 1 (not cut).
export function pieceGrid(w, h) {
  w = Math.max(1, Number(w) || 1);
  h = Math.max(1, Number(h) || 1);
  const r = w >= h ? w / h : h / w;
  const n = r < 1.5 ? 1 : Math.min(MAX_PIECES, Math.ceil(r - 0.25));
  return w >= h ? { cols: n, rows: 1, count: n } : { cols: 1, rows: n, count: n };
}

// KEKW -> KEKW1, KEKW2 … (the name is shortened so the number still fits in 32 characters)
export function pieceNames(name, count) {
  if (count <= 1) return [name];
  const digits = String(count).length;
  let base = String(name).slice(0, 32 - digits);
  if (base.length < 1) base = "emote";
  return Array.from({ length: count }, (_, i) => `${base}${i + 1}`);
}

// Where piece i sits in the resized image (left to right, then top to bottom).
export function pieceBox(i, cols, size) {
  return { mode: "area", left: (i % cols) * size, top: Math.floor(i / cols) * size, width: size, height: size };
}

// Cuts `bytes` (a GIF/PNG/WebP) into pieces with the Images binding.
// Returns [{ bytes, size }] in order. Throws if Images isn't available or a piece can't fit `limit`.
export async function cutWithImages(images, bytes, { cols, rows, animated, limit }) {
  if (!images) throw new Error("Images binding missing");
  const count = cols * rows;
  const format = animated ? "image/gif" : "image/png";
  const one = async (i, size) => {
    const res = await images
      .input(new Response(bytes).body)
      .transform({ width: cols * size, height: rows * size, fit: "squeeze" })
      .transform({ trim: pieceBox(i, cols, size) })
      .output({ format, anim: true });
    const out = new Uint8Array(await res.response().arrayBuffer());
    return { bytes: out, size };
  };
  // All pieces at full size in parallel; only the ones that are too big are made again smaller.
  let pieces = await Promise.all(Array.from({ length: count }, (_, i) => one(i, PIECE_SIZES[0])));
  for (const size of PIECE_SIZES.slice(1)) {
    const big = pieces.map((p, i) => (p.bytes.length > limit ? i : -1)).filter((i) => i >= 0);
    if (!big.length) break;
    const redone = await Promise.all(big.map((i) => one(i, size)));
    big.forEach((i, k) => { pieces[i] = redone[k]; });
  }
  if (pieces.some((p) => p.bytes.length > limit)) throw new Error("A piece is still too big");
  return pieces;
}

// One emote that is too big for 256 KB: make it smaller instead of failing.
export async function shrinkWithImages(images, bytes, { animated, limit }) {
  if (!images) throw new Error("Images binding missing");
  for (const size of PIECE_SIZES) {
    const res = await images
      .input(new Response(bytes).body)
      .transform({ width: size, height: size, fit: "scale-down" })
      .output({ format: animated ? "image/gif" : "image/png", anim: true });
    const out = new Uint8Array(await res.response().arrayBuffer());
    if (out.length <= limit) return out;
  }
  throw new Error("Still too big");
}
