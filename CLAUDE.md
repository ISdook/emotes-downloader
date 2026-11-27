# CLAUDE.md

Notes for Claude (and other AI coding agents) working on **Emotes Downloader**. Read this before changing code.

## What this is

A Windows desktop app by ISdook that downloads 7TV emotes and turns them into Discord emoji (128×128, ≤ 256 KB) and stickers (320×320, ≤ 512 KB). It can cut big emotes into pieces (`:KEKW1: :KEKW2:`). It also has:
- a Converter tab for your own files
- a Browse 7TV window
- a tray icon with a flyout panel
- a private Discord bot that adds emotes to the user's servers
- an auto-updater that installs new versions from GitHub Releases

Users are mostly Arabic speakers on Windows 10/11. Expect Arabic letters in paths and user names.

## Layout

Everything is in one file, **`emotes_downloader.py`** (~5,700 lines). The sections, top to bottom:

| Section (search for the comment) | What's in it |
|---|---|
| top | `__version__`, constants, quality tiers, 7TV API (`EmoteRef`, `fetch_*`, `search_emotes`, `download_emote`, `fetch_thumb*`) |
| `# Image processing` | `load_frames`, `plan_grid`, `compose`, GIF/PNG encoding (shared palette, delta frames), `make_emote` |
| `# Converter` | `convert_media`, ffmpeg download (`NeedFFmpeg`, `install_ffmpeg`), `collect_media` |
| `# Discord` | REST helper `discord_request`, DPAPI `protect`/`unprotect`, OAuth callback server, `DiscordLink` |
| `# Auto-updater` | `check_for_update`, `apply_update`, portable swap (`finish_portable_update`, `portable_handoff`, `cleanup_after_update`), changelog |
| `# Settings` | `DEFAULTS`, `load_config`/`save_config`, save-location presets |
| `# Icons` | `draw_icon`: every icon is drawn with Pillow (no icon files) |
| `# GUI` → `run_gui()` | `FastList` (canvas-based Files list) and `class App`. App sections are marked `# ================= name =================` |
| `# Command line` | `run_cli`, and `__main__` (handles `--finish-update`, `--update`, `--tray`) |

Other files:
- `CHANGELOG.md` – one `## vX.Y.Z — YYYY-MM-DD` section per release. The build fails without it, and it becomes the release notes and the app's "What's new" window.
- `README.md` – the user guide shown on GitHub, with screenshots in `docs/`.
- `.github/workflows/release.yml` – builds everything on `windows-latest` when a `v*` tag is pushed or a release is published.
- `installer.iss` – Inno Setup script for the setup .exe. It installs per user (`PrivilegesRequired=lowest`).
- `tools/release_notes.py` – prints `__version__` or a CHANGELOG section. Used by the workflow.
- `run.bat`, `build_exe.bat`, `requirements.txt` – for running from source and building locally on Windows.
- `assets/` – `icon.png`, `icon.ico`, `tray.png`.

Dependencies: Python 3.12, Pillow, customtkinter, pystray, tkinterdnd2. PyInstaller only for builds.

## Releasing (every user-visible change ships as a release)

1. Bump `__version__` in `emotes_downloader.py`.
2. Add a `## vX.Y.Z — YYYY-MM-DD` section at the top of `CHANGELOG.md`. Write it for users, in plain English.
3. Update `README.md` if a feature or screen changed.
4. Commit, then push to `main`.
5. Publish a GitHub release with the tag `vX.Y.Z`, target `main`, title `vX.Y.Z` and the changelog section as its text (pushing the tag also works). The workflow checks that the tag matches `__version__` and attaches four files. The updater depends on these names, so don't rename them:
   - `Emotes-Downloader-Portable.exe`
   - `Emotes-Downloader-Setup.exe`
   - `emotes_downloader.py`
   - `Emotes-Downloader-Python.zip`
6. Wait for the "Release" workflow to finish, and check that all four files are on the release.

The owner (ISdook) has given standing permission to publish releases without asking first.

## Things that broke before (don't repeat them)

- **Arabic and other non-ASCII paths.** Don't write `.bat`/`cmd` scripts with paths in them: cmd reads them in the legacy code page and garbles the path. That broke the old portable updater. Do file work in Python.
- **The updater runs on the user's OLD version.** A fix to `apply_update` only helps once users have that version. `finish_portable_update` runs inside the newly downloaded .exe, and `installer.iss` belongs to the new setup, so fixes there apply right away.
- **Portable updates must leave one .exe.**
  - The new `X.new.exe` takes over `X.exe`. If the old file is locked, it renames it aside to `X.old.exe` (renaming a running .exe is allowed on Windows).
  - `cleanup_after_update` deletes `X.new.exe` and `X.old*.exe`.
  - Opening a leftover `X.new.exe` hands off to `X.exe` (`portable_handoff`).
  - Only ever delete those leftover names, never other files in the folder.
- **Pillow WebP frame durations.** `info["duration"]` is only filled in after the frame is decoded: call `im.load()` or `convert()` before reading it. Skipping this made the Browse previews play in slow motion. Durations ≤ 10 ms play at 100 ms, as in browsers.
- **`Image.Resampling.NEAREST == 0`**, which is falsy. Write `if method is None`, never `method or LANCZOS`.
- **CTkScrollableFrame** has a 200 px minimum because of its scrollbar. Shrink `_scrollbar` when you want a short box.
- **Never build one CTk widget per list row for big lists.** 800 rows made adding and clicking take seconds. Use `FastList`, which draws only the visible rows on one canvas, or a `tk.Listbox`. Update single rows instead of rebuilding the list.
- **Memory with big batches.** Free `frames`/`raw` after saving or converting items that aren't on screen. The Emotes tab keeps 40 decoded files (LRU in `_remember_loaded`).
- **Late binding in lambdas inside loops.** Bind loop variables as defaults (`lambda lbl=lbl: ...`).
- **Narrow side panels.** Settings rows and the options columns (Emotes 320 px, Converter 330 px) have fixed widths. After adding a control, take a screenshot and check that nothing at the bottom is cut off.

## Discord feature rules (from the owner)

- Each user creates their own bot in the Developer Portal. Linking needs only the bot token: `DiscordLink.link()` finds the app ID and owner from it. Logging in with Discord (OAuth, redirect `http://127.0.0.1:53682/callback`) is optional and only adds servers without the bot to the list.
- The bot must stay private to its owner:
  - `guard()` runs before every send. It checks that Public Bot is off, that the user is the owner, and that the server is the user's own with emoji permission.
  - List only the user's own servers, never every server the bot is in.
  - The bot has no commands and never connects to the gateway.
- The bot's name and picture are fixed: **"ISdook - emotes downloader"** with the ISdook GitHub avatar. `brand()` puts them back on every start.
- Show results line by line: green for success, red with the reason for failure.
- Secrets are stored with Windows DPAPI (`protect`). Never log or commit tokens.

## Testing (no Windows here, so test headless)

- GUI tests run the real app under `xvfb-run -a -s "-screen 0 1400x900x24" python3.12 <script>`.
- A test script imports `emotes_downloader`, monkeypatches `ctk.CTk.mainloop` to schedule actions with `self.after(...)`, takes screenshots with ImageMagick `import -window root`, then calls `os._exit(0)`. The app object is at `root._emotes_app`.
- Point `ed.CONFIG_PATH` at a temp JSON file so tests don't share settings.
- The sandbox can't reach 7TV, Discord or most websites. Fake them:
  - Monkeypatch `ed.http_get` or `ed.search_emotes` with local files.
  - For Discord, use a small fake REST server and set `ed.DISCORD_API` to it.
- Run `python3.12 -m pyflakes emotes_downloader.py` before committing (ignore the `_tkinter_finder` warning).
- When a change affects layout, take a screenshot and look at it.

## Writing style for anything users see

- UI text, CHANGELOG and README use short, plain English, because many users aren't native speakers.
- Say what happened and what to do next ("Couldn't save … Move the app to Documents").
- Don't use jargon in error messages. Suggest a fix when there is one.

## Commits

Use descriptive messages (`vX.Y.Z: what changed`). Never commit secrets, `build/`, `dist/` or `*.new.exe`; `.gitignore` covers these.
