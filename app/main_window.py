"""主窗口：控制区 + 实时曲线 + 实时读数 + 状态栏。"""

from __future__ import annotations

import logging
import os
import subprocess
import sys

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import __app_name__, __version__, i18n
from .chart import LiveChartWidget
from .config import MAX_INTERVAL, MIN_INTERVAL, AppConfig, decode_geometry, encode_geometry
from .i18n import tr
from .metrics import MetricSample
from .recorder import RecorderError
from .sampler import Sampler

logger = logging.getLogger(__name__)

PRESETS = (0.1, 0.5, 1.0, 5.0)
WINDOW_CHOICES = (
    ("window.60", 60),
    ("window.300", 300),
    ("window.900", 900),
    ("window.3600", 3600),
)
HIGH_FREQ_THRESHOLD = 0.5
BYTES_PER_GB = 1024 ** 3


class MainWindow(QMainWindow):
    """主界面。托盘通知通过 notify 信号交给 app/main.py 处理。"""

    notify = Signal(str, str)  # (标题, 内容)
    language_changed = Signal(str)  # 语言切换后通知托盘等外部组件

    def __init__(self, cfg: AppConfig, sampler: Sampler, parent=None) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._sampler = sampler
        self._force_quit = False
        self._cores_text: str | None = None
        self._tray_actions: tuple | None = None
        self._preset_buttons: list[QPushButton] = []

        self.setWindowTitle(tr("window.title", app=__app_name__, version=__version__))
        self.resize(980, 640)
        geometry = decode_geometry(cfg.window_geometry)
        if geometry:
            self.restoreGeometry(geometry)

        self._build_ui()
        self._wire_sampler()
        self._sync_controls_from_cfg()
        self._update_state(recording=False)

    # ---------- 构建界面 ----------

    def _build_ui(self) -> None:
        central = QWidget(self)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 8)
        root.setSpacing(10)

        root.addWidget(self._build_recorder_group())
        root.addWidget(self._build_realtime_group(), 0)
        self._chart = LiveChartWidget(self)
        root.addWidget(self._chart, 1)
        self.setCentralWidget(central)

        self._status_label = QLabel()
        self._file_label = QLabel()
        self._rows_label = QLabel()
        self._last_write_label = QLabel()
        bar = self.statusBar()
        bar.addWidget(self._status_label)
        bar.addPermanentWidget(self._file_label)
        bar.addPermanentWidget(self._rows_label)
        bar.addPermanentWidget(self._last_write_label)
        for label in (self._status_label, self._file_label, self._rows_label, self._last_write_label):
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)

    def _build_recorder_group(self) -> QGroupBox:
        self._group_recorder = QGroupBox(tr("group.recorder"))
        group = self._group_recorder
        grid = QGridLayout(group)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)

        # --- 第一行：开始/停止 + 采样间隔 + 预设 ---
        self._start_btn = QPushButton(tr("btn.start"))
        self._start_btn.clicked.connect(self._on_start_clicked)
        self._stop_btn = QPushButton(tr("btn.stop"))
        self._stop_btn.clicked.connect(self._on_stop_clicked)

        self._interval_spin = QDoubleSpinBox()
        self._interval_spin.setRange(MIN_INTERVAL, MAX_INTERVAL)
        self._interval_spin.setDecimals(2)
        self._interval_spin.setSingleStep(0.1)
        self._interval_spin.setSuffix(tr("interval.suffix"))
        self._interval_spin.setToolTip(tr("interval.tooltip"))
        self._interval_spin.valueChanged.connect(self._on_interval_changed)

        row1 = QHBoxLayout()
        row1.addWidget(self._start_btn)
        row1.addWidget(self._stop_btn)
        row1.addSpacing(16)
        self._label_interval = QLabel(tr("label.interval"))
        row1.addWidget(self._label_interval)
        row1.addWidget(self._interval_spin)
        self._preset_buttons = []
        for preset in PRESETS:
            btn = QPushButton(f"{preset:g}s")
            btn.setFixedWidth(46)
            btn.setToolTip(tr("preset.tooltip", sec=f"{preset:g}"))
            btn.clicked.connect(lambda _=False, v=preset: self._interval_spin.setValue(v))
            self._preset_buttons.append(btn)
            row1.addWidget(btn)
        self._freq_hint = QLabel(tr("hint.highfreq"))
        self._freq_hint.setStyleSheet("color: #C2410C;")
        self._freq_hint.setVisible(False)
        row1.addWidget(self._freq_hint)
        row1.addStretch(1)

        self._label_window = QLabel(tr("label.window"))
        row1.addWidget(self._label_window)
        self._window_combo = QComboBox()
        for key, seconds in WINDOW_CHOICES:
            self._window_combo.addItem(tr(key), seconds)
        self._window_combo.setCurrentIndex(1)
        self._window_combo.setToolTip(tr("window.tooltip"))
        self._window_combo.currentIndexChanged.connect(
            lambda _: self._chart.set_window_seconds(self._window_combo.currentData())
        )
        row1.addWidget(self._window_combo)

        self._auto_check = QCheckBox(tr("check.auto_scale"))
        self._auto_check.setToolTip(tr("auto_scale.tooltip"))
        self._auto_check.toggled.connect(self._on_auto_scale_toggled)
        row1.addWidget(self._auto_check)

        self._history_btn = QPushButton(tr("btn.history"))
        self._history_btn.clicked.connect(self._on_open_history)
        row1.addWidget(self._history_btn)

        grid.addLayout(row1, 0, 0, 1, 3)

        # --- 第二行：保存目录 ---
        self._dir_edit = QLineEdit()
        self._dir_edit.setReadOnly(True)
        self._dir_edit.setToolTip(tr("dir.tooltip"))
        self._label_save_dir = QLabel(tr("label.save_dir"))
        self._dir_btn = QPushButton(tr("btn.choose"))
        self._dir_btn.clicked.connect(self._on_choose_dir)
        self._open_dir_btn = QPushButton(tr("btn.open_dir"))
        self._open_dir_btn.clicked.connect(self._on_open_dir)

        row2 = QHBoxLayout()
        row2.addWidget(self._label_save_dir)
        row2.addWidget(self._dir_edit, 1)
        row2.addWidget(self._dir_btn)
        row2.addWidget(self._open_dir_btn)
        grid.addLayout(row2, 1, 0, 1, 3)

        # --- 第三行：采集项、行为与语言 ---
        self._label_metrics = QLabel(tr("label.metrics"))
        self._cores_check = QCheckBox(tr("check.cores"))
        self._freq_check = QCheckBox(tr("check.freq"))
        self._swap_check = QCheckBox(tr("check.swap"))
        self._tray_check = QCheckBox(tr("check.tray"))
        for check in (self._cores_check, self._freq_check, self._swap_check):
            check.toggled.connect(self._on_metric_toggled)
        self._tray_check.toggled.connect(self._on_tray_toggled)

        row3 = QHBoxLayout()
        row3.addWidget(self._label_metrics)
        row3.addWidget(self._cores_check)
        row3.addWidget(self._freq_check)
        row3.addWidget(self._swap_check)
        row3.addSpacing(20)
        row3.addWidget(self._tray_check)
        row3.addStretch(1)
        self._label_language = QLabel(tr("label.language"))
        row3.addWidget(self._label_language)
        self._language_combo = QComboBox()
        for code, name in i18n.LANGUAGES.items():
            self._language_combo.addItem(name, code)
        self._language_combo.currentIndexChanged.connect(self._on_language_changed)
        row3.addWidget(self._language_combo)
        grid.addLayout(row3, 2, 0, 1, 3)

        grid.setColumnStretch(0, 1)
        return group

    def _build_realtime_group(self) -> QFrame:
        frame = QFrame()
        frame.setFrameShape(QFrame.StyledPanel)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)

        self._cpu_value = QLabel("--")
        self._cpu_value.setStyleSheet("font-size: 22px; font-weight: 600; color: #3D7EFF;")
        self._mem_value = QLabel("--")
        self._mem_value.setStyleSheet("font-size: 22px; font-weight: 600; color: #C2711B;")

        line = QHBoxLayout()
        self._label_cpu = QLabel(tr("label.cpu"))
        line.addWidget(self._label_cpu)
        line.addWidget(self._cpu_value)
        line.addSpacing(28)
        self._label_mem = QLabel(tr("label.mem"))
        line.addWidget(self._label_mem)
        line.addWidget(self._mem_value)
        line.addSpacing(28)
        self._freq_value = QLabel("--")
        self._freq_value.setStyleSheet("font-size: 16px;")
        self._label_freq = QLabel(tr("label.freq"))
        line.addWidget(self._label_freq)
        line.addWidget(self._freq_value)
        line.addStretch(1)
        layout.addLayout(line)

        self._cores_label = QLabel("")
        self._cores_label.setStyleSheet("color: #4B5563;")
        self._cores_label.setWordWrap(True)
        self._cores_label.setVisible(False)
        layout.addWidget(self._cores_label)
        return frame

    # ---------- 多语言 ----------

    def _on_language_changed(self, _index: int) -> None:
        code = self._language_combo.currentData()
        if not code or code == i18n.current_language():
            return
        i18n.set_language(code)
        self._cfg.language = code
        self._cfg.save()
        self._retranslate()
        self.language_changed.emit(code)

    def _retranslate(self) -> None:
        """按当前语言刷新全部静态文案（动态读数在下次采样时刷新）。"""
        self.setWindowTitle(tr("window.title", app=__app_name__, version=__version__))
        self._group_recorder.setTitle(tr("group.recorder"))
        self._start_btn.setText(tr("btn.start"))
        self._stop_btn.setText(tr("btn.stop"))
        self._label_interval.setText(tr("label.interval"))
        self._interval_spin.setSuffix(tr("interval.suffix"))
        self._interval_spin.setToolTip(tr("interval.tooltip"))
        for btn, preset in zip(self._preset_buttons, PRESETS):
            btn.setToolTip(tr("preset.tooltip", sec=f"{preset:g}"))
        self._freq_hint.setText(tr("hint.highfreq"))
        self._label_window.setText(tr("label.window"))
        for index, (key, _seconds) in enumerate(WINDOW_CHOICES):
            self._window_combo.setItemText(index, tr(key))
        self._window_combo.setToolTip(tr("window.tooltip"))
        self._auto_check.setText(tr("check.auto_scale"))
        self._auto_check.setToolTip(tr("auto_scale.tooltip"))
        self._history_btn.setText(tr("btn.history"))
        self._label_save_dir.setText(tr("label.save_dir"))
        self._dir_edit.setToolTip(tr("dir.tooltip"))
        self._dir_btn.setText(tr("btn.choose"))
        self._open_dir_btn.setText(tr("btn.open_dir"))
        self._label_metrics.setText(tr("label.metrics"))
        self._cores_check.setText(tr("check.cores"))
        self._freq_check.setText(tr("check.freq"))
        self._swap_check.setText(tr("check.swap"))
        self._tray_check.setText(tr("check.tray"))
        self._label_language.setText(tr("label.language"))
        self._label_cpu.setText(tr("label.cpu"))
        self._label_mem.setText(tr("label.mem"))
        self._label_freq.setText(tr("label.freq"))
        if not self._tray_check.isEnabled():
            self._tray_check.setToolTip(tr("tray.disabled.tooltip"))
        self._chart.retranslate()
        self._update_state(self._sampler.is_recording)

    # ---------- 与采样线程联动 ----------

    def _wire_sampler(self) -> None:
        self._sampler.sample_ready.connect(self._on_sample)
        self._sampler.write_error.connect(self._on_write_error)
        self._sampler.file_rotated.connect(self._on_file_rotated)

    def _on_sample(self, sample: MetricSample) -> None:
        self._chart.append_point(
            sample.timestamp_unix_ms / 1000.0, sample.cpu_percent, sample.mem_percent
        )

        self._cpu_value.setText("--" if sample.cpu_percent is None else f"{sample.cpu_percent:.1f}%")
        self._mem_value.setText(self._format_memory(sample))

        if sample.cpu_freq_mhz is None:
            self._freq_value.setText("--")
        else:
            self._freq_value.setText(f"{sample.cpu_freq_mhz / 1000:.2f} GHz")

        self._update_cores_label(sample)

        self._rows_label.setText(tr("status.rows", n=self._sampler.row_count))
        self._last_write_label.setText(tr("status.last_write", time=sample.timestamp_iso[11:]))

    def _on_write_error(self, message: str) -> None:
        # 写盘失败信号来自采样线程，此刻线程可能还没走完收尾流程。
        # 这里主动收尾一次，保证"界面状态"和"线程/文件句柄状态"完全一致。
        if self._sampler.is_recording:
            self._sampler.stop_session()
        self._update_state(recording=False)
        self.notify.emit(tr("notify.record_stopped"), message)
        QMessageBox.warning(self, tr("notify.write_failed"), tr("notify.write_failed.body", error=message))

    def _on_file_rotated(self, path: str) -> None:
        logger.info("数据文件已切分: %s", path)
        self._update_file_label(path)
        self.notify.emit(tr("notify.file_rotated"), os.path.basename(path))

    # ---------- 按钮回调 ----------

    def _on_start_clicked(self) -> None:
        if not self._cfg.ensure_save_dir():
            self._warn_dir_unavailable()
            return
        self._dir_edit.setText(self._cfg.save_dir)
        self._chart.clear()
        try:
            path = self._sampler.start_session(self._cfg.save_dir)
        except RecorderError as exc:
            self.notify.emit(tr("notify.cannot_start"), str(exc))
            QMessageBox.critical(self, tr("notify.cannot_start"), str(exc))
            return
        self._update_file_label(path)
        self._update_state(recording=True)
        self.notify.emit(tr("notify.started"), tr("notify.started.body", name=os.path.basename(path)))

    def _on_stop_clicked(self) -> None:
        self._stop_recording(notify=True)

    def _stop_recording(self, notify: bool) -> None:
        if not self._sampler.is_recording:
            self._update_state(recording=False)
            return
        path = self._sampler.stop_session()
        self._update_state(recording=False)
        if notify and path:
            self.notify.emit(
                tr("notify.stopped"),
                tr("notify.stopped.body", n=self._sampler.row_count, name=os.path.basename(path)),
            )

    def _on_interval_changed(self, value: float) -> None:
        self._cfg.interval_sec = float(value)
        self._cfg.save()
        self._sampler.update_interval(float(value))
        self._freq_hint.setVisible(value < HIGH_FREQ_THRESHOLD)

    def _on_metric_toggled(self, _checked: bool = False) -> None:
        self._cfg.enable_cores = self._cores_check.isChecked()
        self._cfg.enable_freq = self._freq_check.isChecked()
        self._cfg.enable_swap = self._swap_check.isChecked()
        self._cfg.save()
        if not self._cfg.enable_cores:
            self._cores_text = None
            self._cores_label.setVisible(False)
        if not self._cfg.enable_freq:
            self._freq_value.setText("--")

    def _on_tray_toggled(self, checked: bool) -> None:
        self._cfg.minimize_to_tray = bool(checked)
        self._cfg.save()

    def _on_auto_scale_toggled(self, checked: bool) -> None:
        self._cfg.auto_scale = bool(checked)
        self._cfg.save()
        self._chart.set_auto_scale(bool(checked))

    def _on_choose_dir(self) -> None:
        start_dir = self._cfg.save_dir if os.path.isdir(self._cfg.save_dir) else ""
        chosen = QFileDialog.getExistingDirectory(self, tr("dialog.choose_dir"), start_dir)
        if not chosen:
            return
        self._cfg.save_dir = chosen
        self._cfg.save()
        self._dir_edit.setText(chosen)

    def _on_open_dir(self) -> None:
        self._open_in_explorer(self._cfg.save_dir)

    def _on_open_history(self) -> None:
        from .history import HistoryDialog

        dialog = HistoryDialog(self._cfg.save_dir, self, auto_scale=self._cfg.auto_scale)
        dialog.exec()

    # ---------- 状态同步 ----------

    def _sync_controls_from_cfg(self) -> None:
        self._interval_spin.blockSignals(True)
        self._interval_spin.setValue(self._cfg.interval_sec)
        self._interval_spin.blockSignals(False)

        self._cores_check.blockSignals(True)
        self._freq_check.blockSignals(True)
        self._swap_check.blockSignals(True)
        self._tray_check.blockSignals(True)
        self._auto_check.blockSignals(True)
        self._cores_check.setChecked(self._cfg.enable_cores)
        self._freq_check.setChecked(self._cfg.enable_freq)
        self._swap_check.setChecked(self._cfg.enable_swap)
        self._tray_check.setChecked(self._cfg.minimize_to_tray)
        self._auto_check.setChecked(self._cfg.auto_scale)
        for check in (
            self._cores_check,
            self._freq_check,
            self._swap_check,
            self._tray_check,
            self._auto_check,
        ):
            check.blockSignals(False)

        self._freq_hint.setVisible(self._cfg.interval_sec < HIGH_FREQ_THRESHOLD)
        self._dir_edit.setText(self._cfg.save_dir)
        self._chart.set_window_seconds(self._window_combo.currentData())
        self._chart.set_auto_scale(self._cfg.auto_scale)
        index = self._language_combo.findData(i18n.current_language())
        if index >= 0:
            self._language_combo.blockSignals(True)
            self._language_combo.setCurrentIndex(index)
            self._language_combo.blockSignals(False)

    def _update_state(self, recording: bool) -> None:
        self._start_btn.setEnabled(not recording)
        self._stop_btn.setEnabled(recording)
        self._dir_btn.setEnabled(not recording)
        self._history_btn.setEnabled(not recording)
        if recording:
            self._status_label.setText(tr("status.recording"))
            self._status_label.setStyleSheet("color: #B91C1C; font-weight: 600;")
            self._update_file_label(self._sampler.data_path)
            self._rows_label.setText(tr("status.rows", n=self._sampler.row_count))
        else:
            self._status_label.setText(tr("status.stopped"))
            self._status_label.setStyleSheet("color: #6B7280; font-weight: 600;")
        self._sync_tray_actions()

    def _update_file_label(self, path: str | None) -> None:
        if path:
            self._file_label.setText(os.path.basename(path))
            self._file_label.setToolTip(path)
        else:
            self._file_label.setText("")

    def _update_cores_label(self, sample: MetricSample) -> None:
        if not self._cfg.enable_cores or not sample.cores:
            return
        cores = sample.cores
        shown = cores[:16]
        text = "  ".join(f"{idx}:{value:.0f}%" for idx, value in enumerate(shown))
        if len(cores) > len(shown):
            text += tr("cores.more", n=len(cores))
        avg = sum(cores) / len(cores)
        self._cores_label.setText(tr("cores.summary", avg=f"{avg:.1f}", text=text))
        self._cores_label.setVisible(True)

    @staticmethod
    def _format_memory(sample: MetricSample) -> str:
        if sample.mem_percent is None or sample.mem_total_bytes is None:
            return "--"
        used_gb = (sample.mem_used_bytes or 0) / BYTES_PER_GB
        total_gb = sample.mem_total_bytes / BYTES_PER_GB
        return f"{used_gb:.2f} / {total_gb:.2f} GB ({sample.mem_percent:.1f}%)"

    # ---------- 对外接口（供托盘调用） ----------

    def show_and_raise(self) -> None:
        """从托盘恢复窗口。"""
        self.show()
        if self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()

    def set_tray_actions(self, start_action, stop_action) -> None:
        """由 app/main.py 注入托盘菜单项，便于同步启用状态。"""
        self._tray_actions = (start_action, stop_action)
        self._sync_tray_actions()

    def _sync_tray_actions(self) -> None:
        if not self._tray_actions:
            return
        recording = self._sampler.is_recording
        start_action, stop_action = self._tray_actions
        start_action.setEnabled(not recording)
        stop_action.setEnabled(recording)

    def open_data_dir(self) -> None:
        self._on_open_dir()

    def open_history(self) -> None:
        self._on_open_history()

    def start_recording(self) -> None:
        if not self._sampler.is_recording:
            self._on_start_clicked()

    def stop_recording(self) -> None:
        self._stop_recording(notify=True)

    def is_recording(self) -> bool:
        return self._sampler.is_recording

    def prepare_quit(self) -> None:
        """真正退出前调用：标记强制退出并保存窗口几何。"""
        self._force_quit = True
        self._save_geometry()

    def disable_tray_minimize(self) -> None:
        """系统不支持托盘时禁用"最小化到托盘"，否则关窗后无法再打开。"""
        self._tray_check.blockSignals(True)
        self._tray_check.setChecked(False)
        self._tray_check.blockSignals(False)
        self._tray_check.setEnabled(False)
        self._tray_check.setToolTip(tr("tray.disabled.tooltip"))

    def _save_geometry(self) -> None:
        try:
            encoded = encode_geometry(self.saveGeometry())
        except Exception:
            logger.debug("保存窗口几何失败", exc_info=True)
            return
        if encoded and encoded != self._cfg.window_geometry:
            self._cfg.window_geometry = encoded
            self._cfg.save()

    # ---------- 生命周期 ----------

    def closeEvent(self, event) -> None:
        if not self._force_quit and self._cfg.minimize_to_tray:
            event.ignore()
            self.hide()
            self.notify.emit(tr("notify.background"), tr("notify.background.body"))
            return
        self._save_geometry()
        event.accept()
        # app 设置了 setQuitOnLastWindowClosed(False)（为了支持托盘驻留），
        # 所以这里必须显式退出，否则用户关闭窗口后进程会变成看不见的残留进程。
        QApplication.instance().quit()

    # ---------- 工具 ----------

    @staticmethod
    def _open_in_explorer(path: str) -> None:
        try:
            os.makedirs(path, exist_ok=True)
            if sys.platform == "win32":
                os.startfile(path)  # noqa: S606 - Windows 专用，打开资源管理器
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except OSError as exc:
            logger.warning("打开目录失败: %s", exc)

    def _warn_dir_unavailable(self) -> None:
        message = tr("error.dir_unavailable.body", dir=self._cfg.save_dir)
        self.notify.emit(tr("error.dir_unavailable"), message)
        QMessageBox.critical(self, tr("error.dir_unavailable"), message)


__all__ = ["MainWindow"]
