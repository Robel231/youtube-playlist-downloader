# Playlist Audio Downloader

Copy a YouTube playlist (or video) link and get MP3 files in
`~/Music/<playlist name>/`, numbered in playlist order, tagged, and with the
thumbnail as square cover art. Switch to **Video (MP4)** to save the videos
themselves in `~/Videos/<playlist name>/` instead.

Built on `yt-dlp`, `ffmpeg` and PySide6, all of which come from Fedora's
repositories:

```sh
sudo dnf install yt-dlp ffmpeg python3-pyside6 wl-clipboard
./install.sh          # launcher entry + `playlist-dl` command
```

## Using it

**Window**: open *Playlist Audio Downloader* from the app launcher, or bind a
shortcut to it (System Settings → Keyboard → Shortcuts → Add New →
Application…, for example Meta+Y). Then:

1. Copy a playlist link in the browser.
2. Open the window (or press the shortcut). The link appears in the link box.
3. Press **Download** (or Enter). A notification tells you when it finishes.

The pickers next to *Save to* choose **Audio (MP3)** or **Video (MP4)** and,
for video, the highest resolution (Best, 2160p … 360p; 1080p by default).
Each format remembers its own folder. The choice applies to the next link you
download, so you can queue one playlist as audio and another as video.

Nothing downloads until you press Download. While the window is open, each
YouTube link you copy replaces the one in the link box. Press Download while
something is already running and the new link is queued and fetched after it.
*Stop* ends the current download and clears the queue.

**Terminal**:

```sh
playlist-dl                       # uses the link on the clipboard
playlist-dl 'https://www.youtube.com/playlist?list=…'
playlist-dl URL --dir ~/Podcasts
playlist-dl URL --video           # MP4 into ~/Videos, up to 1080p
playlist-dl URL --video --res 720 # or best, 2160, 1440, 1080, 480, 360
```

## Where files go

| Link                         | Saved as                                     |
|------------------------------|----------------------------------------------|
| Playlist                     | `~/Music/<playlist>/01 - <title>.mp3`        |
| Single video / short         | `~/Music/Singles/<title>.mp3`                |
| Video opened from a playlist | the whole playlist                           |
| Video opened from a "Mix"    | just that video (Mixes are endless)          |

Video goes the same way under `~/Videos` (`Singles/<title>.mp4` and so on).

Each folder keeps a hidden `.archive.txt` file (`.archive-video.txt` for
video). Running the same playlist again
downloads only the tracks you don't have yet, which makes it an easy way to
sync a playlist that grows. Delete the archive to download everything again.

Files are MP3 at 192 kbps. Each one is tagged with title, artist and date; tracks
from a playlist also get the playlist name as the album and their position as
the track number. Track numbers come from each track's position when it was
downloaded, so reordering the playlist later doesn't renumber files you
already have.

Videos are MP4: the best stream at or below the chosen resolution (or the next
size up if a video is never that small), merged with the best audio. They get
the same tags, the thumbnail as cover and chapter markers. Newer YouTube
uploads usually come as AV1, which current Fedora plays fine.

## Notes

- yt-dlp needs a JavaScript runtime to get every YouTube format. The app
  looks for `deno` or `node` on PATH, then fnm's default node. Without one it
  still works, but prints a warning.
- If YouTube changes something and downloads start failing, updating yt-dlp
  usually fixes it: `sudo dnf upgrade yt-dlp`.
- Tests: `python3 -m pytest tests`
