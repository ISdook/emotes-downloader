#!/usr/bin/env python3
"""
Emotes Downloader - 7TV -> Discord

* Downloads emotes from 7TV (animated or static) by link or ID,
  or whole emote sets / a user's active set.
* Resizes them to Discord's emoji format (128px, under 256 KB).
* Optionally cuts wide/big emotes into pieces (KEKW1, KEKW2, ...) and
  writes the text to paste in Discord (":KEKW1::KEKW2:").

Run with no arguments for the window, or use the command line:
    python emotes_downloader.py https://7tv.app/emotes/<id> --split auto
"""

import argparse
import io
import json
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
    from PIL import Image, ImageSequence, ImageStat
except ImportError:  # pragma: no cover
    print("Pillow is missing. Install it with:  python -m pip install pillow")
    sys.exit(1)

__version__ = "1.0.0"
GITHUB_REPO = "ISdook/emotes-downloader"
EXE_ASSET = "emotes-downloader.exe"
SCRIPT_ASSET = "emotes_downloader.py"
RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{GITHUB_REPO}/releases/latest"

APP_NAME = f"Emotes Downloader v{__version__}  ·  7TV → Discord"
API = "https://7tv.io/v3"
UA = f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) emotes-downloader/{__version__}"
DISCORD_LIMIT = 256 * 1024          # Discord emoji upload limit
SAFE_LIMIT = DISCORD_LIMIT - 2048    # small safety margin
MAX_GRID = 10
CONFIG_PATH = Path.home() / ".emotes_downloader.json"

LANCZOS = Image.Resampling.LANCZOS

# Quality tiers tried in order until every piece fits under the limit:
# (palette colours, keep every Nth frame, scale of tile size)
GIF_TIERS = [
    (256, 1, 1.0), (192, 1, 1.0), (128, 1, 1.0),
    (256, 1, 0.75), (128, 1, 0.75), (96, 1, 0.75),
    (128, 2, 0.75), (64, 2, 0.75), (64, 2, 0.5),
    (48, 3, 0.5), (32, 4, 0.5), (32, 6, 0.375),
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
    def __init__(self, eid, name, animated, host):
        self.id = eid
        self.name = name
        self.animated = bool(animated)
        self.host = host or {}


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


def plan_grid(w, h, mode, cols=1, rows=1):
    if mode == "none":
        return 1, 1
    if mode == "auto":
        r = w / h
        if r >= 1:
            return min(MAX_GRID, max(1, round(r))), 1
        return 1, min(MAX_GRID, max(1, round(1 / r)))
    return max(1, min(MAX_GRID, int(cols))), max(1, min(MAX_GRID, int(rows)))


def fit_frame(img, W, H, fit):
    w, h = img.size
    if fit == "stretch":
        return img.resize((W, H), LANCZOS)
    sc = min(W / w, H / h) if fit == "contain" else max(W / w, H / h)
    nw, nh = max(1, round(w * sc)), max(1, round(h * sc))
    r = img.resize((nw, nh), LANCZOS) if (nw, nh) != (w, h) else img
    if fit == "cover":
        x, y = (nw - W) // 2, (nh - H) // 2
        return r.crop((x, y, x + W, y + H))
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    canvas.paste(r, ((W - nw) // 2, (H - nh) // 2))
    return canvas


def compose(frames, cols, rows, tile, fit):
    W, H = cols * tile, rows * tile
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


def encode_gif(frames, colors=256, step=1, scale=1.0):
    fr = _merge_frames(frames, step)
    if scale < 1.0:
        w, h = fr[0][0].size
        size = (max(1, round(w * scale)), max(1, round(h * scale)))
        fr = [(f.resize(size, LANCZOS), d) for f, d in fr]
    preps = [_prep(f) for f, _ in fr]
    any_t = any(p[2] for p in preps)
    ncol = min(colors, 255) if any_t else min(colors, 256)

    # One shared palette for all frames (no flicker, smaller file).
    sample = preps if len(preps) <= 24 else [preps[round(i * (len(preps) - 1) / 23)] for i in range(24)]
    w, h = sample[0][0].size
    montage = Image.new("RGB", (w * len(sample), h))
    for i, (rgb, _, _) in enumerate(sample):
        montage.paste(rgb, (i * w, 0))
    pal_img = montage.quantize(colors=ncol, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    pal = pal_img.getpalette()[: ncol * 3]
    n = len(pal) // 3
    pal_img.putpalette(pal)
    tindex = n  # first free slot after the real colours

    out = []
    for rgb, tmask, _ in preps:
        q = rgb.quantize(palette=pal_img, dither=Image.Dither.FLOYDSTEINBERG)
        if any_t:
            q.putpalette(pal + [0, 0, 0] * (256 - n))
            q.paste(tindex, mask=tmask)
            q.info["transparency"] = tindex
        out.append(q)

    buf = io.BytesIO()
    kw = dict(save_all=True, append_images=out[1:], duration=[d for _, d in fr],
              loop=0, disposal=2, optimize=False)
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
                return data, "png", _note(quant=quant, scale=scale)
        return data, "png", "WARNING: still over 256 KB"
    data = None
    for colors, step, scale in GIF_TIERS:
        if step > 1 and len(tiles[0]) < step * 2:
            continue
        data = [encode_gif(t, colors, step, scale) for t in tiles]
        if max(map(len, data)) <= SAFE_LIMIT:
            return data, "gif", _note(colors=colors, step=step, scale=scale)
    return data, "gif", "WARNING: still over 256 KB - try fewer pieces or a smaller tile size"


def _note(colors=256, step=1, scale=1.0, quant=False):
    bits = []
    if colors < 256:
        bits.append(f"{colors} colours")
    if quant:
        bits.append("256 colours")
    if step > 1:
        bits.append(f"1 of every {step} frames")
    if scale < 1.0:
        bits.append(f"{int(scale * 100)}% size")
    return ("shrunk to fit 256 KB: " + ", ".join(bits)) if bits else "full quality"


def make_emote(frames, name, out_dir, mode="none", cols=1, rows=1, tile=128,
               fit="contain", original=None):
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
    if count > 1:
        (folder / f"{base}_paste.txt").write_text(paste + "\n", encoding="utf-8")
    if original:
        raw, raw_ext = original
        (folder / f"{base}_original.{raw_ext}").write_bytes(raw)
    return dict(files=files, paste=paste, note=note, folder=folder, grid=(cols, rows))


# --------------------------------------------------------------------------
# Auto-updater (GitHub Releases)
# --------------------------------------------------------------------------

def parse_version(v):
    nums = [int(x) for x in re.findall(r"\d+", v or "")[:3]]
    return tuple(nums + [0] * (3 - len(nums)))


def is_frozen():
    return bool(getattr(sys, "frozen", False))


def check_for_update():
    """Return info about a newer release, or None if up to date / no releases yet."""
    try:
        d = json.loads(http_get(RELEASES_API, timeout=15, site="GitHub",
                                accept="application/vnd.github+json").decode("utf-8"))
    except NotFound:
        return None  # repo has no releases yet
    tag = d.get("tag_name") or ""
    if parse_version(tag) <= parse_version(__version__):
        return None
    return dict(version=tag.lstrip("vV"), tag=tag, notes=(d.get("body") or "").strip(),
                page=d.get("html_url") or RELEASES_PAGE,
                assets={a["name"]: a["browser_download_url"] for a in d.get("assets") or []})


def _relaunch(cmd):
    flags = 0
    if sys.platform.startswith("win"):
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(cmd, creationflags=flags, close_fds=True)


def apply_update(info):
    """Download and install the update. The caller must exit the app right after."""
    if is_frozen():
        url = info["assets"].get(EXE_ASSET) or next(
            (u for n, u in info["assets"].items() if n.lower().endswith(".exe")), None)
        if not url:
            raise RuntimeError("This release has no .exe yet (it may still be building). Try again in a few minutes.")
        exe = Path(sys.executable).resolve()
        data = http_get(url, timeout=120, site="GitHub")
        if len(data) < 1_000_000 or data[:2] != b"MZ":
            raise RuntimeError("The downloaded .exe looks broken, update cancelled.")
        new = exe.with_name(exe.stem + ".new.exe")
        new.write_bytes(data)
        bat = Path(tempfile.gettempdir()) / f"emotes_downloader_update_{os.getpid()}.bat"
        bat.write_text(
            "@echo off\r\n"
            "set n=0\r\n"
            ":wait\r\n"
            f'tasklist /FI "PID eq {os.getpid()}" 2>nul | find "{os.getpid()}" >nul && '
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
        flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
        subprocess.Popen(["cmd", "/c", str(bat)], creationflags=flags, close_fds=True)
        return

    script = Path(__file__).resolve()
    url = info["assets"].get(SCRIPT_ASSET) or \
        f"https://raw.githubusercontent.com/{GITHUB_REPO}/{info['tag']}/emotes_downloader.py"
    code = http_get(url, timeout=60, site="GitHub")
    try:
        compile(code, str(script), "exec")
    except SyntaxError as e:
        raise RuntimeError(f"The downloaded update is broken ({e}), update cancelled.") from None
    if b"__version__" not in code:
        raise RuntimeError("The downloaded file isn't this app, update cancelled.")
    script.with_suffix(".py.bak").write_bytes(script.read_bytes())
    tmp = script.with_suffix(".py.new")
    tmp.write_bytes(code)
    os.replace(tmp, script)
    py = Path(sys.executable)
    pyw = py.with_name("pythonw.exe")
    _relaunch([str(pyw if pyw.exists() else py), str(script)])


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

def default_out_dir():
    dl = Path.home() / "Downloads"
    return str((dl if dl.is_dir() else Path.home()) / "7TV Emotes")


def load_config():
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(cfg):
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception:
        pass


def open_folder(path):
    path = str(path)
    if sys.platform.startswith("win"):
        os.startfile(path)  # noqa
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

BG, PANEL, FIELD = "#1e1f22", "#2b2d31", "#383a40"
FG, MUTED, ACCENT, ACCENT_HI = "#dbdee1", "#949ba4", "#5865f2", "#4752c4"
GOOD, BAD = "#23a55a", "#f23f43"
FIT_LABELS = {"Fit inside (pad)": "contain", "Fill (crop edges)": "cover", "Stretch": "stretch"}


def run_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    from PIL import ImageTk

    if sys.platform.startswith("win"):
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    class App:
        PW, PH = 440, 250  # preview area

        def __init__(self, root):
            self.root = root
            self.cfg = load_config()
            self.q = queue.Queue()
            self.items = []          # dicts: name, ref, path, frames, raw
            self.current = None
            self.anim_job = None
            self.anim_imgs = []
            self.busy = False
            self._build()
            self._poll()
            if self.cfg.get("auto_update", True):
                self.root.after(1500, lambda: self.check_updates(False))

        # ---------- layout ----------
        def _build(self):
            r = self.root
            r.title(APP_NAME)
            r.configure(bg=BG)
            r.minsize(1000, 760)
            st = ttk.Style(r)
            st.theme_use("clam")
            st.configure(".", background=BG, foreground=FG, fieldbackground=FIELD,
                         bordercolor=PANEL, lightcolor=PANEL, darkcolor=PANEL,
                         troughcolor=PANEL, font=("Segoe UI", 10))
            st.configure("TFrame", background=BG)
            st.configure("Panel.TFrame", background=PANEL)
            st.configure("TLabel", background=BG, foreground=FG)
            st.configure("Panel.TLabel", background=PANEL, foreground=FG)
            st.configure("Muted.TLabel", background=PANEL, foreground=MUTED, font=("Segoe UI", 9))
            st.configure("Head.TLabel", background=BG, foreground=FG, font=("Segoe UI Semibold", 15))
            st.configure("Sub.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 9))
            st.configure("Sec.TLabel", background=PANEL, foreground=MUTED, font=("Segoe UI Semibold", 9))
            st.configure("TButton", background=FIELD, foreground=FG, borderwidth=0, padding=(12, 6))
            st.map("TButton", background=[("disabled", PANEL), ("active", "#4e5058")],
                   foreground=[("disabled", MUTED)])
            st.configure("Accent.TButton", background=ACCENT, foreground="white",
                         font=("Segoe UI Semibold", 10), padding=(14, 8))
            st.map("Accent.TButton", background=[("disabled", PANEL), ("active", ACCENT_HI)])
            st.configure("TEntry", fieldbackground=FIELD, foreground=FG, insertcolor=FG,
                         borderwidth=0, padding=6)
            st.configure("TSpinbox", fieldbackground=FIELD, foreground=FG, arrowcolor=FG,
                         background=FIELD, borderwidth=0, padding=4, insertcolor=FG)
            st.configure("TCombobox", fieldbackground=FIELD, foreground=FG, arrowcolor=FG,
                         background=FIELD, borderwidth=0, padding=4)
            st.map("TCombobox", fieldbackground=[("readonly", FIELD)], foreground=[("readonly", FG)])
            r.option_add("*TCombobox*Listbox.background", FIELD)
            r.option_add("*TCombobox*Listbox.foreground", FG)
            st.configure("TRadiobutton", background=PANEL, foreground=FG, indicatorbackground=FIELD)
            st.map("TRadiobutton", background=[("active", PANEL)], indicatorbackground=[("selected", ACCENT)])
            st.configure("TCheckbutton", background=PANEL, foreground=FG, indicatorbackground=FIELD)
            st.map("TCheckbutton", background=[("active", PANEL)], indicatorbackground=[("selected", ACCENT)])
            st.configure("Vertical.TScrollbar", background=FIELD, troughcolor=FIELD, bordercolor=FIELD,
                         arrowcolor=MUTED, gripcount=0)
            st.map("Vertical.TScrollbar", background=[("active", "#4e5058")])

            outer = ttk.Frame(r, padding=14)
            outer.pack(fill="both", expand=True)

            head = ttk.Frame(outer)
            head.grid(row=0, column=0, sticky="ew")
            head.columnconfigure(1, weight=1)
            ttk.Label(head, text="Emotes Downloader", style="Head.TLabel").grid(row=0, column=0, sticky="w")
            ttk.Label(head, text=f"  v{__version__}", style="Sub.TLabel").grid(row=0, column=1, sticky="sw", pady=(0, 3))
            self.update_banner = ttk.Frame(head)
            self.update_banner.grid(row=0, column=2, sticky="e", padx=(0, 8))
            self.update_msg = ttk.Label(self.update_banner, text="", foreground=GOOD)
            self.update_msg.pack(side="left", padx=(0, 8))
            self.update_btn = ttk.Button(self.update_banner, text="Update now", style="Accent.TButton",
                                         command=self.on_update_now)
            self.check_btn = ttk.Button(head, text="Check for updates", command=lambda: self.check_updates(True))
            self.check_btn.grid(row=0, column=3, sticky="e")
            self.update_info = None
            ttk.Label(outer, text="7TV link → Discord-ready emotes  ·  emote, emote-set or user link, an ID, or a local GIF/PNG/WebP",
                      style="Sub.TLabel").grid(row=1, column=0, sticky="w", pady=(0, 10))

            # input row
            top = ttk.Frame(outer)
            top.grid(row=2, column=0, sticky="ew")
            top.columnconfigure(0, weight=1)
            self.url_var = tk.StringVar()
            e = ttk.Entry(top, textvariable=self.url_var)
            e.grid(row=0, column=0, sticky="ew", ipady=2)
            e.bind("<Return>", lambda _e: self.on_load())
            e.focus_set()
            self.load_btn = ttk.Button(top, text="Load", style="Accent.TButton", command=self.on_load)
            self.load_btn.grid(row=0, column=1, padx=(8, 0))
            ttk.Button(top, text="Open file…", command=self.on_open_file).grid(row=0, column=2, padx=(8, 0))

            # main row
            main = ttk.Frame(outer)
            main.grid(row=3, column=0, sticky="nsew", pady=12)
            outer.rowconfigure(3, weight=1)
            outer.columnconfigure(0, weight=1)
            main.columnconfigure(1, weight=1)
            main.rowconfigure(0, weight=1)

            # left: list
            left = ttk.Frame(main, style="Panel.TFrame", padding=10)
            left.grid(row=0, column=0, sticky="nsw")
            left.rowconfigure(1, weight=1)
            self.list_title = ttk.Label(left, text="EMOTES", style="Sec.TLabel")
            self.list_title.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
            self.listbox = tk.Listbox(left, width=24, bg=FIELD, fg=FG, selectbackground=ACCENT,
                                      selectforeground="white", highlightthickness=0, borderwidth=0,
                                      activestyle="none", exportselection=False, selectmode="extended",
                                      font=("Segoe UI", 10))
            self.listbox.grid(row=1, column=0, sticky="ns")
            sb = ttk.Scrollbar(left, orient="vertical", command=self.listbox.yview)
            sb.grid(row=1, column=1, sticky="ns")
            self.listbox.config(yscrollcommand=sb.set)
            self.listbox.bind("<<ListboxSelect>>", self.on_select)
            ttk.Label(left, text="Ctrl/Shift-click to pick several", style="Muted.TLabel").grid(
                row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))

            # middle: preview
            mid = ttk.Frame(main, style="Panel.TFrame", padding=10)
            mid.grid(row=0, column=1, sticky="nsew", padx=12)
            mid.columnconfigure(0, weight=1)
            ttk.Label(mid, text="PREVIEW  (dashed lines = where it gets cut)", style="Sec.TLabel").grid(
                row=0, column=0, sticky="w", pady=(0, 6))
            self.canvas = tk.Canvas(mid, width=self.PW, height=self.PH, bg="#111214",
                                    highlightthickness=0)
            self.canvas.grid(row=1, column=0, sticky="nsew")
            mid.rowconfigure(1, weight=1)
            self.canvas.bind("<Configure>", lambda _e: self.refresh_preview())
            self.info_var = tk.StringVar(value="Paste a 7TV link above and press Load.")
            ttk.Label(mid, textvariable=self.info_var, style="Muted.TLabel", wraplength=440).grid(
                row=2, column=0, sticky="w", pady=(6, 0))

            ttk.Label(mid, text="PASTE THIS IN DISCORD", style="Sec.TLabel").grid(row=3, column=0, sticky="w", pady=(10, 4))
            prow = ttk.Frame(mid, style="Panel.TFrame")
            prow.grid(row=4, column=0, sticky="ew")
            prow.columnconfigure(0, weight=1)
            self.paste_txt = tk.Text(prow, height=3, bg=FIELD, fg=FG, insertbackground=FG,
                                     borderwidth=0, highlightthickness=0, font=("Consolas", 10), wrap="none")
            self.paste_txt.grid(row=0, column=0, sticky="ew")
            ttk.Button(prow, text="Copy", command=self.on_copy).grid(row=0, column=1, padx=(8, 0), sticky="n")

            # right: options
            right = ttk.Frame(main, style="Panel.TFrame", padding=12)
            right.grid(row=0, column=2, sticky="ns")
            rr = 0

            def sec(text):
                nonlocal rr
                ttk.Label(right, text=text, style="Sec.TLabel").grid(row=rr, column=0, columnspan=4,
                                                                     sticky="w", pady=(10 if rr else 0, 4))
                rr += 1

            sec("EMOTE NAME")
            self.name_var = tk.StringVar()
            ttk.Entry(right, textvariable=self.name_var, width=26).grid(row=rr, column=0, columnspan=4, sticky="ew")
            rr += 1
            self.name_var.trace_add("write", self.on_name_change)

            sec("CUT INTO PIECES")
            self.split_var = tk.StringVar(value=self.cfg.get("split", "auto"))
            for val, lbl in (("none", "Don't cut (one emote)"),
                             ("auto", "Auto - cut wide emotes"),
                             ("custom", "Custom grid")):
                ttk.Radiobutton(right, text=lbl, value=val, variable=self.split_var,
                                command=self.refresh_preview).grid(row=rr, column=0, columnspan=4, sticky="w")
                rr += 1
            g = ttk.Frame(right, style="Panel.TFrame")
            g.grid(row=rr, column=0, columnspan=4, sticky="w", padx=(22, 0), pady=(2, 0))
            rr += 1
            self.cols_var = tk.StringVar(value=str(self.cfg.get("cols", 2)))
            self.rows_var = tk.StringVar(value=str(self.cfg.get("rows", 2)))
            ttk.Spinbox(g, from_=1, to=MAX_GRID, width=4, textvariable=self.cols_var,
                        command=self._custom_touched).pack(side="left")
            ttk.Label(g, text=" wide  ×", style="Panel.TLabel").pack(side="left")
            ttk.Spinbox(g, from_=1, to=MAX_GRID, width=4, textvariable=self.rows_var,
                        command=self._custom_touched).pack(side="left", padx=(6, 0))
            ttk.Label(g, text=" tall", style="Panel.TLabel").pack(side="left")
            for v in (self.cols_var, self.rows_var):
                v.trace_add("write", lambda *_: self.refresh_preview())

            sec("FIT")
            inv = {v: k for k, v in FIT_LABELS.items()}
            self.fit_var = tk.StringVar(value=inv.get(self.cfg.get("fit", "contain"), "Fit inside (pad)"))
            cb = ttk.Combobox(right, textvariable=self.fit_var, values=list(FIT_LABELS), state="readonly", width=24)
            cb.grid(row=rr, column=0, columnspan=4, sticky="ew")
            cb.bind("<<ComboboxSelected>>", lambda _e: self.refresh_preview())
            rr += 1

            sec("PIECE SIZE (px)")
            self.tile_var = tk.StringVar(value=str(self.cfg.get("tile", 128)))
            ttk.Combobox(right, textvariable=self.tile_var, values=["128", "112", "96", "64"],
                         width=8).grid(row=rr, column=0, sticky="w")
            ttk.Label(right, text="128 = Discord's size", style="Muted.TLabel").grid(row=rr, column=1, columnspan=3, sticky="w", padx=6)
            rr += 1

            sec("SAVE TO")
            self.out_var = tk.StringVar(value=self.cfg.get("out_dir") or default_out_dir())
            of = ttk.Frame(right, style="Panel.TFrame")
            of.grid(row=rr, column=0, columnspan=4, sticky="ew")
            of.columnconfigure(0, weight=1)
            ttk.Entry(of, textvariable=self.out_var, width=20).grid(row=0, column=0, sticky="ew")
            ttk.Button(of, text="…", width=3, command=self.on_pick_dir).grid(row=0, column=1, padx=(6, 0))
            rr += 1
            self.orig_var = tk.BooleanVar(value=self.cfg.get("keep_original", False))
            ttk.Checkbutton(right, text="Also keep the original file", variable=self.orig_var).grid(
                row=rr, column=0, columnspan=4, sticky="w", pady=(6, 0))
            rr += 1

            self.save_btn = ttk.Button(right, text="Save for Discord", style="Accent.TButton",
                                       command=lambda: self.on_save(False))
            self.save_btn.grid(row=rr, column=0, columnspan=4, sticky="ew", pady=(16, 0))
            rr += 1
            self.save_all_btn = ttk.Button(right, text="Save ALL in list", command=lambda: self.on_save(True))
            self.save_all_btn.grid(row=rr, column=0, columnspan=4, sticky="ew", pady=(6, 0))
            rr += 1
            ttk.Button(right, text="Open output folder", command=self.on_open_out).grid(
                row=rr, column=0, columnspan=4, sticky="ew", pady=(6, 0))
            rr += 1
            self.auto_upd_var = tk.BooleanVar(value=self.cfg.get("auto_update", True))
            ttk.Checkbutton(right, text="Check for updates on start", variable=self.auto_upd_var,
                            command=self.remember).grid(row=rr, column=0, columnspan=4, sticky="w", pady=(10, 0))
            rr += 1

            # log
            self.log_txt = tk.Text(outer, height=5, bg=PANEL, fg=MUTED, borderwidth=0, highlightthickness=0,
                                   font=("Consolas", 9), state="disabled", wrap="word")
            self.log_txt.grid(row=4, column=0, sticky="ew")
            self.log_txt.tag_config("ok", foreground=GOOD)
            self.log_txt.tag_config("err", foreground=BAD)
            self.log_txt.tag_config("fg", foreground=FG)
            self._set_buttons()

        # ---------- helpers ----------
        def log(self, msg, tag=None):
            self.log_txt.config(state="normal")
            self.log_txt.insert("end", msg + "\n", tag or ())
            self.log_txt.see("end")
            self.log_txt.config(state="disabled")

        def _poll(self):
            try:
                while True:
                    fn = self.q.get_nowait()
                    fn()
            except queue.Empty:
                pass
            self.root.after(40, self._poll)

        def ui(self, fn):
            self.q.put(fn)

        def work(self, fn, done=None, label=None):
            if self.busy:
                return
            self.busy = True
            self._set_buttons()
            if label:
                self.log(label)

            def run():
                try:
                    res = fn()
                    self.ui(lambda: done(res) if done else None)
                except Exception as e:  # noqa
                    msg = str(e) or e.__class__.__name__
                    self.ui(lambda: self.log("✖ " + msg, "err"))
                finally:
                    self.ui(self._unbusy)
            threading.Thread(target=run, daemon=True).start()

        def _unbusy(self):
            self.busy = False
            self._set_buttons()

        def _set_buttons(self):
            st = "disabled" if self.busy else "normal"
            self.load_btn.config(state=st)
            self.save_btn.config(state="disabled" if self.busy or self.current is None else "normal")
            self.save_all_btn.config(state="disabled" if self.busy or len(self.items) < 2 else "normal")

        def _custom_touched(self):
            self.split_var.set("custom")
            self.refresh_preview()

        def settings(self):
            try:
                tile = max(16, min(512, int(self.tile_var.get())))
            except ValueError:
                tile = 128
            try:
                cols = int(self.cols_var.get())
                rows = int(self.rows_var.get())
            except ValueError:
                cols, rows = 1, 1
            return dict(mode=self.split_var.get(), cols=cols, rows=rows, tile=tile,
                        fit=FIT_LABELS.get(self.fit_var.get(), "contain"))

        def remember(self):
            s = self.settings()
            self.cfg.update(split=s["mode"], cols=s["cols"], rows=s["rows"], tile=s["tile"], fit=s["fit"],
                            out_dir=self.out_var.get(), keep_original=bool(self.orig_var.get()),
                            auto_update=bool(self.auto_upd_var.get()))
            save_config(self.cfg)

        # ---------- loading ----------
        def on_load(self):
            try:
                kind, val = parse_input(self.url_var.get())
            except ValueError as e:
                self.log("✖ " + str(e), "err")
                return
            if kind == "file":
                self._load_file(val)
                return

            def job():
                if kind == "emote":
                    ref = fetch_emote(val)
                    return ref.name, [ref]
                if kind == "set":
                    return fetch_set(val)
                return fetch_user_set(val)

            def done(res):
                title, refs = res
                if not refs:
                    self.log("That set is empty.", "err")
                    return
                self.set_items(title, [dict(name=r.name, ref=r, path=None, frames=None, raw=None) for r in refs])
                if len(refs) > 1:
                    self.log(f"✔ Loaded set “{title}” with {len(refs)} emotes. Click one to preview, "
                             f"or use “Save ALL in list”.", "ok")
            self.work(job, done, label="Looking up on 7TV…")

        def on_open_file(self):
            p = filedialog.askopenfilename(title="Pick an image",
                                           filetypes=[("Images", "*.gif *.png *.webp *.jpg *.jpeg *.avif"),
                                                      ("All files", "*.*")])
            if p:
                self.url_var.set(p)
                self._load_file(p)

        def _load_file(self, path):
            name = Path(path).stem
            self.set_items(name, [dict(name=name, ref=None, path=path, frames=None, raw=None)])

        def set_items(self, title, items):
            self.items = items
            self.set_title = title if len(items) > 1 else None
            self.listbox.delete(0, "end")
            for it in items:
                self.listbox.insert("end", it["name"])
            self.list_title.config(text=f"EMOTES ({len(items)})" if len(items) > 1 else "EMOTE")
            self.listbox.selection_clear(0, "end")
            self.listbox.selection_set(0)
            self.select_item(0)
            self._set_buttons()

        def ensure_loaded(self, it):
            """Runs in a worker thread."""
            if it["frames"] is None:
                if it["path"]:
                    raw = Path(it["path"]).read_bytes()
                    ext = Path(it["path"]).suffix.lstrip(".").lower() or "img"
                else:
                    raw, ext = download_emote(it["ref"])
                it["raw"] = (raw, ext)
                it["frames"] = load_frames(raw)
            return it

        def on_select(self, _e=None):
            sel = self.listbox.curselection()
            if sel:
                self.select_item(sel[-1])

        def select_item(self, idx):
            it = self.items[idx]
            self.current = None
            self._set_buttons()
            if it["frames"] is not None:
                self._show(it)
                return

            def done(item):
                self._show(item)
            self.work(lambda: self.ensure_loaded(it), done, label=f"{'Downloading' if it['ref'] else 'Opening'} {it['name']}…")

        def _show(self, it):
            self.current = it
            self._name_lock = True
            self.name_var.set(it["name"])
            self._name_lock = False
            self._set_buttons()
            self.refresh_preview()

        def on_name_change(self, *_):
            if getattr(self, "_name_lock", False) or self.current is None:
                return
            self.current["name"] = self.name_var.get()
            self.update_info()

        # ---------- preview ----------
        def refresh_preview(self):
            if self.anim_job:
                self.root.after_cancel(self.anim_job)
                self.anim_job = None
            self.canvas.delete("all")
            it = self.current
            if it is None or it["frames"] is None:
                return
            s = self.settings()
            frames = it["frames"]
            w, h = frames[0][0].size
            cols, rows = plan_grid(w, h, s["mode"], s["cols"], s["rows"])
            cw = max(50, self.canvas.winfo_width())
            ch = max(50, self.canvas.winfo_height())
            pt = max(8, min(s["tile"], (cw - 20) // cols, (ch - 20) // rows))
            preview_frames = frames[:300]
            comp = compose(preview_frames, cols, rows, pt, s["fit"])
            self.anim_imgs = [(ImageTk.PhotoImage(f), d) for f, d in comp]
            self.update_info()
            W, H = cols * pt, rows * pt
            x0, y0 = (cw - W) // 2, (ch - H) // 2
            self.canvas.create_rectangle(x0, y0, x0 + W, y0 + H, outline="#3f4147")
            self.img_id = self.canvas.create_image(x0, y0, anchor="nw", image=self.anim_imgs[0][0])
            for c in range(1, cols):
                self.canvas.create_line(x0 + c * pt, y0, x0 + c * pt, y0 + H, fill=ACCENT, dash=(4, 3))
            for r_ in range(1, rows):
                self.canvas.create_line(x0, y0 + r_ * pt, x0 + W, y0 + r_ * pt, fill=ACCENT, dash=(4, 3))

            if len(self.anim_imgs) > 1:
                self._animate(0)

        def update_info(self):
            it = self.current
            if it is None or it["frames"] is None:
                return
            s = self.settings()
            frames = it["frames"]
            w, h = frames[0][0].size
            cols, rows = plan_grid(w, h, s["mode"], s["cols"], s["rows"])
            n = cols * rows
            kind = f"animated, {len(frames)} frames" if len(frames) > 1 else "static"
            out = "1 emote" if n == 1 else f"{n} pieces ({cols} wide × {rows} tall)"
            self.info_var.set(f"{it['name']}  ·  {w}×{h}px, {kind}  →  {out} of {s['tile']}px "
                              f"({'GIF' if len(frames) > 1 else 'PNG'})")
            base = sanitize_name(it["name"], len(str(n)) if n > 1 else 0)
            names = [base] if n == 1 else [f"{base}{i}" for i in range(1, n + 1)]
            paste = "\n".join("".join(f":{names[r_ * cols + c]}:" for c in range(cols)) for r_ in range(rows))
            self.paste_txt.delete("1.0", "end")
            self.paste_txt.insert("1.0", paste)

        def _animate(self, i):
            img, d = self.anim_imgs[i]
            self.canvas.itemconfig(self.img_id, image=img)
            self.anim_job = self.root.after(d, self._animate, (i + 1) % len(self.anim_imgs))

        # ---------- saving ----------
        def on_save(self, all_items):
            if all_items:
                sel = list(self.listbox.curselection())
                targets = [self.items[i] for i in sel] if len(sel) > 1 else list(self.items)
            else:
                if self.current is None:
                    return
                targets = [self.current]
            out = Path(self.out_var.get().strip() or default_out_dir())
            if getattr(self, "set_title", None):
                out = out / folder_name(self.set_title)
            s = self.settings()
            keep = bool(self.orig_var.get())
            self.remember()

            def job():
                results = []
                for i, it in enumerate(targets, 1):
                    try:
                        self.ensure_loaded(it)
                        res = make_emote(it["frames"], it["name"], out, s["mode"], s["cols"], s["rows"],
                                         s["tile"], s["fit"], it["raw"] if keep else None)
                        results.append((it, res, None))
                        self.ui(lambda it=it, res=res, i=i: self._report(it, res, i, len(targets)))
                    except Exception as e:  # noqa
                        results.append((it, None, e))
                        self.ui(lambda it=it, e=e: self.log(f"✖ {it['name']}: {e}", "err"))
                return results

            def done(results):
                ok = sum(1 for _, r, _ in results if r)
                if len(results) > 1:
                    self.log(f"Done: {ok}/{len(results)} emotes saved to {out}", "ok")
                if len(results) == 1 and results[0][1]:
                    res = results[0][1]
                    self.paste_txt.delete("1.0", "end")
                    self.paste_txt.insert("1.0", res["paste"])
            self.work(job, done, label=f"Processing {len(targets)} emote(s)…")

        def _report(self, it, res, i, total):
            files = res["files"]
            big = max(sz for _, sz in files)
            prefix = f"[{i}/{total}] " if total > 1 else ""
            warn = "WARNING" in res["note"]
            what = files[0][0].name if len(files) == 1 else f"{len(files)} pieces in {res['folder'].name}\\"
            self.log(f"{'⚠' if warn else '✔'} {prefix}{it['name']} → {what}  "
                     f"(largest {big / 1024:.0f} KB, {res['note']})", "err" if warn else "ok")

        def on_copy(self):
            txt = self.paste_txt.get("1.0", "end").strip()
            if txt:
                self.root.clipboard_clear()
                self.root.clipboard_append(txt)
                self.log("Copied to clipboard. Upload the pieces to your server first, then paste.", "fg")

        def on_pick_dir(self):
            d = filedialog.askdirectory(initialdir=self.out_var.get() or str(Path.home()))
            if d:
                self.out_var.set(d)
                self.remember()

        def on_open_out(self):
            p = Path(self.out_var.get().strip() or default_out_dir())
            p.mkdir(parents=True, exist_ok=True)
            open_folder(p)

        # ---------- updates ----------
        def check_updates(self, manual):
            self.check_btn.config(state="disabled", text="Checking…")

            def run():
                try:
                    info, err = check_for_update(), None
                except Exception as e:  # noqa
                    info, err = None, e
                self.ui(lambda: self._update_checked(info, err, manual))
            threading.Thread(target=run, daemon=True).start()

        def _update_checked(self, info, err, manual):
            self.check_btn.config(state="normal", text="Check for updates")
            if err:
                if manual:
                    self.log(f"✖ Update check failed: {err}", "err")
                return
            if not info:
                if manual:
                    self.log(f"✔ You're on the latest version (v{__version__}).", "ok")
                return
            self.update_info = info
            self.update_msg.config(text=f"v{info['version']} is available")
            self.update_btn.pack(side="left")
            self.log(f"New version v{info['version']} is available. Click “Update now”.", "ok")
            notes = info["notes"][:600] or "(no release notes)"
            if messagebox.askyesno("Update available",
                                   f"Emotes Downloader v{info['version']} is out (you have v{__version__}).\n\n"
                                   f"What's new:\n{notes}\n\nUpdate now? The app will restart."):
                self.on_update_now()

        def on_update_now(self):
            info = self.update_info
            if not info or self.busy:
                return
            self.update_btn.config(state="disabled", text="Updating…")

            def job():
                apply_update(info)
                return True

            def done(_):
                self.log("✔ Update installed, restarting…", "ok")
                self.root.after(600, lambda: (self.root.destroy(), os._exit(0)))

            def fail_reset():
                self.update_btn.config(state="normal", text="Update now")
            self.work(job, done, label=f"Downloading v{info['version']} from GitHub…")
            self.root.after(100, lambda: self._watch_update(fail_reset))

        def _watch_update(self, reset):
            if self.busy:
                self.root.after(200, lambda: self._watch_update(reset))
            elif self.update_btn.cget("text") == "Updating…":
                reset()

    root = tk.Tk()
    App(root)
    root.geometry("1140x820")
    root.mainloop()


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

def run_cli(argv):
    ap = argparse.ArgumentParser(description="Download 7TV emotes and make them Discord-ready.")
    ap.add_argument("source", help="7TV emote / emote-set / user link, emote ID, or a local image")
    ap.add_argument("--split", default="auto",
                    help="none | auto | COLSxROWS (e.g. 3x1 or 2x2). Default: auto")
    ap.add_argument("--fit", default="contain", choices=["contain", "cover", "stretch"])
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
            if len(res["files"]) > 1:
                print("    paste: " + res["paste"].replace("\n", " / "))
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
    elif len(sys.argv) > 1:
        run_cli(sys.argv[1:])
    else:
        run_gui()
