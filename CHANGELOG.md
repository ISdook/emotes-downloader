# Changelog

Every release gets an entry here. The release build fails without one, and the text becomes the GitHub release notes and the app's "What's new" window.

## v1.2.0 — 2026-11-27
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
