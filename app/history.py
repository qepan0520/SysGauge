"""历史数据读取：打开已记录的 CSV，绘制曲线并给出统计摘要。

容错策略（对应"断电后最后一行可能不完整"）：
- 字段数少于表头的行、时间戳无法解析的行、空行，一律跳过并计数，不中断加载。
- 单个数值列解析失败时该点记为缺失（曲线断开），不影响其它列。
"""

from __future__ import annotations

import csv
import logging
import os
from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from .i18n import tr

from .chart import LiveChartWidget

logger = logging.getLogger(__name__)

BYTES_PER_GB = 1024 ** 3
BIG_FILE_ROWS = 500_000


class HistoryDialog(QDialog):
    """读取并查看历史 CSV 文件。"""

    def __init__(self, default_dir: str, parent=None, auto_scale: bool = False) -> None:
        super().__init__(parent)
        self._default_dir = default_dir
        self._current_path: str | None = None
        self._auto_scale_default = bool(auto_scale)

        self.setWindowTitle(tr("history.title"))
        self.resize(1000, 660)
        self._build_ui()

    # ---------- 界面 ----------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        toolbar = QHBoxLayout()
        self._open_btn = QPushButton(tr("history.open"))
        self._open_btn.clicked.connect(self._on_open)
        toolbar.addWidget(self._open_btn)

        self._reload_btn = QPushButton(tr("history.reload"))
        self._reload_btn.setEnabled(False)
        self._reload_btn.clicked.connect(lambda: self._load(self._current_path))
        toolbar.addWidget(self._reload_btn)

        self._auto_check = QCheckBox(tr("check.auto_scale"))
        self._auto_check.setToolTip(tr("auto_scale.tooltip"))
        self._auto_check.setChecked(self._auto_scale_default)
        toolbar.addWidget(self._auto_check)

        self._path_label = QLabel(tr("history.no_file"))
        self._path_label.setStyleSheet("color: #4B5563;")
        self._path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        toolbar.addWidget(self._path_label, 1)
        root.addLayout(toolbar)

        self._stats_group = QGroupBox(tr("history.summary"))
        self._stats_grid = QGridLayout(self._stats_group)
        self._stats_grid.setHorizontalSpacing(18)
        self._stats_grid.setVerticalSpacing(6)
        root.addWidget(self._stats_group)

        self._notice = QLabel("")
        self._notice.setStyleSheet("color: #C2410C;")
        self._notice.setVisible(False)
        root.addWidget(self._notice)

        frame = QFrame()
        frame.setFrameShape(QFrame.StyledPanel)
        frame_layout = QVBoxLayout(frame)
        frame_layout.setContentsMargins(0, 0, 0, 0)
        self._chart = LiveChartWidget(self, date_axis=True)
        self._chart.set_auto_scale(self._auto_check.isChecked())
        self._auto_check.toggled.connect(self._chart.set_auto_scale)
        frame_layout.addWidget(self._chart)
        root.addWidget(frame, 1)

    def _set_stats(self, rows: list[tuple[str, str]]) -> None:
        while self._stats_grid.count():
            item = self._stats_grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for index, (key, value) in enumerate(rows):
            row, col = divmod(index, 4)
            key_label = QLabel(f"{key}{tr('history.key_suffix')}")
            key_label.setStyleSheet("color: #6B7280;")
            value_label = QLabel(value)
            value_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self._stats_grid.addWidget(key_label, row, col * 2)
            self._stats_grid.addWidget(value_label, row, col * 2 + 1)

    # ---------- 交互 ----------

    def _on_open(self) -> None:
        start_dir = self._default_dir if os.path.isdir(self._default_dir) else ""
        path, _ = QFileDialog.getOpenFileName(
            self, tr("history.choose"), start_dir, tr("history.filter")
        )
        if path:
            self._load(path)

    # ---------- 加载与解析 ----------

    def _load(self, path: str | None) -> None:
        if not path or not os.path.isfile(path):
            return
        self._current_path = path
        self._path_label.setText(path)
        self._reload_btn.setEnabled(True)

        QGuiApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            data = parse_csv(path)
        finally:
            QGuiApplication.restoreOverrideCursor()

        if data.error:
            self._chart.clear()
            self._set_stats([(tr("history.error"), data.error)])
            self._notice.setText(tr("history.cannot_read", error=data.error))
            self._notice.setVisible(True)
            return

        if data.count == 0:
            self._chart.clear()
            self._set_stats([(tr("history.samples"), "0")])
            self._notice.setText(tr("history.empty"))
            self._notice.setVisible(True)
            return

        self._chart.set_series(data.times, data.cpu, data.mem)
        self._set_stats(self._build_stats(data))
        self._render_notices(data)

    def _build_stats(self, data) -> list[tuple[str, str]]:
        rows = [
            (tr("history.samples"), f"{data.count}"),
            (tr("history.start"), data.start_text),
            (tr("history.end"), data.end_text),
            (tr("history.duration"), data.duration_text),
        ]
        if data.cpu_count:
            rows += [
                (tr("history.cpu_mean"), f"{data.cpu_mean:.1f}%"),
                (tr("history.cpu_max"), f"{data.cpu_max:.1f}%"),
                (tr("history.cpu_min"), f"{data.cpu_min:.1f}%"),
            ]
        else:
            rows.append(("CPU", tr("history.no_data")))
        if data.mem_count:
            rows += [
                (tr("history.mem_mean"), f"{data.mem_mean:.1f}%"),
                (tr("history.mem_max"), f"{data.mem_max:.1f}%"),
                (tr("history.mem_min"), f"{data.mem_min:.1f}%"),
                (
                    tr("history.mem_mean_gb"),
                    f"{data.mem_used_mean / BYTES_PER_GB:.2f} / {data.mem_total_gb:.2f} GB",
                ),
            ]
        else:
            rows.append((tr("chart.mem"), tr("history.no_data")))
        return rows

    def _render_notices(self, data) -> None:
        messages = []
        if data.skipped_rows:
            messages.append(tr("history.skipped", n=data.skipped_rows))
        if data.count >= BIG_FILE_ROWS:
            messages.append(tr("history.downsampled", n=data.count))
        self._notice.setText("  ".join(messages))
        self._notice.setVisible(bool(messages))


# ---------- 解析结果 ----------


class _ParsedData:
    def __init__(self) -> None:
        self.times: list[float] = []
        self.cpu: list[float] = []
        self.mem: list[float] = []
        self.count = 0
        self.skipped_rows = 0
        self.error: str | None = None
        self.start_text = "--"
        self.end_text = "--"
        self.duration_text = "--"
        self.cpu_count = 0
        self.cpu_mean = self.cpu_max = self.cpu_min = 0.0
        self.mem_count = 0
        self.mem_mean = self.mem_max = self.mem_min = 0.0
        self.mem_used_mean = 0.0
        self.mem_total_gb = 0.0


def _to_float(text: str) -> float | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _parse_timestamp(row: dict[str, str]) -> float | None:
    """优先使用 Unix 毫秒时间戳，退化到 ISO 文本。"""
    raw_ms = (row.get("timestamp_unix_ms") or "").strip()
    if raw_ms:
        try:
            return int(float(raw_ms)) / 1000.0
        except ValueError:
            pass
    raw_iso = (row.get("timestamp_iso") or "").strip()
    if raw_iso:
        try:
            return datetime.fromisoformat(raw_iso).timestamp()
        except ValueError:
            return None
    return None


def _format_seconds(seconds: float) -> str:
    seconds = int(round(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return tr("duration.hms", h=hours, m=minutes, s=secs)
    if minutes:
        return tr("duration.ms", m=minutes, s=secs)
    return tr("duration.s", s=secs)


def parse_csv(path: str) -> _ParsedData:
    """解析 CSV。任何结构性异常都被降级为"跳过该行"。"""
    data = _ParsedData()
    cpu_values: list[float] = []
    mem_values: list[float] = []
    mem_used_values: list[int] = []
    mem_total_bytes: int | None = None

    try:
        with open(path, "r", newline="", encoding="utf-8", errors="replace") as fh:
            reader = csv.reader(fh)
            try:
                header = next(reader)
            except StopIteration:
                data.error = tr("parse.error.empty")
                return data

            header = [h.strip() for h in header]
            index = {name: i for i, name in enumerate(header)}
            if "timestamp_unix_ms" not in index and "timestamp_iso" not in index:
                data.error = tr("parse.error.no_timestamp")
                return data

            width = len(header)

            for raw_row in reader:
                if not raw_row or all(not cell.strip() for cell in raw_row):
                    continue
                # 末行可能因断电被截断：字段数不足直接跳过
                if len(raw_row) < width:
                    data.skipped_rows += 1
                    continue

                row = {name: raw_row[pos] for name, pos in index.items()}
                timestamp = _parse_timestamp(row)
                if timestamp is None:
                    data.skipped_rows += 1
                    continue

                cpu = _to_float(row.get("cpu_percent", ""))
                mem = _to_float(row.get("mem_percent", ""))
                used = _to_float(row.get("mem_used_bytes", ""))
                total = _to_float(row.get("mem_total_bytes", ""))

                if used is not None:
                    mem_used_values.append(int(used))
                if total:
                    mem_total_bytes = int(total)

                data.times.append(timestamp)
                data.cpu.append(float("nan") if cpu is None else cpu)
                data.mem.append(float("nan") if mem is None else mem)
                if cpu is not None:
                    cpu_values.append(cpu)
                if mem is not None:
                    mem_values.append(mem)
                data.count += 1
    except OSError as exc:
        data.error = str(exc)
        return data
    except csv.Error as exc:
        data.error = tr("parse.error.csv", error=exc)
        return data

    if data.count:
        start = datetime.fromtimestamp(data.times[0])
        end = datetime.fromtimestamp(data.times[-1])
        data.start_text = start.strftime("%Y-%m-%d %H:%M:%S")
        data.end_text = end.strftime("%Y-%m-%d %H:%M:%S")
        data.duration_text = _format_seconds(data.times[-1] - data.times[0])

    _fill_stats(data, cpu_values, mem_values, mem_used_values, mem_total_bytes)
    return data


def _fill_stats(
    data: _ParsedData,
    cpu_values: list[float],
    mem_values: list[float],
    mem_used_values: list[int],
    mem_total_bytes: int | None,
) -> None:
    if cpu_values:
        data.cpu_count = len(cpu_values)
        data.cpu_mean = sum(cpu_values) / len(cpu_values)
        data.cpu_max = max(cpu_values)
        data.cpu_min = min(cpu_values)
    if mem_values:
        data.mem_count = len(mem_values)
        data.mem_mean = sum(mem_values) / len(mem_values)
        data.mem_max = max(mem_values)
        data.mem_min = min(mem_values)
    if mem_used_values:
        data.mem_used_mean = sum(mem_used_values) / len(mem_used_values)
    if mem_total_bytes:
        data.mem_total_gb = mem_total_bytes / BYTES_PER_GB


__all__ = ["HistoryDialog", "parse_csv"]
