"""程序入口：日志、异常兜底、单实例、系统托盘装配。"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from . import __app_name__, __version__
from .config import AppConfig, log_dir
from .main_window import MainWindow
from .recorder import CsvRecorder
from .sampler import Sampler

logger = logging.getLogger(__name__)

SINGLE_INSTANCE_KEY = "SysGauge_SingleInstance_9f2c"
ICON_SIZE = 64

# 托盘图标随 CPU 占用率变色：绿 → 黄 → 红
COLOR_LOW = "#10B981"
COLOR_MID = "#F59E0B"
COLOR_HIGH = "#EF4444"
COLOR_IDLE = "#6B7280"

_ICON_CACHE: dict[str, QIcon] = {}


# ---------- 日志与异常 ----------


def setup_logging() -> str:
    directory = log_dir()
    os.makedirs(directory, exist_ok=True)
    log_path = os.path.join(directory, "sysgauge.log")

    root = logging.getLogger()
    root.setLevel(logging.WARNING)

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)
    root.addHandler(console)

    # 只把本程序的日志放开到 DEBUG，避免第三方库刷屏
    logging.getLogger("app").setLevel(logging.DEBUG)
    logger.info("%s v%s 启动，日志: %s", __app_name__, __version__, log_path)
    return log_path


def install_excepthook() -> None:
    def hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logger.critical("未捕获异常", exc_info=(exc_type, exc_value, exc_tb))
        try:
            QMessageBox.critical(
                None,
                "程序异常",
                f"发生未预期的错误：{exc_value}\n\n详情已写入日志：{log_dir()}",
            )
        except Exception:  # pragma: no cover - 异常处理中不再抛异常
            pass

    sys.excepthook = hook


# ---------- 托盘图标 ----------


def _make_icon(color: str) -> QIcon:
    """用 QPainter 画一个圆点图标，免去外部图片资源。"""
    cached = _ICON_CACHE.get(color)
    if cached is not None:
        return cached

    pixmap = QPixmap(ICON_SIZE, ICON_SIZE)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setBrush(QBrush(QColor(color)))
    painter.setPen(QPen(QColor("#1F2937"), max(2, ICON_SIZE // 16)))
    inset = ICON_SIZE // 8
    painter.drawEllipse(inset, inset, ICON_SIZE - inset * 2, ICON_SIZE - inset * 2)
    painter.end()

    icon = QIcon(pixmap)
    _ICON_CACHE[color] = icon
    return icon


def _resource_path(relative: str) -> str:
    """开发环境与 PyInstaller 单文件模式下的资源路径。"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, relative)


def _load_app_icon() -> QIcon:
    """优先使用打包的 ICO，缺失时退回运行时绘制的圆点图标。"""
    path = _resource_path(os.path.join("assets", "sysgauge.ico"))
    if os.path.isfile(path):
        icon = QIcon(path)
        if not icon.isNull():
            return icon
    logger.warning("未找到图标文件，使用运行时绘制图标: %s", path)
    return _make_icon(COLOR_LOW)


def _color_for(cpu_percent: float | None) -> str:
    if cpu_percent is None:
        return COLOR_IDLE
    if cpu_percent < 50:
        return COLOR_LOW
    if cpu_percent < 80:
        return COLOR_MID
    return COLOR_HIGH


# ---------- 单实例 ----------


class SingleInstanceGuard:
    """用本地命名管道做互斥：第二个实例会激活已有窗口后退出。"""

    def __init__(self, key: str = SINGLE_INSTANCE_KEY) -> None:
        self._key = key
        self._server: QLocalServer | None = None
        self._pending: list[QLocalSocket] = []
        self.on_activate = None

    def try_acquire(self) -> bool:
        probe = QLocalSocket()
        probe.connectToServer(self._key)
        if probe.waitForConnected(300):
            probe.write(b"ACTIVATE")
            probe.flush()
            probe.waitForBytesWritten(300)
            probe.disconnectFromServer()
            logger.info("检测到已有实例在运行，已请求激活该窗口")
            return False

        # 上次异常退出可能残留命名管道，先清理再监听
        QLocalServer.removeServer(self._key)
        self._server = QLocalServer()
        self._server.newConnection.connect(self._on_new_connection)
        if not self._server.listen(self._key):
            logger.warning("单实例监听失败：%s", self._server.errorString())
        return True  # 监听失败也允许启动，不因互斥机制阻塞用户

    def _on_new_connection(self) -> None:
        if self._server is None:
            return
        socket = self._server.nextPendingConnection()
        if socket is None:
            return
        self._pending.append(socket)
        socket.disconnected.connect(lambda s=socket: self._drop(s))
        if callable(self.on_activate):
            self.on_activate()

    def _drop(self, socket: QLocalSocket) -> None:
        if socket in self._pending:
            self._pending.remove(socket)
        socket.deleteLater()


# ---------- 托盘 ----------


def _build_tray(app: QApplication, window: MainWindow) -> QSystemTrayIcon:
    tray = QSystemTrayIcon(app)
    tray.setIcon(_make_icon(COLOR_IDLE))
    tray.setToolTip(f"{__app_name__} - 未在记录")

    menu = QMenu()
    action_show = menu.addAction("显示主窗口")
    action_show.triggered.connect(window.show_and_raise)

    menu.addSeparator()
    action_start = menu.addAction("开始记录")
    action_start.triggered.connect(window.start_recording)
    action_stop = menu.addAction("停止记录")
    action_stop.setEnabled(False)
    action_stop.triggered.connect(window.stop_recording)

    menu.addSeparator()
    action_dir = menu.addAction("打开数据目录")
    action_dir.triggered.connect(window.open_data_dir)
    action_history = menu.addAction("打开历史数据…")
    action_history.triggered.connect(window.open_history)

    menu.addSeparator()
    action_quit = menu.addAction("退出")
    action_quit.triggered.connect(lambda: _quit(app, window))

    tray.setContextMenu(menu)
    tray.activated.connect(
        lambda reason: window.show_and_raise()
        if reason in (QSystemTrayIcon.DoubleClick, QSystemTrayIcon.Trigger)
        else None
    )
    window.set_tray_actions(action_start, action_stop)
    return tray


def _update_tray(tray: QSystemTrayIcon | None, sample) -> None:
    if tray is None or sample is None:
        return
    tray.setIcon(_make_icon(_color_for(sample.cpu_percent)))

    cpu_text = "--" if sample.cpu_percent is None else f"{sample.cpu_percent:.0f}%"
    mem_text = "--" if sample.mem_percent is None else f"{sample.mem_percent:.0f}%"
    state = "记录中" if sample.cpu_percent is not None else "未在记录"
    tray.setToolTip(f"{__app_name__} - CPU {cpu_text} / 内存 {mem_text}（{state}）")


def _quit(app: QApplication, window: MainWindow) -> None:
    window.prepare_quit()
    app.quit()


# ---------- 主流程 ----------


def main() -> int:
    log_path = setup_logging()
    install_excepthook()
    logger.info("日志文件: %s", log_path)

    app = QApplication(sys.argv)
    app.setApplicationName(__app_name__)
    app.setApplicationVersion(__version__)
    app.setStyle("Fusion")
    # 关窗只隐藏到托盘，不能因为最后一个窗口关闭就退出
    app.setQuitOnLastWindowClosed(False)

    guard = SingleInstanceGuard()
    if not guard.try_acquire():
        return 0

    cfg = AppConfig.load()
    cfg.ensure_save_dir()

    recorder = CsvRecorder(cfg.save_dir, cfg.rotate_mb)
    sampler = Sampler(cfg, recorder)
    window = MainWindow(cfg, sampler)

    app_icon = _load_app_icon()
    window.setWindowIcon(app_icon)
    app.setWindowIcon(app_icon)

    tray: QSystemTrayIcon | None = None
    if QSystemTrayIcon.isSystemTrayAvailable():
        tray = _build_tray(app, window)
    else:
        logger.warning("系统托盘不可用，已禁用【关闭窗口时最小化到托盘】")
        cfg.minimize_to_tray = False
        window.disable_tray_minimize()

    def on_notify(title: str, message: str) -> None:
        logger.info("通知: %s - %s", title, message)
        if tray is not None:
            tray.showMessage(title, message, app_icon, 4000)

    def on_shutdown() -> None:
        if sampler.is_recording:
            sampler.stop_session()
        else:
            recorder.stop()

    window.notify.connect(on_notify)
    app.aboutToQuit.connect(on_shutdown)
    guard.on_activate = window.show_and_raise
    sampler.sample_ready.connect(lambda sample: _update_tray(tray, sample))

    window.show_and_raise()
    if tray is not None:
        tray.show()
        window.notify.emit("SysGauge 已启动", "点击【开始记录】即可开始记录 CPU 与内存占用。")

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
