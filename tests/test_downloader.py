import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import downloader as dl  # noqa: E402


@pytest.mark.parametrize('text, expected', [
    ('https://www.youtube.com/playlist?list=PL123', 'https://www.youtube.com/playlist?list=PL123'),
    ('  https://youtu.be/abc123?si=x \n', 'https://youtu.be/abc123?si=x'),
    ('youtube.com/watch?v=abc123', 'https://youtube.com/watch?v=abc123'),
    ('https://music.youtube.com/playlist?list=OLAK5', 'https://music.youtube.com/playlist?list=OLAK5'),
    ('https://m.youtube.com/watch?v=abc&list=PL9', 'https://m.youtube.com/watch?v=abc&list=PL9'),
    ('https://www.youtube.com/shorts/abc123', 'https://www.youtube.com/shorts/abc123'),
    # A video opened from an auto-generated Mix keeps only the video.
    ('https://www.youtube.com/watch?v=abc&list=RDabc&start_radio=1',
     'https://www.youtube.com/watch?v=abc'),
])
def test_normalize_accepts_youtube_links(text, expected):
    assert dl.normalize_url(text) == expected


@pytest.mark.parametrize('text', [
    None, '', 'hello', 'https://example.com/playlist?list=PL1',
    'https://www.youtube.com/', 'https://www.youtube.com/playlist',
    'https://www.youtube.com/watch', 'check this https://youtu.be/abc out',
    'ftp://youtube.com/watch?v=abc', 'https://notyoutube.com/watch?v=abc',
])
def test_normalize_rejects_other_text(text):
    assert dl.normalize_url(text) is None
    assert not dl.is_youtube_url(text)


def test_target_dir_playlist_and_single(tmp_path):
    playlist = dl.Probe('u', 'Road Trip / Mix', True, [])
    single = dl.Probe('u', 'Some Song', False, [])
    assert dl.target_dir(tmp_path, playlist).parent == tmp_path
    assert '/' not in dl.target_dir(tmp_path, playlist).name
    assert dl.target_dir(tmp_path, playlist).name.startswith('Road Trip')
    assert dl.target_dir(tmp_path, single) == tmp_path / 'Singles'
    assert dl.target_dir(tmp_path, dl.Probe('u', '...', True, [])).name == 'Playlist'


def test_build_argv_playlist_is_accepted_by_yt_dlp(tmp_path):
    argv = dl.build_argv(tmp_path, True, ('node', '/usr/bin/node'), track_count=150)
    opts = dl.ydl_options(argv)
    keys = [pp['key'] for pp in opts['postprocessors']]
    assert opts['format'] == 'bestaudio/best'
    assert opts['outtmpl']['default'] == '%(playlist_index&{:03d} - |)s%(title)s.%(ext)s'
    assert opts['paths'] == {'home': str(tmp_path)}
    assert opts['download_archive'] == str(tmp_path / '.archive.txt')
    assert {'MetadataParser', 'FFmpegExtractAudio', 'FFmpegMetadata', 'EmbedThumbnail'} <= set(keys)
    extract = next(pp for pp in opts['postprocessors'] if pp['key'] == 'FFmpegExtractAudio')
    assert (extract['preferredcodec'], extract['preferredquality']) == ('mp3', '192')
    assert 'crop=' in ' '.join(opts['postprocessor_args']['embedthumbnail+ffmpeg_o'])
    assert opts['js_runtimes']['node'] == {'path': '/usr/bin/node'}
    assert opts['remote_components'] == ['ejs:github']


def test_build_argv_single_video_has_no_album_tags(tmp_path):
    opts = dl.ydl_options(dl.build_argv(tmp_path, False, None))
    assert 'MetadataParser' not in [pp['key'] for pp in opts['postprocessors']]
    assert opts['outtmpl']['default'].startswith('%(playlist_index&{:02d} - |)s')
    assert 'remote_components' not in opts or not opts['remote_components']


def _fake_exe(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/sh\n')
    path.chmod(0o755)
    return path


def test_js_runtime_prefers_deno_then_node_then_fnm(tmp_path):
    bin_dir, home = tmp_path / 'bin', tmp_path / 'home'
    bin_dir.mkdir()
    assert dl.detect_js_runtime(path=str(bin_dir), home=home) is None

    fnm = _fake_exe(home / '.local/share/fnm/aliases/default/bin/node')
    assert dl.detect_js_runtime(path=str(bin_dir), home=home) == ('node', str(fnm))

    node = _fake_exe(bin_dir / 'node')
    assert dl.detect_js_runtime(path=str(bin_dir), home=home) == ('node', str(node))

    deno = _fake_exe(bin_dir / 'deno')
    assert dl.detect_js_runtime(path=str(bin_dir), home=home) == ('deno', str(deno))


def test_archived_ids(tmp_path):
    assert dl.archived_ids(tmp_path) == set()
    (tmp_path / '.archive.txt').write_text('youtube abc\nyoutube def\n\n', encoding='utf-8')
    assert dl.archived_ids(tmp_path) == {'abc', 'def'}


def test_log_patterns_match_yt_dlp_messages():
    m = dl.ARCHIVED_RE.match('[download] jNQXAC9IVRw: Me at the zoo has already been recorded in the archive')
    assert m and m.group(1) == 'jNQXAC9IVRw'
    m = dl.ERROR_RE.match('[youtube] abc_DEF-123: Private video. Sign in if you have access')
    assert m and m.groups() == ('abc_DEF-123', 'Private video. Sign in if you have access')


def test_summary_describe():
    tracks = [dl.Track(1, 'a', 'A', dl.DONE), dl.Track(2, 'b', 'B', dl.SKIPPED),
              dl.Track(3, 'c', 'C', dl.FAILED, 'gone')]
    assert dl.Summary('P', Path('/x'), tracks).describe() == '1 new, 1 already had, 1 failed'
    assert dl.Summary('P', Path('/x'), tracks[:1], cancelled=True).describe() == '1 new (stopped)'


def test_cli_rejects_non_youtube_link(capsys):
    assert dl.main(['https://example.com', '--dir', os.devnull]) == 2
    assert 'No YouTube link' in capsys.readouterr().err
