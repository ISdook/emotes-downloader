#!/usr/bin/env python3
"""
Emotes Downloader - 7TV -> Discord

* Downloads emotes from 7TV (animated or static) by link or ID,
  or whole emote sets / a user's active set.
* Resizes them to Discord's emoji format (128px, under 256 KB).
* Optionally cuts wide/big emotes into pieces (KEKW1, KEKW2, ...) and
  writes the text to paste in Discord (":KEKW1::KEKW2:").

Needs: pillow, customtkinter  (run.bat installs them)

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

__version__ = "1.1.0"
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
    for extra in ("CHANGELOG.md", "assets/icon.png", "assets/icon.ico"):
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
    pyw = py.with_name("pythonw.exe")
    _relaunch([str(pyw if pyw.exists() else py), str(script)])


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

DEFAULTS = dict(mode="auto", cols=2, rows=2, fit="contain", tile=128, keep_original=False,
                check_updates=True, auto_install=True)


def default_out_dir():
    dl = Path.home() / "Downloads"
    return str((dl if dl.is_dir() else Path.home()) / "7TV Emotes")


def load_config():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except Exception:
        pass
    cfg.setdefault("out_dir", default_out_dir())
    return cfg


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


def paste_text(name, cols, rows):
    n = cols * rows
    base = sanitize_name(name, len(str(n)) if n > 1 else 0)
    names = [base] if n == 1 else [f"{base}{i}" for i in range(1, n + 1)]
    return "\n".join("".join(f":{names[r * cols + c]}:" for c in range(cols)) for r in range(rows))


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

C = dict(
    bg="#0d0f14", card="#151821", card_hi="#1c2030", border="#252a38", field="#0f1218",
    text="#eceef4", muted="#8b91a5", faint="#5c6275",
    accent="#7c5cff", accent_hover="#6847f5", blue="#3d8bff", blue_hover="#2f78e8",
    good="#34c77b", bad="#ff5d6e", warn="#f5b14c", discord="#313338",
)
MODES = {"None": "none", "Auto": "auto", "Grid": "custom"}
FITS = {"Pad": "contain", "Crop": "cover", "Stretch": "stretch"}
MODE_HELP = {"none": "Saved as one emote.",
             "auto": "Wide emotes are cut into square pieces.",
             "custom": "Cut into the grid you choose."}


def _import_ctk():
    try:
        import customtkinter as ctk
        return ctk
    except ImportError:
        pass
    if not is_frozen():  # Python version: install the UI library on the fly
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "customtkinter"],
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


def run_gui():
    ctk = _import_ctk()
    import tkinter as tk
    from tkinter import filedialog
    from PIL import ImageTk
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
            self.F = lambda size, bold=False: ctk.CTkFont(family=FAMILY, size=size,
                                                         weight="bold" if bold else "normal")
            self._build()
            self._poll()
            self.root.after(400, self._after_start)

        # ================= layout =================
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
            r.geometry("1180x760")
            r.minsize(1020, 680)
            set_icon(r)
            r.grid_columnconfigure(0, weight=1)
            r.grid_rowconfigure(2, weight=1)

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

            right = ctk.CTkFrame(top, fg_color="transparent")
            right.grid(row=0, column=2, rowspan=2, sticky="e")
            self.update_btn = self.button(right, "Check for updates", self.on_update_btn, "ghost",
                                          corner_radius=18, height=34)
            self.update_btn.pack(side="left", padx=(0, 8))
            self.button(right, "What's new", self.show_changelog, "flat", width=96, height=34).pack(side="left", padx=(0, 4))
            self.button(right, "⚙  Settings", self.open_settings, "flat", width=100, height=34).pack(side="left")

            # ---- input bar
            bar = ctk.CTkFrame(r, fg_color="transparent")
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
            self.button(bar, "Open file", self.on_open_file, "ghost", width=110, height=48,
                        corner_radius=12).grid(row=0, column=2, padx=(10, 0))

            # ---- body
            body = ctk.CTkFrame(r, fg_color="transparent")
            body.grid(row=2, column=0, sticky="nsew", padx=22)
            body.grid_columnconfigure(1, weight=1)
            body.grid_rowconfigure(0, weight=1)
            self.body = body

            # sidebar (only for sets)
            self.side = self.card(body, width=250)
            self.side.grid_rowconfigure(1, weight=1)
            self.side.grid_columnconfigure(0, weight=1)
            sh = ctk.CTkFrame(self.side, fg_color="transparent")
            sh.grid(row=0, column=0, sticky="ew", padx=14, pady=(14, 6))
            self.side_title = self.section(sh, "Emotes")
            self.side_title.pack(side="left")
            self.all_var = tk.BooleanVar(value=False)
            ctk.CTkCheckBox(sh, text="All", variable=self.all_var, command=self.on_check_all, width=20,
                            checkbox_width=18, checkbox_height=18, corner_radius=5, font=self.F(12),
                            fg_color=C["accent"], hover_color=C["accent_hover"], border_color=C["faint"],
                            text_color=C["muted"]).pack(side="right")
            self.list_frame = ctk.CTkScrollableFrame(self.side, fg_color="transparent", width=220,
                                                     scrollbar_button_color=C["card_hi"],
                                                     scrollbar_button_hover_color=C["border"])
            self.list_frame.grid(row=1, column=0, sticky="nsew", padx=6, pady=(0, 10))

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

            self.section(ctl, "Cut into pieces").grid(row=0, column=0, pady=(16, 8), **pad)
            self.mode_seg = self.segmented(ctl, list(MODES), lambda _v: self.on_mode())
            self.mode_seg.grid(row=1, column=0, **pad)
            self.mode_help = self.label(ctl, "", 12, "muted", anchor="w", justify="left", wraplength=270)
            self.mode_help.grid(row=2, column=0, pady=(6, 0), **pad)
            self.grid_box = ctk.CTkFrame(ctl, fg_color="transparent")
            self.cols_var = tk.IntVar(value=int(self.cfg["cols"]))
            self.rows_var = tk.IntVar(value=int(self.cfg["rows"]))
            self._stepper(self.grid_box, "Wide", self.cols_var).pack(side="left")
            self._stepper(self.grid_box, "Tall", self.rows_var).pack(side="left", padx=(14, 0))

            self.section(ctl, "Fit").grid(row=4, column=0, pady=(18, 8), **pad)
            self.fit_seg = self.segmented(ctl, list(FITS), lambda _v: self.on_option())
            self.fit_seg.grid(row=5, column=0, **pad)

            self.save_btn = self.button(ctl, "Save for Discord", lambda: self.on_save(False), "accent",
                                        height=48, corner_radius=12, font=self.F(15, True))
            self.save_btn.grid(row=6, column=0, pady=(22, 0), **pad)
            self.save_all_btn = self.button(ctl, "Save all", lambda: self.on_save(True), "ghost", height=38)

            self.section(ctl, "Paste in Discord").grid(row=8, column=0, pady=(20, 8), **pad)
            self.paste_box = ctk.CTkTextbox(ctl, height=76, corner_radius=10, border_width=1,
                                            border_color=C["border"], fg_color=C["field"],
                                            text_color=C["text"], font=(MONO, 12), wrap="char")
            self.paste_box.grid(row=9, column=0, **pad)
            brow = ctk.CTkFrame(ctl, fg_color="transparent")
            brow.grid(row=10, column=0, pady=(8, 16), **pad)
            brow.grid_columnconfigure((0, 1), weight=1)
            self.button(brow, "Copy", self.on_copy, "blue").grid(row=0, column=0, sticky="ew", padx=(0, 4))
            self.button(brow, "Open folder", self.on_open_out, "ghost").grid(row=0, column=1, sticky="ew", padx=(4, 0))
            ctl.grid_rowconfigure(11, weight=1)

            # ---- status bar
            sb = ctk.CTkFrame(r, fg_color="transparent")
            sb.grid(row=3, column=0, sticky="ew", padx=24, pady=(10, 14))
            sb.grid_columnconfigure(1, weight=1)
            self.dot = self.label(sb, "●", 12, "faint")
            self.dot.grid(row=0, column=0, padx=(0, 8))
            self.status = self.label(sb, "Ready. Paste a 7TV link and press Load.", 12, "muted", anchor="w")
            self.status.grid(row=0, column=1, sticky="ew")
            self.progress = ctk.CTkProgressBar(sb, width=140, height=6, corner_radius=3, mode="indeterminate",
                                               progress_color=C["accent"], fg_color=C["card_hi"])
            self.button(sb, "Log", self.show_log, "flat", width=54, height=26, font=self.F(12)).grid(row=0, column=3)

            # initial values
            inv_m = {v: k for k, v in MODES.items()}
            inv_f = {v: k for k, v in FITS.items()}
            self.mode_seg.set(inv_m.get(self.cfg["mode"], "Auto"))
            self.fit_seg.set(inv_f.get(self.cfg["fit"], "Pad"))
            self._show_sidebar(False)
            self.on_mode(save=False)
            self._set_buttons()
            self.refresh_preview()

        def _stepper(self, parent, text, var):
            f = ctk.CTkFrame(parent, fg_color="transparent")
            self.label(f, text, 12, "muted").pack(side="left", padx=(0, 6))

            def bump(d):
                var.set(max(1, min(MAX_GRID, var.get() + d)))
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

        def _show_sidebar(self, show):
            if show:
                self.side.grid(row=0, column=0, sticky="ns", padx=(0, 14))
            else:
                self.side.grid_forget()

        # ================= plumbing =================
        def _poll(self):
            try:
                while True:
                    self.q.get_nowait()()
            except queue.Empty:
                pass
            self.root.after(40, self._poll)

        def ui(self, fn):
            self.q.put(fn)

        def say(self, msg, kind="info"):
            colors = {"info": "muted", "ok": "good", "err": "bad", "warn": "warn", "busy": "accent"}
            self.status.configure(text=msg, text_color=C["text"] if kind in ("ok", "err", "warn") else C["muted"])
            self.dot.configure(text_color=C[colors.get(kind, "muted")])
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
            return dict(mode=MODES.get(self.mode_seg.get(), "auto"), cols=self.cols_var.get(),
                        rows=self.rows_var.get(), tile=int(self.cfg["tile"]),
                        fit=FITS.get(self.fit_seg.get(), "contain"))

        def remember(self):
            s = self.settings()
            self.cfg.update(mode=s["mode"], cols=s["cols"], rows=s["rows"], fit=s["fit"])
            save_config(self.cfg)

        # ================= options =================
        def on_mode(self, save=True):
            mode = MODES.get(self.mode_seg.get(), "auto")
            self.mode_help.configure(text=MODE_HELP[mode])
            if mode == "custom":
                self.grid_box.grid(row=3, column=0, padx=18, pady=(10, 0), sticky="w")
            else:
                self.grid_box.grid_forget()
            if save:
                self.on_option()

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
                self.set_items(title, [dict(name=x.name, ref=x, path=None, frames=None, raw=None) for x in refs])
                if len(refs) > 1:
                    self.say(f"Loaded “{title}” with {len(refs)} emotes. Pick one to preview, or Save all.", "ok")
            self.work(job, done, "Looking it up on 7TV…")

        def on_open_file(self):
            p = filedialog.askopenfilename(title="Pick an image", filetypes=[
                ("Images", "*.gif *.png *.webp *.jpg *.jpeg *.avif"), ("All files", "*.*")])
            if p:
                self.url_entry.delete(0, "end")
                self.url_entry.insert(0, p)
                self._load_file(p)

        def _load_file(self, path):
            name = Path(path).stem
            self.set_items(None, [dict(name=name, ref=None, path=path, frames=None, raw=None)])

        def set_items(self, title, items):
            self.items = items
            self.set_title = title if len(items) > 1 else None
            for w in self.list_frame.winfo_children():
                w.destroy()
            self.rows = []
            multi = len(items) > 1
            self._show_sidebar(multi)
            if multi:
                self.side_title.configure(text=f"IN THIS SET · {len(items)}")
                for i, it in enumerate(items):
                    self._add_row(i, it)
                self.save_all_btn.grid(row=7, column=0, padx=18, pady=(8, 0), sticky="ew")
                self.all_var.set(False)
                self._update_save_all()
            else:
                self.save_all_btn.grid_forget()
            self.select_item(0)

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
                    raw = Path(it["path"]).read_bytes()
                    ext = Path(it["path"]).suffix.lstrip(".").lower() or "img"
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
            if not self.work(lambda: self.ensure_loaded(it), self._show,
                             f"{'Downloading' if it['ref'] else 'Opening'} {it['name']}…"):
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
                self.update_texts()
                return
            s = self.settings()
            frames = it["frames"]
            w, h = frames[0][0].size
            cols, rows = plan_grid(w, h, s["mode"], s["cols"], s["rows"])
            pt = max(8, min(s["tile"] * 2, (cw - 40) // cols, (ch - 40) // rows))
            comp = compose(frames[:300], cols, rows, pt, s["fit"])
            self.anim_imgs = [(ImageTk.PhotoImage(f), d) for f, d in comp]
            W, H = cols * pt, rows * pt
            x0, y0 = (cw - W) // 2, (ch - H) // 2
            self.img_id = cv.create_image(x0, y0, anchor="nw", image=self.anim_imgs[0][0])
            n = cols * rows
            if n > 1:
                for c in range(1, cols):
                    cv.create_line(x0 + c * pt, y0, x0 + c * pt, y0 + H, fill=C["accent"], dash=(5, 4), width=2)
                for rr in range(1, rows):
                    cv.create_line(x0, y0 + rr * pt, x0 + W, y0 + rr * pt, fill=C["accent"], dash=(5, 4), width=2)
                for i in range(n):
                    tx, ty = x0 + (i % cols) * pt + 6, y0 + (i // cols) * pt + 6
                    cv.create_rectangle(tx, ty, tx + 20, ty + 18, fill=C["accent"], outline="")
                    cv.create_text(tx + 10, ty + 9, text=str(i + 1), fill="white", font=(FAMILY, 9, "bold"))
            cv.create_rectangle(x0 - 1, y0 - 1, x0 + W, y0 + H, outline="#4a4d57")
            fmt = "GIF" if len(frames) > 1 else "PNG"
            self.info_lbl.configure(text=f"{'1 emote' if n == 1 else f'{n} pieces · {cols}×{rows}'} · {s['tile']}px {fmt}")
            self.update_texts()
            if len(self.anim_imgs) > 1:
                self._animate(0)

        def _animate(self, i):
            img, d = self.anim_imgs[i]
            self.canvas.itemconfigure(self.img_id, image=img)
            self.anim_job = self.root.after(d, self._animate, (i + 1) % len(self.anim_imgs))

        def update_texts(self):
            if not hasattr(self, "paste_box"):
                return
            self.paste_box.configure(state="normal")
            self.paste_box.delete("1.0", "end")
            it = self.current
            if it is not None and it["frames"] is not None:
                s = self.settings()
                w, h = it["frames"][0][0].size
                cols, rows = plan_grid(w, h, s["mode"], s["cols"], s["rows"])
                self.paste_box.insert("1.0", paste_text(it["name"], cols, rows))
            self.paste_box.configure(state="disabled")

        # ================= saving =================
        def out_dir(self):
            return Path(self.cfg.get("out_dir") or default_out_dir())

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
                        self.log_lines.append(f"  ✔ {it['name']}: {len(res['files'])} file(s), {res['note']}")
                    except Exception as e:  # noqa
                        results.append((it, None))
                        self.log_lines.append(f"  ✖ {it['name']}: {e}")
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

        def on_copy(self):
            txt = self.paste_box.get("1.0", "end").strip()
            if txt:
                self.root.clipboard_clear()
                self.root.clipboard_append(txt)
                self.say("Copied! Upload the pieces to your server first, then paste.", "ok")

        def on_open_out(self):
            p = getattr(self, "last_out", None) or self.out_dir()
            p.mkdir(parents=True, exist_ok=True)
            open_folder(p)

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

        def open_settings(self):
            top = self.dialog("Settings", 520, 600)
            f = ctk.CTkFrame(top, fg_color="transparent")
            f.grid(row=0, column=0, sticky="nsew", padx=24, pady=20)
            f.grid_columnconfigure(0, weight=1)
            self.label(f, "Settings", 18, bold=True, anchor="w").grid(row=0, column=0, sticky="ew", pady=(0, 14))

            self.section(f, "Save emotes to").grid(row=1, column=0, sticky="ew", pady=(0, 6))
            dr = ctk.CTkFrame(f, fg_color="transparent")
            dr.grid(row=2, column=0, sticky="ew")
            dr.grid_columnconfigure(0, weight=1)
            dir_var = tk.StringVar(value=str(self.out_dir()))
            ctk.CTkEntry(dr, textvariable=dir_var, height=36, corner_radius=10, border_width=1,
                         border_color=C["border"], fg_color=C["field"], text_color=C["text"],
                         font=self.F(12)).grid(row=0, column=0, sticky="ew")

            def browse():
                d = filedialog.askdirectory(parent=top, initialdir=dir_var.get() or str(Path.home()))
                if d:
                    dir_var.set(d)
            self.button(dr, "Browse", browse, "ghost", width=90).grid(row=0, column=1, padx=(8, 0))

            self.section(f, "Piece size").grid(row=3, column=0, sticky="ew", pady=(18, 6))
            size_seg = self.segmented(f, ["128 px", "96 px", "64 px"], None)
            size_seg.set(f"{self.cfg['tile']} px" if self.cfg["tile"] in (128, 96, 64) else "128 px")
            size_seg.grid(row=4, column=0, sticky="w")
            self.label(f, "128 px is Discord's emoji size.", 11, "faint", anchor="w").grid(row=5, column=0, sticky="ew", pady=(4, 0))

            def switch(row, text, key):
                var = tk.BooleanVar(value=bool(self.cfg.get(key)))
                ctk.CTkSwitch(f, text=text, variable=var, onvalue=True, offvalue=False, font=self.F(13),
                              progress_color=C["accent"], button_color=C["text"], button_hover_color="white",
                              fg_color=C["card_hi"], text_color=C["text"]).grid(row=row, column=0, sticky="w", pady=(12, 0))
                return var
            self.section(f, "Options").grid(row=6, column=0, sticky="ew", pady=(18, 0))
            keep_v = switch(7, "Also keep the original file", "keep_original")
            chk_v = switch(8, "Check for updates when the app starts", "check_updates")
            auto_v = switch(9, "Install updates automatically", "auto_install")

            info = ctk.CTkFrame(f, fg_color=C["card"], corner_radius=12)
            info.grid(row=10, column=0, sticky="ew", pady=(20, 0))
            self.label(info, f"Version {__version__} · {KIND_LABEL[install_kind()]}", 12, "muted").pack(side="left", padx=14, pady=10)
            self.button(info, "GitHub page", lambda: __import__("webbrowser").open(f"https://github.com/{GITHUB_REPO}"),
                        "flat", width=100, height=30).pack(side="right", padx=8)

            def save_close():
                self.cfg.update(out_dir=dir_var.get().strip() or default_out_dir(),
                                tile=int(size_seg.get().split()[0]),
                                keep_original=bool(keep_v.get()), check_updates=bool(chk_v.get()),
                                auto_install=bool(auto_v.get()))
                save_config(self.cfg)
                top.destroy()
                self.refresh_preview()
            row = ctk.CTkFrame(top, fg_color="transparent")
            row.grid(row=1, column=0, sticky="e", padx=24, pady=(0, 18))
            self.button(row, "Cancel", top.destroy, "ghost", width=100).pack(side="left", padx=(0, 8))
            self.button(row, "Save", save_close, "accent", width=100).pack(side="left")

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

        def _set_update_btn(self, text, kind="ghost"):
            styles = {"ghost": (C["card_hi"], C["border"], C["text"]),
                      "accent": (C["accent"], C["accent_hover"], "white"),
                      "ok": (C["card_hi"], C["border"], C["good"])}
            fg, hov, tc = styles[kind]
            self.update_btn.configure(text=text, fg_color=fg, hover_color=hov, text_color=tc)

        def on_update_btn(self):
            if self.update_info:
                self.confirm_update(self.update_info)
            else:
                self.check_updates(manual=True)

        def check_updates(self, manual):
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
            if err:
                self._set_update_btn("Check for updates")
                if manual:
                    self.say(f"Update check failed: {err}", "err")
                return
            if not info:
                self._set_update_btn("✓  Up to date", "ok")
                if manual:
                    self.say(f"You're on the latest version (v{__version__}).", "ok")
                return
            self.update_info = info
            self._set_update_btn(f"⬇  Update to v{info['version']}", "accent")
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

            def prog(p):
                self.ui(lambda: self._set_update_btn(f"Downloading {int(p * 100)}%", "accent"))

            def done(_):
                self.say("Update downloaded. Restarting…", "ok")
                self.root.after(700, lambda: (self.root.destroy(), os._exit(0)))

            def job():
                try:
                    apply_update(info, prog)
                except Exception:
                    self.ui(lambda: (self.update_btn.configure(state="normal"),
                                     self._set_update_btn(f"⬇  Update to v{info['version']}", "accent")))
                    raise
            self.work(job, done, f"Downloading v{info['version']} from GitHub…")

    root = ctk.CTk()
    App(root)
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
