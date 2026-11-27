#!/usr/bin/env python3
"""
Emotes Downloader - 7TV -> Discord

* Downloads emotes from 7TV (animated or static) by link or ID,
  or whole emote sets / a user's active set.
* Resizes them to Discord's emoji format (128px, under 256 KB).
* Optionally cuts wide/big emotes into pieces (KEKW1, KEKW2, ...) and
  writes the text to paste in Discord (":KEKW1::KEKW2:").

Needs: pillow, customtkinter, pystray, tkinterdnd2  (run.bat installs them)

Run with no arguments for the window, or use the command line:
    python emotes_downloader.py https://7tv.app/emotes/<id> --split auto
"""

import argparse
import io
import json
import math
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    from PIL import Image, ImageChops, ImageSequence, ImageStat
except ImportError:  # pragma: no cover
    print("Pillow is missing. Install it with:  python -m pip install pillow")
    sys.exit(1)

__version__ = "1.5.1"
UA = f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) emotes-downloader/{__version__}"
GITHUB_REPO = "ISdook/emotes-downloader"
PORTABLE_ASSET = "Emotes-Downloader-Portable.exe"
SETUP_ASSET = "Emotes-Downloader-Setup.exe"
LEGACY_EXE_ASSET = "emotes-downloader.exe"   # v1.0.0 portable name
SCRIPT_ASSET = "emotes_downloader.py"
RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases"
RELEASES_PAGE = f"https://github.com/{GITHUB_REPO}/releases/latest"

API = "https://7tv.io/v3"
DISCORD_LIMIT = 256 * 1024          # Discord emoji upload limit
SAFE_LIMIT = DISCORD_LIMIT - 2048    # small safety margin
MAX_GRID = 10
CONFIG_PATH = Path.home() / ".emotes_downloader.json"

LANCZOS = Image.Resampling.LANCZOS

# Quality tiers tried in order until every piece fits under the limit:
# (palette colours, keep every Nth frame, scale of tile size)
# (palette colours, keep every Nth frame, scale of piece size, "unchanged pixel" threshold)
# Keeps every frame as long as possible and shrinks the size first: Discord never shows an
# emote bigger than 48 px, so 96/72/64/48 px pieces still look sharp, but dropped frames look choppy.
GIF_TIERS = [
    (256, 1, 1.0, 0), (256, 1, 1.0, 10), (160, 1, 1.0, 16),
    (256, 1, 0.75, 10), (160, 1, 0.75, 16),
    (192, 1, 0.5625, 12), (128, 1, 0.5, 14), (128, 1, 0.4375, 16),
    (128, 1, 0.375, 16),
    (96, 2, 0.5, 16), (96, 2, 0.375, 18),
    (64, 1, 0.25, 18), (64, 2, 0.25, 22), (48, 3, 0.25, 28),
]
PNG_TIERS = [(False, 1.0), (True, 1.0), (True, 0.75), (True, 0.5)]


# --------------------------------------------------------------------------
# 7TV
# --------------------------------------------------------------------------

_ID = r"([0-9A-Za-z]{24,26})"


def parse_input(text):
    """Return (kind, value): kind is 'file', 'emote', 'set' or 'user'."""
    t = text.strip().strip('"').strip("'").strip()
    if not t:
        raise ValueError("Paste a 7TV link or emote ID first.")
    if os.path.isfile(t):
        return "file", t
    m = re.search(r"emote-sets/(global|" + _ID[1:-1] + r")", t)
    if m:
        return "set", m.group(1)
    m = re.search(r"7tv\.(?:app|io/v3)/users/" + _ID, t)
    if m:
        return "user", m.group(1)
    m = re.search(r"(?:7tv\.app/emotes/|cdn\.7tv\.app/emote/|7tv\.io/v3/emotes/)" + _ID, t)
    if m:
        return "emote", m.group(1)
    if re.fullmatch(_ID, t):
        return "emote", t
    raise ValueError("That doesn't look like a 7TV emote / emote-set / user link, an ID, or a file path.")


class NotFound(RuntimeError):
    pass


def http_get(url, timeout=30, site="7TV", accept="*/*"):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise NotFound(f"Not found on {site} (404): {url}") from None
        raise RuntimeError(f"{site} returned HTTP {e.code} for {url}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"Couldn't reach {site} ({e.reason}). Check your internet connection.") from None


def api_json(path):
    return json.loads(http_get(API + path).decode("utf-8"))


class EmoteRef:
    def __init__(self, eid, name, animated, host, owner=None):
        self.id = eid
        self.name = name
        self.animated = bool(animated)
        self.host = host or {}
        self.owner = owner


def fetch_emote(eid):
    d = api_json(f"/emotes/{eid}")
    return EmoteRef(d.get("id", eid), d.get("name", eid), d.get("animated"), d.get("host"))


def fetch_set(sid):
    d = api_json(f"/emote-sets/{sid}")
    refs = []
    for e in d.get("emotes") or []:
        data = e.get("data") or {}
        # use the name as it's used in the set (aliases included)
        refs.append(EmoteRef(e.get("id"), e.get("name") or data.get("name"),
                             data.get("animated"), data.get("host")))
    return d.get("name") or sid, refs


def fetch_user_set(uid):
    u = api_json(f"/users/{uid}")
    set_id = None
    for c in u.get("connections") or []:
        set_id = c.get("emote_set_id") or (c.get("emote_set") or {}).get("id")
        if set_id:
            break
    if not set_id and u.get("emote_sets"):
        set_id = u["emote_sets"][0].get("id")
    if not set_id:
        raise RuntimeError("This user has no emote set.")
    name, refs = fetch_set(set_id)
    who = u.get("display_name") or u.get("username") or uid
    return f"{who} - {name}", refs


GQL_URL = "https://7tv.io/v3/gql"
SEARCH_QUERY = """query SearchEmotes($query: String!, $page: Int, $limit: Int, $filter: EmoteSearchFilter, $sort: Sort) {
  emotes(query: $query, page: $page, limit: $limit, filter: $filter, sort: $sort) {
    count
    items { id name animated host { url files { name width height format } } owner { display_name } }
  }
}"""
CATEGORIES = {"Trending": "TRENDING_DAY", "Top": "TOP", "New": "NEW"}


def http_post_json(url, payload, timeout=20):
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST",
                                 headers={"User-Agent": UA, "Content-Type": "application/json",
                                          "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"7TV returned HTTP {e.code}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"Couldn't reach 7TV ({e.reason}). Check your internet connection.") from None


def search_emotes(query="", category="TOP", page=1, limit=36):
    """Search 7TV. Returns (total_count, [EmoteRef])."""
    j = http_post_json(GQL_URL, {"operationName": "SearchEmotes", "query": SEARCH_QUERY, "variables": {
        "query": query, "page": page, "limit": limit,
        "filter": {"category": category, "exact_match": False, "case_sensitive": False, "ignore_tags": False,
                   "zero_width": False, "animated": False, "aspect_ratio": ""},
        "sort": {"value": "popularity", "order": "DESCENDING"}}})
    if j.get("errors"):
        raise RuntimeError("7TV search failed: " + "; ".join(e.get("message", "?") for e in j["errors"]))
    data = (j.get("data") or {}).get("emotes") or {}
    refs = [EmoteRef(e["id"], e["name"], e.get("animated"), e.get("host"), (e.get("owner") or {}).get("display_name"))
            for e in data.get("items") or []]
    return data.get("count") or len(refs), refs


def emote_size(ref):
    """(width, height) of the biggest file, from the API data (no download)."""
    best = max((f for f in ref.host.get("files") or [] if f.get("width")), default=None,
               key=lambda f: f["width"] * f.get("height", 1))
    return (best["width"], best["height"]) if best else (1, 1)


def fetch_thumb(ref):
    """Small static preview (first frame of 2x.webp)."""
    url = ref.host.get("url") or f"//cdn.7tv.app/emote/{ref.id}"
    if url.startswith("//"):
        url = "https:" + url
    im = Image.open(io.BytesIO(http_get(f"{url}/2x.webp", timeout=20)))
    return im.convert("RGBA").copy()


def download_emote(ref):
    """Download the best-quality file. Returns (bytes, extension)."""
    url = ref.host.get("url") or f"//cdn.7tv.app/emote/{ref.id}"
    if url.startswith("//"):
        url = "https:" + url
    files = ref.host.get("files") or []
    # WebP keeps full alpha + all frames; GIF/PNG as fallbacks.
    formats = ["webp", "gif"] if ref.animated else ["png", "webp", "gif"]
    errors = []
    for fmt in formats:
        cands = [f for f in files if f.get("name", "").lower().endswith("." + fmt)]
        cands.sort(key=lambda f: f.get("width", 0) * f.get("height", 0), reverse=True)
        names = [f["name"] for f in cands] or [f"4x.{fmt}"]
        for name in names[:1]:
            try:
                return http_get(f"{url}/{name}"), fmt
            except RuntimeError as e:
                errors.append(str(e))
    raise RuntimeError("Couldn't download the emote file.\n" + "\n".join(errors))


# --------------------------------------------------------------------------
# Image processing
# --------------------------------------------------------------------------

def load_frames(data):
    """Decode GIF / WebP / PNG / AVIF bytes into [(RGBA image, duration_ms)]."""
    im = Image.open(io.BytesIO(data) if isinstance(data, (bytes, bytearray)) else data)
    frames = []
    for f in ImageSequence.Iterator(im):
        rgba = f.convert("RGBA").copy()  # decodes the frame (fills info["duration"])
        d = f.info.get("duration") or im.info.get("duration") or 100
        frames.append((rgba, max(20, int(d))))
    if not frames:
        raise RuntimeError("The image has no frames.")
    return frames


def sanitize_name(name, reserve_digits=0):
    n = re.sub(r"[^A-Za-z0-9_]", "_", name or "").strip("_") or "emote"
    if len(n) < 2:
        n = n + "_"
    return n[: 32 - reserve_digits]


def folder_name(title):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title or "").strip(" .") or "emotes"


def auto_grid(w, h):
    """How many square pieces an emote needs so each piece is one full-height square.
    e.g. 384x112 (3.4:1) -> 4 pieces, 256x128 (2:1) -> 2, 128x112 -> 1 (not cut)."""
    r = w / h
    if r >= 1:
        return (1 if r < 1.5 else min(MAX_GRID, math.ceil(r - 0.25))), 1
    r = 1 / r
    return 1, (1 if r < 1.5 else min(MAX_GRID, math.ceil(r - 0.25)))


def plan_grid(w, h, mode, cols=1, rows=1):
    if mode == "none":
        return 1, 1
    if mode == "auto":
        return auto_grid(w, h)
    return max(1, min(MAX_GRID, int(cols))), max(1, min(MAX_GRID, int(rows)))


def resolve_fit(fit, w, h, cols, rows):
    """'auto' = fill every piece completely (stretch a little) when the shape is close,
    otherwise add transparent bars so nothing gets squashed."""
    if fit != "auto":
        return fit
    d = (cols / rows) / (w / h)
    if cols * rows > 1:
        return "stretch" if 0.75 <= d <= 1.34 else "contain"
    return "stretch" if 0.95 <= d <= 1.05 else "contain"


def piece_names(name, count):
    if count == 1:
        return [sanitize_name(name)]
    base = sanitize_name(name, len(str(count)))
    return [f"{base}{i}" for i in range(1, count + 1)]


def fit_frame(img, W, H, fit, method=None):
    if method is None:  # (NEAREST is 0, so don't use "or" here)
        method = LANCZOS
    w, h = img.size
    if fit == "stretch":
        return img.resize((W, H), method)
    sc = min(W / w, H / h) if fit == "contain" else max(W / w, H / h)
    nw, nh = max(1, round(w * sc)), max(1, round(h * sc))
    r = img.resize((nw, nh), method) if (nw, nh) != (w, h) else img
    if fit == "cover":
        x, y = (nw - W) // 2, (nh - H) // 2
        return r.crop((x, y, x + W, y + H))
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    canvas.paste(r, ((W - nw) // 2, (H - nh) // 2))
    return canvas


def compose(frames, cols, rows, tile, fit):
    W, H = cols * tile, rows * tile
    fit = resolve_fit(fit, *frames[0][0].size, cols, rows)
    return [(fit_frame(f, W, H, fit), d) for f, d in frames]


def cut_tiles(composed, cols, rows, tile):
    tiles = []
    for r in range(rows):
        for c in range(cols):
            box = (c * tile, r * tile, (c + 1) * tile, (r + 1) * tile)
            tiles.append([(f.crop(box), d) for f, d in composed])
    return tiles


def _merge_frames(frames, step):
    if step <= 1:
        return frames
    out = []
    for i in range(0, len(frames), step):
        group = frames[i:i + step]
        out.append((group[0][0], sum(d for _, d in group)))
    return out


_TRANSPARENT_LUT = [255 if a < 128 else 0 for a in range(256)]
_OPAQUE_LUT = [0 if a < 128 else 255 for a in range(256)]


def _prep(img):
    """RGB copy with transparent areas flattened to the mean colour + a mask of them."""
    alpha = img.getchannel("A")
    tmask = alpha.point(_TRANSPARENT_LUT)
    rgb = img.convert("RGB")
    has_t = tmask.getbbox() is not None
    if has_t:
        omask = alpha.point(_OPAQUE_LUT)
        if omask.getbbox() is not None:
            mean = tuple(int(v) for v in ImageStat.Stat(rgb, mask=omask).mean)
        else:
            mean = (0, 0, 0)
        rgb.paste(mean, mask=tmask)
    return rgb, tmask, has_t


def _palette(rgbs, ncol):
    """One shared palette for all frames (no flicker, smaller file)."""
    sample = rgbs if len(rgbs) <= 24 else [rgbs[round(i * (len(rgbs) - 1) / 23)] for i in range(24)]
    w, h = sample[0].size
    montage = Image.new("RGB", (w * len(sample), h))
    for i, im in enumerate(sample):
        montage.paste(im, (i * w, 0))
    pal_img = montage.quantize(colors=ncol, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    pal = pal_img.getpalette()[: ncol * 3]
    pal_img.putpalette(pal)
    return pal_img, pal, len(pal) // 3


def encode_gif(frames, colors=256, step=1, scale=1.0, thresh=0):
    fr = _merge_frames(frames, step)
    if scale < 1.0:
        w, h = fr[0][0].size
        size = (max(1, round(w * scale)), max(1, round(h * scale)))
        fr = [(f.resize(size, LANCZOS), d) for f, d in fr]
    preps = [_prep(f) for f, _ in fr]
    any_t = any(p[2] for p in preps)
    durations = [d for _, d in fr]
    buf = io.BytesIO()

    if not any_t and len(preps) > 1:
        # Opaque animation (video-like): store only what changes between frames.
        # Pixels that look the same as what's already on screen become transparent,
        # and the previous frame stays visible underneath (disposal 1). Same trick as gifsicle.
        pal_img, pal, n = _palette([p[0] for p in preps], min(colors, 255))
        tindex = n
        lut = [255 if v <= thresh else 0 for v in range(256)]
        out, shown = [], None
        for rgb, _, _ in preps:
            q = rgb.quantize(palette=pal_img, dither=Image.Dither.NONE)
            q.putpalette(pal + [0, 0, 0] * (256 - n))
            qrgb = q.convert("RGB")
            if shown is not None:
                r, g, b = ImageChops.difference(rgb, shown).split()
                same = ImageChops.lighter(ImageChops.lighter(r, g), b).point(lut)
                q.paste(tindex, mask=same)
                shown = Image.composite(shown, qrgb, same)
            else:
                shown = qrgb
            q.info["transparency"] = tindex
            out.append(q)
        out[0].save(buf, "GIF", save_all=True, append_images=out[1:], duration=durations, loop=0,
                    disposal=1, transparency=tindex, optimize=False)
        return buf.getvalue()

    pal_img, pal, n = _palette([p[0] for p in preps], min(colors, 255) if any_t else min(colors, 256))
    tindex = n  # first free slot after the real colours
    out = []
    for rgb, tmask, _ in preps:
        q = rgb.quantize(palette=pal_img, dither=Image.Dither.FLOYDSTEINBERG)
        if any_t:
            q.putpalette(pal + [0, 0, 0] * (256 - n))
            q.paste(tindex, mask=tmask)
            q.info["transparency"] = tindex
        out.append(q)
    kw = dict(save_all=True, append_images=out[1:], duration=durations, loop=0, disposal=2, optimize=False)
    if any_t:
        kw.update(transparency=tindex, background=tindex)
    out[0].save(buf, "GIF", **kw)
    return buf.getvalue()


def encode_png(img, quantize=False, scale=1.0):
    if scale < 1.0:
        img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), LANCZOS)
    if quantize:
        img = img.quantize(colors=256, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.FLOYDSTEINBERG)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def encode_tiles(tiles, animated):
    """Encode every piece with the SAME settings (keeps animations in sync).
    Returns (list_of_bytes, ext, note)."""
    if not animated:
        for quant, scale in PNG_TIERS:
            data = [encode_png(t[0][0], quant, scale) for t in tiles]
            if max(map(len, data)) <= SAFE_LIMIT:
                return data, "png", _note(256 if not quant else 255, 1, scale, tile=tiles[0][0][0].size[0])
        return data, "png", "WARNING: still over 256 KB"
    data, worst = None, 0
    tile = tiles[0][0][0].size[0]
    for colors, step, scale, thresh in GIF_TIERS:
        if step > 1 and len(tiles[0]) < step * 2:
            continue
        # try the piece that was biggest last time first, and skip this level early if it doesn't fit
        first = encode_gif(tiles[worst], colors, step, scale, thresh)
        if len(first) > SAFE_LIMIT:
            data = None
            continue
        data = [first if i == worst else encode_gif(t, colors, step, scale, thresh) for i, t in enumerate(tiles)]
        worst = max(range(len(data)), key=lambda i: len(data[i]))
        if len(data[worst]) <= SAFE_LIMIT:
            return data, "gif", _note(colors, step, scale, tile=tile, frames=len(tiles[0]))
    if data is None:
        data = [encode_gif(t, *GIF_TIERS[-1]) for t in tiles]
    return data, "gif", "WARNING: still over 256 KB - try fewer pieces"


def _note(colors=256, step=1, scale=1.0, tile=128, frames=1):
    px = max(1, round(tile * scale))
    bits = [f"{px}×{px} px"]
    if frames > 1:
        bits.append("all frames" if step == 1 else f"1 in {step} frames")
    if colors < 160:
        bits.append("fewer colours")
    if scale >= 1.0 and step == 1 and colors >= 255:
        return f"full quality, {px}×{px} px"
    return "fit under 256 KB: " + ", ".join(bits)


# --------------------------------------------------------------------------
# Converter: any image / GIF / video -> emote, sticker or custom format
# --------------------------------------------------------------------------

IMAGE_EXT = {".gif", ".png", ".apng", ".webp", ".jpg", ".jpeg", ".jfif", ".bmp", ".avif", ".tif", ".tiff", ".ico"}
VIDEO_EXT = {".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v", ".flv", ".wmv", ".mpg", ".mpeg"}
MEDIA_EXT = IMAGE_EXT | VIDEO_EXT
PRESETS = {  # name: (width, height, size limit KB)
    "Emoji": (128, 128, 256),
    "Sticker": (320, 320, 512),
    "Custom": None,
}


class NeedFFmpeg(RuntimeError):
    """Video support needs ffmpeg, which is downloaded once on first use."""


def data_dir():
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "EmotesDownloader"
    return Path.home() / ".local" / "share" / "emotes-downloader"


def _local_ffmpeg():
    return data_dir() / ("ffmpeg.exe" if sys.platform.startswith("win") else "ffmpeg")


def ffmpeg_exe():
    local = _local_ffmpeg()
    if local.exists():
        return str(local)
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        import shutil
        return shutil.which("ffmpeg")


def install_ffmpeg(progress=None):
    """One-time download of ffmpeg (the build that the imageio-ffmpeg package ships on PyPI)."""
    import zipfile
    info = json.loads(http_get("https://pypi.org/pypi/imageio-ffmpeg/json", timeout=30, site="PyPI").decode("utf-8"))
    import platform
    arm = platform.machine().lower() in ("arm64", "aarch64")
    if sys.platform.startswith("win"):
        tags = ["win_arm64"] if arm else ["win_amd64"]
    elif sys.platform == "darwin":
        tags = ["arm64"] if arm else ["x86_64"]
    else:
        tags = ["aarch64"] if arm else ["x86_64"]
    plat = "win" if sys.platform.startswith("win") else "macosx" if sys.platform == "darwin" else "linux"
    wheel = next((u for u in info.get("urls", []) if u["filename"].endswith(".whl") and plat in u["filename"]
                  and any(t in u["filename"] for t in tags)), None)
    if wheel is None and sys.platform.startswith("win"):  # Windows on ARM can run the x64 build
        wheel = next((u for u in info.get("urls", []) if u["filename"].endswith("win_amd64.whl")), None)
    if not wheel:
        raise RuntimeError("Couldn't find an ffmpeg download for this system.")
    data = download(wheel["url"], progress, timeout=900)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        member = next((n for n in z.namelist() if "/binaries/ffmpeg" in n and not n.endswith((".py", ".md"))), None)
        if not member:
            raise RuntimeError("The ffmpeg download didn't contain ffmpeg.")
        dest = _local_ffmpeg()
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".part")
        tmp.write_bytes(z.read(member))
        os.replace(tmp, dest)
    if not sys.platform.startswith("win"):
        dest.chmod(0o755)
    return str(dest)


def load_video(path, fps=30, max_seconds=20, max_side=512, max_frames=600):
    """Decode a video clip into [(RGBA image, duration_ms)] using ffmpeg."""
    exe = ffmpeg_exe()
    if not exe:
        raise NeedFFmpeg("Video files need ffmpeg (a one-time download).")
    vf = f"fps={fps},scale={max_side}:{max_side}:force_original_aspect_ratio=decrease:flags=lanczos"
    cmd = [exe, "-v", "error", "-i", str(path), "-t", str(max_seconds), "-an", "-vf", vf,
           "-f", "image2pipe", "-vcodec", "png", "-"]
    flags = subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0
    r = subprocess.run(cmd, capture_output=True, timeout=300, creationflags=flags)
    sig = b"\x89PNG\r\n\x1a\n"
    parts = r.stdout.split(sig)[1:]
    if not parts:
        msg = r.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError("Couldn't read this video" + (f": {msg[-1]}" if msg else "."))
    dur = max(20, round(1000 / fps))
    return [(Image.open(io.BytesIO(sig + p)).convert("RGBA"), dur) for p in parts[:max_frames]]


def load_any(path):
    """Frames from any supported file (image, animated image or video)."""
    path = Path(path)
    if path.suffix.lower() in VIDEO_EXT:
        return load_video(path)
    return load_frames(path.read_bytes())


def collect_media(paths, videos=True, limit=500):
    """Expand dropped files/folders into a list of media files."""
    ok = MEDIA_EXT if videos else IMAGE_EXT
    out = []
    for p in paths:
        p = Path(p)
        if p.is_dir():
            found = sorted(f for f in p.iterdir() if f.is_file() and f.suffix.lower() in ok)
            if not found:  # look one level deeper
                found = sorted(f for d in p.iterdir() if d.is_dir() for f in d.iterdir()
                               if f.is_file() and f.suffix.lower() in ok)
            out.extend(found)
        elif p.is_file() and p.suffix.lower() in ok:
            out.append(p)
        if len(out) >= limit:
            break
    return out[:limit]


def retime(frames, fps):
    """Resample an animation to a frame rate (fewer frames = smaller files)."""
    if not fps or len(frames) < 2:
        return frames
    step = 1000.0 / fps
    total = sum(d for _, d in frames)
    out, t, idx, end = [], 0.0, 0, frames[0][1]
    while t < total - 1e-6:
        while t >= end and idx < len(frames) - 1:
            idx += 1
            end += frames[idx][1]
        if out and out[-1][0] is frames[idx][0]:
            out[-1][1] += step
        else:
            out.append([frames[idx][0], step])
        t += step
    return [(im, max(20, int(round(d)))) for im, d in out]


def convert_media(frames, width, height, fmt="auto", fit="auto", pixel=False, colors=256, fps=None, limit_kb=256):
    """Resize/convert frames to GIF (animated) or PNG (still). Returns dict(data, ext, note, size, frames)."""
    method = Image.Resampling.NEAREST if pixel else LANCZOS
    animated = len(frames) > 1
    fr = retime(frames, fps) if animated else frames[:1]
    fr = [(fit_frame(im, width, height, resolve_fit(fit, im.width, im.height, width, height), method), d) for im, d in fr]
    fmt = (("gif" if animated else "png") if fmt == "auto" else fmt).lower()
    limit = limit_kb * 1024 if limit_kb else None
    colors = max(2, min(256, int(colors)))
    notes = []

    def fits(b):
        return limit is None or len(b) <= limit

    def shrink(f, scale):
        size = (max(1, round(width * scale)), max(1, round(height * scale)))
        return [(im.resize(size, method), d) for im, d in f]

    data = None
    if fmt == "gif":
        tiers = [(colors, 1, 1.0, 0)]
        if limit:
            for c in (colors, 192, 128, 96, 64, 48, 32):
                if c <= colors:
                    tiers += [(c, 1, 1.0, 10), (c, 1, 1.0, 20)]
            tiers += [(64, 2, 1.0, 20), (48, 2, 1.0, 24), (48, 3, 1.0, 28),
                      (64, 1, 0.75, 20), (48, 2, 0.75, 24), (48, 2, 0.5, 24), (32, 3, 0.5, 28), (32, 4, 0.375, 32)]
        for c, step, scale, thr in tiers:
            src = fr if scale >= 1 else shrink(fr, scale)
            data = encode_gif(src, c, step, 1.0, thr if len(src) > 1 else 0)
            if fits(data):
                if c < colors:
                    notes.append(f"{c} colours")
                if step > 1:
                    notes.append(f"1 in {step} frames")
                if scale < 1:
                    notes.append(f"{round(width * scale)}×{round(height * scale)} px")
                break
    elif fmt == "png":
        img = fr[0][0]
        if animated:
            notes.append("first frame only (PNG isn't animated on Discord)")
        for c, scale in ([(colors, 1.0)] + ([(128, 1.0), (64, 1.0), (256, .75), (128, .5)] if limit else [])):
            im = img if scale >= 1 else img.resize((max(1, round(width * scale)), max(1, round(height * scale))), method)
            if c < 256:
                im = im.quantize(colors=c, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.FLOYDSTEINBERG)
            buf = io.BytesIO()
            im.save(buf, "PNG", optimize=True)
            data = buf.getvalue()
            if fits(data):
                if c < colors or (c < 256 and colors >= 256):
                    notes.append(f"{c} colours")
                if scale < 1:
                    notes.append(f"{round(width * scale)}×{round(height * scale)} px")
                break
    else:
        raise ValueError(f"Unknown format: {fmt}")
    if not fits(data):
        notes.append(f"still over {limit_kb} KB")
    out = Image.open(io.BytesIO(data))
    return dict(data=data, ext=fmt, note=", ".join(notes) or "full quality", size=out.size,
                frames=getattr(out, "n_frames", 1), over=not fits(data))


def make_emote(frames, name, out_dir, mode="none", cols=1, rows=1, tile=128,
               fit="auto", original=None):
    """Process and save. Returns dict(files, paste, note, folder)."""
    w, h = frames[0][0].size
    cols, rows = plan_grid(w, h, mode, cols, rows)
    count = cols * rows
    animated = len(frames) > 1
    tiles = cut_tiles(compose(frames, cols, rows, tile, fit), cols, rows, tile)
    data, ext, note = encode_tiles(tiles, animated)

    out_dir = Path(out_dir)
    if count == 1:
        base = sanitize_name(name)
        names = [base]
        folder = out_dir
    else:
        base = sanitize_name(name, len(str(count)))
        names = [f"{base}{i}" for i in range(1, count + 1)]
        folder = out_dir / base
    folder.mkdir(parents=True, exist_ok=True)

    files = []
    for nm, b in zip(names, data):
        p = folder / f"{nm}.{ext}"
        p.write_bytes(b)
        files.append((p, len(b)))

    paste = "\n".join("".join(f":{names[r * cols + c]}:" for c in range(cols)) for r in range(rows))
    if original:
        raw, raw_ext = original
        (folder / f"{base}_original.{raw_ext}").write_bytes(raw)
    return dict(files=files, paste=paste, note=note, folder=folder, grid=(cols, rows))


# --------------------------------------------------------------------------
# Auto-updater (GitHub Releases) + changelog
# --------------------------------------------------------------------------

def parse_version(v):
    nums = [int(x) for x in re.findall(r"\d+", v or "")[:3]]
    return tuple(nums + [0] * (3 - len(nums)))


def is_frozen():
    return bool(getattr(sys, "frozen", False))


def install_kind():
    """'setup' (installed with the setup .exe), 'portable' (.exe) or 'python'."""
    if not is_frozen():
        return "python"
    if (Path(sys.executable).parent / "unins000.exe").exists():
        return "setup"
    return "portable"


KIND_LABEL = {"setup": "Installed", "portable": "Portable", "python": "Python"}


def resource_path(rel):
    base = getattr(sys, "_MEIPASS", None) or Path(__file__).resolve().parent
    return Path(base) / rel


def _gh_json(url):
    return json.loads(http_get(url, timeout=15, site="GitHub",
                               accept="application/vnd.github+json").decode("utf-8"))


def check_for_update():
    """Return info about a newer release, or None if up to date / no releases yet."""
    try:
        d = _gh_json(f"{RELEASES_API}/latest")
    except NotFound:
        return None  # no releases yet
    tag = d.get("tag_name") or ""
    if parse_version(tag) <= parse_version(__version__):
        return None
    return dict(version=tag.lstrip("vV"), tag=tag, notes=(d.get("body") or "").strip(),
                page=d.get("html_url") or RELEASES_PAGE,
                assets={a["name"]: a["browser_download_url"] for a in d.get("assets") or []})


_CL_HEAD = re.compile(r"^##\s*\[?v?(\d+(?:\.\d+){1,2})\]?\s*(?:[-—–·]\s*(.*))?$")


def parse_changelog(text):
    """CHANGELOG.md -> [(version, date, notes)] newest first."""
    out, cur = [], None
    for line in (text or "").splitlines():
        m = _CL_HEAD.match(line.strip())
        if m:
            cur = [m.group(1), (m.group(2) or "").strip(), []]
            out.append(cur)
        elif cur is not None:
            cur[2].append(line)
    return [(v, d, "\n".join(n).strip()) for v, d, n in out]


def local_changelog():
    try:
        return parse_changelog(resource_path("CHANGELOG.md").read_text(encoding="utf-8"))
    except Exception:
        return []


def get_changelog(after=None, upto=None):
    """Changelog entries with after < version <= upto, newest first (GitHub first, bundled file as fallback)."""
    lo = parse_version(after) if after else (0, 0, 0)
    hi = parse_version(upto) if upto else (999, 0, 0)
    entries = []
    try:
        for r in _gh_json(f"{RELEASES_API}?per_page=40"):
            if r.get("draft") or r.get("prerelease"):
                continue
            entries.append((r["tag_name"].lstrip("vV"), (r.get("published_at") or "")[:10],
                            (r.get("body") or "").strip()))
    except Exception:
        pass
    pick = [e for e in entries if lo < parse_version(e[0]) <= hi]
    if not pick:
        pick = [e for e in local_changelog() if lo < parse_version(e[0]) <= hi]
    return sorted(pick, key=lambda e: parse_version(e[0]), reverse=True)


def format_changelog(entries):
    blocks = []
    for v, date, notes in entries:
        notes = re.sub(r"\*\*(.+?)\*\*", r"\1", notes).replace("`", "")
        body = "\n".join("•  " + l.strip()[2:] if l.strip()[:2] in ("- ", "* ") else l.rstrip()
                         for l in notes.splitlines() if l.strip())
        blocks.append(f"v{v}" + (f"   ·   {date}" if date else "") + "\n" + (body or "•  (no notes)"))
    return "\n\n".join(blocks) or "No changelog available."


def download(url, progress=None, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            total = int(r.headers.get("Content-Length") or 0)
            buf, done = io.BytesIO(), 0
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                buf.write(chunk)
                done += len(chunk)
                if progress and total:
                    progress(done / total)
            return buf.getvalue()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"GitHub returned HTTP {e.code} for {url}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"Couldn't reach GitHub ({e.reason}).") from None


def _detached():
    if sys.platform.startswith("win"):
        return subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    return 0


def _relaunch(cmd):
    subprocess.Popen(cmd, creationflags=_detached(), close_fds=True)


def _check_exe(data):
    if len(data) < 1_000_000 or data[:2] != b"MZ":
        raise RuntimeError("The downloaded file looks broken, update cancelled.")


def apply_update(info, progress=None):
    """Download and start installing the update. The caller must quit the app right after."""
    kind = install_kind()
    assets = info["assets"]

    if kind == "setup":
        url = assets.get(SETUP_ASSET)
        if not url:
            raise RuntimeError("This release has no setup .exe yet (it may still be building). Try again in a few minutes.")
        data = download(url, progress, timeout=180)
        _check_exe(data)
        path = Path(tempfile.gettempdir()) / f"Emotes-Downloader-Setup-{info['version']}.exe"
        path.write_bytes(data)
        # The installer closes this app, updates it in place and starts it again.
        subprocess.Popen([str(path), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"],
                         creationflags=_detached(), close_fds=True)
        return

    if kind == "portable":
        url = assets.get(PORTABLE_ASSET) or assets.get(LEGACY_EXE_ASSET)
        if not url:
            raise RuntimeError("This release has no portable .exe yet (it may still be building). Try again in a few minutes.")
        exe = Path(sys.executable).resolve()
        data = download(url, progress, timeout=180)
        _check_exe(data)
        new = exe.with_name(exe.stem + ".new.exe")
        new.write_bytes(data)
        pid = os.getpid()
        bat = Path(tempfile.gettempdir()) / f"emotes_downloader_update_{pid}.bat"
        bat.write_text(
            "@echo off\r\n"
            "set n=0\r\n"
            ":wait\r\n"
            f'tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul && '
            "(timeout /t 1 /nobreak >nul & goto wait)\r\n"
            ":move\r\n"
            f'move /y "{new}" "{exe}" >nul 2>nul\r\n'
            "if not errorlevel 1 goto done\r\n"
            "set /a n+=1\r\n"
            "if %n% geq 30 goto done\r\n"
            "timeout /t 1 /nobreak >nul\r\n"
            "goto move\r\n"
            ":done\r\n"
            f'start "" "{exe}"\r\n'
            'del "%~f0"\r\n', encoding="utf-8")
        subprocess.Popen(["cmd", "/c", str(bat)],
                         creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS, close_fds=True)
        return

    # Python version: replace this script (and the changelog), then restart.
    script = Path(__file__).resolve()
    raw_base = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{info['tag']}"
    code = download(assets.get(SCRIPT_ASSET) or f"{raw_base}/emotes_downloader.py", progress)
    try:
        compile(code, str(script), "exec")
    except SyntaxError as e:
        raise RuntimeError(f"The downloaded update is broken ({e}), update cancelled.") from None
    if b"__version__" not in code:
        raise RuntimeError("The downloaded file isn't this app, update cancelled.")
    for extra in ("CHANGELOG.md", "assets/icon.png", "assets/icon.ico", "assets/tray.png"):
        try:
            data = download(f"{raw_base}/{extra}")
            dest = script.parent / extra
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        except Exception:
            pass
    script.with_suffix(".py.bak").write_bytes(script.read_bytes())
    tmp = script.with_suffix(".py.new")
    tmp.write_bytes(code)
    os.replace(tmp, script)
    py = Path(sys.executable)
    try:
        subprocess.run([str(py), "-m", "pip", "install", "-q", "pillow", "customtkinter", "pystray", "tkinterdnd2"], timeout=180,
                       creationflags=subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0)
    except Exception:
        pass
    pyw = py.with_name("pythonw.exe")
    _relaunch([str(pyw if pyw.exists() else py), str(script)])


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

DEFAULTS = dict(auto_grid=True, cols=2, rows=1, fit="auto", tile=128, keep_original=False,
                check_updates=True, auto_install=True, close_to_tray=True, watch_clipboard=True,
                save_preset="app", out_dir="", cfg_version=3)

SAVE_PRESETS = {"app": "App folder", "downloads": "Downloads", "pictures": "Pictures",
                "desktop": "Desktop", "custom": "Custom folder…"}


def app_dir():
    return Path(sys.executable).resolve().parent if is_frozen() else Path(__file__).resolve().parent


def _known(name):
    home = Path.home()
    for c in (home / name, home / "OneDrive" / name):
        if c.is_dir():
            return c
    return home


def preset_dir(key, custom=""):
    if key == "app":
        return app_dir() / "Emotes"
    if key == "custom" and custom:
        return Path(custom)
    return _known({"pictures": "Pictures", "desktop": "Desktop"}.get(key, "Downloads")) / "7TV Emotes"


def default_out_dir():
    cfg = load_config()
    return str(preset_dir(cfg.get("save_preset", "app"), cfg.get("out_dir", "")))


def load_config():
    cfg = dict(DEFAULTS)
    try:
        saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        saved = {}
    cfg.update(saved)
    if saved and "cfg_version" not in saved:  # settings file from v1.0/v1.1
        cfg["cfg_version"] = 1
    if cfg.get("cfg_version", 1) < 2:  # v1.1 settings: switch to the new auto cut + auto fill
        cfg.update(auto_grid=cfg.get("mode", "auto") != "custom", fit="auto", cfg_version=2)
    if cfg.get("cfg_version", 1) < 3:  # v1.3: save into the app folder unless a custom folder was picked
        old = cfg.get("out_dir") or ""
        was_default = (not old) or Path(old).name == "7TV Emotes"
        cfg.update(save_preset="app" if was_default else "custom", out_dir="" if was_default else old,
                   cfg_version=3)
    return cfg


def save_config(cfg):
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception:
        pass


def _autostart_cmd():
    if is_frozen():
        return f'"{Path(sys.executable).resolve()}" --tray'
    py = Path(sys.executable)
    pyw = py.with_name("pythonw.exe")
    return f'"{pyw if pyw.exists() else py}" "{Path(__file__).resolve()}" --tray'


def autostart_enabled():
    if not sys.platform.startswith("win"):
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
            winreg.QueryValueEx(k, "EmotesDownloader")
            return True
    except Exception:
        return False


def set_autostart(on):
    if not sys.platform.startswith("win"):
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                            winreg.KEY_SET_VALUE) as k:
            if on:
                winreg.SetValueEx(k, "EmotesDownloader", 0, winreg.REG_SZ, _autostart_cmd())
            else:
                try:
                    winreg.DeleteValue(k, "EmotesDownloader")
                except FileNotFoundError:
                    pass
        return True
    except Exception:
        return False


def open_folder(path):
    path = str(path)
    if sys.platform.startswith("win"):
        os.startfile(path)  # noqa
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def paste_text(name, cols, rows):
    names = piece_names(name, cols * rows)
    return "\n".join("".join(f":{names[r * cols + c]}:" for c in range(cols)) for r in range(rows))


# --------------------------------------------------------------------------
# Icons, drawn with Pillow so they look the same on every PC
# --------------------------------------------------------------------------

def draw_icon(name, color="#ffffff", size=16):
    from PIL import ImageDraw
    S = size * 4
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    w = max(4, S // 9)  # stroke width
    c = color
    if name == "search":
        d.ellipse((S * .12, S * .12, S * .66, S * .66), outline=c, width=w)
        d.line((S * .6, S * .6, S * .88, S * .88), fill=c, width=int(w * 1.2))
    elif name == "download":
        d.line((S * .5, S * .1, S * .5, S * .62), fill=c, width=w)
        d.line((S * .27, S * .42, S * .5, S * .65, S * .73, S * .42), fill=c, width=w, joint="curve")
        d.line((S * .16, S * .86, S * .84, S * .86), fill=c, width=w)
    elif name == "check":
        d.line((S * .16, S * .52, S * .4, S * .76, S * .86, S * .26), fill=c, width=int(w * 1.15), joint="curve")
    elif name == "close":
        d.line((S * .22, S * .22, S * .78, S * .78), fill=c, width=w)
        d.line((S * .78, S * .22, S * .22, S * .78), fill=c, width=w)
    elif name == "folder":
        d.rounded_rectangle((S * .1, S * .3, S * .9, S * .82), radius=S * .08, outline=c, width=w)
        d.polygon([(S * .1, S * .34), (S * .1, S * .2), (S * .4, S * .2), (S * .5, S * .32)], fill=c)
    elif name == "gear":
        import math as _m
        cx = cy = S / 2
        for k in range(8):
            a = k * _m.pi / 4
            x, y = cx + _m.cos(a) * S * .36, cy + _m.sin(a) * S * .36
            d.ellipse((x - S * .08, y - S * .08, x + S * .08, y + S * .08), fill=c)
        d.ellipse((S * .2, S * .2, S * .8, S * .8), fill=c)
        d.ellipse((S * .37, S * .37, S * .63, S * .63), fill=(0, 0, 0, 0))
    elif name == "paste":
        d.rounded_rectangle((S * .18, S * .16, S * .82, S * .9), radius=S * .08, outline=c, width=w)
        d.rounded_rectangle((S * .34, S * .06, S * .66, S * .26), radius=S * .05, fill=c)
    elif name == "external":
        d.rounded_rectangle((S * .12, S * .2, S * .76, S * .86), radius=S * .08, outline=c, width=w)
        d.rectangle((S * .44, S * .1, S * .92, S * .54), fill=(0, 0, 0, 0))
        d.line((S * .42, S * .56, S * .86, S * .12), fill=c, width=w)
        d.line((S * .56, S * .12, S * .88, S * .12, S * .88, S * .44), fill=c, width=w)
    elif name == "refresh":
        d.arc((S * .14, S * .14, S * .86, S * .86), start=40, end=330, fill=c, width=w)
        d.polygon([(S * .64, S * .06), (S * .92, S * .3), (S * .6, S * .36)], fill=c)
    return im.resize((size, size), Image.Resampling.LANCZOS)


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

C = dict(
    bg="#0d0f14", card="#151821", card_hi="#1c2030", border="#252a38", field="#0f1218",
    text="#eceef4", muted="#8b91a5", faint="#5c6275",
    accent="#7c5cff", accent_hover="#6847f5", blue="#3d8bff", blue_hover="#2f78e8",
    good="#34c77b", bad="#ff5d6e", warn="#f5b14c", discord="#313338",
)
FITS = {"Fill": "auto", "Stretch": "stretch", "Bars": "contain", "Crop": "cover"}
FIT_HELP = {"auto": "Each piece is filled edge to edge, like a normal emote.",
            "stretch": "Always stretched to fill every piece.",
            "contain": "Nothing is cut off; empty space becomes transparent.",
            "cover": "Fills every piece by trimming the edges."}


def _import_ctk():
    try:
        import customtkinter as ctk
        return ctk
    except ImportError:
        pass
    if not is_frozen():  # Python version: install the UI library on the fly
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "customtkinter", "pystray", "tkinterdnd2"],
                       creationflags=subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0)
        try:
            import customtkinter as ctk
            return ctk
        except ImportError:
            pass
    import tkinter as tk
    from tkinter import messagebox
    r = tk.Tk()
    r.withdraw()
    messagebox.showerror("Emotes Downloader",
                         "The UI library 'customtkinter' is missing.\n\nInstall it with:\n  python -m pip install customtkinter")
    sys.exit(1)


def run_gui(start_hidden=False):
    ctk = _import_ctk()
    import tkinter as tk
    from tkinter import filedialog
    from PIL import ImageTk, ImageDraw
    try:
        import PIL._tkinter_finder  # noqa: F401  (makes PyInstaller bundle it; ImageTk needs it)
    except ImportError:
        pass

    if sys.platform.startswith("win"):
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ISdook.EmotesDownloader")
        except Exception:
            pass

    ctk.set_appearance_mode("dark")
    FAMILY = "Segoe UI" if sys.platform.startswith("win") else "DejaVu Sans"
    MONO = "Consolas" if sys.platform.startswith("win") else "DejaVu Sans Mono"

    def set_icon(win):
        def apply():
            try:
                ico = resource_path("assets/icon.ico")
                if sys.platform.startswith("win") and ico.exists():
                    win.iconbitmap(str(ico))
                else:
                    png = resource_path("assets/icon.png")
                    if png.exists():
                        win._icon_ref = tk.PhotoImage(file=str(png))
                        win.iconphoto(False, win._icon_ref)
            except Exception:
                pass
        win.after(250, apply)  # after CustomTkinter sets its own icon

    class App:
        def __init__(self, root):
            self.root = root
            root._emotes_app = self  # handy for tests and debugging
            self.cfg = load_config()
            self.q = queue.Queue()
            self.items = []
            self.rows = []
            self.current = None
            self.set_title = None
            self.anim_job = None
            self.anim_imgs = []
            self.busy = False
            self.update_info = None
            self.log_lines = []
            self.tray = None
            self.flyout = None
            self._flyout_closed = 0.0
            self.upd_state = "idle"   # idle | checking | uptodate | available | downloading | error
            self.upd_progress = 0
            self.saved_count = 0
            self.toasts = []
            self.browse_win = None
            self.thumb_cache = {}
            self.dlq = queue.Queue()
            self._last_clip = None
            self.clip_ref = None
            self.F = lambda size, bold=False: ctk.CTkFont(family=FAMILY, size=size,
                                                         weight="bold" if bold else "normal")
            self._build()
            self._poll()
            threading.Thread(target=self._dl_worker, daemon=True).start()
            self.root.protocol("WM_DELETE_WINDOW", self.on_close)
            self.root.after(300, self._start_tray)
            self.root.after(400, self._after_start)
            self.root.after(1500, self._watch_clipboard)

        # ================= layout =================
        def ico(self, name, color="#ffffff", size=16):
            key = (name, color, size)
            cache = self.__dict__.setdefault("_ico_cache", {})
            if key not in cache:
                im = draw_icon(name, color, size)
                cache[key] = ctk.CTkImage(im, im, size=(size, size))
            return cache[key]

        def card(self, parent, **kw):
            return ctk.CTkFrame(parent, fg_color=C["card"], corner_radius=16, border_width=1,
                                border_color=C["border"], **kw)

        def label(self, parent, text, size=13, color="text", bold=False, **kw):
            return ctk.CTkLabel(parent, text=text, font=self.F(size, bold), text_color=C[color], **kw)

        def section(self, parent, text):
            return ctk.CTkLabel(parent, text=text.upper(), font=self.F(11, True), text_color=C["muted"], anchor="w")

        def button(self, parent, text, cmd, kind="ghost", **kw):
            styles = {
                "accent": dict(fg_color=C["accent"], hover_color=C["accent_hover"], text_color="white"),
                "blue": dict(fg_color=C["blue"], hover_color=C["blue_hover"], text_color="white"),
                "ghost": dict(fg_color=C["card_hi"], hover_color=C["border"], text_color=C["text"]),
                "flat": dict(fg_color="transparent", hover_color=C["card_hi"], text_color=C["muted"]),
            }
            opts = dict(corner_radius=10, height=36, font=self.F(13, kind in ("accent", "blue")))
            opts.update(styles[kind])
            opts.update(kw)
            return ctk.CTkButton(parent, text=text, command=cmd, **opts)

        def segmented(self, parent, values, cmd):
            return ctk.CTkSegmentedButton(
                parent, values=values, command=cmd, height=34, corner_radius=10, font=self.F(13),
                fg_color=C["field"], selected_color=C["accent"], selected_hover_color=C["accent_hover"],
                unselected_color=C["field"], unselected_hover_color=C["card_hi"], text_color=C["text"])

        def _build(self):
            r = self.root
            r.title("Emotes Downloader")
            r.configure(fg_color=C["bg"])
            r.geometry("1220x820")
            r.minsize(1060, 760)
            set_icon(r)
            r.grid_columnconfigure(0, weight=1)
            r.grid_rowconfigure(3, weight=1)

            # ---- top bar
            top = ctk.CTkFrame(r, fg_color="transparent")
            top.grid(row=0, column=0, sticky="ew", padx=22, pady=(18, 10))
            top.grid_columnconfigure(1, weight=1)
            try:
                logo = Image.open(resource_path("assets/icon.png"))
                self._logo = ctk.CTkImage(logo, logo, size=(38, 38))
                ctk.CTkLabel(top, text="", image=self._logo).grid(row=0, column=0, rowspan=2, padx=(0, 12))
            except Exception as e:  # noqa
                self.log_lines.append(f"Couldn't load the logo: {e!r}")
            tbox = ctk.CTkFrame(top, fg_color="transparent")
            tbox.grid(row=0, column=1, sticky="w")
            self.label(tbox, "Emotes Downloader", 20, bold=True).pack(side="left")
            self.label(tbox, f"  v{__version__}", 12, "faint").pack(side="left", pady=(6, 0))
            self.label(top, "7TV emotes → Discord, resized and cut into pieces", 12, "muted").grid(
                row=1, column=1, sticky="w")

            self.tab = "Emotes"
            self.tab_seg = ctk.CTkSegmentedButton(
                top, values=["Emotes", "Converter"], command=self.switch_tab, height=36, corner_radius=12,
                font=self.F(13, True), fg_color=C["card"], selected_color=C["accent"],
                selected_hover_color=C["accent_hover"], unselected_color=C["card"],
                unselected_hover_color=C["card_hi"], text_color=C["text"])
            self.tab_seg.set("Emotes")
            self.tab_seg.grid(row=0, column=2, rowspan=2, padx=(0, 18))
            right = ctk.CTkFrame(top, fg_color="transparent")
            right.grid(row=0, column=3, rowspan=2, sticky="e")
            self.update_btn = self.button(right, "Check for updates", self.on_update_btn, "ghost",
                                          corner_radius=18, height=34)
            self.update_btn.pack(side="left", padx=(0, 8))
            self.button(right, "What's new", self.show_changelog, "flat", width=96, height=34).pack(side="left", padx=(0, 4))
            self.button(right, "Settings", self.open_settings, "flat", width=104, height=34,
                        image=self.ico("gear", C["muted"]), compound="left").pack(side="left")

            # ---- input bar
            bar = ctk.CTkFrame(r, fg_color="transparent")
            self.bar = bar
            bar.grid(row=1, column=0, sticky="ew", padx=22, pady=(0, 14))
            bar.grid_columnconfigure(0, weight=1)
            self.url_entry = ctk.CTkEntry(
                bar, height=48, corner_radius=12, border_width=1, border_color=C["border"],
                fg_color=C["card"], text_color=C["text"], placeholder_text_color=C["faint"], font=self.F(14),
                placeholder_text="Paste a 7TV emote, emote-set or user link…")
            self.url_entry.grid(row=0, column=0, sticky="ew")
            self.url_entry.bind("<Return>", lambda _e: self.on_load())
            self.load_btn = self.button(bar, "Load", self.on_load, "accent", width=110, height=48, corner_radius=12)
            self.load_btn.grid(row=0, column=1, padx=(10, 0))
            self.button(bar, "Browse 7TV", self.open_browser, "blue", width=146, height=48, corner_radius=12,
                        image=self.ico("search", "#ffffff", 17), compound="left").grid(row=0, column=2, padx=(10, 0))
            self.button(bar, "Open file", self.on_open_file, "ghost", width=104, height=48,
                        corner_radius=12).grid(row=0, column=3, padx=(10, 0))

            # ---- "7TV link copied" banner (hidden until a link is copied)
            self.banner = ctk.CTkFrame(r, fg_color=C["card_hi"], corner_radius=14, border_width=1,
                                       border_color=C["accent"])
            self.banner.grid_columnconfigure(1, weight=1)
            self.banner_img = ctk.CTkLabel(self.banner, text="", width=40, image=self.ico("paste", C["accent"], 20))
            self.banner_img.grid(row=0, column=0, padx=(12, 6), pady=8)
            self.banner_lbl = self.label(self.banner, "", 13, anchor="w")
            self.banner_lbl.grid(row=0, column=1, sticky="ew")
            self.button(self.banner, "Open", self.on_clip_open, "ghost", width=80, height=32).grid(row=0, column=2, padx=4)
            self.banner_save = self.button(self.banner, "Save now", self.on_clip_save, "accent", width=112, height=32,
                                           image=self.ico("download"), compound="left")
            self.banner_save.grid(row=0, column=3, padx=4)
            self.button(self.banner, "", self.hide_banner, "flat", width=32, height=32,
                        image=self.ico("close", C["muted"], 14)).grid(row=0, column=4, padx=(0, 8))

            # ---- body
            body = ctk.CTkFrame(r, fg_color="transparent")
            body.grid(row=3, column=0, sticky="nsew", padx=22)
            body.grid_columnconfigure(1, weight=1)
            body.grid_rowconfigure(0, weight=1)
            self.body = body

            # files panel (same as the Converter's)
            self.side = self.card(body, width=270)
            self.side.grid_propagate(False)
            self.side.grid_rowconfigure(1, weight=1)
            self.side.grid_columnconfigure(0, weight=1)
            sh = ctk.CTkFrame(self.side, fg_color="transparent")
            sh.grid(row=0, column=0, sticky="ew", padx=14, pady=(14, 6))
            self.side_title = self.section(sh, "Files")
            self.side_title.pack(side="left")
            self.all_var = tk.BooleanVar(value=False)
            ctk.CTkCheckBox(sh, text="All", variable=self.all_var, command=self.on_check_all, width=20,
                            checkbox_width=18, checkbox_height=18, corner_radius=5, font=self.F(12),
                            fg_color=C["accent"], hover_color=C["accent_hover"], border_color=C["faint"],
                            text_color=C["muted"]).pack(side="right")
            lw = ctk.CTkFrame(self.side, fg_color=C["field"], corner_radius=12)
            lw.grid(row=1, column=0, sticky="nsew", padx=12)
            lw.grid_rowconfigure(0, weight=1)
            lw.grid_columnconfigure(0, weight=1)
            self.list_frame = ctk.CTkScrollableFrame(lw, fg_color="transparent",
                                                     scrollbar_button_color=C["card_hi"],
                                                     scrollbar_button_hover_color=C["border"])
            self.list_frame.grid(row=0, column=0, sticky="nsew", padx=2, pady=4)
            self.em_empty = ctk.CTkFrame(lw, fg_color="transparent")
            ctk.CTkLabel(self.em_empty, text="", image=self.ico("download", C["faint"], 34)).pack(pady=(0, 10))
            self.label(self.em_empty, "Drop files here", 14, bold=True).pack()
            self.label(self.em_empty, "images, GIFs, videos, a folder\nor a 7TV link", 12, "muted",
                       justify="center").pack(pady=(2, 0))
            self.em_empty.place(relx=0.5, rely=0.45, anchor="center")
            ebr = ctk.CTkFrame(self.side, fg_color="transparent")
            ebr.grid(row=2, column=0, sticky="ew", padx=12, pady=12)
            ebr.grid_columnconfigure((0, 1), weight=1)
            self.button(ebr, "Add files", self.em_pick_files, "ghost", height=34).grid(row=0, column=0, sticky="ew", padx=(0, 4))
            self.button(ebr, "Add folder", self.em_pick_folder, "ghost", height=34,
                        image=self.ico("folder", C["text"], 14), compound="left").grid(row=0, column=1, sticky="ew", padx=(4, 0))
            self.button(ebr, "Remove", self.em_remove, "flat", height=30).grid(row=1, column=0, sticky="ew", padx=(0, 4), pady=(6, 0))
            self.button(ebr, "Clear all", self.em_clear, "flat", height=30).grid(row=1, column=1, sticky="ew", padx=(4, 0), pady=(6, 0))

            # preview
            prev = self.card(body)
            prev.grid(row=0, column=1, sticky="nsew")
            prev.grid_columnconfigure(0, weight=1)
            prev.grid_rowconfigure(1, weight=1)
            ph = ctk.CTkFrame(prev, fg_color="transparent")
            ph.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 8))
            self.section(ph, "Preview").pack(side="left")
            self.info_lbl = self.label(ph, "", 12, "muted")
            self.info_lbl.pack(side="right")
            cv_wrap = ctk.CTkFrame(prev, fg_color=C["discord"], corner_radius=12)
            cv_wrap.grid(row=1, column=0, sticky="nsew", padx=18)
            cv_wrap.grid_columnconfigure(0, weight=1)
            cv_wrap.grid_rowconfigure(0, weight=1)
            self.canvas = tk.Canvas(cv_wrap, bg=C["discord"], highlightthickness=0, bd=0)
            self.canvas.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
            self.canvas.bind("<Configure>", lambda _e: self.refresh_preview())
            nrow = ctk.CTkFrame(prev, fg_color="transparent")
            nrow.grid(row=2, column=0, sticky="ew", padx=18, pady=16)
            nrow.grid_columnconfigure(1, weight=1)
            self.label(nrow, "Name", 13, "muted").grid(row=0, column=0, padx=(0, 12))
            self.name_var = tk.StringVar()
            self.name_entry = ctk.CTkEntry(nrow, textvariable=self.name_var, height=38, corner_radius=10,
                                           border_width=1, border_color=C["border"], fg_color=C["field"],
                                           text_color=C["text"], font=self.F(14))
            self.name_entry.grid(row=0, column=1, sticky="ew")
            self.name_var.trace_add("write", self.on_name_change)

            # controls
            ctl = self.card(body, width=320)
            ctl.grid(row=0, column=2, sticky="ns", padx=(14, 0))
            ctl.grid_propagate(False)
            ctl.grid_columnconfigure(0, weight=1)
            pad = dict(padx=18, sticky="ew")

            hdr = ctk.CTkFrame(ctl, fg_color="transparent")
            hdr.grid(row=0, column=0, pady=(16, 8), **pad)
            self.section(hdr, "Cut into pieces").pack(side="left")
            self.auto = bool(self.cfg.get("auto_grid", True))
            self.auto_btn = self.button(hdr, "Auto", self.on_auto, "accent", width=64, height=26,
                                        corner_radius=13, font=self.F(12, True))
            self.auto_btn.pack(side="right")
            self.grid_box = ctk.CTkFrame(ctl, fg_color="transparent")
            self.grid_box.grid(row=1, column=0, **pad)
            self.cols_var = tk.IntVar(value=int(self.cfg["cols"]))
            self.rows_var = tk.IntVar(value=int(self.cfg["rows"]))
            self._stepper(self.grid_box, "Wide", self.cols_var).pack(side="left")
            self._stepper(self.grid_box, "Tall", self.rows_var).pack(side="left", padx=(12, 0))
            self.mode_help = self.label(ctl, "", 12, "muted", anchor="w", justify="left", wraplength=280)
            self.mode_help.grid(row=2, column=0, pady=(8, 0), **pad)

            self.section(ctl, "Fit").grid(row=4, column=0, pady=(18, 8), **pad)
            self.fit_seg = self.segmented(ctl, list(FITS), lambda _v: self.on_option())
            self.fit_seg.grid(row=5, column=0, **pad)
            self.fit_help = self.label(ctl, "", 12, "muted", anchor="w", justify="left", wraplength=280)
            self.fit_help.grid(row=6, column=0, pady=(6, 0), **pad)

            self.save_btn = self.button(ctl, "Save", lambda: self.on_save(False), "accent",
                                        height=48, corner_radius=12, font=self.F(15, True),
                                        image=self.ico("download", "#ffffff", 18), compound="left")
            self.save_btn.grid(row=7, column=0, pady=(20, 0), **pad)
            self.save_all_btn = self.button(ctl, "Save all", lambda: self.on_save(True), "ghost", height=38)

            st = ctk.CTkFrame(ctl, fg_color="transparent")
            st.grid(row=9, column=0, pady=(14, 0), **pad)
            st.grid_columnconfigure(1, weight=1)
            self.label(st, "Save to", 12, "muted").grid(row=0, column=0, padx=(0, 8))
            self.save_menu = ctk.CTkOptionMenu(
                st, values=list(SAVE_PRESETS.values()), command=self.on_save_preset, height=30, corner_radius=8,
                font=self.F(12), dropdown_font=self.F(12), fg_color=C["field"], button_color=C["card_hi"],
                button_hover_color=C["border"], text_color=C["text"], dropdown_fg_color=C["card_hi"],
                dropdown_hover_color=C["border"], dropdown_text_color=C["text"])
            self.save_menu.grid(row=0, column=1, sticky="ew")
            self.path_lbl = ctk.CTkLabel(ctl, text="", font=self.F(11), text_color=C["faint"], anchor="w",
                                         justify="left", wraplength=280, cursor="hand2")
            self.path_lbl.grid(row=10, column=0, pady=(4, 0), **pad)
            self.path_lbl.bind("<Button-1>", lambda _e: self.on_open_out(True))

            self.button(ctl, "Open folder", lambda: self.on_open_out(), "ghost", height=38,
                        image=self.ico("folder", C["text"], 15), compound="left").grid(row=11, column=0, pady=(14, 16), **pad)
            ctl.grid_rowconfigure(12, weight=1)

            # ---- status bar
            sb = ctk.CTkFrame(r, fg_color="transparent")
            sb.grid(row=4, column=0, sticky="ew", padx=24, pady=(10, 14))
            sb.grid_columnconfigure(1, weight=1)
            self.dot = ctk.CTkFrame(sb, width=8, height=8, corner_radius=4, fg_color=C["faint"])
            self.dot.grid(row=0, column=0, padx=(2, 10))
            self.status = self.label(sb, "Ready. Paste a 7TV link and press Load.", 12, "muted", anchor="w")
            self.status.grid(row=0, column=1, sticky="ew")
            self.progress = ctk.CTkProgressBar(sb, width=140, height=6, corner_radius=3, mode="indeterminate",
                                               progress_color=C["accent"], fg_color=C["card_hi"])
            self.button(sb, "Log", self.show_log, "flat", width=54, height=26, font=self.F(12)).grid(row=0, column=3)

            # initial values
            inv_f = {v: k for k, v in FITS.items()}
            self.fit_seg.set(inv_f.get(self.cfg["fit"], "Fill"))
            self._show_sidebar(True)
            self._sync_save_to()
            self._build_converter(r)
            self._build_drop_overlay(r)
            self._set_buttons()
            self.refresh_preview()

        def _stepper(self, parent, text, var):
            f = ctk.CTkFrame(parent, fg_color="transparent")
            self.label(f, text, 12, "muted").pack(side="left", padx=(0, 6))

            def bump(d):
                var.set(max(1, min(MAX_GRID, var.get() + d)))
                self.auto = False  # picking a number yourself turns Auto off
                self.on_option()
            box = ctk.CTkFrame(f, fg_color=C["field"], corner_radius=10)
            box.pack(side="left")
            ctk.CTkButton(box, text="−", width=30, height=30, corner_radius=8, fg_color="transparent",
                          hover_color=C["card_hi"], text_color=C["text"], font=self.F(15),
                          command=lambda: bump(-1)).pack(side="left", padx=2, pady=2)
            ctk.CTkLabel(box, textvariable=var, width=26, font=self.F(14, True), text_color=C["text"]).pack(side="left")
            ctk.CTkButton(box, text="+", width=30, height=30, corner_radius=8, fg_color="transparent",
                          hover_color=C["card_hi"], text_color=C["text"], font=self.F(15),
                          command=lambda: bump(1)).pack(side="left", padx=2, pady=2)
            return f

        def _show_sidebar(self, show=True):
            self.side.grid(row=0, column=0, sticky="ns", padx=(0, 14))  # always shown now

        # ================= plumbing =================
        def _poll(self):
            # run what the background threads handed us; one failing callback must never stop this loop
            while True:
                try:
                    fn = self.q.get_nowait()
                except queue.Empty:
                    break
                try:
                    fn()
                except Exception as e:  # noqa
                    import traceback
                    self.log_lines.append("UI error: " + "".join(traceback.format_exception_only(type(e), e)).strip())
                    traceback.print_exc()
            self.root.after(40, self._poll)

        def ui(self, fn):
            self.q.put(fn)

        def say(self, msg, kind="info"):
            colors = {"info": "muted", "ok": "good", "err": "bad", "warn": "warn", "busy": "accent"}
            self.status.configure(text=msg, text_color=C["text"] if kind in ("ok", "err", "warn") else C["muted"])
            self.dot.configure(fg_color=C[colors.get(kind, "muted")])
            self.log_lines.append(time.strftime("%H:%M:%S  ") + msg)

        def work(self, fn, done=None, label=None):
            if self.busy:
                return False
            self.busy = True
            self._set_buttons()
            self.progress.grid(row=0, column=2, padx=(10, 12))
            self.progress.start()
            if label:
                self.say(label, "busy")

            def run():
                try:
                    res = fn()
                except Exception as e:  # noqa
                    msg = str(e) or e.__class__.__name__
                    self.ui(self._unbusy)
                    self.ui(lambda: self.say(msg, "err"))
                    return
                self.ui(self._unbusy)  # first, so `done` may start the next job
                if done:
                    self.ui(lambda: done(res))
            threading.Thread(target=run, daemon=True).start()
            return True

        def _unbusy(self):
            self.busy = False
            self.progress.stop()
            self.progress.grid_forget()
            self._set_buttons()

        def _set_buttons(self):
            self.load_btn.configure(state="disabled" if self.busy else "normal")
            self.save_btn.configure(state="disabled" if self.busy or self.current is None else "normal")
            self.save_all_btn.configure(state="disabled" if self.busy else "normal")

        def settings(self):
            return dict(mode="auto" if self.auto else "custom", cols=self.cols_var.get(),
                        rows=self.rows_var.get(), tile=int(self.cfg["tile"]),
                        fit=FITS.get(self.fit_seg.get(), "auto"))

        def remember(self):
            s = self.settings()
            self.cfg.update(auto_grid=self.auto, cols=s["cols"], rows=s["rows"], fit=s["fit"])
            save_config(self.cfg)

        # ================= options =================
        def on_auto(self):
            self.auto = not self.auto  # press again to turn Auto off and keep the current numbers
            self.on_option()

        def _sync_grid(self):
            """Show Auto's choice in the Wide/Tall boxes and explain the cut in words."""
            it = self.current
            loaded = it is not None and it["frames"] is not None
            if self.auto and loaded:
                c, r = auto_grid(*it["frames"][0][0].size)
                self.cols_var.set(c)
                self.rows_var.set(r)
            self.auto_btn.configure(fg_color=C["accent"] if self.auto else C["card_hi"],
                                    hover_color=C["accent_hover"] if self.auto else C["border"],
                                    text_color="white" if self.auto else C["muted"])
            c, r = self.cols_var.get(), self.rows_var.get()
            n = c * r
            if not loaded:
                msg = ("Auto cuts wide emotes into square pieces, one per full height."
                       if self.auto else f"{n} piece{'s' if n > 1 else ''}: {c} wide × {r} tall.")
            else:
                w, h = it["frames"][0][0].size
                if n == 1:
                    msg = f"Not cut: saved as 1 emote ({w}×{h} is about square)." if self.auto else "Not cut: saved as 1 emote."
                elif self.auto:
                    msg = f"This {w}×{h} emote is {w / h:.1f}× wider than tall, so Auto cuts it into {n} pieces."
                else:
                    msg = f"Cut into {n} pieces: {c} wide × {r} tall. Press Auto to let the app choose."
            self.mode_help.configure(text=msg)
            fit = FITS.get(self.fit_seg.get(), "auto")
            txt = FIT_HELP[fit]
            if fit == "auto" and loaded:
                real = resolve_fit("auto", *it["frames"][0][0].size, c, r)
                txt = FIT_HELP["auto"] if real == "stretch" else "This shape is too different, so bars are added instead of squashing it."
            self.fit_help.configure(text=txt)

        def on_option(self):
            self.remember()
            self.refresh_preview()

        # ================= loading =================
        def on_load(self):
            try:
                kind, val = parse_input(self.url_entry.get())
            except ValueError as e:
                self.say(str(e), "err")
                return
            if kind == "file":
                self._load_file(val)
                return

            def job():
                if kind == "emote":
                    ref = fetch_emote(val)
                    return None, [ref]
                return fetch_set(val) if kind == "set" else fetch_user_set(val)

            def done(res):
                title, refs = res
                if not refs:
                    self.say("That emote set is empty.", "warn")
                    return
                self.set_items(title, [dict(name=x.name, ref=x, path=None, frames=None, raw=None) for x in refs],
                               append=(kind == "emote"))
                if len(refs) > 1:
                    self.say(f"Loaded “{title}” with {len(refs)} emotes. Pick one to preview, or Save all.", "ok")
            self.work(job, done, "Looking it up on 7TV…")

        def on_open_file(self):
            p = filedialog.askopenfilename(title="Pick an image, GIF or video", filetypes=[
                ("Images and videos", " ".join("*" + e for e in sorted(MEDIA_EXT))), ("All files", "*.*")])
            if p:
                self.url_entry.delete(0, "end")
                self.url_entry.insert(0, p)
                self._load_file(p)

        def _load_file(self, path):
            name = Path(path).stem
            self.set_items(None, [dict(name=name, ref=None, path=path, frames=None, raw=None)], append=True)

        @staticmethod
        def _item_key(it):
            return ("7tv", it["ref"].id) if it.get("ref") is not None else ("file", str(it.get("path")))

        def set_items(self, title, items, append=False):
            """Show items in the Files list. append=True adds to the list (skipping ones already there)."""
            if append and self.items:
                have = {self._item_key(it) for it in self.items}
                new = [it for it in items if self._item_key(it) not in have]
                if not new:  # already in the list: just select it
                    keys = [self._item_key(it) for it in self.items]
                    self.select_item(keys.index(self._item_key(items[0])))
                    return
                first = len(self.items)
                self.items = self.items + new
                self.set_title = None
            else:
                first = 0
                self.items = list(items)
                self.set_title = title if len(items) > 1 else None
            self._rebuild_rows()
            if self.items:
                self.select_item(first)

        def _rebuild_rows(self):
            for w in self.list_frame.winfo_children():
                w.destroy()
            self.rows = []
            for i, it in enumerate(self.items):
                self._add_row(i, it)
            n = len(self.items)
            self.side_title.configure(text=f"FILES · {n}" if n else "FILES")
            if n:
                self.em_empty.place_forget()
            else:
                self.em_empty.place(relx=0.5, rely=0.45, anchor="center")
            if n > 1:
                self.save_all_btn.grid(row=8, column=0, padx=18, pady=(8, 0), sticky="ew")
            else:
                self.save_all_btn.grid_forget()
            self.all_var.set(False)
            self._update_save_all()

        def em_pick_files(self):
            ps = filedialog.askopenfilenames(title="Add images, GIFs or videos", filetypes=[
                ("Images and videos", " ".join("*" + e for e in sorted(MEDIA_EXT))), ("All files", "*.*")])
            if ps:
                self.load_paths(list(ps))

        def em_pick_folder(self):
            d = filedialog.askdirectory(title="Add every image, GIF and video in a folder")
            if d:
                self.load_paths([d])

        def em_remove(self):
            gone = self._checked() or ([self.current] if self.current is not None else [])
            if not gone:
                return
            ids = {id(it) for it in gone}
            self.items = [it for it in self.items if id(it) not in ids]
            if self.current is not None and id(self.current) in ids:
                self.current = None
            self._rebuild_rows()
            if self.items and self.current is None:
                self.select_item(0)
            elif not self.items:
                self.em_clear()
            else:
                idx = self.items.index(self.current)
                for j, (_, _, btn) in enumerate(self.rows):
                    btn.configure(fg_color=C["card_hi"] if j == idx else "transparent")
            self.say(f"Removed {len(gone)} from the list.", "info")

        def em_clear(self):
            self.items, self.current, self.set_title = [], None, None
            self._rebuild_rows()
            self._name_lock = True
            self.name_var.set("")
            self._name_lock = False
            self._set_buttons()
            self.refresh_preview()

        def _add_row(self, i, it):
            row = ctk.CTkFrame(self.list_frame, fg_color="transparent", corner_radius=8)
            row.pack(fill="x", pady=1)
            var = tk.BooleanVar(value=False)
            ctk.CTkCheckBox(row, text="", variable=var, width=22, checkbox_width=18, checkbox_height=18,
                            corner_radius=5, fg_color=C["accent"], hover_color=C["accent_hover"],
                            border_color=C["faint"], command=self._update_save_all).pack(side="left", padx=(6, 2))
            btn = ctk.CTkButton(row, text=it["name"], anchor="w", height=30, corner_radius=8,
                                fg_color="transparent", hover_color=C["card_hi"], text_color=C["text"],
                                font=self.F(13), command=lambda i=i: self.select_item(i))
            btn.pack(side="left", fill="x", expand=True)
            self.rows.append((row, var, btn))

        def on_check_all(self):
            for _, var, _ in self.rows:
                var.set(self.all_var.get())
            self._update_save_all()

        def _checked(self):
            return [self.items[i] for i, (_, var, _) in enumerate(self.rows) if var.get()]

        def _update_save_all(self):
            k = len(self._checked())
            self.save_all_btn.configure(text=f"Save selected ({k})" if k else f"Save all ({len(self.items)})")

        def ensure_loaded(self, it):
            if it["frames"] is None:
                if it["path"]:
                    pth = Path(it["path"])
                    raw = pth.read_bytes()
                    ext = pth.suffix.lstrip(".").lower() or "img"
                    it["raw"] = (raw, ext)
                    it["frames"] = load_any(pth)
                else:
                    raw, ext = download_emote(it["ref"])
                    it["raw"] = (raw, ext)
                    it["frames"] = load_frames(raw)
            return it

        def select_item(self, idx):
            it = self.items[idx]
            for j, (_, _, btn) in enumerate(self.rows):
                btn.configure(fg_color=C["card_hi"] if j == idx else "transparent")
            if it["frames"] is not None:
                self._show(it)
                return
            self.current = None
            self._set_buttons()

            def load():
                try:
                    return self.ensure_loaded(it)
                except NeedFFmpeg:
                    self.ui(lambda: self.ask_ffmpeg(lambda: self.select_item(idx)))
                    raise
            if not self.work(load, self._show, f"{'Downloading' if it['ref'] else 'Opening'} {it['name']}…"):
                self.say("Busy, try again in a second.", "warn")

        def _show(self, it):
            self.current = it
            self._name_lock = True
            self.name_var.set(it["name"])
            self._name_lock = False
            self._set_buttons()
            n = len(it["frames"])
            w, h = it["frames"][0][0].size
            self.say(f"{it['name']} loaded · {w}×{h}px · {'animated, %d frames' % n if n > 1 else 'static'}", "ok")
            self.refresh_preview()

        def on_name_change(self, *_):
            if getattr(self, "_name_lock", False) or self.current is None:
                return
            self.current["name"] = self.name_var.get()
            for (_, _, btn), it in zip(self.rows, self.items):
                if it is self.current:
                    btn.configure(text=it["name"])
            self.update_texts()

        # ================= preview =================
        def refresh_preview(self):
            if not hasattr(self, "fit_seg"):
                return
            if self.anim_job:
                self.root.after_cancel(self.anim_job)
                self.anim_job = None
            cv = self.canvas
            cv.delete("all")
            cw, ch = max(60, cv.winfo_width()), max(60, cv.winfo_height())
            it = self.current
            if it is None or it["frames"] is None:
                cv.create_text(cw // 2, ch // 2 - 12, text="Nothing loaded yet", fill=C["text"],
                               font=(FAMILY, 15, "bold"))
                cv.create_text(cw // 2, ch // 2 + 16, text="Paste a 7TV link above, or open a GIF / PNG / WebP",
                               fill=C["muted"], font=(FAMILY, 11))
                self.info_lbl.configure(text="")
                self._sync_grid()
                self.update_texts()
                return
            self._sync_grid()
            s = self.settings()
            frames = it["frames"]
            w, h = frames[0][0].size
            cols, rows = plan_grid(w, h, s["mode"], s["cols"], s["rows"])
            n = cols * rows
            names = piece_names(it["name"], n)
            gap = 14 if n > 1 else 0          # pieces are drawn apart so the cut is obvious
            lab = 24 if n > 1 else 0          # room for each piece's :name: under it
            pt = max(8, min(s["tile"] * 2, (cw - 40 - gap * (cols - 1)) // cols,
                            (ch - 40 - lab * rows - gap * (rows - 1)) // rows))
            comp = compose(frames[:300], cols, rows, pt, s["fit"])
            W, H = cols * pt + gap * (cols - 1), rows * (pt + lab) + gap * (rows - 1)
            pos = [(c * (pt + gap), r * (pt + lab + gap)) for r in range(rows) for c in range(cols)]
            imgs = []
            for f, d in comp:
                if n > 1:
                    sheet = Image.new("RGBA", (W, H), (0, 0, 0, 0))
                    for i, (px, py) in enumerate(pos):
                        c, r = i % cols, i // cols
                        sheet.paste(f.crop((c * pt, r * pt, (c + 1) * pt, (r + 1) * pt)), (px, py))
                    f = sheet
                imgs.append((ImageTk.PhotoImage(f), d))
            self.anim_imgs = imgs
            x0, y0 = (cw - W) // 2, (ch - H) // 2
            for px, py in pos:
                cv.create_rectangle(x0 + px - 1, y0 + py - 1, x0 + px + pt, y0 + py + pt, outline="#4a4d57")
            self.img_id = cv.create_image(x0, y0, anchor="nw", image=self.anim_imgs[0][0])
            if n > 1:
                font = (MONO, 10 if pt >= 70 else 8)
                for i, (px, py) in enumerate(pos):
                    text = f":{names[i]}:" if pt >= 70 else str(i + 1)
                    cv.create_text(x0 + px + pt // 2, y0 + py + pt + 12, text=text, fill=C["text"], font=font)
            fmt = "GIF" if len(frames) > 1 else "PNG"
            self.info_lbl.configure(text=f"{'1 emote' if n == 1 else f'{n} pieces · {cols} wide × {rows} tall'}"
                                         f" · square {fmt}{'s' if n > 1 else ''}")
            self.update_texts()
            if len(self.anim_imgs) > 1:
                self._animate(0)

        def _animate(self, i):
            img, d = self.anim_imgs[i]
            self.canvas.itemconfigure(self.img_id, image=img)
            self.anim_job = self.root.after(d, self._animate, (i + 1) % len(self.anim_imgs))

        def update_texts(self):
            pass  # (the "paste in Discord" box was removed in v1.4.1)

        # ================= saving =================
        def out_dir(self):
            return preset_dir(self.cfg.get("save_preset", "app"), self.cfg.get("out_dir", ""))

        def _sync_save_to(self):
            self.save_menu.set(SAVE_PRESETS.get(self.cfg.get("save_preset", "app"), "App folder"))
            self.path_lbl.configure(text=f"  {self.out_dir()}", image=self.ico("folder", C["faint"], 13), compound="left")

        def on_save_preset(self, label):
            key = next(k for k, v in SAVE_PRESETS.items() if v == label)
            if key == "custom":
                d = filedialog.askdirectory(title="Pick where emotes are saved",
                                            initialdir=self.cfg.get("out_dir") or str(self.out_dir()))
                if not d:
                    self._sync_save_to()
                    return
                self.cfg["out_dir"] = d
            self.cfg["save_preset"] = key
            save_config(self.cfg)
            self.last_out = None
            self._sync_save_to()
            self.say(f"Emotes will be saved to {self.out_dir()}", "ok")

        def on_save(self, batch):
            if batch:
                targets = self._checked() or list(self.items)
            elif self.current is not None:
                targets = [self.current]
            else:
                return
            out = self.out_dir()
            if self.set_title and batch:
                out = out / folder_name(self.set_title)
            s = self.settings()
            keep = bool(self.cfg.get("keep_original"))
            self.remember()

            def job():
                results = []
                for i, it in enumerate(targets, 1):
                    if len(targets) > 1:
                        self.ui(lambda i=i, it=it: self.say(f"Saving {i}/{len(targets)}: {it['name']}…", "busy"))
                    try:
                        self.ensure_loaded(it)
                        res = make_emote(it["frames"], it["name"], out, s["mode"], s["cols"], s["rows"],
                                         s["tile"], s["fit"], it["raw"] if keep else None)
                        results.append((it, res))
                        self.log_lines.append(f"  OK      {it['name']}: {len(res['files'])} file(s), {res['note']}")
                    except Exception as e:  # noqa
                        results.append((it, None))
                        self.log_lines.append(f"  FAILED  {it['name']}: {e}")
                return results

            def done(results):
                ok = [r for _, r in results if r]
                failed = len(results) - len(ok)
                self.last_out = out
                if len(results) == 1 and ok:
                    res = ok[0]
                    big = max(sz for _, sz in res["files"])
                    what = f"{len(res['files'])} pieces" if len(res["files"]) > 1 else res["files"][0][0].name
                    warn = "WARNING" in res["note"]
                    self.say(f"Saved {what} · largest {big / 1024:.0f} KB · {res['note']}", "warn" if warn else "ok")
                elif ok:
                    self.say(f"Saved {len(ok)} emotes to {out}" + (f" · {failed} failed (see Log)" if failed else ""),
                             "warn" if failed else "ok")
                else:
                    self.say("Nothing was saved. See Log for details.", "err")
            self.work(job, done, f"Saving {len(targets)} emote{'s' if len(targets) > 1 else ''}…")

        def on_open_out(self, root_folder=False):
            p = (None if root_folder else getattr(self, "last_out", None)) or self.out_dir()
            try:
                p.mkdir(parents=True, exist_ok=True)
                open_folder(p)
            except Exception as e:  # noqa
                self.say(f"Couldn't open {p}: {e}", "err")

        # ================= dialogs =================
        def dialog(self, title, w=520, h=440):
            top = ctk.CTkToplevel(self.root)
            top.title(title)
            top.configure(fg_color=C["bg"])
            top.resizable(False, False)
            self.root.update_idletasks()
            x = self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2
            y = self.root.winfo_rooty() + (self.root.winfo_height() - h) // 3
            top.geometry(f"{w}x{h}+{max(0, x)}+{max(0, y)}")
            top.transient(self.root)
            set_icon(top)
            top.after(150, top.lift)

            def grab():
                try:
                    top.grab_set()
                except Exception:
                    pass
            top.after(200, grab)
            top.grid_columnconfigure(0, weight=1)
            return top

        def text_dialog(self, title, heading, sub, text, buttons):
            top = self.dialog(title, 560, 480)
            top.grid_rowconfigure(2, weight=1)
            self.label(top, heading, 18, bold=True, anchor="w").grid(row=0, column=0, sticky="ew", padx=24, pady=(22, 2))
            self.label(top, sub, 12, "muted", anchor="w").grid(row=1, column=0, sticky="ew", padx=24, pady=(0, 12))
            box = ctk.CTkTextbox(top, corner_radius=12, border_width=1, border_color=C["border"],
                                 fg_color=C["card"], text_color=C["text"], font=self.F(13), wrap="word")
            box.grid(row=2, column=0, sticky="nsew", padx=24)
            box.insert("1.0", text)
            box.configure(state="disabled")
            row = ctk.CTkFrame(top, fg_color="transparent")
            row.grid(row=3, column=0, sticky="e", padx=24, pady=18)
            for label, kind, cmd in buttons:
                self.button(row, label, lambda c=cmd: (top.destroy(), c and c()), kind, width=120).pack(side="left", padx=(8, 0))
            return top

        def show_log(self):
            self.text_dialog("Log", "Activity log", "Everything that happened since the app started.",
                             "\n".join(self.log_lines[-400:]) or "Nothing yet.", [("Close", "ghost", None)])

        def show_changelog(self, after=None):
            def job():
                return get_changelog(after=after, upto=__version__)

            def done(entries):
                head = f"What's new in v{__version__}" if after else "Changelog"
                sub = (f"You were updated from v{after}." if after else
                       f"You're on v{__version__} ({KIND_LABEL[install_kind()]}).")
                self.text_dialog("What's new", head, sub, format_changelog(entries), [("Got it", "accent", None)])
            threading.Thread(target=lambda: self.ui(lambda e=job(): done(e)), daemon=True).start()

        def open_settings(self, page="General"):
            if getattr(self, "settings_win", None) is not None and self.settings_win.winfo_exists():
                self.settings_win.lift()
                return
            top = self.dialog("Settings", 780, 560)
            self.settings_win = top
            top.grid_columnconfigure(1, weight=1)
            top.grid_rowconfigure(0, weight=1)
            nav = ctk.CTkFrame(top, fg_color=C["card"], corner_radius=0, width=200)
            nav.grid(row=0, column=0, rowspan=2, sticky="nsew")
            top.grid_columnconfigure(0, weight=0, minsize=200)
            nav.grid_propagate(False)
            hd = ctk.CTkFrame(nav, fg_color="transparent")
            hd.pack(fill="x", padx=16, pady=(20, 18))
            if getattr(self, "_logo", None) is not None:
                ctk.CTkLabel(hd, text="", image=ctk.CTkImage(self._logo._light_image, self._logo._light_image,
                                                             size=(28, 28))).pack(side="left", padx=(0, 10))
            tx = ctk.CTkFrame(hd, fg_color="transparent")
            tx.pack(side="left")
            self.label(tx, "Settings", 15, bold=True, anchor="w").pack(anchor="w")
            self.label(tx, f"v{__version__} · {KIND_LABEL[install_kind()]}", 11, "faint", anchor="w").pack(anchor="w")
            body = ctk.CTkFrame(top, fg_color="transparent")
            body.grid(row=0, column=1, sticky="nsew", padx=28, pady=(22, 0))
            body.grid_columnconfigure(0, weight=1)
            foot = ctk.CTkFrame(top, fg_color="transparent")
            foot.grid(row=1, column=1, sticky="ew", padx=28, pady=18)
            ok_lbl = ctk.CTkLabel(foot, text="  Changes are saved automatically", font=self.F(12),
                                  text_color=C["faint"], image=self.ico("check", C["faint"], 13), compound="left")
            ok_lbl.pack(side="left")
            self.button(foot, "Done", top.destroy, "accent", width=110).pack(side="right")
            navs = {}

            def save():
                save_config(self.cfg)

            def head(title, sub):
                self.label(body, title, 20, bold=True, anchor="w").grid(row=0, column=0, sticky="ew")
                self.label(body, sub, 12, "muted", anchor="w").grid(row=1, column=0, sticky="ew", pady=(2, 16))

            def group(row, title=None):
                if title:
                    self.section(body, title).grid(row=row, column=0, sticky="ew", pady=(4, 6))
                    row += 1
                g = ctk.CTkFrame(body, fg_color=C["card"], corner_radius=14, border_width=1, border_color=C["border"])
                g.grid(row=row, column=0, sticky="ew", pady=(0, 14))
                g.grid_columnconfigure(0, weight=1)
                g._rows = 0
                return g, row + 1

            def line(g, title, desc, control_factory):
                if g._rows:
                    ctk.CTkFrame(g, fg_color=C["border"], height=1, corner_radius=0).grid(
                        row=g._rows * 2 - 1, column=0, columnspan=2, sticky="ew", padx=16)
                r_ = g._rows * 2
                txt = ctk.CTkFrame(g, fg_color="transparent")
                txt.grid(row=r_, column=0, sticky="ew", padx=(16, 12), pady=12)
                self.label(txt, title, 13, anchor="w").pack(anchor="w")
                if desc:
                    self.label(txt, desc, 11, "muted", anchor="w", justify="left", wraplength=360).pack(anchor="w", pady=(1, 0))
                ctl = control_factory(g)
                ctl.grid(row=r_, column=1, sticky="e", padx=16)
                g._rows += 1
                return ctl

            def switch(key, on_change=None, value=None):
                def make(g):
                    var = tk.BooleanVar(value=bool(self.cfg.get(key)) if value is None else value)

                    def changed():
                        if on_change:
                            on_change(bool(var.get()))
                        else:
                            self.cfg[key] = bool(var.get())
                            save()
                    return ctk.CTkSwitch(g, text="", variable=var, onvalue=True, offvalue=False, width=46,
                                         command=changed, progress_color=C["accent"], button_color="#ffffff",
                                         button_hover_color="#ffffff", fg_color=C["card_hi"])
                return make

            def clear():
                for w in body.winfo_children():
                    w.destroy()

            def page_general():
                clear()
                head("General", "How the app behaves on your PC.")
                g, row = group(2, "Window and tray")
                line(g, "Keep running in the tray", "Closing the window hides it to the tray instead of quitting.",
                     switch("close_to_tray"))
                if sys.platform.startswith("win"):
                    line(g, "Start with Windows", "Starts quietly in the tray when you sign in.",
                         switch("autostart", lambda on: (set_autostart(on), self.cfg.update(autostart=on), save()),
                                value=autostart_enabled()))
                g, row = group(row, "7TV")
                line(g, "Notice copied 7TV links", "When you copy an emote link on 7tv.app, offer to save it.",
                     switch("watch_clipboard"))

            def page_saving():
                clear()
                head("Saving", "Where emotes go and how they're made.")
                g, row = group(2, "Location")

                def loc(gg):
                    f = ctk.CTkFrame(gg, fg_color="transparent")
                    m = ctk.CTkOptionMenu(f, values=list(SAVE_PRESETS.values()), width=170, height=32, corner_radius=8,
                                          font=self.F(12), dropdown_font=self.F(12), fg_color=C["field"],
                                          button_color=C["card_hi"], button_hover_color=C["border"], text_color=C["text"],
                                          dropdown_fg_color=C["card_hi"], dropdown_hover_color=C["border"],
                                          dropdown_text_color=C["text"],
                                          command=lambda v: (self.on_save_preset(v), path.configure(text=str(self.out_dir()))))
                    m.set(SAVE_PRESETS.get(self.cfg.get("save_preset", "app"), "App folder"))
                    m.pack(side="left")
                    return f
                line(g, "Save emotes to", "Converted files go into a “Converted” folder inside it.", loc)
                pr = ctk.CTkFrame(g, fg_color=C["field"], corner_radius=10)
                pr.grid(row=g._rows * 2, column=0, columnspan=2, sticky="ew", padx=16, pady=(0, 14))
                pr.grid_columnconfigure(0, weight=1)
                path = ctk.CTkLabel(pr, text=str(self.out_dir()), font=self.F(11), text_color=C["muted"], anchor="w")
                path.grid(row=0, column=0, sticky="ew", padx=12, pady=8)
                self.button(pr, "Open", lambda: self.on_open_out(True), "flat", width=70, height=28,
                            image=self.ico("folder", C["muted"], 13), compound="left").grid(row=0, column=1, padx=6)
                g._rows += 1
                g, row = group(row, "Emotes")

                def size(gg):
                    sg = self.segmented(gg, ["128 px", "96 px", "64 px"], lambda v: (self.cfg.update(tile=int(v.split()[0])),
                                                                                    save(), self.refresh_preview()))
                    sg.set(f"{self.cfg['tile']} px" if self.cfg["tile"] in (128, 96, 64) else "128 px")
                    return sg
                line(g, "Piece size", "128 px is Discord's emoji size. Big video emotes may be shrunk to fit 256 KB.", size)
                line(g, "Also keep the original file", "Saves the file from 7TV next to the pieces.", switch("keep_original"))

            def page_updates():
                clear()
                head("Updates", "New versions come straight from GitHub.")
                g, row = group(2)
                line(g, "Check for updates when the app starts", None, switch("check_updates"))
                line(g, "Install updates automatically", "Updates install and the app restarts when it's idle.",
                     switch("auto_install"))
                g, row = group(row)
                st_txt = {"uptodate": "You're up to date", "available": "An update is ready",
                          "checking": "Checking…", "error": "Couldn't check", "downloading": "Downloading…"}

                def now(gg):
                    f = ctk.CTkFrame(gg, fg_color="transparent")
                    self.button(f, "What's new", self.show_changelog, "flat", width=100, height=32).pack(side="left", padx=(0, 6))
                    self.button(f, "Check now", lambda: (self.check_updates(True), top.after(1500, page_updates)), "ghost",
                                width=110, height=32, image=self.ico("refresh", C["text"], 13), compound="left").pack(side="left")
                    return f
                line(g, f"Version {__version__}", st_txt.get(self.upd_state, "Not checked yet"), now)

            def page_about():
                clear()
                head("About", "Emotes Downloader, made by ISdook.")
                g, row = group(2)
                line(g, "Emotes Downloader", f"Version {__version__} · {KIND_LABEL[install_kind()]} · 7TV emotes to Discord",
                     lambda gg: self.button(gg, "GitHub", lambda: __import__("webbrowser").open(f"https://github.com/{GITHUB_REPO}"),
                                            "ghost", width=100, height=32, image=self.ico("external", C["text"], 13), compound="left"))
                line(g, "Settings file", str(CONFIG_PATH),
                     lambda gg: self.button(gg, "Show", lambda: open_folder(CONFIG_PATH.parent), "flat", width=80, height=32))

                def reset(gg):
                    def do():
                        keep = {k: self.cfg.get(k) for k in ("last_version", "told_tray")}
                        self.cfg.clear()
                        self.cfg.update(DEFAULTS)
                        self.cfg.update({k: v for k, v in keep.items() if v is not None})
                        save()
                        self._sync_save_to()
                        self.refresh_preview()
                        self.say("Settings were reset.", "ok")
                        page_about()
                    return self.button(gg, "Reset", do, "ghost", width=90, height=32, text_color=C["bad"])
                line(g, "Reset all settings", "Puts every option back to how it was on first start.", reset)

            pages = {"General": ("gear", page_general), "Saving": ("folder", page_saving),
                     "Updates": ("refresh", page_updates), "About": ("external", page_about)}

            def go(name):
                for n_, b_ in navs.items():
                    on = n_ == name
                    b_.configure(fg_color=C["card_hi"] if on else "transparent",
                                 text_color=C["text"] if on else C["muted"],
                                 image=self.ico(pages[n_][0], C["accent"] if on else C["muted"], 15))
                pages[name][1]()
            for name, (icon, _) in pages.items():
                b_ = ctk.CTkButton(nav, text="  " + name, anchor="w", height=38, corner_radius=10,
                                   fg_color="transparent", hover_color=C["card_hi"], text_color=C["muted"],
                                   font=self.F(13), image=self.ico(icon, C["muted"], 15), compound="left",
                                   command=lambda n_=name: go(n_))
                b_.pack(fill="x", padx=10, pady=2)
                navs[name] = b_
            go(page if page in pages else "General")


        # ================= tabs =================
        def switch_tab(self, name):
            self.tab = name
            self.tab_seg.set(name)
            if name == "Converter":
                for w in (self.bar, self.banner, self.body):
                    w.grid_remove()
                self.conv.grid(row=1, column=0, rowspan=3, sticky="nsew", padx=22)
                self.say("Converter: drop any images, GIFs or videos, pick a size and format, then Convert.", "info")
            else:
                self.conv.grid_remove()
                self.bar.grid()
                self.body.grid()
                if getattr(self, "clip_ref", None) is not None and self.banner.winfo_manager() == "" and \
                        getattr(self, "_banner_visible", False):
                    self.banner.grid()

        # ================= drag & drop =================
        def _build_drop_overlay(self, r):
            ov = ctk.CTkFrame(r, fg_color=C["bg"], corner_radius=0)
            box = ctk.CTkFrame(ov, fg_color=C["card"], corner_radius=20, border_width=2, border_color=C["accent"])
            box.place(relx=0.5, rely=0.5, relwidth=0.86, relheight=0.78, anchor="center")
            inner = ctk.CTkFrame(box, fg_color="transparent")
            inner.place(relx=0.5, rely=0.5, anchor="center")
            ctk.CTkLabel(inner, text="", image=self.ico("download", C["accent"], 54)).pack(pady=(0, 14))
            self.drop_title = self.label(inner, "Drop to load", 22, bold=True)
            self.drop_title.pack()
            self.drop_sub = self.label(inner, "", 13, "muted")
            self.drop_sub.pack(pady=(6, 0))
            self.drop_overlay = ov
            dnd = getattr(self.root, "_dnd", None)
            if not dnd:
                return
            try:
                self.root.drop_target_register(*dnd)
                self.root.dnd_bind("<<DropEnter>>", self._drop_enter)
                self.root.dnd_bind("<<DropLeave>>", self._drop_leave)
                self.root.dnd_bind("<<Drop>>", self.on_drop)
            except Exception as e:  # noqa
                self.log_lines.append(f"Drag and drop not available: {e!r}")

        def _drop_enter(self, e):
            if self.tab == "Converter":
                self.drop_title.configure(text="Drop to add to the Converter")
                self.drop_sub.configure(text="Images, GIFs, WebP, AVIF, videos (MP4, WebM, MOV…) or whole folders")
            else:
                self.drop_title.configure(text="Drop to load")
                self.drop_sub.configure(text="Images, GIFs, videos or a folder of them, or a 7TV link")
            self.drop_overlay.place(x=0, y=0, relwidth=1, relheight=1)
            self.drop_overlay.lift()
            return getattr(e, "action", "copy")

        def _drop_leave(self, e=None):
            self.drop_overlay.place_forget()
            return getattr(e, "action", "copy")

        def on_drop(self, e):
            self._drop_leave()
            data = getattr(e, "data", "") or ""
            try:
                parts = list(self.root.tk.splitlist(data))
            except Exception:
                parts = [data]
            paths = [p for p in parts if p and os.path.exists(p)]
            if not paths:
                text = data.strip().strip("{}")
                try:
                    kind, val = parse_input(text)
                except ValueError:
                    kind = None
                if kind in ("emote", "set", "user"):
                    self.switch_tab("Emotes")
                    self.url_entry.delete(0, "end")
                    self.url_entry.insert(0, text)
                    self.on_load()
                else:
                    self.say("That can't be used. Drop images, GIFs, videos, a folder or a 7TV link.", "warn")
                return getattr(e, "action", "copy")
            if self.tab == "Converter":
                self.conv_add(paths)
            else:
                self.load_paths(paths)
            return getattr(e, "action", "copy")

        def load_paths(self, paths):
            files = collect_media(paths, videos=True)
            if not files:
                self.say("No images, GIFs or videos found in what you dropped.", "warn")
                return
            items = [dict(name=f.stem, ref=None, path=str(f), frames=None, raw=None) for f in files]
            dirs = [Path(p) for p in paths if Path(p).is_dir()]
            if not self.items and len(dirs) == 1 and len(paths) == 1 and len(items) > 1:
                self.set_items(dirs[0].name, items)  # a folder into an empty list: save all goes into that folder name
            else:
                self.set_items(None, items, append=True)
            if len(items) > 1:
                self.say(f"Added {len(items)} files. Pick one to preview, or Save all.", "ok")

        def ask_ffmpeg(self, then=None):
            if getattr(self, "_ffmpeg_busy", False):
                return

            def go():
                self._ffmpeg_busy = True

                def prog(p):
                    self.ui(lambda: self.say(f"Downloading video support… {int(p * 100)}%", "busy"))

                def job():
                    try:
                        install_ffmpeg(prog)
                        err = None
                    except Exception as e:  # noqa
                        err = e

                    def done():
                        self._ffmpeg_busy = False
                        if err:
                            self.say(f"Couldn't download video support: {err}", "err")
                            return
                        self.say("Video support is ready.", "ok")
                        self.toast("Video support installed", "You can now open and convert video files.", "ok")
                        if then:
                            then()
                    self.ui(done)
                threading.Thread(target=job, daemon=True).start()
            self.text_dialog("Video support", "Turn on video support?",
                             "Needed once to open MP4, WebM, MOV and other videos.",
                             "To read video files the app uses ffmpeg, a free and widely used video tool.\n\n"
                             "It's a one-time download of about 40 MB from PyPI (the official Python package site), "
                             f"saved to:\n{data_dir()}\n\nImages and GIFs work without it.",
                             [("Not now", "ghost", None), ("Download", "accent", go)])

        # ================= Converter tab =================
        def _build_converter(self, r):
            cf = ctk.CTkFrame(r, fg_color="transparent")
            self.conv = cf
            cf.grid_columnconfigure(1, weight=1)
            cf.grid_rowconfigure(0, weight=1)
            self.conv_items, self.conv_sel, self.conv_gen = [], None, 0
            self.conv_job, self.conv_anim, self.conv_imgs, self.conv_view = None, None, [], "After"

            # ---- files (left)
            left = self.card(cf, width=300)
            left.grid(row=0, column=0, sticky="ns", padx=(0, 14))
            left.grid_propagate(False)
            left.grid_rowconfigure(1, weight=1)
            left.grid_columnconfigure(0, weight=1)
            hd = ctk.CTkFrame(left, fg_color="transparent")
            hd.grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 8))
            self.section(hd, "Files").pack(side="left")
            self.conv_count = self.label(hd, "", 12, "faint")
            self.conv_count.pack(side="right")
            lw = ctk.CTkFrame(left, fg_color=C["field"], corner_radius=12)
            lw.grid(row=1, column=0, sticky="nsew", padx=12)
            lw.grid_rowconfigure(0, weight=1)
            lw.grid_columnconfigure(0, weight=1)
            self.conv_list = tk.Listbox(lw, bg=C["field"], fg=C["text"], selectbackground=C["accent"],
                                        selectforeground="white", highlightthickness=0, bd=0, activestyle="none",
                                        font=(FAMILY, 10), exportselection=False, selectmode="extended")
            self.conv_list.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)
            self.conv_list.bind("<<ListboxSelect>>", lambda _e: self._conv_select())
            sb = ctk.CTkScrollbar(lw, command=self.conv_list.yview, button_color=C["card_hi"],
                                  button_hover_color=C["border"])
            sb.grid(row=0, column=1, sticky="ns", pady=6)
            self.conv_list.configure(yscrollcommand=sb.set)
            self.conv_empty = ctk.CTkFrame(lw, fg_color="transparent")
            self.conv_empty.place(relx=0.5, rely=0.45, anchor="center")
            ctk.CTkLabel(self.conv_empty, text="", image=self.ico("download", C["faint"], 34)).pack(pady=(0, 10))
            self.label(self.conv_empty, "Drop files here", 14, bold=True).pack()
            self.label(self.conv_empty, "images, GIFs, videos or a folder", 12, "muted").pack(pady=(2, 0))
            br = ctk.CTkFrame(left, fg_color="transparent")
            br.grid(row=2, column=0, sticky="ew", padx=12, pady=12)
            br.grid_columnconfigure((0, 1), weight=1)
            self.button(br, "Add files", self.conv_pick_files, "ghost", height=34).grid(row=0, column=0, sticky="ew", padx=(0, 4))
            self.button(br, "Add folder", self.conv_pick_folder, "ghost", height=34,
                        image=self.ico("folder", C["text"], 14), compound="left").grid(row=0, column=1, sticky="ew", padx=(4, 0))
            self.button(br, "Remove", self.conv_remove, "flat", height=30).grid(row=1, column=0, sticky="ew", padx=(0, 4), pady=(6, 0))
            self.button(br, "Clear all", self.conv_clear, "flat", height=30).grid(row=1, column=1, sticky="ew", padx=(4, 0), pady=(6, 0))

            # ---- preview (middle)
            mid = self.card(cf)
            mid.grid(row=0, column=1, sticky="nsew")
            mid.grid_columnconfigure(0, weight=1)
            mid.grid_rowconfigure(1, weight=1)
            mh = ctk.CTkFrame(mid, fg_color="transparent")
            mh.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 8))
            self.section(mh, "Preview").pack(side="left")
            self.conv_view_seg = self.segmented(mh, ["Before", "After"], self._conv_view)
            self.conv_view_seg.configure(height=28, font=self.F(12))
            self.conv_view_seg.set("After")
            self.conv_view_seg.pack(side="right")
            cw = ctk.CTkFrame(mid, fg_color=C["discord"], corner_radius=12)
            cw.grid(row=1, column=0, sticky="nsew", padx=18)
            cw.grid_columnconfigure(0, weight=1)
            cw.grid_rowconfigure(0, weight=1)
            self.conv_canvas = tk.Canvas(cw, bg=C["discord"], highlightthickness=0, bd=0)
            self.conv_canvas.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
            self.conv_canvas.bind("<Configure>", lambda _e: self._conv_draw())
            info = ctk.CTkFrame(mid, fg_color="transparent")
            info.grid(row=2, column=0, sticky="ew", padx=18, pady=14)
            info.grid_columnconfigure((0, 1), weight=1)
            b1 = ctk.CTkFrame(info, fg_color=C["field"], corner_radius=10)
            b1.grid(row=0, column=0, sticky="ew", padx=(0, 6))
            self.label(b1, "BEFORE", 10, "faint", bold=True).pack(anchor="w", padx=12, pady=(8, 0))
            self.conv_before = self.label(b1, "-", 12, anchor="w", justify="left", wraplength=250)
            self.conv_before.pack(anchor="w", padx=12, pady=(0, 8))
            b2 = ctk.CTkFrame(info, fg_color=C["field"], corner_radius=10)
            b2.grid(row=0, column=1, sticky="ew", padx=(6, 0))
            self.label(b2, "AFTER", 10, "faint", bold=True).pack(anchor="w", padx=12, pady=(8, 0))
            self.conv_after = self.label(b2, "-", 12, anchor="w", justify="left", wraplength=250)
            self.conv_after.pack(anchor="w", padx=12, pady=(0, 8))

            # ---- options (right)
            opt = self.card(cf, width=330)
            opt.grid(row=0, column=2, sticky="ns", padx=(14, 0))
            opt.grid_propagate(False)
            opt.grid_columnconfigure(0, weight=1)
            pad = dict(padx=18, sticky="ew")
            cfg = self.cfg.setdefault("conv", {})
            self.section(opt, "Make it a").grid(row=0, column=0, pady=(16, 8), **pad)
            self.cv_preset = self.segmented(opt, list(PRESETS), lambda v: self._conv_preset(v))
            self.cv_preset.grid(row=1, column=0, **pad)
            sz = ctk.CTkFrame(opt, fg_color="transparent")
            sz.grid(row=2, column=0, pady=(10, 0), **pad)

            def num_entry(parent, val, width=62):
                v = tk.StringVar(value=str(val))
                e = ctk.CTkEntry(parent, textvariable=v, width=width, height=32, corner_radius=8, border_width=1,
                                 border_color=C["border"], fg_color=C["field"], text_color=C["text"], font=self.F(13),
                                 justify="center")
                v.trace_add("write", lambda *_: self._conv_changed(custom=True))
                return v, e
            self.label(sz, "Size", 12, "muted").pack(side="left", padx=(0, 8))
            self.cv_w, ew = num_entry(sz, cfg.get("w", 128))
            ew.pack(side="left")
            self.label(sz, "×", 13, "muted").pack(side="left", padx=6)
            self.cv_h, eh = num_entry(sz, cfg.get("h", 128))
            eh.pack(side="left")
            self.label(sz, "px", 12, "muted").pack(side="left", padx=(6, 0))
            lm = ctk.CTkFrame(opt, fg_color="transparent")
            lm.grid(row=3, column=0, pady=(8, 0), **pad)
            self.label(lm, "Max file size", 12, "muted").pack(side="left", padx=(0, 8))
            self.cv_limit = ctk.CTkOptionMenu(
                lm, values=["256 KB", "512 KB", "1 MB", "2 MB", "8 MB", "No limit"], width=120, height=30,
                command=lambda _v: self._conv_changed(custom=True), corner_radius=8, font=self.F(12),
                dropdown_font=self.F(12), fg_color=C["field"], button_color=C["card_hi"], button_hover_color=C["border"],
                text_color=C["text"], dropdown_fg_color=C["card_hi"], dropdown_hover_color=C["border"],
                dropdown_text_color=C["text"])
            self.cv_limit.set(cfg.get("limit", "256 KB"))
            self.cv_limit.pack(side="left")
            self.cv_preset_help = self.label(opt, "", 11, "faint", anchor="w", justify="left", wraplength=290)
            self.cv_preset_help.grid(row=4, column=0, pady=(6, 0), **pad)

            def seg_row(row, title, values, key, default):
                self.section(opt, title).grid(row=row, column=0, pady=(14, 6), **pad)
                sg = self.segmented(opt, values, lambda _v: self._conv_changed())
                sg.set(cfg.get(key, default))
                sg.grid(row=row + 1, column=0, **pad)
                return sg
            if cfg.get("fmt") not in ("Auto", "GIF", "PNG"):
                cfg["fmt"] = "Auto"
            self.cv_fmt = seg_row(5, "Format", ["Auto", "GIF", "PNG"], "fmt", "Auto")
            self.cv_fit = seg_row(7, "Fit", list(FITS), "fit", "Fill")
            self.cv_scale = seg_row(9, "Scaling", ["Smooth", "Pixel art"], "scale", "Smooth")
            self.cv_fps = seg_row(11, "Frame rate", ["Keep", "30", "20", "15", "10"], "fps", "Keep")
            self.section(opt, "Colours").grid(row=13, column=0, pady=(14, 6), **pad)
            cr = ctk.CTkFrame(opt, fg_color="transparent")
            cr.grid(row=14, column=0, **pad)
            cr.grid_columnconfigure(0, weight=1)
            self.cv_colors = ctk.CTkSlider(cr, from_=8, to=256, number_of_steps=31, height=16,
                                           progress_color=C["accent"], button_color="#ffffff",
                                           button_hover_color="#ffffff", fg_color=C["card_hi"],
                                           command=lambda v: (self.cv_colors_lbl.configure(text=str(int(v))),
                                                              self._conv_changed()))
            self.cv_colors.set(cfg.get("colors", 256))
            self.cv_colors.grid(row=0, column=0, sticky="ew")
            self.cv_colors_lbl = self.label(cr, str(int(self.cv_colors.get())), 12, bold=True, width=36)
            self.cv_colors_lbl.grid(row=0, column=1, padx=(8, 0))
            self.conv_btn = self.button(opt, "Convert", self.conv_run, "accent", height=46, corner_radius=12,
                                        font=self.F(15, True), image=self.ico("download", "#ffffff", 18), compound="left")
            self.conv_btn.grid(row=15, column=0, pady=(20, 0), **pad)
            self.conv_dest = ctk.CTkLabel(opt, text="", font=self.F(11), text_color=C["faint"], anchor="w",
                                          justify="left", wraplength=290, cursor="hand2")
            self.conv_dest.grid(row=16, column=0, pady=(6, 0), **pad)
            self.conv_dest.bind("<Button-1>", lambda _e: self._conv_open_out())
            opt.grid_rowconfigure(17, weight=1)
            self.cv_preset.set(cfg.get("preset", "Emoji"))
            self._conv_preset(self.cv_preset.get(), init=True)
            self._conv_refresh_list()

        def conv_out_dir(self):
            return self.out_dir() / "Converted"

        def _conv_open_out(self):
            p = self.conv_out_dir()
            try:
                p.mkdir(parents=True, exist_ok=True)
                open_folder(p)
            except Exception as e:  # noqa
                self.say(f"Couldn't open {p}: {e}", "err")

        def _conv_preset(self, name, init=False):
            pr = PRESETS.get(name)
            self._conv_lock = True
            if pr:
                w, h, kb = pr
                self.cv_w.set(str(w))
                self.cv_h.set(str(h))
                self.cv_limit.set(f"{kb} KB")
            self._conv_lock = False
            help_ = {"Emoji": "Discord emoji: 128×128, up to 256 KB. GIF for animated, PNG for still.",
                     "Sticker": "Discord sticker: 320×320, up to 512 KB.",
                     "Custom": "Any size and file size limit you want."}[name]
            self.cv_preset_help.configure(text=help_)
            if not init:
                self._conv_changed()

        def conv_opts(self):
            def num(v, d):
                try:
                    return max(1, min(4096, int(v.get())))
                except ValueError:
                    return d
            lim = self.cv_limit.get()
            kb = None if lim == "No limit" else (int(lim.split()[0]) * (1024 if "MB" in lim else 1))
            fps = self.cv_fps.get()
            return dict(width=num(self.cv_w, 128), height=num(self.cv_h, 128),
                        fmt={"Auto": "auto", "GIF": "gif", "PNG": "png"}.get(self.cv_fmt.get(), "auto"),
                        fit=FITS.get(self.cv_fit.get(), "auto"), pixel=self.cv_scale.get() == "Pixel art",
                        colors=int(self.cv_colors.get()), fps=None if fps == "Keep" else int(fps), limit_kb=kb)

        def _conv_changed(self, custom=False):
            if getattr(self, "_conv_lock", False) or not hasattr(self, "cv_colors_lbl"):
                return
            if custom and self.cv_preset.get() != "Custom":
                pr = PRESETS[self.cv_preset.get()]
                o = self.conv_opts()
                if (o["width"], o["height"], o["limit_kb"]) != pr:
                    self.cv_preset.set("Custom")
                    self.cv_preset_help.configure(text="Any size and file size limit you want.")
            self.cfg["conv"] = dict(preset=self.cv_preset.get(), w=self.cv_w.get(), h=self.cv_h.get(),
                                    limit=self.cv_limit.get(), fmt=self.cv_fmt.get(), fit=self.cv_fit.get(),
                                    scale=self.cv_scale.get(), fps=self.cv_fps.get(), colors=int(self.cv_colors.get()))
            save_config(self.cfg)
            for it in self.conv_items:
                it["result"] = None
                if it.get("status") in ("done", "over"):
                    it["status"] = "ready"
            self._conv_refresh_list(keep_sel=True)
            if self.conv_job:
                self.root.after_cancel(self.conv_job)
            self.conv_job = self.root.after(350, self._conv_preview)

        def conv_pick_files(self):
            ps = filedialog.askopenfilenames(title="Add images, GIFs or videos", filetypes=[
                ("Images and videos", " ".join("*" + e for e in sorted(MEDIA_EXT))), ("All files", "*.*")])
            if ps:
                self.conv_add(list(ps))

        def conv_pick_folder(self):
            d = filedialog.askdirectory(title="Add every image, GIF and video in a folder")
            if d:
                self.conv_add([d])

        def conv_add(self, paths):
            files = collect_media(paths, videos=True)
            have = {it["path"] for it in self.conv_items}
            new = [f for f in files if str(f) not in have]
            if not new:
                self.say("No new images, GIFs or videos found." if not files else "Those files are already in the list.", "warn")
                return
            for f in new:
                self.conv_items.append(dict(path=str(f), name=f.name, frames=None, result=None, status="ready",
                                            src_kb=max(1, f.stat().st_size // 1024)))
            self.switch_tab("Converter")
            self._conv_refresh_list()
            if self.conv_sel is None:
                self.conv_list.selection_set(len(self.conv_items) - len(new))
                self._conv_select()
            self.say(f"Added {len(new)} file{'s' if len(new) > 1 else ''} to the Converter.", "ok")

        def conv_remove(self):
            sel = sorted(self.conv_list.curselection(), reverse=True)
            for i in sel:
                del self.conv_items[i]
            self.conv_sel = None
            self._conv_refresh_list()
            self._conv_draw()

        def conv_clear(self):
            self.conv_items = []
            self.conv_sel = None
            self._conv_refresh_list()
            self._conv_draw()

        def _conv_line(self, it):
            st = it.get("status")
            res = it.get("result")
            tail = {"ready": "", "loading": "  ·  loading…", "working": "  ·  converting…",
                    "error": "  ·  couldn't read"}.get(st, "")
            if res is not None and st in ("done", "over", "ready"):
                tail = f"  ·  {len(res['data']) // 1024} KB {res['ext'].upper()}" + ("  (over limit)" if res["over"] else "")
                if st == "done":
                    tail += "  ·  saved"
            return f"  {it['name']}{tail}"

        def _conv_refresh_list(self, keep_sel=False):
            sel = self.conv_list.curselection()
            self.conv_list.delete(0, "end")
            for i, it in enumerate(self.conv_items):
                self.conv_list.insert("end", self._conv_line(it))
                col = {"done": C["good"], "over": C["warn"], "error": C["bad"]}.get(it.get("status"))
                if col:
                    self.conv_list.itemconfigure(i, fg=col)
            if keep_sel:
                for i in sel:
                    if i < len(self.conv_items):
                        self.conv_list.selection_set(i)
            n = len(self.conv_items)
            self.conv_count.configure(text=f"{n} file{'s' if n != 1 else ''}" if n else "")
            if n:
                self.conv_empty.place_forget()
            else:
                self.conv_empty.place(relx=0.5, rely=0.45, anchor="center")
            self.conv_btn.configure(text=f" Convert {n} file{'s' if n != 1 else ''}" if n > 1 else " Convert",
                                    state="normal" if n else "disabled")
            self.conv_dest.configure(text=f"  {self.conv_out_dir()}", image=self.ico("folder", C["faint"], 13), compound="left")

        def _conv_select(self):
            sel = self.conv_list.curselection()
            if not sel:
                return
            self.conv_sel = sel[-1]
            self._conv_preview()

        def _conv_view(self, v):
            self.conv_view = v
            self._conv_draw()

        def _conv_preview(self):
            self.conv_job = None
            if self.conv_sel is None or self.conv_sel >= len(self.conv_items):
                self._conv_draw()
                return
            it = self.conv_items[self.conv_sel]
            self.conv_gen += 1
            gen, opts = self.conv_gen, self.conv_opts()
            self.conv_after.configure(text="converting…")

            def job():
                try:
                    if it["frames"] is None:
                        it["frames"] = load_any(it["path"])
                    res = convert_media(it["frames"], **opts)
                    err = None
                except Exception as e:  # noqa
                    res, err = None, e

                def done():
                    if isinstance(err, NeedFFmpeg):
                        self.conv_after.configure(text="Video support isn't installed yet.")
                        self.ask_ffmpeg(self._conv_preview)
                        return
                    if err:
                        it["status"] = "error"
                        self.conv_after.configure(text=f"Couldn't convert: {err}")
                        self._conv_refresh_list(keep_sel=True)
                        return
                    it["result"] = res
                    if gen == self.conv_gen:
                        self._conv_refresh_list(keep_sel=True)
                        self._conv_draw()
                self.ui(done)
            threading.Thread(target=job, daemon=True).start()

        def _conv_draw(self):
            if self.conv_anim:
                self.root.after_cancel(self.conv_anim)
                self.conv_anim = None
            cv = self.conv_canvas
            cv.delete("all")
            cw, ch = max(80, cv.winfo_width()), max(80, cv.winfo_height())
            it = self.conv_items[self.conv_sel] if self.conv_sel is not None and self.conv_sel < len(self.conv_items) else None
            if it is None:
                cv.create_text(cw // 2, ch // 2 - 12, text="Nothing selected", fill=C["text"], font=(FAMILY, 15, "bold"))
                cv.create_text(cw // 2, ch // 2 + 16, text="Drop files on the window, or use Add files",
                               fill=C["muted"], font=(FAMILY, 11))
                self.conv_before.configure(text="-")
                self.conv_after.configure(text="-")
                return
            if it["frames"] is not None:
                fw, fh = it["frames"][0][0].size
                ext = Path(it["path"]).suffix.lstrip(".").upper()
                n = len(it["frames"])
                self.conv_before.configure(text=f"{fw}×{fh}  ·  {ext}  ·  {n} frame{'s' if n > 1 else ''}  ·  {it['src_kb']:,} KB")
            res = it.get("result")
            if res is not None:
                kb = len(res["data"]) // 1024
                self.conv_after.configure(
                    text=f"{res['size'][0]}×{res['size'][1]}  ·  {res['ext'].upper()}  ·  {res['frames']} frame"
                         f"{'s' if res['frames'] > 1 else ''}  ·  {kb:,} KB" + (f"  ·  {res['note']}" if res["note"] != "full quality" else ""),
                    text_color=C["warn"] if res["over"] else C["good"])
            if self.conv_view == "After" and res is not None:
                frames = load_frames(res["data"])
                pixel = self.cv_scale.get() == "Pixel art"
            elif it["frames"] is not None:
                frames, pixel = it["frames"], False
            else:
                return
            frames = frames[:300]
            w, h = frames[0][0].size
            k = min((cw - 40) / w, (ch - 40) / h)
            if self.conv_view == "After":
                k = max(1, int(k)) if k >= 1 else k   # whole-number zoom keeps the result honest
            nw, nh = max(1, int(w * k)), max(1, int(h * k))
            method = Image.Resampling.NEAREST if (pixel or (self.conv_view == "After" and k >= 2)) else LANCZOS
            checker = Image.new("RGBA", (nw, nh), (60, 62, 70, 255))
            d = ImageDraw.Draw(checker)
            t = 10
            for yy in range(0, nh, t):
                for xx in range((yy // t) % 2 * t, nw, t * 2):
                    d.rectangle((xx, yy, xx + t - 1, yy + t - 1), fill=(72, 74, 84, 255))
            imgs = []
            for f, dur in frames:
                im = checker.copy()
                im.alpha_composite(f.resize((nw, nh), method))
                imgs.append((ImageTk.PhotoImage(im), dur))
            self.conv_imgs = imgs
            x0, y0 = (cw - nw) // 2, (ch - nh) // 2
            img_id = cv.create_image(x0, y0, anchor="nw", image=imgs[0][0])
            cv.create_rectangle(x0 - 1, y0 - 1, x0 + nw, y0 + nh, outline="#4a4d57")

            def step(i=0):
                cv.itemconfigure(img_id, image=imgs[i][0])
                self.conv_anim = self.root.after(imgs[i][1], step, (i + 1) % len(imgs))
            if len(imgs) > 1:
                step()

        def conv_run(self):
            if not self.conv_items or getattr(self, "_conv_running", False):
                return
            if not ffmpeg_exe() and any(Path(it["path"]).suffix.lower() in VIDEO_EXT and it["frames"] is None
                                        for it in self.conv_items):
                self.ask_ffmpeg(self.conv_run)
                return
            opts = self.conv_opts()
            out = self.conv_out_dir()
            items = list(self.conv_items)
            self._conv_running = True
            self.conv_btn.configure(state="disabled", text=" Converting…")

            def job():
                ok = over = bad = 0
                for k, it in enumerate(items, 1):
                    self.ui(lambda k=k, it=it: (self.say(f"Converting {k}/{len(items)}: {it['name']}", "busy"),
                                                it.update(status="working"), self._conv_refresh_list(keep_sel=True)))
                    try:
                        if it["frames"] is None:
                            it["frames"] = load_any(it["path"])
                        res = it.get("result") or convert_media(it["frames"], **opts)
                        it["result"] = res
                        out.mkdir(parents=True, exist_ok=True)
                        base = Path(it["path"]).stem
                        dest = out / f"{base}.{res['ext']}"
                        n = 2
                        while dest.exists():
                            dest = out / f"{base} ({n}).{res['ext']}"
                            n += 1
                        dest.write_bytes(res["data"])
                        it["status"] = "over" if res["over"] else "done"
                        over += res["over"]
                        ok += 1
                    except Exception as e:  # noqa
                        it["status"] = "error"
                        bad += 1
                        self.log_lines.append(f"  FAILED  {it['name']}: {e}")
                return ok, over, bad

            def done(r_):
                ok, over, bad = r_
                self._conv_running = False
                self._conv_refresh_list(keep_sel=True)
                self.last_out = out
                msg = f"Converted {ok} file{'s' if ok != 1 else ''} into {out}"
                if over:
                    msg += f" · {over} still over the size limit"
                if bad:
                    msg += f" · {bad} failed (see Log)"
                self.say(msg, "warn" if over or bad else "ok")
                self.toast("Conversion finished", msg, "warn" if over or bad else "ok")

            def run():
                try:
                    r_ = job()
                finally:
                    self.ui(lambda: setattr(self, "_conv_running", False))
                self.ui(lambda: done(r_))
            threading.Thread(target=run, daemon=True).start()

        # ================= toasts =================
        def toast(self, title, msg="", kind="ok", ms=4500):
            if self.tray and self.root.state() == "withdrawn":
                try:
                    self.tray.notify(msg or title, title)
                except Exception:
                    pass
            color = {"ok": C["good"], "err": C["bad"], "warn": C["warn"]}.get(kind, C["accent"])
            t = ctk.CTkFrame(self.root, fg_color=C["card_hi"], corner_radius=12, border_width=1, border_color=C["border"],
                             width=10, height=10)
            ctk.CTkFrame(t, fg_color=color, width=4, height=10, corner_radius=2).pack(side="left", fill="y", padx=(8, 10), pady=10)
            box = ctk.CTkFrame(t, fg_color="transparent", width=10, height=10)
            box.pack(side="left", padx=(0, 16), pady=10)
            self.label(box, title, 13, bold=True, anchor="w").pack(anchor="w")
            if msg:
                self.label(box, msg, 12, "muted", anchor="w", justify="left", wraplength=320).pack(anchor="w")
            self.toasts.append(t)
            self._place_toasts()
            self.root.after(ms, lambda: self._drop_toast(t))

        def _place_toasts(self):
            y = -56
            for t in reversed(self.toasts):
                t.place(relx=1.0, rely=1.0, x=-24, y=y, anchor="se")
                t.update_idletasks()
                y -= t.winfo_height() + 10

        def _drop_toast(self, t):
            if t in self.toasts:
                self.toasts.remove(t)
                t.destroy()
                self._place_toasts()

        # ================= download queue (Browse 7TV + copied links) =================
        def enqueue_download(self, ref, on_done=None):
            s = self.settings()
            snapshot = dict(mode="auto" if self.auto else "custom", cols=s["cols"], rows=s["rows"],
                            tile=s["tile"], fit=s["fit"], out=self.out_dir(), keep=bool(self.cfg.get("keep_original")))
            self.dlq.put((ref, snapshot, on_done))

        def _dl_worker(self):
            while True:
                ref, sn, on_done = self.dlq.get()
                try:
                    raw, ext = download_emote(ref)
                    frames = load_frames(raw)
                    res = make_emote(frames, ref.name, sn["out"], sn["mode"], sn["cols"], sn["rows"],
                                     sn["tile"], sn["fit"], (raw, ext) if sn["keep"] else None)
                    err = None
                except Exception as e:  # noqa
                    res, err = None, e
                self.ui(lambda ref=ref, res=res, err=err, cb=on_done: self._dl_done(ref, res, err, cb))

        def _dl_done(self, ref, res, err, cb):
            if err:
                self.log_lines.append(f"  FAILED  {ref.name}: {err}")
                self.toast(f"Couldn't save {ref.name}", str(err), "err")
            else:
                n = len(res["files"])
                self.saved_count += 1
                self.last_out = res["folder"]
                self.log_lines.append(f"  OK      {ref.name}: {n} file(s) -> {res['folder']}")
                self.toast(f"Saved {ref.name}", (f"{n} pieces" if n > 1 else res["files"][0][0].name) +
                           f" → {res['folder']}", "ok")
            if cb:
                cb(res, err)

        # ================= clipboard =================
        def _watch_clipboard(self):
            try:
                txt = self.root.clipboard_get()
            except Exception:
                txt = ""
            if self._last_clip is None:
                self._last_clip = txt  # ignore whatever was copied before the app started
            elif txt != self._last_clip:
                self._last_clip = txt
                if self.cfg.get("watch_clipboard", True) and txt and len(txt) < 400 and "7tv" in txt.lower():
                    try:
                        kind, val = parse_input(txt)
                    except ValueError:
                        kind = None
                    if kind in ("emote", "set", "user"):
                        self.on_clip_link(txt.strip(), kind, val)
            self.root.after(900, self._watch_clipboard)

        def on_clip_link(self, txt, kind, val):
            self.clip = (txt, kind, val)
            self.clip_ref = None
            what = {"emote": "emote", "set": "emote set", "user": "user's emote set"}[kind]
            self.banner_lbl.configure(text=f"7TV {what} link copied. Looking it up…")
            self.banner_save.configure(state="disabled" if kind != "emote" else "normal",
                                       text="Save now" if kind == "emote" else "Open to save")
            self._banner_visible = True
            if getattr(self, "tab", "Emotes") == "Emotes":
                self.banner.grid(row=2, column=0, sticky="ew", padx=22, pady=(0, 12))
            if kind != "emote":
                self.banner_lbl.configure(text=f"7TV {what} link copied. Open it to pick emotes.")
                self.toast("7TV link copied", "Open it in the app to pick emotes.", "info")
                return

            def look():
                try:
                    ref = fetch_emote(val)
                    w, h = emote_size(ref)
                    thumb = None
                    try:
                        thumb = fetch_thumb(ref)
                    except Exception:
                        pass
                    self.ui(lambda: self._clip_ready(ref, w, h, thumb))
                except Exception as e:  # noqa
                    self.ui(lambda e=e: self.banner_lbl.configure(text=f"Copied 7TV link, but: {e}"))
            threading.Thread(target=look, daemon=True).start()

        def _clip_ready(self, ref, w, h, thumb):
            self.clip_ref = ref
            c, r = auto_grid(w, h) if self.auto else (self.cols_var.get(), self.rows_var.get())
            n = c * r
            self.banner_lbl.configure(text=f"Copied from 7TV:  {ref.name}   ·   {w}×{h}   →   "
                                           f"{'1 emote' if n == 1 else f'{n} pieces'}")
            if thumb is not None:
                th = thumb.copy()
                th.thumbnail((64, 28))
                self._banner_ctk = ctk.CTkImage(th, th, size=th.size)
                self.banner_img.configure(image=self._banner_ctk, text="")
            self.toast("7TV link copied", f"{ref.name}: press Save now, or use the tray menu.", "info")

        def hide_banner(self):
            self._banner_visible = False
            self.banner.grid_forget()

        def on_clip_open(self):
            txt = getattr(self, "clip", (None,))[0]
            if txt:
                self.url_entry.delete(0, "end")
                self.url_entry.insert(0, txt)
                self.hide_banner()
                self.show_window()
                self.on_load()

        def on_clip_save(self):
            ref = self.clip_ref
            if ref is None:
                return
            self.banner_save.configure(state="disabled", text="Saving…", image=self.ico("download"))

            def done(res, err):
                self.banner_save.configure(state="normal", text="Saved" if not err else "Retry",
                                           image=self.ico("check") if not err else self.ico("refresh"))
                if not err:
                    self.root.after(1500, self.hide_banner)
            self.enqueue_download(ref, done)

        def save_clipboard_link(self):
            """Tray menu: save whatever 7TV emote link is on the clipboard."""
            try:
                kind, val = parse_input(self.root.clipboard_get())
            except Exception:
                kind = None
            if kind != "emote":
                self.toast("No 7TV emote link copied", "Copy an emote link from 7tv.app first.", "warn")
                return

            def go():
                try:
                    ref = fetch_emote(val)
                    self.ui(lambda: self.enqueue_download(ref))
                except Exception as e:  # noqa
                    self.ui(lambda e=e: self.toast("Couldn't look it up", str(e), "err"))
            threading.Thread(target=go, daemon=True).start()

        # ================= Browse 7TV =================
        def open_browser(self):
            """Browse 7TV. The grid is drawn on one canvas (fast, smooth scrolling) instead of hundreds of widgets."""
            if self.browse_win is not None and self.browse_win.winfo_exists():
                self.browse_win.deiconify()
                self.browse_win.lift()
                return
            try:
                S = ctk.ScalingTracker.get_widget_scaling(self.root)
            except Exception:
                S = 1.0
            px = lambda v: int(round(v * S))  # noqa: E731
            top = ctk.CTkToplevel(self.root)
            self.browse_win = top
            top.title("Browse 7TV · Emotes Downloader")
            top.configure(fg_color=C["bg"])
            top.geometry("1060x740")
            top.minsize(760, 520)
            set_icon(top)
            top.after(150, top.lift)
            top.grid_columnconfigure(0, weight=1)
            top.grid_rowconfigure(2, weight=1)
            st = dict(query="", cat="TRENDING_DAY", page=1, total=0, loading=False, gen=0, refs=[], cols=0,
                      saved={}, thumbs={}, imgs={})

            head = ctk.CTkFrame(top, fg_color="transparent")
            head.grid(row=0, column=0, sticky="ew", padx=20, pady=(18, 8))
            head.grid_columnconfigure(1, weight=1)
            self.label(head, "Browse 7TV", 18, bold=True).grid(row=0, column=0, padx=(0, 16))
            q = ctk.CTkEntry(head, height=40, corner_radius=12, border_width=1, border_color=C["border"],
                             fg_color=C["card"], text_color=C["text"], placeholder_text_color=C["faint"],
                             font=self.F(14), placeholder_text="Search emotes, e.g. KEKW, catJAM, OdaMeeting…")
            q.grid(row=0, column=1, sticky="ew")
            self.button(head, "Search", lambda: run(reset=True, query=q.get().strip()), "accent", width=100,
                        height=40, image=self.ico("search", "#ffffff", 15), compound="left").grid(row=0, column=2, padx=(8, 0))
            cat = self.segmented(head, list(CATEGORIES), lambda v: run(reset=True, cat=CATEGORIES[v]))
            cat.set("Trending")
            cat.grid(row=0, column=3, padx=(12, 0))
            self.button(head, "7tv.app", lambda: __import__("webbrowser").open("https://7tv.app/emotes"),
                        "flat", width=96, height=40, image=self.ico("external", C["muted"], 14),
                        compound="left").grid(row=0, column=4, padx=(6, 0))
            q.bind("<Return>", lambda _e: run(reset=True, query=q.get().strip()))

            sub = self.label(top, "", 12, "muted", anchor="w")
            sub.grid(row=1, column=0, sticky="ew", padx=22, pady=(0, 6))
            wrap = ctk.CTkFrame(top, fg_color=C["card"], corner_radius=16, border_width=1, border_color=C["border"])
            wrap.grid(row=2, column=0, sticky="nsew", padx=20)
            wrap.grid_columnconfigure(0, weight=1)
            wrap.grid_rowconfigure(0, weight=1)
            cv = tk.Canvas(wrap, bg=C["card"], highlightthickness=0, bd=0, yscrollincrement=px(24))
            cv.grid(row=0, column=0, sticky="nsew", padx=(px(10), 0), pady=px(10))
            bar = ctk.CTkScrollbar(wrap, command=cv.yview, button_color=C["card_hi"], button_hover_color=C["border"])
            bar.grid(row=0, column=1, sticky="ns", padx=(0, 4), pady=8)
            cv.configure(yscrollcommand=lambda a, b_: (bar.set(a, b_), maybe_more()))

            foot = ctk.CTkFrame(top, fg_color="transparent")
            foot.grid(row=3, column=0, sticky="ew", padx=20, pady=12)
            more = self.button(foot, "Load more", lambda: run(), "ghost", width=140)
            more.pack(side="left")
            self.label(foot, "Tip: you can also copy any emote link on 7tv.app, and the app notices it.", 12,
                       "faint").pack(side="right")

            CW, CH, GAP, TH = px(160), px(186), px(12), px(78)
            F_NAME = (FAMILY, -px(13), "bold")
            F_TAG = (FAMILY, -px(11))
            F_BTN = (FAMILY, -px(12), "bold")
            ICONS = {}
            for nm_, col_ in (("download", "#ffffff"), ("check", "#ffffff"), ("refresh", "#ffffff")):
                ICONS[nm_] = ImageTk.PhotoImage(draw_icon(nm_, col_, px(13)))

            def rrect(x1, y1, x2, y2, r, **kw):
                pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2, x2 - r, y2,
                       x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
                return cv.create_polygon(pts, smooth=True, **kw)

            def sub_text():
                sub.configure(text=f"{st['total']:,} emotes  ·  Save puts it straight into {self.out_dir()} "
                                   f"with your cut settings", text_color=C["muted"])

            def layout_cols():
                w = max(cv.winfo_width(), CW)
                return max(2, (w + GAP) // (CW + GAP))

            def origin(i):
                cols = st["cols"]
                w = cv.winfo_width()
                left = max(0, (w - (cols * CW + (cols - 1) * GAP)) // 2)
                return left + (i % cols) * (CW + GAP), (i // cols) * (CH + GAP)

            def set_region():
                rows = (len(st["refs"]) + st["cols"] - 1) // max(1, st["cols"])
                cv.configure(scrollregion=(0, 0, cv.winfo_width(), max(cv.winfo_height(), rows * (CH + GAP))))

            def button(x1, y1, x2, y2, text, base, hot, cmd, tag, icon=None):
                r = rrect(x1, y1, x2, y2, px(8), fill=base, outline="", tags=(tag,))
                tx = (x1 + x2) // 2 + (px(8) if icon else 0)
                t = cv.create_text(tx, (y1 + y2) // 2, text=text, fill="white" if base != C["card"] else C["text"],
                                   font=F_BTN, tags=(tag,))
                ic = None
                if icon:
                    bb = cv.bbox(t)
                    ic = cv.create_image(bb[0] - px(5), (y1 + y2) // 2, image=ICONS[icon], anchor="e", tags=(tag,))
                cv.tag_bind(tag, "<Enter>", lambda _e: (cv.itemconfigure(r, fill=hot), cv.configure(cursor="hand2")))
                cv.tag_bind(tag, "<Leave>", lambda _e: (cv.itemconfigure(r, fill=st["base"].get(tag, base)),
                                                        cv.configure(cursor="")))
                cv.tag_bind(tag, "<ButtonRelease-1>", lambda _e: cmd())
                st.setdefault("base", {})[tag] = base
                return r, t, ic

            def draw_card(i):
                ref = st["refs"][i]
                x, y = origin(i)
                w, h = emote_size(ref)
                c, r_ = auto_grid(w, h)
                n = c * r_
                rrect(x, y, x + CW, y + CH, px(12), fill=C["card_hi"], outline="", tags=(f"card{i}",))
                rrect(x + px(8), y + px(8), x + CW - px(8), y + px(8) + TH, px(8), fill=C["discord"], outline="",
                      tags=(f"thumbbg{i}", f"openarea{i}"))
                if ref.id in st["thumbs"]:
                    place_thumb(i)
                name = ref.name if len(ref.name) <= 17 else ref.name[:16] + "…"
                cv.create_text(x + CW // 2, y + px(8) + TH + px(17), text=name, fill=C["text"], font=F_NAME,
                               tags=(f"openarea{i}",))
                tag = f"{'GIF' if ref.animated else 'PNG'} · " + ("1 emote" if n == 1 else f"{n} pieces")
                cv.create_text(x + CW // 2, y + px(8) + TH + px(36), text=tag, font=F_TAG,
                               fill=C["accent"] if n > 1 else C["faint"])
                by1, by2 = y + CH - px(38), y + CH - px(10)
                mid = x + CW // 2
                button(x + px(8), by1, mid - px(3), by2, "Open", C["card"], C["border"],
                       lambda ref=ref: open_it(ref), f"open{i}")
                state = st["saved"].get(ref.id)
                if state == "ok":
                    base, txt, icon = C["good"], "Saved", "check"
                elif state == "err":
                    base, txt, icon = C["bad"], "Retry", "refresh"
                elif state == "busy":
                    base, txt, icon = C["accent_hover"], "Saving…", None
                else:
                    base, txt, icon = C["accent"], "Save", "download"
                button(mid + px(3), by1, x + CW - px(8), by2, txt, base, C["accent_hover"] if state != "ok" else base,
                       lambda i=i, ref=ref: save_it(i, ref), f"save{i}", icon)
                cv.tag_bind(f"openarea{i}", "<Double-Button-1>", lambda _e, ref=ref: open_it(ref))

            def place_thumb(i):
                ref = st["refs"][i]
                im = st["thumbs"].get(ref.id)
                if im is None:
                    return
                x, y = origin(i)
                if ref.id not in st["imgs"]:
                    th = im.copy()
                    th.thumbnail((CW - px(28), TH - px(10)), Image.Resampling.LANCZOS)
                    st["imgs"][ref.id] = ImageTk.PhotoImage(th)
                cv.delete(f"thumb{i}")
                cv.create_image(x + CW // 2, y + px(8) + TH // 2, image=st["imgs"][ref.id], tags=(f"thumb{i}", f"openarea{i}"))

            def redraw_all():
                cv.delete("all")
                st["cols"] = layout_cols()
                if not st["refs"] and not st["loading"]:
                    cv.create_text(cv.winfo_width() // 2, px(60), text="No emotes found.", fill=C["muted"],
                                   font=(FAMILY, -px(14)))
                for i in range(len(st["refs"])):
                    draw_card(i)
                set_region()

            def redraw_card(i):
                for t in (f"card{i}", f"thumbbg{i}", f"thumb{i}", f"openarea{i}", f"open{i}", f"save{i}"):
                    cv.delete(t)
                draw_card(i)

            resize_job = {"id": None}

            def on_resize(_e=None):
                if resize_job["id"]:
                    top.after_cancel(resize_job["id"])

                def do():
                    resize_job["id"] = None
                    if layout_cols() != st["cols"]:
                        redraw_all()
                    else:
                        set_region()
                resize_job["id"] = top.after(80, do)
            cv.bind("<Configure>", on_resize)

            def wheel(e):
                if getattr(e, "num", None) == 4:
                    cv.yview_scroll(-3, "units")
                elif getattr(e, "num", None) == 5:
                    cv.yview_scroll(3, "units")
                else:
                    cv.yview_scroll(int(-e.delta / 120 * 3) or (-1 if e.delta > 0 else 1), "units")
            for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                cv.bind(seq, wheel)
                wrap.bind(seq, wheel)

            def maybe_more():
                if st["loading"] or len(st["refs"]) >= st["total"] or not st["refs"]:
                    return
                if cv.yview()[1] > 0.8:
                    top.after(50, run)

            def run(reset=False, query=None, cat=None):
                if st["loading"] and not reset:
                    return
                if reset:
                    if query is not None:
                        st["query"] = query
                        if query and st["cat"] in ("TRENDING_DAY", "NEW"):
                            st["cat"] = "TOP"
                            cat_w = {v: k for k, v in CATEGORIES.items()}
                            seg.set(cat_w["TOP"])
                    if cat is not None:
                        st["cat"] = cat
                    st.update(page=1, refs=[], gen=st["gen"] + 1)
                    cv.yview_moveto(0)
                    redraw_all()
                st["loading"] = True
                gen = st["gen"]
                more.configure(state="disabled", text="Loading…")

                def job():
                    try:
                        total, refs = search_emotes(st["query"], st["cat"], st["page"])
                        self.ui(lambda: got(gen, total, refs))
                    except Exception as e:  # noqa
                        self.ui(lambda e=e: fail(gen, e))
                threading.Thread(target=job, daemon=True).start()

            def fail(gen, e):
                if gen != st["gen"] or not top.winfo_exists():
                    return
                st["loading"] = False
                more.configure(state="normal", text="Try again")
                sub.configure(text=f"Couldn't load emotes: {e}", text_color=C["bad"])

            def got(gen, total, refs):
                if gen != st["gen"] or not top.winfo_exists():
                    return
                st["loading"] = False
                st["total"] = total
                st["page"] += 1
                start = len(st["refs"])
                st["refs"].extend(refs)
                sub_text()
                if start == 0:
                    redraw_all()
                else:
                    for i in range(start, len(st["refs"])):
                        draw_card(i)
                    set_region()
                for i in range(start, len(st["refs"])):
                    load_thumb(i, gen)
                left = len(st["refs"]) < total and refs
                more.configure(state="normal" if left else "disabled", text="Load more" if left else "That's all")

            def load_thumb(i, gen):
                ref = st["refs"][i]
                if ref.id in self.thumb_cache:
                    st["thumbs"][ref.id] = self.thumb_cache[ref.id]
                    place_thumb(i)
                    return

                def job():
                    try:
                        im = fetch_thumb(ref)
                    except Exception:
                        im = None
                    self.thumb_cache[ref.id] = im

                    def show():
                        if gen != st["gen"] or not top.winfo_exists() or im is None:
                            return
                        st["thumbs"][ref.id] = im
                        place_thumb(i)
                    self.ui(show)
                self.thumb_pool.submit(job)

            def open_it(ref):
                it = dict(name=ref.name, ref=ref, path=None, frames=None, raw=None)
                self.url_entry.delete(0, "end")
                self.url_entry.insert(0, f"https://7tv.app/emotes/{ref.id}")
                self.set_items(None, [it], append=True)
                self.show_window()

            def save_it(i, ref):
                if st["saved"].get(ref.id) in ("busy", "ok"):
                    return
                st["saved"][ref.id] = "busy"
                redraw_card(i)

                def done(res, err, i=i, ref=ref):
                    st["saved"][ref.id] = "err" if err else "ok"
                    if top.winfo_exists() and i < len(st["refs"]) and st["refs"][i] is ref:
                        redraw_card(i)
                self.enqueue_download(ref, done)

            seg = cat
            if not hasattr(self, "thumb_pool"):
                from concurrent.futures import ThreadPoolExecutor
                self.thumb_pool = ThreadPoolExecutor(max_workers=6)
            top.after(120, lambda: run(reset=True))

        # ================= tray + window =================
        def _start_tray(self):
            try:
                import pystray
                img = Image.open(resource_path("assets/tray.png"))
                M = pystray.MenuItem
                menu = pystray.Menu(
                    M("Open Emotes Downloader", lambda: self.ui(self.show_window), default=True),
                    M("Browse 7TV", lambda: self.ui(lambda: (self.show_window(), self.open_browser()))),
                    M("Save copied 7TV link", lambda: self.ui(self.save_clipboard_link)),
                    M("Open emotes folder", lambda: self.ui(lambda: self.on_open_out(True))),
                    pystray.Menu.SEPARATOR,
                    M("Check for updates", lambda: self.ui(lambda: (self.show_window(), self.check_updates(True)))),
                    M("Quit", lambda: self.ui(self.quit_app)))
                app = self

                class TrayIcon(pystray.Icon):
                    """Left click opens the app; right click opens our own tray panel instead of the plain menu."""
                    def _on_notify(self, wparam, lparam):
                        if lparam in (0x0202, 0x0203):  # WM_LBUTTONUP / WM_LBUTTONDBLCLK: show the app
                            app.ui(app.show_window)
                            return
                        if lparam == 0x0205:  # WM_RBUTTONUP: tray panel
                            try:
                                import ctypes
                                from ctypes import wintypes
                                pt = wintypes.POINT()
                                ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
                                app.ui(lambda x=pt.x, y=pt.y: app.toggle_flyout(x, y))
                                return
                            except Exception:
                                pass
                        return super()._on_notify(wparam, lparam)

                self.tray = TrayIcon("EmotesDownloader", img, f"Emotes Downloader v{__version__}", menu)
                self.tray.run_detached()
            except Exception as e:  # noqa  (no tray on this system: the app works without it)
                self.tray = None
                self.log_lines.append(f"Tray icon not available: {e!r}")


        # ================= tray flyout (same look as Plume / LiteDock) =================
        FL = dict(bg="#13151E", panel="#1A1D2A", hover="#242840", press="#2E2858", line="#262A3B",
                  edge="#2E3348", text="#E8EAF2", muted="#8A91A6", dim="#5C6378", accent="#7C5CFF",
                  accent_hi="#9479FF", accent_press="#6A4BEB", chip="#1F2333", green="#34D399",
                  amber="#FBBF24", red="#F87171", red_bg="#2C2030", tint="#1F1B3A")

        def _icon_font(self):
            if not hasattr(self, "_icfont"):
                import tkinter.font as tkfont
                fams = set(tkfont.families(self.root))
                self._icfont = next((f for f in ("Segoe Fluent Icons", "Segoe MDL2 Assets") if f in fams), None)
            return self._icfont

        def _glyph(self, name):
            # Segoe Fluent Icons / MDL2 code points, with plain fallbacks for other systems
            win = {"search": "", "paste": "", "folder": "", "settings": "",
                   "sync": "", "download": "", "open": "", "power": "", "check": ""}
            plain = {"search": "⌕", "paste": "⎘", "folder": "▤", "settings": "⚙", "sync": "↻", "download": "↓",
                     "open": "↗", "power": "⏻", "check": "✓"}
            return (win if self._icon_font() else plain)[name]

        def toggle_flyout(self, x=None, y=None):
            if self.flyout is not None and self.flyout.winfo_exists():
                self.close_flyout()
                return
            if time.time() - self._flyout_closed < 0.3:  # the click that just closed it (focus moved to the tray)
                return
            self.open_flyout(x, y)

        def close_flyout(self):
            if self.flyout is not None:
                try:
                    self.flyout.destroy()
                except Exception:
                    pass
            self.flyout = None
            self._flyout_closed = time.time()

        def _flyout_refresh(self):
            if self.flyout is not None and self.flyout.winfo_exists():
                x, y = self._flyout_anchor
                self.close_flyout()
                self._flyout_closed = 0
                self.open_flyout(x, y, animate=False)

        def _work_area(self, x, y):
            try:
                import ctypes
                from ctypes import wintypes

                class MONITORINFO(ctypes.Structure):
                    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]
                mon = ctypes.windll.user32.MonitorFromPoint(wintypes.POINT(x, y), 2)
                mi = MONITORINFO()
                mi.cbSize = ctypes.sizeof(mi)
                ctypes.windll.user32.GetMonitorInfoW(mon, ctypes.byref(mi))
                w = mi.rcWork
                return w.left, w.top, w.right, w.bottom
            except Exception:
                sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
                return 0, 0, sw, sh - 48

        def open_flyout(self, x=None, y=None, animate=True):
            L = self.FL
            if x is None:
                x, y = self.root.winfo_pointerxy()
            self._flyout_anchor = (x, y)
            try:
                scale = ctk.ScalingTracker.get_widget_scaling(self.root)
            except Exception:
                scale = 1.0
            ic = self._icon_font()
            ICF = (ic, 13) if ic else (FAMILY, 13)
            win = tk.Toplevel(self.root)
            win.withdraw()
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            win.configure(bg=L["edge"])
            self.flyout = win
            frame = ctk.CTkFrame(win, fg_color=L["bg"], corner_radius=0, border_width=0)
            frame.pack(fill="both", expand=True, padx=1, pady=1)
            body = ctk.CTkFrame(frame, fg_color="transparent", width=288)
            body.pack(fill="both", expand=True, padx=8, pady=8)

            def hoverable(f, parts, on_click, base, hot, icon_lbl=None, icon_hot=None, icon_base=None,
                          text_lbl=None, text_hot=None, text_base=None):
                def enter(_e=None):
                    f.configure(fg_color=hot)
                    if icon_lbl is not None and icon_hot:
                        icon_lbl.configure(text_color=icon_hot)
                    if text_lbl is not None and text_hot:
                        text_lbl.configure(text_color=text_hot)

                def leave(_e=None):
                    wx, wy = f.winfo_pointerxy()
                    under = f.winfo_containing(wx, wy)
                    if under is not None and str(under).startswith(str(f)):
                        return
                    f.configure(fg_color=base)
                    if icon_lbl is not None and icon_base:
                        icon_lbl.configure(text_color=icon_base)
                    if text_lbl is not None and text_base:
                        text_lbl.configure(text_color=text_base)

                def press(_e=None):
                    f.configure(fg_color=L["press"] if base != L["accent"] else L["accent_press"])

                def click(_e=None):
                    self.close_flyout()
                    on_click()
                for w in [f] + parts:
                    w.bind("<Enter>", enter)
                    w.bind("<Leave>", leave)
                    w.bind("<ButtonPress-1>", press)
                    w.bind("<ButtonRelease-1>", click)
                    try:
                        w.configure(cursor="hand2")
                    except Exception:
                        pass

            def row(icon, text, cmd, right=None, right_color=None, badge=None, tint=None, enabled=True):
                base = tint or L["bg"]
                f = ctk.CTkFrame(body, fg_color=base, corner_radius=9, height=36)
                f.pack(fill="x", pady=1)
                f.pack_propagate(False)
                il = ctk.CTkLabel(f, text=self._glyph(icon), font=ICF, width=22,
                                  text_color=L["accent_hi"] if tint else L["muted"])
                il.pack(side="left", padx=(14, 10))
                tl = ctk.CTkLabel(f, text=text, font=ctk.CTkFont(family=FAMILY, size=13),
                                  text_color=L["text"] if enabled else L["dim"], anchor="w")
                tl.pack(side="left", fill="x", expand=True)
                parts = [il, tl]
                if badge:
                    b = ctk.CTkLabel(f, text=badge, font=ctk.CTkFont(family=FAMILY, size=11, weight="bold"),
                                     text_color="white", fg_color=L["accent"], corner_radius=9, width=40, height=18)
                    b.pack(side="right", padx=12)
                    parts.append(b)
                elif right:
                    rl = ctk.CTkLabel(f, text=right, font=ctk.CTkFont(family=FAMILY, size=11),
                                      text_color=right_color or L["dim"])
                    rl.pack(side="right", padx=12)
                    parts.append(rl)
                if enabled and cmd:
                    hoverable(f, parts, cmd, base, L["hover"], il, L["accent_hi"], il.cget("text_color"))
                return f

            # header card: logo, name + version, status line
            hc = ctk.CTkFrame(body, fg_color=L["panel"], corner_radius=10, height=58)
            hc.pack(fill="x", pady=(0, 6))
            hc.pack_propagate(False)
            try:
                logo = Image.open(resource_path("assets/icon.png"))
                self._fly_logo = ctk.CTkImage(logo, logo, size=(30, 30))
                ctk.CTkLabel(hc, text="", image=self._fly_logo, width=30).pack(side="left", padx=(12, 10))
            except Exception:
                pass
            who = ctk.CTkFrame(hc, fg_color="transparent")
            who.pack(side="left", fill="x", expand=True)
            nm = ctk.CTkFrame(who, fg_color="transparent")
            nm.pack(anchor="w")
            ctk.CTkLabel(nm, text="Emotes Downloader", font=ctk.CTkFont(family=FAMILY, size=13, weight="bold"),
                         text_color=L["text"], height=18).pack(side="left")
            ctk.CTkLabel(nm, text=f"  v{__version__}", font=ctk.CTkFont(family=FAMILY, size=11),
                         text_color=L["muted"], height=18).pack(side="left")
            if self.busy:
                sub, dot = (self.status.cget("text") or "Working…"), L["amber"]
            elif self.upd_state == "available" and self.update_info:
                sub, dot = f"Update v{self.update_info['version']} is ready", L["accent_hi"]
            else:
                done = f" · {self.saved_count} saved" if self.saved_count else ""
                where = SAVE_PRESETS.get(self.cfg.get("save_preset", "app"), "App folder").replace("…", "")
                sub, dot = f"Ready · {where}{done}", L["green"]
            sl = ctk.CTkFrame(who, fg_color="transparent")
            sl.pack(anchor="w")
            ctk.CTkFrame(sl, width=7, height=7, corner_radius=4, fg_color=dot).pack(side="left", padx=(1, 6))
            ctk.CTkLabel(sl, text=sub if len(sub) <= 32 else sub[:31] + "…", font=ctk.CTkFont(family=FAMILY, size=11),
                         text_color=L["muted"], height=16).pack(side="left")

            # copied-link card
            if self.clip_ref is not None:
                cc = ctk.CTkFrame(body, fg_color=L["tint"], corner_radius=10, height=50)
                cc.pack(fill="x", pady=(0, 6))
                cc.pack_propagate(False)
                if getattr(self, "_banner_ctk", None) is not None:
                    ctk.CTkLabel(cc, text="", image=self._banner_ctk, width=40).pack(side="left", padx=(12, 8))
                tx = ctk.CTkFrame(cc, fg_color="transparent")
                tx.pack(side="left", fill="x", expand=True, padx=(0 if getattr(self, "_banner_ctk", None) else 12, 0))
                ctk.CTkLabel(tx, text="Copied from 7TV", font=ctk.CTkFont(family=FAMILY, size=10),
                             text_color=L["muted"], height=14, anchor="w").pack(anchor="w")
                ctk.CTkLabel(tx, text=self.clip_ref.name, font=ctk.CTkFont(family=FAMILY, size=13, weight="bold"),
                             text_color=L["text"], height=18, anchor="w").pack(anchor="w")
                sv = ctk.CTkFrame(cc, fg_color=L["accent"], corner_radius=8, height=30, width=74)
                sv.pack(side="right", padx=10)
                sv.pack_propagate(False)
                sl1 = ctk.CTkLabel(sv, text=" Save", font=ctk.CTkFont(family=FAMILY, size=12, weight="bold"),
                                   text_color="white", image=self.ico("download", "#ffffff", 14), compound="left")
                sl1.pack(expand=True)
                hoverable(sv, [sl1], self.on_clip_save, L["accent"], L["accent_hi"])

            row("search", "Browse 7TV", lambda: (self.show_window(), self.open_browser()))
            row("paste", "Save copied 7TV link", self.save_clipboard_link)
            row("folder", "Open emotes folder", lambda: self.on_open_out(True),
                right=SAVE_PRESETS.get(self.cfg.get("save_preset", "app"), "").replace("…", ""))
            row("settings", "Settings", lambda: (self.show_window(), self.open_settings()))
            ctk.CTkFrame(body, fg_color=L["line"], height=1, corner_radius=0).pack(fill="x", padx=6, pady=6)

            st = self.upd_state
            if st == "available" and self.update_info:
                row("download", f"Update to v{self.update_info['version']}", lambda: self.start_update(self.update_info),
                    badge="New", tint=L["tint"])
            elif st == "checking":
                row("sync", "Checking for updates…", None, enabled=False)
            elif st == "downloading":
                row("download", "Downloading the update…", None, right=f"{self.upd_progress}%",
                    right_color=L["accent_hi"], enabled=False)
            else:
                row("sync", "Check for updates", lambda: self.check_updates(manual=True),
                    right={"uptodate": "Up to date", "error": "Couldn't check"}.get(st),
                    right_color=L["green"] if st == "uptodate" else L["red"])

            # footer: Open + Exit
            ft = ctk.CTkFrame(body, fg_color="transparent", height=38)
            ft.pack(fill="x", pady=(8, 0))
            ft.pack_propagate(False)
            def pill(parent, icon, text, bold, fg, tc):
                f = ctk.CTkFrame(parent, fg_color=fg, corner_radius=9)
                inner = ctk.CTkFrame(f, fg_color="transparent")
                inner.place(relx=0.5, rely=0.5, anchor="center")
                il = ctk.CTkLabel(inner, text=self._glyph(icon), font=ICF, text_color=tc, width=16)
                il.pack(side="left", padx=(0, 7))
                tl = ctk.CTkLabel(inner, text=text, font=ctk.CTkFont(family=FAMILY, size=13, weight="bold" if bold else "normal"),
                                  text_color=tc)
                tl.pack(side="left")
                return f, [inner, il, tl], il, tl
            ex, ex_parts, ei, et = pill(ft, "power", "Exit", False, L["chip"], L["text"])
            ex.configure(width=86)
            ex.pack(side="right", fill="y")
            ex.pack_propagate(False)
            op, op_parts, _, _ = pill(ft, "open", "Open app", True, L["accent"], "white")
            op.pack(side="left", fill="both", expand=True, padx=(0, 6))
            hoverable(op, op_parts, self.show_window, L["accent"], L["accent_hi"])
            hoverable(ex, ex_parts, self.quit_app, L["chip"], L["red_bg"], ei, L["red"], L["text"], et, L["red"], L["text"])

            # place it next to the taskbar, like Windows' own flyouts
            win.update_idletasks()
            pw = int(304 * scale)
            ph = win.winfo_reqheight()
            left, top, right, bottom = self._work_area(x, y)
            gap = int(12 * scale)
            fx = max(left + gap, min(x - pw // 2, right - pw - gap))
            if y >= bottom - 4:          # taskbar at the bottom (the usual)
                fy, d = bottom - ph - gap, 1
            elif y <= top + 4:           # taskbar at the top
                fy, d = top + gap, -1
            else:                        # side taskbar / auto-hide
                fy, d = max(top + gap, min(y - ph + gap * 2, bottom - ph - gap)), 1
                fx = right - pw - gap if x > (left + right) // 2 else left + gap
            win.geometry(f"{pw}x{ph}+{fx}+{fy + (10 * d if animate else 0)}")
            win.deiconify()
            self._round_corners(win)
            win.lift()
            win.focus_force()
            win.bind("<Escape>", lambda _e: self.close_flyout())

            def lost(_e=None):
                def check():
                    if self.flyout is not win:
                        return
                    f = win.focus_get()
                    if f is None or not str(f).startswith(str(win)):
                        self.close_flyout()
                win.after(120, check)
            win.bind("<FocusOut>", lost)
            if animate:
                try:
                    win.attributes("-alpha", 0.0)
                except Exception:
                    pass

                def step(i=1, n=8):
                    if self.flyout is not win:
                        return
                    k = 1 - (1 - i / n) ** 3   # ease-out
                    win.geometry(f"+{fx}+{fy + int(round(10 * d * (1 - k)))}")
                    try:
                        win.attributes("-alpha", k)
                    except Exception:
                        pass
                    if i < n:
                        win.after(15, step, i + 1, n)
                step()

        def _round_corners(self, win):
            """Windows 11: rounded corners + a subtle border colour, like the system flyouts."""
            if not sys.platform.startswith("win"):
                return
            try:
                import ctypes
                hwnd = ctypes.windll.user32.GetParent(win.winfo_id()) or win.winfo_id()
                pref = ctypes.c_int(2)  # DWMWCP_ROUND
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), 4)
                edge = self.FL["edge"].lstrip("#")
                color = ctypes.c_uint(int(edge[4:6] + edge[2:4] + edge[0:2], 16))  # COLORREF is 0x00BBGGRR
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(color), 4)
            except Exception:
                pass

        def show_window(self):
            self.close_flyout()
            self.root.deiconify()
            self.root.lift()
            try:
                self.root.focus_force()
            except Exception:
                pass

        def on_close(self):
            if self.tray and self.cfg.get("close_to_tray", True):
                self.root.withdraw()
                if not self.cfg.get("told_tray"):
                    self.cfg["told_tray"] = True
                    save_config(self.cfg)
                    try:
                        self.tray.notify("Still running here. Right-click the icon for options, or Quit.",
                                         "Emotes Downloader")
                    except Exception:
                        pass
            else:
                self.quit_app()

        def quit_app(self):
            self.close_flyout()
            try:
                if self.tray:
                    self.tray.stop()
            except Exception:
                pass
            try:
                self.root.destroy()
            finally:
                os._exit(0)

        # ================= updates =================
        def _after_start(self):
            last = self.cfg.get("last_version")
            if last and parse_version(last) < parse_version(__version__):
                self.show_changelog(after=last)
            if self.cfg.get("last_version") != __version__:
                self.cfg["last_version"] = __version__
                save_config(self.cfg)
            if self.cfg.get("check_updates", True):
                self.root.after(1200, lambda: self.check_updates(manual=False))

        def _set_update_btn(self, text, kind="ghost", icon=None):
            styles = {"ghost": (C["card_hi"], C["border"], C["text"]),
                      "accent": (C["accent"], C["accent_hover"], "white"),
                      "ok": (C["card_hi"], C["border"], C["good"])}
            fg, hov, tc = styles[kind]
            img = self.ico(icon, tc if tc.startswith("#") else "#ffffff", 15) if icon else None
            self.update_btn.configure(text=text, fg_color=fg, hover_color=hov, text_color=tc, image=img,
                                      compound="left")

        def on_update_btn(self):
            if self.update_info:
                self.confirm_update(self.update_info)
            else:
                self.check_updates(manual=True)

        def check_updates(self, manual):
            self.upd_state = "checking"
            self._flyout_refresh()
            self._set_update_btn("Checking…")
            self.update_btn.configure(state="disabled")

            def run():
                try:
                    info, err = check_for_update(), None
                except Exception as e:  # noqa
                    info, err = None, e
                self.ui(lambda: self._update_checked(info, err, manual))
            threading.Thread(target=run, daemon=True).start()

        def _update_checked(self, info, err, manual):
            self.update_btn.configure(state="normal")
            self.upd_state = "error" if err else ("available" if info else "uptodate")
            self._flyout_refresh()
            if err:
                self._set_update_btn("Check for updates")
                if manual:
                    self.say(f"Update check failed: {err}", "err")
                return
            if not info:
                self._set_update_btn(" Up to date", "ok", "check")
                if manual:
                    self.say(f"You're on the latest version (v{__version__}).", "ok")
                return
            self.update_info = info
            self._set_update_btn(f" Update to v{info['version']}", "accent", "download")
            idle = not self.busy and not self.items
            if not manual and self.cfg.get("auto_install", True) and idle:
                self.say(f"Installing update v{info['version']}…", "busy")
                self.start_update(info)
            else:
                self.confirm_update(info)

        def confirm_update(self, info):
            notes = format_changelog([(info["version"], "", info["notes"])])
            self.text_dialog("Update available", f"Update available: v{info['version']}",
                             f"You have v{__version__}. The app restarts after updating.", notes,
                             [("Later", "ghost", None), ("Update now", "accent", lambda: self.start_update(info))])

        def start_update(self, info):
            if self.busy:
                self.say("Finish the current job first, then update.", "warn")
                return
            self.update_btn.configure(state="disabled")

            self.upd_state, self.upd_progress = "downloading", 0

            def prog(p):
                def show():
                    self.upd_progress = int(p * 100)
                    self._set_update_btn(f" Downloading {self.upd_progress}%", "accent", "download")
                    self._flyout_refresh()
                self.ui(show)

            def done(_):
                self.say("Update downloaded. Restarting…", "ok")
                self.root.after(700, self.quit_app)

            def job():
                try:
                    apply_update(info, prog)
                except Exception:
                    def back():
                        self.upd_state = "available"
                        self.update_btn.configure(state="normal")
                        self._set_update_btn(f" Update to v{info['version']}", "accent", "download")
                        self._flyout_refresh()
                    self.ui(back)
                    raise
            self.work(job, done, f"Downloading v{info['version']} from GitHub…")

    try:
        from tkinterdnd2 import TkinterDnD, DND_FILES, DND_TEXT

        class Root(ctk.CTk, TkinterDnD.DnDWrapper):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                self.TkdndVersion = TkinterDnD._require(self)
        root = Root()
        root._dnd = (DND_FILES, DND_TEXT)
    except Exception:
        root = ctk.CTk()
        root._dnd = None
    app = App(root)
    if start_hidden:
        root.withdraw()
        root.after(4000, lambda: None if app.tray else app.show_window())  # no tray? show the window after all
    root.mainloop()


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

def run_cli(argv):
    ap = argparse.ArgumentParser(description="Download 7TV emotes and make them Discord-ready.")
    ap.add_argument("source", help="7TV emote / emote-set / user link, emote ID, or a local image")
    ap.add_argument("--split", default="auto",
                    help="none | auto | COLSxROWS (e.g. 3x1 or 2x2). Default: auto")
    ap.add_argument("--fit", default="auto", choices=["auto", "stretch", "contain", "cover"],
                    help="auto (default) fills each piece; contain adds bars; cover crops")
    ap.add_argument("--size", type=int, default=128, help="piece size in px (default 128)")
    ap.add_argument("--out", default=default_out_dir(), help="output folder")
    ap.add_argument("--name", help="emote name (single emote only)")
    ap.add_argument("--keep-original", action="store_true")
    ap.add_argument("--version", action="version", version=f"Emotes Downloader {__version__}")
    a = ap.parse_args(argv)

    mode, cols, rows = a.split, 1, 1
    m = re.fullmatch(r"(\d+)x(\d+)", a.split)
    if m:
        mode, cols, rows = "custom", int(m.group(1)), int(m.group(2))
    elif a.split not in ("none", "auto"):
        ap.error("--split must be none, auto, or COLSxROWS")

    kind, val = parse_input(a.source)
    jobs = []
    out = Path(a.out)
    if kind == "file":
        raw = Path(val).read_bytes()
        jobs.append((a.name or Path(val).stem, lambda raw=raw, ext=Path(val).suffix.lstrip("."): (raw, ext)))
    else:
        if kind == "emote":
            refs = [fetch_emote(val)]
        else:
            title, refs = fetch_set(val) if kind == "set" else fetch_user_set(val)
            out = out / folder_name(title)
            print(f"{title}: {len(refs)} emotes")
        for r in refs:
            jobs.append(((a.name if len(refs) == 1 and a.name else r.name), lambda r=r: download_emote(r)))

    for name, get in jobs:
        try:
            raw, ext = get()
            res = make_emote(load_frames(raw), name, out, mode, cols, rows, a.size, a.fit,
                             (raw, ext) if a.keep_original else None)
            sizes = ", ".join(f"{p.name} {s // 1024}KB" for p, s in res["files"])
            print(f"OK  {name}: {sizes}  [{res['note']}]")
        except Exception as e:  # noqa
            print(f"ERR {name}: {e}")
    print(f"Saved to {out}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--update":
        info = check_for_update()
        if not info:
            print(f"Already up to date (v{__version__}).")
        else:
            print(f"Updating to v{info['version']}...")
            apply_update(info)
            print("Done.")
            os._exit(0)
    elif len(sys.argv) > 1 and sys.argv[1] == "--tray":
        run_gui(start_hidden=True)
    elif len(sys.argv) > 1:
        run_cli(sys.argv[1:])
    else:
        run_gui()
