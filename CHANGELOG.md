# Changelog

Every release gets an entry here. The release build fails without one, and the text becomes the GitHub release notes and the app's "What's new" window.

## v1.5.1 — 2026-11-27
- Left-click the tray icon to open the app; right-click opens the tray panel
- The Converter now makes only GIF and PNG, the formats that matter for Discord emotes (Auto picks GIF for animated, PNG for still)
- The Emotes tab now has a Files panel like the Converter: Add files, Add folder, Remove and Clear all. Files, folders, drops and single 7TV emotes are added to the list; loading a 7TV set still replaces it

## v1.5.0 — 2026-11-27
- New **Converter** tab: turn any image, GIF, WebP, AVIF or video (MP4, WebM, MOV…) into a Discord emoji, a sticker or any custom size, all on your own PC
  - Presets for Emoji (128×128, 256 KB) and Sticker (320×320, 512 KB), or a custom size and file size limit
  - Output as GIF, PNG or WebP, with Fill / Stretch / Bars / Crop, Smooth or Pixel art scaling, colours and frame rate
  - Live before/after preview with the final size, and batch converting into a "Converted" folder
  - Video support downloads ffmpeg once (about 40 MB) the first time you open a video
- Drag and drop: drop files, a whole folder or a 7TV link anywhere on the window
- Redesigned Settings window with pages (General, Saving, Updates, About); changes save instantly
- New option: start with Windows (quietly in the tray)
- Fixed: the Auto button can now be switched off again
- The Emotes tab can also open video files

## v1.4.1 — 2026-11-27
- Fixed: the Browse 7TV window glitched and smeared while scrolling. It's rebuilt to scroll smoothly, and it loads more emotes by itself as you reach the bottom
- Fixed: icons showed as empty boxes on Windows (Browse 7TV, Save and others). All icons are now drawn by the app itself
- Simpler saving: the button is now just "Save", with Save to and the folder path right under it
- Removed the "Paste in Discord" box (and the _paste.txt file)
- Fixed a rare problem where the app could stop reacting after an error in a background task

## v1.4.0 — 2026-11-27
- New tray panel in the same style as Plume and LiteDock: click the tray icon (left or right) to open it
- Shows the app's status, the last copied 7TV emote with a one-click Save, and quick actions (Browse 7TV, open the emotes folder, Settings)
- The update row shows "Up to date", a "New" badge when an update is ready, or the download progress
- Open app and Exit buttons; the panel slides in next to the taskbar and closes when you click away

## v1.3.0 — 2026-11-27
- New **Browse 7TV** window: search 7TV, or look through Trending, Top and New, right inside the app. Press ⬇ Save on any emote and it's saved straight away with your cut settings
- Copy any emote link on 7tv.app and the app notices it: a bar pops up with **Save now**
- Pick where emotes are saved, right under the Save button: the app's own folder (new default), Downloads, Pictures, Desktop or any folder you like
- Tray icon: keep the app running in the system tray, with quick actions (Browse 7TV, save the copied link, open the emotes folder, check for updates)
- Pop-up notifications when an emote is saved
- Clearer cutting: the preview now shows every piece separately with its :name: under it
- Auto now cuts wide emotes into enough square pieces that each one fills the full height (a 384×112 emote like OdaMeeting becomes 4 pieces, :OdaMeeting1: … :OdaMeeting4:)
- Pick the number of pieces with Wide / Tall buttons; press Auto to go back to the automatic choice
- New "Fill" fit (default): pieces are filled edge to edge with no empty bars
- Much better quality for video-like emotes: only the parts that change between frames are stored, so pieces keep every frame and stay as big as possible under Discord's 256 KB limit

## v1.1.0 — 2026-11-27
- Brand-new look: a modern dark design with rounded controls, an app icon and a cleaner layout
- Simpler controls: pick how to cut (None / Auto / Grid) and how to fit (Pad / Crop / Stretch) with one click
- Extra options (save folder, piece size, keeping originals, updates) moved into ⚙ Settings
- The preview shows numbered pieces and how the emote looks on Discord's background
- New Setup installer (Emotes-Downloader-Setup.exe) next to the portable .exe
- Updates now install automatically when the app starts (you can turn this off in Settings)
- The update button shows download progress, and the "What's new" window opens after every update
- Emote sets: tick the emotes you want and save only those

## v1.0.0 — 2026-11-27
- First release
- Download 7TV emotes by link or ID, whole emote sets, or a user's set
- Resize to Discord's 128 px emoji size and stay under the 256 KB limit
- Cut wide or big emotes into pieces (:KEKW1::KEKW2:) with the paste text ready to copy
- Built-in updater that pulls new versions from GitHub
