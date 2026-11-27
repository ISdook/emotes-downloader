# Changelog

Every release gets an entry here. The release build fails without one, and the text becomes the GitHub release notes and the app's "What's new" window.

## v1.8.2 — 2026-11-27
- Fixed: for some people, "Update now" did nothing or the app closed and didn't come back
  - Portable version: updating broke when the app's folder had Arabic or other non-English letters in its path (for example a Windows user name or OneDrive's Desktop folder). The new version now finishes the update by itself, and works in any folder
  - If an update fails, a window now says why, with **Try again** and **Download it myself** buttons. Before, the reason only flashed in the status bar
  - Pressing Update while a save or download is running now waits for it to finish, instead of doing nothing
  - The app checks it can save files in its own folder before downloading, and explains what to do if it can't
  - Installed version: if Windows needs permission to run the installer, it now asks instead of failing
  - Update problems are written to update.log in the app's data folder
- If your current version still can't update itself, download the new version once from the GitHub releases page. Updates work by themselves after that

## v1.8.1 — 2026-11-27
- Browse 7TV: animated emotes now play in the grid, like on 7tv.app, instead of showing only the first frame
  - Only the emotes you can see are animated, so scrolling stays smooth
  - Animations pause while the Browse window is minimized

## v1.8.0 — 2026-11-27
- Linking your bot is much simpler: paste **one** thing, the bot token. The app finds your Discord app and your account from it, makes the bot private when Discord allows it, names it and opens the invite page
- New step-by-step setup guide in Settings → Discord. Each step has buttons that open the right Developer Portal page, a Paste button, and turns green when it's done. It also explains the "Private application cannot have a default authorization link" error and how to fix it
- **Discord settings** link under **Send to Discord** (Emotes tab and Converter)
- Logging in with Discord is now optional. Without it, "Send to Discord" lists your servers where the bot already is, with an "Add the bot to another server" button. With it, your servers without the bot show too (dark, with Invite bot)
- Still only your servers: without the login, the app asks Discord which of the bot's servers you're in and can add emojis to
- The Emotes tab controls are a little more compact so everything fits

## v1.7.1 — 2026-11-27
- Send to Discord is now locked to you. Before every send, the app checks with Discord that:
  - your bot is still private (Public Bot off)
  - you're logged in with the account that owns the bot
  - the server is one of your own servers where you can add emojis, and the bot is in it

  If any check fails, nothing is sent and a red line says why
- Only your servers are listed: the list comes from your own Discord login, never from the servers the bot is in
- The bot has no commands and never goes online, so other people in your server can't use it

## v1.7.0 — 2026-11-27
- New **Send to Discord**: add finished emotes and stickers straight to your servers with your own private bot
  - Set it up once in Settings → Discord: make an app in the Discord Developer Portal, paste its 3 values and log in with Discord in your browser. The invite link for your server opens by itself
  - Nothing to host: the app talks to Discord directly, so the bot only works while the app is open
  - The panel lists every server you can add emotes to. Servers with the bot are in colour and show free slots; servers without it are dark, with an **Invite bot** button
  - Pick a server and press Send. Each emote shows a green line when it's added, or a red line saying why it wasn't (no free slots, missing permission, bad file…)
  - **Send to Discord** buttons on the Emotes tab and in the Converter, and the panel opens by itself after saving (you can turn this off)
- The bot is always named "ISdook - emotes downloader" and uses the ISdook GitHub picture. The app puts both back on every start if they were changed
- Privacy check: Settings warns in red if the bot is set to Public or the app belongs to another account, with a Fix button
- Your Discord tokens are stored encrypted on your PC (Windows DPAPI)
- Animated stickers longer than 5 seconds are cut to 5 seconds, Discord's limit
- Converter: Convert and Send sit side by side, so everything fits on smaller screens

## v1.6.0 — 2026-11-27
- New "Make it a: Emoji / Sticker" choice on the Emotes tab
  - **Emoji**: 128×128 pieces up to 256 KB, cut into pieces like before
  - **Sticker**: exactly 320×320 and up to 512 KB, the size Discord needs for stickers. It's never cut into pieces, and it's saved in a "Stickers" folder
- Better quality for wide animated emotes with transparent bars: they now store only what changes between frames, so they keep more frames
- Command line: new --sticker option

## v1.5.2 — 2026-11-27
- Settings: long descriptions now wrap instead of getting cut off
- Converter: the Before / After boxes are the same width and show their full text
- New README with screenshots and a full guide to every feature

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
