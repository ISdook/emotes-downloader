# Emotes Downloader: 7TV to Discord

Made by [ISdook](https://github.com/ISdook).

Emotes Downloader gets emotes from 7TV and turns them into files you can add as Discord emoji. It works with both animated and static emotes. Each file comes out at 128 px and under Discord's 256 KB limit. It can also cut wide or big emotes into pieces like `:KEKW1::KEKW2::KEKW3:`.

## Download

**[Get the latest version](https://github.com/ISdook/emotes-downloader/releases/latest)**

- **`emotes-downloader.exe`**: just run it. You don't need Python.
- **`emotes-downloader-python.zip`**: the Python version. Unzip it and double-click `run.bat`. You need [Python](https://www.python.org/downloads/) installed, with **"Add python.exe to PATH"** ticked. The first run installs Pillow, the image library, by itself.

## Updates

The app checks GitHub for a new version when it starts. You can also click **Check for updates** at the top right. When a new version is out, click **Update now**. It downloads the new version from this repo's latest release, replaces itself and restarts.

- The `.exe` updates to the new `.exe`.
- The Python version updates `emotes_downloader.py`. It keeps the old file as `emotes_downloader.py.bak`.
- You can turn off the check on start with the checkbox under the buttons.

## Use it

1. Paste one of these into the box and press **Load**:
   - an emote link: `https://7tv.app/emotes/01F6MZGCNG000255K4X1K7NTHR`
   - an emote set link: `https://7tv.app/emote-sets/<id>` (loads every emote in the set)
   - a user link: `https://7tv.app/users/<id>` (loads their active emote set)
   - just an emote ID, or a GIF/PNG/WebP on your PC (**Open file…**)
2. Pick how to cut it:
   - **Don't cut**: one emote.
   - **Auto**: wide emotes are cut into square pieces. A 3:1 emote becomes 3 pieces, and a normal square emote stays as 1.
   - **Custom grid**: for example 2 wide × 2 tall, which makes a big 4-piece emote.
3. Press **Save for Discord**. Files go to `Downloads\7TV Emotes` unless you pick another folder.
   - With a set loaded, **Save ALL in list** saves every emote. If you Ctrl-click to select some, it saves only those.
4. Upload the pieces to your server (Server Settings → Emoji), then press **Copy** and paste the text into Discord.

## What it does to the files

- **Animated emotes become GIF and static emotes become PNG.** These are the formats Discord accepts for emoji.
- If a piece would be over Discord's 256 KB limit, the tool lowers quality step by step until it fits: first fewer colours, then a smaller size, then fewer frames. Every piece of one emote gets the same settings, so the pieces stay in sync when they animate.
- **Fit** settings:
  - *Fit inside*: adds transparent padding.
  - *Fill*: crops the edges.
  - *Stretch*: distorts the image to fill the grid.
- Piece names follow Discord's rules: letters, numbers and `_`, 2 to 32 characters.
- For cut emotes, each emote gets its own folder with the pieces and a `NAME_paste.txt`.

## Tips

- In Discord, a message with only emotes shows them bigger, which is the best way to show off a cut emote.
- 7TV's biggest files are 128 px tall. A 2×2 "big" version of a square emote is therefore upscaled and looks a bit softer.
- Your server needs one emoji slot per piece. Animated pieces use animated slots.

## Command line (optional)

```
python emotes_downloader.py https://7tv.app/emotes/<id> --split auto
python emotes_downloader.py <emote-id> --split 2x2 --fit cover --name BigKEKW
python emotes_downloader.py https://7tv.app/emote-sets/<id> --out "D:\Emotes"
python emotes_downloader.py --update
```

## Releasing a new version (for the maintainer)

1. Change `__version__` near the top of `emotes_downloader.py`, for example to `"1.0.1"`.
2. Commit and push, then either publish a release on GitHub (Releases → Draft a new release → new tag `v1.0.1`) or tag the commit:
   ```
   git tag v1.0.1
   git push origin v1.0.1
   ```
3. GitHub Actions builds `emotes-downloader.exe` and publishes the release. Everyone's app then sees the update.

The tag has to match `__version__`, or the build stops.
