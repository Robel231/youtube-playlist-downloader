#!/usr/bin/python3
"""Download a YouTube playlist (or single video) as tagged MP3 files.

Core logic shared by the command line (this file) and the window (app.py).
Nothing in here imports Qt.

    playlist-dl                      # use the link on the clipboard
    playlist-dl URL [--dir ~/Music]
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import yt_dlp
from yt_dlp.postprocessor.common import PostProcessor
from yt_dlp.utils import DownloadCancelled, sanitize_filename

YOUTUBE_HOSTS = {
    'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com',
    'youtu.be', 'www.youtu.be',
}
SINGLES_DIR = 'Singles'
ARCHIVE_NAME = '.archive.txt'
FNM_NODE = Path('~/.local/share/fnm/aliases/default/bin/node')

# Square-crop the 16:9 thumbnail so it looks right as album art.
SQUARE_COVER = (
    'EmbedThumbnail+FFmpeg_o:-c:v mjpeg -qmin 1 -qscale:v 1 '
    "-vf crop=\"'if(gt(ih,iw),iw,ih)':'if(gt(iw,ih),ih,iw)'\""
)

ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')
ARCHIVED_RE = re.compile(r'^\[download\] ([\w-]+): .*has already been recorded in the archive')
ERROR_RE = re.compile(r'^(?:ERROR: )?\[[\w:]+\] ([\w-]+): (.+)$')

# Track states, in the order a track normally moves through them.
PENDING, DOWNLOADING, CONVERTING, DONE, SKIPPED, FAILED = (
    'pending', 'downloading', 'converting', 'done', 'skipped', 'failed')


def default_music_dir():
    try:
        out = subprocess.run(['xdg-user-dir', 'MUSIC'], capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip() and out.stdout.strip() != str(Path.home()):
            return Path(out.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        pass
    return Path.home() / 'Music'


def normalize_url(text):
    """Return a cleaned-up YouTube URL, or None if `text` isn't one.

    The whole text must be a single link, so a copied paragraph that merely
    contains a link is ignored. A video link that carries an auto-generated
    Mix (`list=RD...`) is reduced to just that video.
    """
    if not text:
        return None
    text = text.strip()
    if not text or any(c.isspace() for c in text):
        return None
    if '://' not in text:
        text = 'https://' + text
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    if parts.scheme not in ('http', 'https') or (parts.hostname or '') not in YOUTUBE_HOSTS:
        return None

    query = parse_qs(parts.query)
    path = parts.path.rstrip('/')
    if parts.hostname.endswith('youtu.be'):
        ok = bool(path.strip('/'))
    elif path == '/watch':
        ok = 'v' in query or 'list' in query
    elif path == '/playlist':
        ok = 'list' in query
    elif path.startswith('/shorts/'):
        ok = len(path) > len('/shorts/')
    else:
        ok = False
    if not ok:
        return None

    has_video = 'v' in query or parts.hostname.endswith('youtu.be')
    if has_video and query.get('list', [''])[0].startswith('RD'):
        query.pop('list', None)
        query.pop('start_radio', None)
        query.pop('index', None)
    return urlunsplit(('https', parts.netloc, parts.path, urlencode(query, doseq=True), ''))


def is_youtube_url(text):
    return normalize_url(text) is not None


def read_clipboard():
    """Text on the Wayland clipboard, or None. Works without window focus."""
    if not shutil.which('wl-paste'):
        return None
    try:
        out = subprocess.run(['wl-paste', '--no-newline', '--type', 'text'],
                             capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout if out.returncode == 0 and out.stdout else None


def detect_js_runtime(path=None, home=None):
    """Find a JavaScript runtime for YouTube's player challenges.

    Returns (name, executable) or None. A launcher started from the menu
    doesn't inherit the fnm PATH set up by the shell, so the fnm default
    alias is checked explicitly.
    """
    for name in ('deno', 'node'):
        exe = shutil.which(name, path=path)
        if exe:
            return name, exe
    fnm_node = Path(home or Path.home()) / FNM_NODE.relative_to('~')
    if os.access(fnm_node, os.X_OK):
        return 'node', str(fnm_node)
    return None


def js_runtime_args(runtime):
    if not runtime:
        return []
    name, exe = runtime
    return ['--js-runtimes', f'{name}:{exe}', '--remote-components', 'ejs:github']


def build_argv(dest_dir, is_playlist, runtime, track_count=1):
    digits = max(2, len(str(track_count)))
    argv = [
        '-f', 'bestaudio/best',
        '-x', '--audio-format', 'mp3', '--audio-quality', '192K',
        '--embed-metadata', '--embed-thumbnail', '--convert-thumbnails', 'jpg',
        '--ppa', SQUARE_COVER,
        '--download-archive', str(Path(dest_dir) / ARCHIVE_NAME),
        '--no-overwrites', '--no-mtime', '--color', 'no_color',
        '-P', str(dest_dir),
        '-o', f'%(playlist_index&{{:0{digits}d}} - |)s%(title)s.%(ext)s',
    ]
    if is_playlist:
        argv += [
            '--parse-metadata', 'playlist_title:%(album)s',
            '--parse-metadata', 'playlist_index:%(track_number)s',
        ]
    return argv + js_runtime_args(runtime)


def ydl_options(argv):
    return yt_dlp.parse_options(argv).ydl_opts


@dataclass
class Track:
    index: int
    id: str
    title: str
    status: str = PENDING
    error: str = ''


@dataclass
class Probe:
    url: str
    title: str
    is_playlist: bool
    tracks: list[Track]


@dataclass
class Summary:
    title: str
    dest_dir: Path
    tracks: list[Track] = field(default_factory=list)
    cancelled: bool = False

    def count(self, status):
        return sum(t.status == status for t in self.tracks)

    def describe(self):
        parts = [f'{self.count(DONE)} new']
        if self.count(SKIPPED):
            parts.append(f'{self.count(SKIPPED)} already had')
        if self.count(FAILED):
            parts.append(f'{self.count(FAILED)} failed')
        text = ', '.join(parts)
        return text + ' (stopped)' if self.cancelled else text


class _Logger:
    """Receives everything yt-dlp would print and turns it into events."""

    def __init__(self, on_message):
        self.on_message = on_message

    def debug(self, msg):
        self.on_message('info', msg)

    def info(self, msg):
        self.on_message('info', msg)

    def warning(self, msg):
        self.on_message('warning', msg)

    def error(self, msg):
        self.on_message('error', msg)


class _TrackDone(PostProcessor):
    """Runs once per video after its file is in its final place."""

    def __init__(self, callback):
        super().__init__()
        self.callback = callback

    def run(self, info):
        self.callback(info)
        return [], info


def probe(url, runtime=None, logger=None):
    """List a playlist's tracks without downloading anything."""
    opts = ydl_options(['--flat-playlist', '--skip-download', '--color', 'no_color']
                       + js_runtime_args(runtime))
    opts['logger'] = logger or _Logger(lambda level, msg: None)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        raise yt_dlp.utils.DownloadError(f'Could not read {url}')

    if info.get('_type') == 'playlist':
        tracks = [Track(i, e.get('id') or '', e.get('title') or e.get('id') or '?')
                  for i, e in enumerate(info.get('entries') or [], 1) if e]
        return Probe(url, info.get('title') or info.get('id') or 'Playlist', True, tracks)
    return Probe(url, info.get('title') or info.get('id') or 'Video', False,
                 [Track(1, info.get('id') or '', info.get('title') or '?')])


def target_dir(root, probed):
    if not probed.is_playlist:
        return Path(root) / SINGLES_DIR
    name = sanitize_filename(probed.title).strip(' .') or 'Playlist'
    return Path(root) / name


def archived_ids(dest_dir):
    try:
        lines = (Path(dest_dir) / ARCHIVE_NAME).read_text(encoding='utf-8').splitlines()
    except OSError:
        return set()
    return {parts[1] for parts in (line.split() for line in lines) if len(parts) >= 2}


def download(url, root, on_event=lambda kind, data: None, cancel=None):
    """Download `url` as MP3s under `root`. Blocks until finished.

    on_event(kind, data) is called from this thread with:
      'probed'  {'title', 'dest', 'is_playlist', 'tracks': [Track, ...]}
      'track'   {'track': Track, 'percent': float | None}
      'message' {'level', 'text'}
    Setting `cancel` (a threading.Event) stops at the next progress update.
    """
    cancel = cancel or threading.Event()
    url = normalize_url(url) or url
    runtime = detect_js_runtime()
    if not runtime:
        on_event('message', {'level': 'warning', 'text':
                 'No JavaScript runtime (deno or node) found; some YouTube formats may be missing.'})

    summary = Summary(url, Path(root))
    by_id = {}
    errors = []

    def mark(video_id, status, error='', percent=None, info=None):
        t = by_id.get(video_id)
        if t is None and info is not None:
            # Not in the probe (e.g. a single video whose id differs); track it anyway.
            t = Track(len(summary.tracks) + 1, video_id, info.get('title') or video_id)
            summary.tracks.append(t)
            by_id[video_id] = t
        if t is None or (t.status in (DONE, SKIPPED) and status != DONE):
            return
        if info is not None and info.get('title'):
            t.title = info['title']
        t.status, t.error = status, error or t.error
        on_event('track', {'track': t, 'percent': percent})

    def log(level, msg):
        msg = ANSI_RE.sub('', msg)
        if level == 'info':
            if m := ARCHIVED_RE.match(msg):
                mark(m.group(1), SKIPPED)
            return
        if level == 'error':
            text = msg.removeprefix('ERROR: ')
            errors.append(text)
            if m := ERROR_RE.match(text):
                mark(m.group(1), FAILED, m.group(2))
        on_event('message', {'level': level, 'text': msg})

    logger = _Logger(log)
    try:
        probed = probe(url, runtime, logger)
    except yt_dlp.utils.DownloadError as e:
        raise yt_dlp.utils.DownloadError(errors[-1] if errors else str(e)) from e
    dest = target_dir(root, probed)
    dest.mkdir(parents=True, exist_ok=True)
    summary.title, summary.dest_dir, summary.tracks = probed.title, dest, probed.tracks
    for t in probed.tracks:
        by_id.setdefault(t.id, t)

    already = archived_ids(dest)
    for t in probed.tracks:
        if t.id in already:
            t.status = SKIPPED
    on_event('probed', {'title': probed.title, 'dest': dest,
                        'is_playlist': probed.is_playlist, 'tracks': probed.tracks})

    last_percent = {}

    def on_progress(d):
        if cancel.is_set():
            raise DownloadCancelled('Stopped by user')
        info = d.get('info_dict') or {}
        vid = info.get('id')
        if d['status'] == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate')
            pct = int(d.get('downloaded_bytes', 0) * 100 / total) if total else None
            if last_percent.get(vid) != pct:
                last_percent[vid] = pct
                mark(vid, DOWNLOADING, percent=pct, info=info)

    def on_postprocess(d):
        if cancel.is_set():
            raise DownloadCancelled('Stopped by user')
        if d['status'] == 'started' and d.get('postprocessor') == 'ExtractAudio':
            mark((d.get('info_dict') or {}).get('id'), CONVERTING, info=d.get('info_dict'))

    opts = ydl_options(build_argv(dest, probed.is_playlist, runtime, len(probed.tracks)))
    opts.update(logger=logger, progress_hooks=[on_progress], postprocessor_hooks=[on_postprocess])
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.add_post_processor(_TrackDone(lambda info: mark(info.get('id'), DONE, info=info)),
                                   when='after_move')
            ydl.download([url])
    except DownloadCancelled:
        summary.cancelled = True
    except yt_dlp.utils.DownloadError as e:
        on_event('message', {'level': 'error', 'text': str(e)})

    for t in summary.tracks:
        if t.status in (DONE, SKIPPED, FAILED):
            continue
        if summary.cancelled:
            if t.status == PENDING:
                continue
            t.status = PENDING
        else:
            t.status, t.error = FAILED, t.error or 'Not downloaded (see log)'
        on_event('track', {'track': t, 'percent': None})
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='playlist-dl',
        description='Download a YouTube playlist or video as MP3 files. '
                    'With no URL, the link on the clipboard is used.')
    parser.add_argument('url', nargs='?', help='playlist or video link (default: clipboard)')
    parser.add_argument('--dir', type=Path, default=default_music_dir(),
                        help='folder to save into (default: %(default)s)')
    args = parser.parse_args(argv)

    source = args.url if args.url is not None else read_clipboard()
    url = normalize_url(source)
    if not url:
        where = 'given' if args.url is not None else 'on the clipboard'
        print(f'No YouTube link {where}. Copy a playlist link and try again.', file=sys.stderr)
        return 2
    if args.url is None:
        print(f'Using link from clipboard:\n  {url}')

    tty = sys.stdout.isatty()
    total = 0

    def on_event(kind, data):
        nonlocal total
        if kind == 'probed':
            total = len(data['tracks'])
            what = 'Playlist' if data['is_playlist'] else 'Video'
            print(f"{what}: {data['title']} ({total} track{'s' * (total != 1)})")
            print(f"Saving to {data['dest']}")
            if had := sum(t.status == SKIPPED for t in data['tracks']):
                print(f'{had} already downloaded, skipping those.')
        elif kind == 'track':
            t, pct = data['track'], data['percent']
            line = f'[{t.index:>{len(str(total))}}/{total}] {t.title} ... '
            if t.status in (DONE, SKIPPED, FAILED):
                label = {DONE: 'done', SKIPPED: 'already had', FAILED: f'FAILED: {t.error}'}
                print(('\r\x1b[K' if tty else '') + line + label[t.status], flush=True)
            elif tty:
                state = 'converting' if t.status == CONVERTING else f'{pct}%' if pct is not None else ''
                print('\r\x1b[K' + line + state, end='', flush=True)
        elif kind == 'message' and data['level'] in ('warning', 'error'):
            print(('\r\x1b[K' if tty else '') + data['text'], file=sys.stderr, flush=True)

    try:
        summary = download(url, args.dir, on_event)
    except KeyboardInterrupt:
        print('\nStopped.', file=sys.stderr)
        return 130
    except yt_dlp.utils.YoutubeDLError as e:
        print(f'Could not read that link: {e}', file=sys.stderr)
        return 1
    print(f'{summary.title}: {summary.describe()}  →  {summary.dest_dir}')
    return 1 if summary.count(FAILED) else 0


if __name__ == '__main__':
    sys.exit(main())
