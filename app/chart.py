"""实时 / 历史曲线控件（CPU 与内存占用率双曲线）。

交互约定：
- 纵轴永远限制在 0% ~ 100%，不会拖到负值或超过 100%。
- 默认纵轴固定 0 ~ 100，鼠标只能横向平移 / 缩放（历史图表同样如此）。
- 开启“纵轴自适应”后，纵轴高度自动贴合当前可见数据的最低 / 最高值，
  并用虚线标出谷值与峰值；只有该模式允许纵向缩放（Ctrl+滚轮或右键拖拽），
  但任何时候都不允许纵向平移，图表的高度位置保持固定。
- 鼠标悬停时显示虚线竖线与浮窗，标注该时刻的 CPU / 内存占用。
- 所有配色取自 QPalette，跟随窗口明暗主题，不再写死白色背景。
"""

from __future__ import annotations

import logging
from datetime import datetime

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from .i18n import tr, tr_raw

logger = logging.getLogger(__name__)

CPU_COLOR = "#3D7EFF"
MEM_COLOR = "#FF9F43"
# 暗色主题下适当提亮，保证曲线对比度
CPU_COLOR_DARK = "#7CB4FF"
MEM_COLOR_DARK = "#FFC078"

Y_MIN = 0.0
Y_MAX = 100.0

# 实时模式的缓冲上限，仅作内存兜底；历史数据完整载入，不截断
MAX_POINTS = 200_000

DEFAULT_WINDOW_SEC = 300.0


class _ChartViewBox(pg.ViewBox):
    """限制图表的鼠标交互。

    - 纵向平移永远禁用（高度位置固定）。
    - 纵轴自适应关闭时，滚轮 / 右键拖拽只缩放 X 轴。
    - 纵轴自适应开启时，允许纵向缩放（Ctrl+滚轮可只缩放纵轴）。
    """

    def __init__(self, chart: "LiveChartWidget", *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._chart = chart

    def wheelEvent(self, ev, axis=None) -> None:
        auto = self._chart.auto_scale_enabled()
        if axis == 1:
            if not auto:
                ev.ignore()
                return
            super().wheelEvent(ev, axis=1)
            return
        if not auto:
            super().wheelEvent(ev, axis=0)
            return
        if ev.modifiers() & Qt.KeyboardModifier.ControlModifier:
            super().wheelEvent(ev, axis=1)
        else:
            super().wheelEvent(ev)

    def mouseDragEvent(self, ev, axis=None) -> None:
        if ev.button() in (
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.MiddleButton,
        ):
            # 平移只作用于 X 轴
            super().mouseDragEvent(ev, axis=0)
        elif self._chart.auto_scale_enabled():
            super().mouseDragEvent(ev)
        else:
            super().mouseDragEvent(ev, axis=0)


class LiveChartWidget(QWidget):
    """CPU / 内存占用率曲线。"""

    def __init__(self, parent=None, date_axis: bool = False, window_sec: float = DEFAULT_WINDOW_SEC):
        super().__init__(parent)
        self._date_axis = date_axis
        self._window_sec = float(window_sec)
        self._follow = True  # 实时模式自动跟随最新
        self._auto_scale = False

        # 用于“从设备休眠/长时间暂停后继续”等场景计算相对时间
        self._t0: float | None = None
        self._t_last: float | None = None

        # X 轴显示值（实时为相对秒，历史为 Unix 秒）
        self._t = np.empty(0, dtype=float)
        self._cpu = np.empty(0, dtype=float)
        self._mem = np.empty(0, dtype=float)
        # 每个点对应的真实 Unix 时间戳，供悬停浮窗显示
        self._wall = np.empty(0, dtype=float)

        self._vb = _ChartViewBox(self)
        axis_items = {"bottom": pg.DateAxisItem(orientation="bottom")} if date_axis else None
        self.plot = pg.PlotWidget(viewBox=self._vb, axisItems=axis_items)
        self.plot.setMenuEnabled(False)
        self.plot.showGrid(x=True, y=True, alpha=0.2)
        self.plot.setLabel("left", tr("chart.y_label"), units="%")
        self.plot.setLabel(
            "bottom",
            tr("chart.x_time") if date_axis else tr("chart.x_elapsed"),
            units=None if date_axis else "s",
        )
        self.plot.setYRange(Y_MIN, Y_MAX, padding=0)
        self.plot.hideButtons()

        # 纵轴限制：视图范围不能超出 0 ~ 100
        self._vb.setLimits(yMin=Y_MIN, yMax=Y_MAX)
        self._vb.setMouseEnabled(x=True, y=False)
        self._vb.sigXRangeChanged.connect(self._on_x_range_changed)

        self._legend = self.plot.addLegend(offset=(-10, 10))
        self._cpu_curve = self.plot.plot([], [], pen=pg.mkPen(CPU_COLOR, width=2), name="CPU")
        self._mem_curve = self.plot.plot([], [], pen=pg.mkPen(MEM_COLOR, width=2), name=tr("chart.mem"))
        for curve in (self._cpu_curve, self._mem_curve):
            # pyqtgraph 0.13/0.14 的形参为 (ds, auto, method)：
            # pyqtgraph 在软件层面按下采样方法抽稀，点数远超像素宽度时仍然流畅。
            curve.setDownsampling(auto=True, method="peak")
            curve.setClipToView(True)

        # 自适应模式下的谷值 / 峰值标记线
        self._low_line = pg.InfiniteLine(
            angle=0,
            movable=False,
            pen=pg.mkPen("#0EA5E9", width=1, style=Qt.PenStyle.DashLine),
            label=tr_raw("chart.low"),
            labelOpts={"position": 0.06, "color": "#0EA5E9"},
        )
        self._high_line = pg.InfiniteLine(
            angle=0,
            movable=False,
            pen=pg.mkPen("#F97316", width=1, style=Qt.PenStyle.DashLine),
            label=tr_raw("chart.high"),
            labelOpts={"position": 0.06, "color": "#F97316"},
        )
        for line in (self._low_line, self._high_line):
            line.setZValue(20)
            line.setVisible(False)
            self.plot.addItem(line)

        # 悬停竖线
        self._hover_line = pg.InfiniteLine(
            angle=90,
            movable=False,
            pen=pg.mkPen("#64748B", width=1, style=Qt.PenStyle.DashLine),
        )
        self._hover_line.setZValue(30)
        self._hover_line.setVisible(False)
        self.plot.addItem(self._hover_line)

        # 悬停浮窗（覆盖在图表上的普通 QLabel，不拦截鼠标）
        self._tooltip = QLabel(self)
        self._tooltip.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._tooltip.setVisible(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.plot)

        self._mouse_proxy = pg.SignalProxy(
            self.plot.scene().sigMouseMoved, rateLimit=60, slot=self._on_mouse_moved
        )
        self.plot.viewport().installEventFilter(self)
        self.plot.installEventFilter(self)

        self._apply_theme()

    # ---------- 实时模式 ----------

    def append_point(self, t: float, cpu: float | None, mem: float | None) -> None:
        """追加一个数据点。t 为 Unix 时间戳（秒）。"""
        t = float(t)
        if self._t0 is None:
            self._t0 = t
        self._t_last = t
        x = t if self._date_axis else (t - self._t0)
        self._t = np.append(self._t, x)
        self._cpu = np.append(self._cpu, float("nan") if cpu is None else float(cpu))
        self._mem = np.append(self._mem, float("nan") if mem is None else float(mem))
        self._wall = np.append(self._wall, t)
        if self._t.size > MAX_POINTS:
            self._t = self._t[-MAX_POINTS:]
            self._cpu = self._cpu[-MAX_POINTS:]
            self._mem = self._mem[-MAX_POINTS:]
            self._wall = self._wall[-MAX_POINTS:]
        self._refresh_live()

    def set_window_seconds(self, seconds: float) -> None:
        self._window_sec = max(5.0, float(seconds))
        self._refresh_live()

    def set_follow(self, follow: bool) -> None:
        self._follow = bool(follow)
        self._refresh_live()

    def _refresh_live(self) -> None:
        if self._t.size == 0:
            return
        self._cpu_curve.setData(self._t, self._cpu)
        self._mem_curve.setData(self._t, self._mem)
        if self._follow:
            right = float(self._t[-1])
            self.plot.setXRange(right - self._window_sec, right, padding=0)
        if self._auto_scale:
            self._update_auto_range()

    # ---------- 历史模式 ----------

    def set_series(self, times: list[float], cpu: list[float], mem: list[float]) -> None:
        """一次性载入整段序列（times 为 Unix 秒）。"""
        times_array = np.asarray(times, dtype=float)
        self._t = times_array
        self._wall = times_array.copy()
        self._cpu = np.asarray(cpu, dtype=float)
        self._mem = np.asarray(mem, dtype=float)
        self._t0 = float(times_array[0]) if times_array.size else None
        self._t_last = float(times_array[-1]) if times_array.size else None

        self._cpu_curve.setData(self._t, self._cpu)
        self._mem_curve.setData(self._t, self._mem)
        if times_array.size:
            self.plot.setXRange(float(times_array[0]), float(times_array[-1]), padding=0.02)
        if self._auto_scale:
            self._update_auto_range()
        else:
            self.plot.setYRange(Y_MIN, Y_MAX, padding=0)
        self._hide_hover()

    # ---------- 纵轴自适应 ----------

    def auto_scale_enabled(self) -> bool:
        return self._auto_scale

    def set_auto_scale(self, enabled: bool) -> None:
        """开启后纵轴贴合当前可见数据的谷值/峰值；关闭后恢复固定 0~100。"""
        enabled = bool(enabled)
        if enabled == self._auto_scale:
            if enabled:
                self._update_auto_range()
            return

        self._auto_scale = enabled
        # 自适应模式下才允许纵向缩放；平移仍被 _ChartViewBox 限制为横向
        self._vb.setMouseEnabled(x=True, y=enabled)
        if enabled:
            self._update_auto_range()
        else:
            self._low_line.setVisible(False)
            self._high_line.setVisible(False)
            self.plot.setYRange(Y_MIN, Y_MAX, padding=0)

    def _on_x_range_changed(self, *_args) -> None:
        if self._auto_scale:
            self._update_auto_range()

    def _update_auto_range(self) -> None:
        if not self._auto_scale:
            return
        stats = self._visible_min_max()
        if stats is None:
            self._low_line.setVisible(False)
            self._high_line.setVisible(False)
            self.plot.setYRange(Y_MIN, Y_MAX, padding=0)
            return

        low, high = stats
        pad = max(1.0, (high - low) * 0.08)
        bottom = max(Y_MIN, low - pad)
        top = min(Y_MAX, high + pad)
        if top - bottom < 1.0:
            center = (bottom + top) / 2.0
            bottom = max(Y_MIN, center - 0.5)
            top = min(Y_MAX, center + 0.5)
        if top <= bottom:
            top = min(Y_MAX, bottom + 1.0)

        self.plot.setYRange(bottom, top, padding=0)
        self._low_line.setPos(low)
        self._high_line.setPos(high)
        self._low_line.setVisible(True)
        self._high_line.setVisible(True)

    def _visible_min_max(self) -> tuple[float, float] | None:
        """当前可见 X 区间内所有有效数据的最低值与最高值。"""
        if self._t.size == 0:
            return None
        x0, x1 = self._vb.viewRange()[0]
        if x1 < x0:
            x0, x1 = x1, x0
        left = int(np.searchsorted(self._t, x0, side="left"))
        right = int(np.searchsorted(self._t, x1, side="right"))
        if right <= left:
            return None

        lows: list[float] = []
        highs: list[float] = []
        for series in (self._cpu[left:right], self._mem[left:right]):
            valid = series[np.isfinite(series)]
            if valid.size:
                lows.append(float(valid.min()))
                highs.append(float(valid.max()))
        if not lows:
            return None
        return min(lows), max(highs)

    # ---------- 悬停交互 ----------

    def _on_mouse_moved(self, event) -> None:
        pos = event[0] if isinstance(event, (tuple, list)) else event
        if self._t.size == 0 or not self._vb.sceneBoundingRect().contains(pos):
            self._hide_hover()
            return

        view_point = self._vb.mapSceneToView(pos)
        x = float(view_point.x())
        if x < float(self._t[0]) or x > float(self._t[-1]):
            self._hide_hover()
            return

        index = int(np.searchsorted(self._t, x))
        if index <= 0:
            index = 0
        elif index >= self._t.size:
            index = int(self._t.size) - 1
        else:
            left_index = index - 1
            if abs(float(self._t[left_index]) - x) <= abs(float(self._t[index]) - x):
                index = left_index
        self._show_hover(index, pos)

    def _show_hover(self, index: int, pos) -> None:
        self._hover_line.setPos(float(self._t[index]))
        self._hover_line.setVisible(True)

        wall = float(self._wall[index]) if self._wall.size else float(self._t[index])
        time_text = datetime.fromtimestamp(wall).strftime("%m-%d %H:%M:%S")
        cpu_text = "--" if not np.isfinite(self._cpu[index]) else f"{float(self._cpu[index]):.1f}%"
        mem_text = "--" if not np.isfinite(self._mem[index]) else f"{float(self._mem[index]):.1f}%"
        self._tooltip.setText(tr("chart.tooltip", time=time_text, cpu=cpu_text, mem=mem_text))
        self._tooltip.adjustSize()

        plot_pos = self.plot.mapFromScene(pos)
        margin = 14
        tip_width = self._tooltip.width()
        tip_height = self._tooltip.height()
        x = plot_pos.x() + margin
        y = plot_pos.y() - tip_height - margin
        if x + tip_width > self.width():
            x = plot_pos.x() - tip_width - margin
        if y < 0:
            y = plot_pos.y() + margin
        x = max(4, min(x, self.width() - tip_width - 4))
        y = max(4, min(y, self.height() - tip_height - 4))
        self._tooltip.move(int(x), int(y))
        self._tooltip.show()
        self._tooltip.raise_()

    def _hide_hover(self) -> None:
        self._hover_line.setVisible(False)
        self._tooltip.setVisible(False)

    def eventFilter(self, obj, event) -> bool:
        if obj in (self.plot, self.plot.viewport()) and event.type() in (
            QEvent.Type.Leave,
            QEvent.Type.Hide,
        ):
            self._hide_hover()
        return super().eventFilter(obj, event)

    # ---------- 主题 ----------

    def changeEvent(self, event) -> None:
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.StyleChange):
            self._apply_theme()
        super().changeEvent(event)

    @staticmethod
    def _rgba(color: QColor, alpha: int) -> str:
        return f"rgba({color.red()},{color.green()},{color.blue()},{alpha})"

    def _apply_theme(self) -> None:
        """让图表背景、坐标轴、图例、浮窗全部跟随当前调色板。"""
        if not hasattr(self, "_legend"):
            return
        palette = self.palette()
        background = palette.color(QPalette.ColorRole.Base)
        if not background.isValid():
            background = palette.color(QPalette.ColorRole.Window)
        text_color = palette.color(QPalette.ColorRole.Text)
        dark = background.lightness() < 128

        self.plot.setBackground(pg.mkBrush(background))
        self.plot.showGrid(x=True, y=True, alpha=0.16 if dark else 0.22)

        for name in ("left", "bottom"):
            axis = self.plot.getAxis(name)
            axis.setPen(pg.mkPen(text_color, width=1, alpha=130 if dark else 150))
            axis.setTextPen(pg.mkPen(text_color))

        self._legend.setLabelTextColor(text_color)
        self._legend.setBrush(pg.mkBrush(background.red(), background.green(), background.blue(), 220))
        self._legend.setPen(pg.mkPen(text_color, alpha=70))

        self._cpu_curve.setPen(pg.mkPen(CPU_COLOR_DARK if dark else CPU_COLOR, width=2))
        self._mem_curve.setPen(pg.mkPen(MEM_COLOR_DARK if dark else MEM_COLOR, width=2))

        self._hover_line.setPen(
            pg.mkPen(text_color, width=1, style=Qt.PenStyle.DashLine, alpha=180)
        )

        low_color = "#38BDF8" if dark else "#0284C7"
        high_color = "#FB923C" if dark else "#EA580C"
        for line, color in ((self._low_line, low_color), (self._high_line, high_color)):
            line.setPen(pg.mkPen(color, width=1, style=Qt.PenStyle.DashLine))
            label = getattr(line, "label", None)
            if label is not None:
                label.setColor(text_color)
                label.fill = pg.mkBrush(background.red(), background.green(), background.blue(), 215)
                label.border = pg.mkPen(text_color, alpha=60)
                label.update()

        tip_background = palette.color(QPalette.ColorRole.ToolTipBase)
        tip_text = palette.color(QPalette.ColorRole.ToolTipText)
        self._tooltip.setStyleSheet(
            "QLabel {"
            f"background-color: {self._rgba(tip_background, 238)};"
            f"color: {tip_text.name()};"
            f"border: 1px solid {self._rgba(text_color, 70)};"
            "border-radius: 6px;"
            "padding: 6px 9px;"
            "font-size: 12px;"
            "}"
        )

    # ---------- 通用 ----------

    def retranslate(self) -> None:
        """语言切换后刷新坐标轴、图例与极值标记。"""
        self.plot.setLabel("left", tr("chart.y_label"), units="%")
        self.plot.setLabel(
            "bottom",
            tr("chart.x_time") if self._date_axis else tr("chart.x_elapsed"),
            units=None if self._date_axis else "s",
        )
        self._legend.clear()
        self._legend.addItem(self._cpu_curve, "CPU")
        self._legend.addItem(self._mem_curve, tr("chart.mem"))
        for line, key in ((self._low_line, "chart.low"), (self._high_line, "chart.high")):
            if getattr(line, "label", None) is not None:
                line.label.setFormat(tr_raw(key))
        self._hide_hover()

    def clear(self) -> None:
        self._t = np.empty(0, dtype=float)
        self._cpu = np.empty(0, dtype=float)
        self._mem = np.empty(0, dtype=float)
        self._wall = np.empty(0, dtype=float)
        self._t0 = None
        self._t_last = None
        self._cpu_curve.setData([], [])
        self._mem_curve.setData([], [])
        self._low_line.setVisible(False)
        self._high_line.setVisible(False)
        self._hide_hover()
        self.plot.setYRange(Y_MIN, Y_MAX, padding=0)


__all__ = ["LiveChartWidget", "CPU_COLOR", "MEM_COLOR", "Y_MIN", "Y_MAX"]
