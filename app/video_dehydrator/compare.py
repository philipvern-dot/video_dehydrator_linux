"""Side-by-side preview. Sound is the new file and is not locked to the picture."""

from __future__ import annotations

import os
import subprocess

from PySide6.QtCore import QThread, Qt, QUrl, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPushButton, QSlider, QVBoxLayout,
)

from .engine import COMPARE_FPS, compare_arguments, compare_frame_info

try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
except ImportError:
    QAudioOutput = None
    QMediaPlayer = None


class FramePump(QThread):
    frame = Signal(bytes)
    failed = Signal(str)

    def __init__(self, ffmpeg, left, right, start_sec):
        super().__init__()
        self.ffmpeg = ffmpeg
        self.left = left
        self.right = right
        self.start_sec = start_sec
        self._stop = False
        self.proc = None

    def stop(self):
        self._stop = True
        proc = self.proc
        if proc and proc.poll() is None:
            proc.terminate()

    def run(self):
        info = compare_frame_info()
        need = info['bytes']
        command = [self.ffmpeg] + compare_arguments(self.left, self.right, self.start_sec)
        try:
            self.proc = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            )
        except OSError as exc:
            self.failed.emit(str(exc))
            return
        buf = b''
        try:
            while not self._stop:
                chunk = self.proc.stdout.read(need - len(buf))
                if not chunk:
                    break
                buf += chunk
                if len(buf) >= need:
                    self.frame.emit(bytes(buf[:need]))
                    buf = b''
        finally:
            if self.proc and self.proc.poll() is None:
                self.proc.kill()


class CompareDialog(QDialog):
    def __init__(self, parent, ffmpeg, ffplay, left, right, duration):
        super().__init__(parent)
        self.setWindowTitle('Compare')
        self.resize(1000, 680)
        self.ffmpeg = ffmpeg
        self.ffplay = ffplay
        self.left = left
        self.right = right
        self.duration = max(0.0, float(duration or 0))
        self.pump = None
        self.audio = None
        self.playing = False
        self.player = None
        if QMediaPlayer is not None:
            self.audio_out = QAudioOutput(self)
            self.audio_out.setVolume(1.0)
            self.player = QMediaPlayer(self)
            self.player.setAudioOutput(self.audio_out)
        self.origin = 0.0
        self.frames = 0
        note = QLabel('Sound is the new file, and it will not stay in sync with the picture. Go to the file on disk to confirm the audio.')
        note.setWordWrap(True)
        self.picture = QLabel('Starting the preview...')
        self.picture.setAlignment(Qt.AlignCenter)
        self.picture.setMinimumHeight(420)
        self.picture.setStyleSheet('background:#111; color:#e5e7eb;')
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, max(1, int(self.duration)))
        self.play = QPushButton('Play')
        self.play.clicked.connect(self.toggle)
        close = QPushButton('Close')
        close.clicked.connect(self.reject)
        row = QHBoxLayout()
        row.addWidget(self.play)
        row.addWidget(self.slider, 1)
        row.addWidget(close)
        layout = QVBoxLayout(self)
        layout.addWidget(note)
        layout.addWidget(self.picture, 1)
        layout.addLayout(row)
        self.slider.sliderReleased.connect(self.seek)
        self.start_at(0)

    def start_at(self, second):
        self.stop_pump()
        self.origin = float(second)
        self.frames = 0
        self.pump = FramePump(self.ffmpeg, self.left, self.right, second)
        self.pump.frame.connect(self.show_frame)
        self.pump.failed.connect(self.picture.setText)
        self.pump.start()
        self.playing = True
        self.play.setText('Pause')
        self.start_audio(second)

    def show_frame(self, payload: bytes):
        info = compare_frame_info()
        image = QImage(payload, info['width'], info['height'], info['width'] * 3, QImage.Format_BGR888)
        self.picture.setPixmap(QPixmap.fromImage(image.copy()).scaled(
            self.picture.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation,
        ))
        self.frames += 1
        if self.playing and self.duration > 0 and not self.slider.isSliderDown():
            self.slider.setValue(min(int(self.duration), int(self.origin + (self.frames / COMPARE_FPS))))

    def start_audio(self, second):
        self.stop_audio()
        if self.ffplay and os.path.isfile(self.ffplay):
            try:
                self.audio = subprocess.Popen(
                    [self.ffplay, '-nodisp', '-vn', '-autoexit', '-ss', str(second), '-loglevel', 'error', self.right],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except OSError:
                self.audio = None
            return
        if self.player is None:
            return
        self.player.setSource(QUrl.fromLocalFile(self.right))
        self.player.setPosition(max(0, int(float(second) * 1000)))
        self.player.play()

    def toggle(self):
        if self.playing:
            self.stop_pump()
            self.stop_audio()
            self.playing = False
            self.play.setText('Play')
            return
        self.start_at(self.slider.value())

    def seek(self):
        self.start_at(self.slider.value())

    def stop_pump(self):
        if self.pump:
            self.pump.stop()
            self.pump.wait(1500)
            self.pump = None

    def stop_audio(self):
        if self.audio and self.audio.poll() is None:
            self.audio.terminate()
        self.audio = None
        if self.player is not None:
            self.player.stop()

    def reject(self):
        self.stop_pump()
        self.stop_audio()
        super().reject()

    def closeEvent(self, event):
        self.stop_pump()
        self.stop_audio()
        super().closeEvent(event)
