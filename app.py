#!/usr/bin/python3
"""Playlist Audio Downloader: copy a YouTube link, get MP3s in ~/Music.

The window watches the clipboard and puts each YouTube link you copy in the
link box. Nothing downloads until you press Download; links added while a
download is running are queued and fetched in turn by downloader.py.
Starting the app a second time hands its link to the window already open.
"""

import dataclasses
import json
import os
import shutil
import sys
import threading
from collections import deque
from pathlib import Path

from PySide6.QtCore import QProcess, QSettings, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QGuiApplication, QIcon, QPalette
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMessageBox, QProgressBar, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

import downloader as dl

APP_ID = 'playlist-audio-downloader'
APP_NAME = 'Playlist Audio Downloader'
SERVER_NAME = f'{APP_ID}-{os.getuid()}'
CLIPBOARD_POLL_MS = 1000

STATUS_TEXT = {
    dl.PENDING: '',
    dl.CONVERTING: 'converting…',
    dl.DONE: '✓ done',
    dl.SKIPPED: '↷ already had',
}


class Worker(QThread):
    """Runs one download off the GUI thread and reports through signals."""

    event = Signal(str, object)
    job_done = Signal(object, str)  # Summary (or None), error text

    def __init__(self, url, root, parent):
        super().__init__(parent)  # owned by the window, so Python can't free it mid-run
        self.url, self.root = url, root
        self.cancel = threading.Event()

    def _emit(self, kind, data):
        # Hand the GUI copies, so it never reads a Track this thread is changing.
        if kind == 'track':
            data = {**data, 'track': dataclasses.replace(data['track'])}
        elif kind == 'probed':
            data = {**data, 'tracks': [dataclasses.replace(t) for t in data['tracks']]}
        self.event.emit(kind, data)

    def run(self):
        try:
            self.job_done.emit(dl.download(self.url, self.root, self._emit, self.cancel), '')
        except Exception as e:  # yt-dlp errors, bad folders: shown in the status line
            self.job_done.emit(None, str(e).removeprefix('ERROR: '))


class Window(QWidget):
    def __init__(self):
        super().__init__()
        self.settings = QSettings(APP_ID, APP_ID)
        self.queue = deque()
        self.last_clip = None
        self.worker = None
        self.current_url = None
        self.dest = None
        self.items = {}            # track index -> QTreeWidgetItem
        self.progress = {}         # track index -> percent of that track finished
        self.quit_when_idle = False

        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(QIcon.fromTheme('folder-music'))
        self._build_ui()
        if geometry := self.settings.value('geometry'):
            self.restoreGeometry(geometry)
        else:
            self.resize(640, 520)

        self.clip_proc = None
        self.clip_timer = QTimer(self, interval=CLIPBOARD_POLL_MS, timeout=self.poll_clipboard)
        self.clip_timer.start()
        QTimer.singleShot(0, self.poll_clipboard)

    # ----- layout -------------------------------------------------------

    def _build_ui(self):
        self.link = QLineEdit(placeholderText='Copy a YouTube playlist or video link…',
                              clearButtonEnabled=True)
        self.link.returnPressed.connect(self.download_clicked)
        self.download_btn = QPushButton(QIcon.fromTheme('download'), 'Download')
        self.download_btn.clicked.connect(self.download_clicked)
        self.stop_btn = QPushButton(QIcon.fromTheme('process-stop'), 'Stop', enabled=False)
        self.stop_btn.setToolTip('Stop the current download and clear the queue')
        self.stop_btn.clicked.connect(self.stop_clicked)

        self.dir_edit = QLineEdit(self.settings.value('root', str(dl.default_music_dir())))
        self.dir_edit.editingFinished.connect(
            lambda: self.settings.setValue('root', self.dir_edit.text()))
        browse = QPushButton(QIcon.fromTheme('document-open-folder'), 'Browse…')
        browse.clicked.connect(self.browse_clicked)

        self.title = QLabel('Nothing downloading yet')
        font = self.title.font()
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() * 1.15)
        self.title.setFont(font)
        self.title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.open_btn = QPushButton(QIcon.fromTheme('folder-open'), 'Open folder', enabled=False)
        self.open_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.dest))))

        self.bar = QProgressBar(textVisible=True)
        self.bar.setRange(0, 1)
        self.bar.setValue(0)
        self.bar.setFormat('')

        self.tree = QTreeWidget(rootIsDecorated=False, uniformRowHeights=True,
                                alternatingRowColors=True)
        self.tree.setHeaderLabels(['#', 'Title', 'Status'])
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Interactive)
        header.resizeSection(2, 150)

        self.status = QLabel(wordWrap=True)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)

        row = QHBoxLayout()
        row.addWidget(self.link, 1)
        row.addWidget(self.download_btn)
        row.addWidget(self.stop_btn)
        dir_row = QHBoxLayout()
        dir_row.addWidget(QLabel('Save to:'))
        dir_row.addWidget(self.dir_edit, 1)
        dir_row.addWidget(browse)
        title_row = QHBoxLayout()
        title_row.addWidget(self.title, 1)
        title_row.addWidget(self.open_btn)

        layout = QVBoxLayout(self)
        layout.addLayout(row)
        layout.addLayout(dir_row)
        layout.addSpacing(8)
        layout.addLayout(title_row)
        layout.addWidget(self.bar)
        layout.addWidget(self.tree, 1)
        layout.addWidget(self.status)
        self.update_status()

    # ----- clipboard ----------------------------------------------------

    def poll_clipboard(self):
        if not shutil.which('wl-paste'):
            self.clipboard_text(QGuiApplication.clipboard().text())
            return
        if self.clip_proc is not None:
            return  # previous read still running
        proc = QProcess(self)
        proc.finished.connect(lambda code, _status: self._clip_read(proc, code))
        proc.errorOccurred.connect(lambda _err: self._clip_read(proc, -1))
        self.clip_proc = proc
        proc.start('wl-paste', ['--no-newline', '--type', 'text'])

    def _clip_read(self, proc, code):
        if self.clip_proc is not proc:
            return
        self.clip_proc = None
        text = bytes(proc.readAllStandardOutput()).decode('utf-8', 'replace') if code == 0 else ''
        proc.deleteLater()
        self.clipboard_text(text)

    def clipboard_text(self, text):
        if text == self.last_clip:
            return
        self.last_clip = text
        if url := dl.normalize_url(text):
            self.link.setText(url)
            self.status.setText('Link ready. Press Download to start.')

    # ----- queue --------------------------------------------------------

    def download_clicked(self):
        url = dl.normalize_url(self.link.text())
        if not url:
            self.status.setText('That is not a YouTube playlist or video link.')
            return
        self.enqueue(url)

    def enqueue(self, url):
        if url == self.current_url or url in self.queue:
            self.status.setText('That link is already downloading or queued.')
            return
        self.link.setText(url)
        self.queue.append(url)
        self.start_next()

    def start_next(self):
        if self.worker is not None or not self.queue:
            self.update_status()
            return
        root = Path(os.path.expanduser(self.dir_edit.text().strip() or str(dl.default_music_dir())))
        self.settings.setValue('root', str(root))
        self.current_url = self.queue.popleft()
        self.tree.clear()
        self.items.clear()
        self.progress.clear()
        self.title.setText('Reading link…')
        self.open_btn.setEnabled(False)
        self.bar.setRange(0, 0)  # busy until the track count is known
        self.stop_btn.setEnabled(True)

        self.worker = Worker(self.current_url, root, self)
        self.worker.event.connect(self.on_event)
        self.worker.job_done.connect(self.on_job_done)
        self.worker.finished.connect(self.worker.deleteLater)
        self.worker.start()
        self.update_status()

    def stop_clicked(self):
        self.queue.clear()
        if self.worker is not None:
            self.worker.cancel.set()
            self.stop_btn.setEnabled(False)
            self.status.setText('Stopping…')

    # ----- events from the worker ---------------------------------------

    def on_event(self, kind, data):
        if kind == 'probed':
            self.dest = data['dest']
            n = len(data['tracks'])
            self.title.setText(f"{data['title']}  ·  {n} track{'s' * (n != 1)}")
            self.title.setToolTip(str(self.dest))
            self.open_btn.setEnabled(True)
            self.bar.setRange(0, max(n, 1) * 100)
            for t in data['tracks']:
                self.add_item(t)
            self.refresh_bar()
        elif kind == 'track':
            t = data['track']
            item = self.items.get(t.index) or self.add_item(t)
            item.setText(1, t.title)
            self.set_item_status(item, t, data['percent'])
            self.refresh_bar()
            if t.status in (dl.DOWNLOADING, dl.CONVERTING):
                self.tree.scrollToItem(item)
        elif kind == 'message' and data['level'] == 'error':
            self.status.setText(data['text'])

    def add_item(self, track):
        item = QTreeWidgetItem([str(track.index), track.title, ''])
        item.setTextAlignment(0, Qt.AlignRight | Qt.AlignVCenter)
        self.tree.addTopLevelItem(item)
        self.items[track.index] = item
        self.set_item_status(item, track, None)
        return item

    def set_item_status(self, item, t, percent):
        if t.status == dl.DOWNLOADING:
            text = f'{percent}%' if percent is not None else 'downloading…'
        elif t.status == dl.FAILED:
            text = '✗ ' + (t.error or 'failed')
        else:
            text = STATUS_TEXT[t.status]
        item.setText(2, text)
        item.setToolTip(2, t.error or text)
        palette = self.palette()
        if t.status == dl.FAILED:
            color = QColor('#da4453')
        elif t.status == dl.SKIPPED:
            color = palette.color(QPalette.PlaceholderText)
        else:
            color = palette.color(QPalette.Text)
        for col in range(3):
            item.setForeground(col, color)

        if t.status in (dl.DONE, dl.SKIPPED, dl.FAILED):
            self.progress[t.index] = 100
        elif t.status == dl.CONVERTING:
            self.progress[t.index] = 95
        elif t.status == dl.DOWNLOADING:
            self.progress[t.index] = int((percent or 0) * 0.9)
        else:
            self.progress.pop(t.index, None)

    def refresh_bar(self):
        total = len(self.items)
        finished = sum(v == 100 for v in self.progress.values())
        self.bar.setValue(sum(self.progress.values()))
        self.bar.setFormat(f'{finished} / {total}')
        self.update_status()

    def on_job_done(self, summary, error):
        self.worker = None
        url, self.current_url = self.current_url, None
        self.stop_btn.setEnabled(False)
        if self.bar.maximum() == 0:
            self.bar.setRange(0, 1)
            self.bar.setFormat('')
        if summary is None:
            self.title.setText('Could not download that link')
            self.status.setText(error or url)
            self.notify('Download failed', error or url, 'dialog-error')
        else:
            text = summary.describe()
            self.status.setText(f'{summary.title}: {text}. Saved in {summary.dest_dir}')
            self.notify(summary.title, f'{text}\n{summary.dest_dir}')
        if self.quit_when_idle:
            QApplication.quit()
            return
        self.start_next()

    def update_status(self):
        if self.worker is None and not self.queue:
            if not self.status.text():
                self.status.setText('Copy a YouTube playlist link, then press Download.')
            return
        parts = []
        if self.items:
            finished = sum(v == 100 for v in self.progress.values())
            parts.append(f'{finished} of {len(self.items)} finished')
        if self.queue:
            parts.append(f'{len(self.queue)} more link{"s" * (len(self.queue) != 1)} queued')
        if parts:
            self.status.setText(' · '.join(parts))

    def notify(self, title, body, icon='folder-music'):
        if self.isActiveWindow() or not shutil.which('notify-send'):
            return
        QProcess.startDetached('notify-send', ['-a', APP_NAME, '-i', icon, title, body])

    # ----- other UI -----------------------------------------------------

    def browse_clicked(self):
        start = os.path.expanduser(self.dir_edit.text()) or str(dl.default_music_dir())
        folder = QFileDialog.getExistingDirectory(self, 'Save playlists in', start)
        if folder:
            self.dir_edit.setText(folder)
            self.settings.setValue('root', folder)

    def bring_to_front(self, url='', token=''):
        if url and (url := dl.normalize_url(url)):
            self.link.setText(url)
        if token:
            # Qt's Wayland plugin uses this to let KWin raise the window.
            os.environ['XDG_ACTIVATION_TOKEN'] = token
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event):
        self.settings.setValue('geometry', self.saveGeometry())
        if self.worker is None:
            event.accept()
            return
        answer = QMessageBox.question(
            self, APP_NAME, 'A download is still running. Stop it and quit?\n'
            'Finished tracks are kept; the rest can be resumed later.')
        if answer != QMessageBox.Yes:
            event.ignore()
            return
        self.quit_when_idle = True
        self.stop_clicked()
        self.hide()
        event.ignore()  # quit once the worker has wound down


def hand_off_to_running(url):
    """If a window is already open, give it our link and return True."""
    sock = QLocalSocket()
    sock.connectToServer(SERVER_NAME)
    if not sock.waitForConnected(500):
        return False
    msg = {'url': url, 'token': os.environ.get('XDG_ACTIVATION_TOKEN', '')}
    sock.write(json.dumps(msg).encode() + b'\n')
    sock.waitForBytesWritten(1000)
    sock.disconnectFromServer()
    return True


def listen_for_others(window):
    QLocalServer.removeServer(SERVER_NAME)  # clear a socket left by a crash
    server = QLocalServer(window)
    server.setSocketOptions(QLocalServer.UserAccessOption)
    server.listen(SERVER_NAME)

    def on_connection():
        conn = server.nextPendingConnection()

        def read():
            if not conn.canReadLine():
                return
            try:
                msg = json.loads(bytes(conn.readLine()).decode())
            except ValueError:
                msg = {}
            window.bring_to_front(msg.get('url', ''), msg.get('token', ''))
            conn.disconnectFromServer()

        conn.readyRead.connect(read)
        conn.disconnected.connect(conn.deleteLater)
        read()

    server.newConnection.connect(on_connection)
    return server


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_ID)
    app.setApplicationDisplayName(APP_NAME)
    app.setDesktopFileName(APP_ID)
    url = sys.argv[1] if len(sys.argv) > 1 else ''
    if hand_off_to_running(url):
        return 0
    window = Window()
    listen_for_others(window)
    window.show()
    if url:
        window.bring_to_front(url)
    return app.exec()


if __name__ == '__main__':
    sys.exit(main())
