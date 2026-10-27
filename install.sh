#!/usr/bin/env bash
# Add Playlist Audio Downloader to the KDE app launcher and put `playlist-dl`
# on PATH. Safe to run again after moving the project folder.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
bin="$HOME/.local/bin"
desktop="$apps/playlist-audio-downloader.desktop"

case "$here" in
  *[\"\`\$\\]*) echo "Move the project to a folder without \" \` \$ or \\ in its path first." >&2; exit 1 ;;
esac

for cmd in yt-dlp ffmpeg wl-paste; do
  command -v "$cmd" >/dev/null || echo "Warning: $cmd is not installed (sudo dnf install $cmd)" >&2
done
/usr/bin/python3 -c 'import yt_dlp, PySide6' 2>/dev/null ||
  echo "Warning: install the Python modules with: sudo dnf install yt-dlp python3-pyside6" >&2

mkdir -p "$apps" "$bin"
chmod +x "$here/app.py" "$here/downloader.py"

cat > "$desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Playlist Audio Downloader
GenericName=YouTube Audio Downloader
Comment=Copy a YouTube playlist link and save it as MP3 files
Exec=/usr/bin/python3 "$here/app.py" %u
Icon=folder-music
Terminal=false
Categories=AudioVideo;Audio;
Keywords=youtube;playlist;mp3;music;download;
StartupNotify=true
SingleMainWindow=true
EOF
command -v desktop-file-validate >/dev/null && desktop-file-validate "$desktop"
command -v kbuildsycoca6 >/dev/null && kbuildsycoca6 >/dev/null 2>&1 || true

ln -sfn "$here/downloader.py" "$bin/playlist-dl"

echo "Installed:"
echo "  launcher entry  $desktop"
echo "  command         $bin/playlist-dl"
case ":$PATH:" in *":$bin:"*) ;; *) echo "  (add $bin to PATH to use playlist-dl from any terminal)" ;; esac
cat <<'EOF'

Optional keyboard shortcut to open the window:
  System Settings > Keyboard > Shortcuts > Add New > Application...
  pick "Playlist Audio Downloader", then set e.g. Meta+Y.
EOF
