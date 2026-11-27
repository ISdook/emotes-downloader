# Emotes Downloader: 7TV to Discord

Made by [ISdook](https://github.com/ISdook).

Emotes Downloader gets emotes from 7TV and turns them into files you can add as Discord emoji. It works with both animated and static emotes. Each file comes out at 128 px and under Discord's 256 KB limit. It can also cut wide or big emotes into pieces like `:KEKW1::KEKW2::KEKW3:`.

## Download

**[Get the latest version](https://github.com/ISdook/emotes-downloader/releases/latest)**. There are three ways to run it:

| File | What it is |
|---|---|
| **Emotes-Downloader-Setup.exe** | The installer. Adds the app to the Start menu (and the desktop if you want). It installs for your user only, so no admin is needed. |
| **Emotes-Downloader-Portable.exe** | One file. Just run it from anywhere, even a USB stick. |
| **Emotes-Downloader-Python.zip** | For running from source. Unzip it and double-click `run.bat`. You need [Python](https://www.python.org/downloads/) with "Add python.exe to PATH" ticked. |

Windows may show "Windows protected your PC" the first time, because the app isn't code-signed. Click **More info → Run anyway**.

## Updates and changelog

- The app checks GitHub for a new version when it starts. By default it installs the update by itself and restarts.
- The button at the top right shows **✓ Up to date** or **⬇ Update to vX**. Click it to check again or to update right away.
- After every update, a **What's new** window shows the changelog. You can open it any time with the **What's new** button.
- Each version updates the matching way:
  - The installer version downloads the new setup and updates in place.
  - The portable version swaps in the new `.exe`.
  - The Python version updates `emotes_downloader.py`. It keeps the old file as `.bak`.
- You can turn off the check on start, or the automatic install, in **⚙ Settings**.

The full history is in [CHANGELOG.md](CHANGELOG.md).

## Use it

1. Paste one of these into the box and press **Load**:
   - an emote link: `https://7tv.app/emotes/<id>`
   - an emote set link: `https://7tv.app/emote-sets/<id>` (every emote in the set)
   - a user link: `https://7tv.app/users/<id>` (their active set)
   - just an emote ID, or a GIF/PNG/WebP from your PC (**Open file**)
2. Pick how to cut it:
   - **None**: one emote.
   - **Auto**: wide emotes become square pieces.
   - **Grid**: choose the pieces yourself, for example 2 wide × 2 tall.
3. Pick how it fits:
   - **Pad**: adds transparent edges.
   - **Crop**: trims the edges.
   - **Stretch**: distorts the image to fill.
4. Press **Save for Discord**.
   - With a set loaded, tick the emotes you want and press **Save selected**. With nothing ticked, the button says **Save all** and saves the whole set.
5. Upload the pieces to your server (Server Settings → Emoji), then press **Copy** and paste the text into Discord.

**⚙ Settings** has the save folder, the piece size, keeping the original file, and the update options.

## What it does to the files

- **Animated emotes become GIF and static emotes become PNG.** These are the formats Discord accepts for emoji.
- If a piece would be over 256 KB, the tool lowers quality step by step until it fits: first fewer colours, then a smaller size, then fewer frames. Every piece of one emote gets the same settings, so the pieces stay in sync when they animate.
- Names follow Discord's rules: letters, numbers and `_`, 2 to 32 characters.
- For cut emotes, each emote gets its own folder with the pieces and a `NAME_paste.txt`.

## Tips

- In Discord, a message with only emotes shows them bigger, which is the best way to show off a cut emote.
- 7TV's biggest files are 128 px tall, so a 2×2 version of a square emote is upscaled and looks a bit softer.
- Your server needs one emoji slot per piece. Animated pieces use animated slots.

## Command line (optional)

```
python emotes_downloader.py https://7tv.app/emotes/<id> --split auto
python emotes_downloader.py <emote-id> --split 2x2 --fit cover --name BigKEKW
python emotes_downloader.py https://7tv.app/emote-sets/<id> --out "D:\Emotes"
python emotes_downloader.py --update
```

## Releasing a new version (maintainer)

1. Change `__version__` near the top of `emotes_downloader.py`, for example to `"1.2.0"`.
2. Add a section at the top of **CHANGELOG.md**:
   ```
   ## v1.2.0 — 2026-12-01
   - What changed
   ```
   The build refuses to run without it. This text becomes the release notes and the app's "What's new" window.
3. Commit and push.
4. On GitHub, open **Releases → Draft a new release**, create the tag `v1.2.0` and publish it.

GitHub Actions then builds the portable `.exe`, the setup `.exe` and the Python zip, and attaches them to the release. Everyone's app picks up the update automatically.

To build locally on Windows, run `build_exe.bat`. The setup `.exe` also needs [Inno Setup 6](https://jrsoftware.org/isdl.php).
