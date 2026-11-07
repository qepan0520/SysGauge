"""采样线程：按绝对时间点精确定时，采集 → 落盘 → 通知界面。

定时策略（避免累积漂移）：
    next_deadline += interval
    wait(next_deadline - perf_counter())
而不是"睡 interval 秒"。每次采样记录真实时间戳，因此即使某次 tick 迟到，
写入的数据依然自洽。

线程只负责采集与写盘；UI 更新通过 Qt 信号投递回主线程，保证界面不卡顿。
"""

from __future__ import annotations

import logging
import threading
import time

from PySide6.QtCore import QThread, Signal

from .config import MAX_INTERVAL, MIN_INTERVAL, AppConfig
from .metrics import collect_sample, warm_up
from .recorder import CsvRecorder, RecorderError

logger = logging.getLogger(__name__)


class Sampler(QThread):
    """一次"记录会话"对应一次 start_session / stop_session。"""

    sample_ready = Signal(object)  # MetricSample
    session_started = Signal(str)  # 数据文件路径
    session_stopped = Signal(str)  # 数据文件路径（可能为空字符串）
    file_rotated = Signal(str)  # 切分后的新文件路径
    write_error = Signal(str)  # 写盘失败原因

    def __init__(self, cfg: AppConfig, recorder: CsvRecorder, parent=None) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._recorder = recorder
        self._stop_event = threading.Event()
        self._interval = float(cfg.interval_sec)
        self._interval_lock = threading.Lock()
        self._active = False

    # ---------- 状态 ----------

    @property
    def is_recording(self) -> bool:
        return self._active

    @property
    def interval(self) -> float:
        with self._interval_lock:
            return self._interval

    @property
    def data_path(self) -> str | None:
        return self._recorder.current_path

    @property
    def row_count(self) -> int:
        return self._recorder.row_count

    # ---------- 控制（主线程调用） ----------

    def start_session(self, save_dir: str | None = None) -> str:
        """打开新文件并启动采样。文件创建失败会抛 RecorderError。"""
        if self._active:
            return self._recorder.current_path or ""

        # 在启动采集线程之前完成建文件与表头写入，避免与写盘并发。
        path = self._recorder.start(save_dir)
        warm_up()  # 预热 psutil 计数器，避免首个数据点恒为 0
        self._stop_event.clear()
        self._interval = float(self._cfg.interval_sec)
        self._active = True
        super().start()
        logger.info("开始记录: %s (间隔 %.2fs)", path, self._interval)
        self.session_started.emit(path)
        return path

    def stop_session(self) -> str:
        """停止采样并确保数据刷盘；返回数据文件路径。"""
        if not self._active:
            return self._recorder.current_path or ""
        self._active = False
        self._stop_event.set()
        if not self.wait(5000):
            logger.warning("采样线程未在 5 秒内退出")
        path = self._recorder.stop()
        logger.info("停止记录: %s (共 %d 行)", path, self._recorder.row_count)
        self.session_stopped.emit(path or "")
        return path or ""

    def update_interval(self, interval_sec: float) -> None:
        """即时生效：下一次调度就使用新间隔。"""
        value = max(MIN_INTERVAL, min(MAX_INTERVAL, float(interval_sec)))
        with self._interval_lock:
            self._interval = value
        logger.info("采样间隔更新为 %.2fs", value)

    # ---------- 线程主体 ----------

    def run(self) -> None:  # noqa: C901 - 定时循环逻辑集中在此
        recorder = self._recorder
        try:
            self._loop(recorder)
        except RecorderError as exc:
            logger.error("写盘失败，已停止记录: %s", exc)
            self.write_error.emit(str(exc))
        except Exception as exc:  # pragma: no cover - 兜底，避免线程静默死亡
            logger.exception("采样线程异常")
            self.write_error.emit(f"采样线程异常: {exc}")
        finally:
            # 无论正常停止还是异常退出，都要把缓冲刷进磁盘并关闭句柄。
            recorder.stop()
            self._active = False

    def _loop(self, recorder: CsvRecorder) -> None:
        cfg = self._cfg
        last_path = recorder.current_path
        next_deadline = time.perf_counter()

        while not self._stop_event.is_set():
            sample = collect_sample(cfg)
            recorder.append(sample)

            current_path = recorder.current_path
            if current_path and current_path != last_path:
                last_path = current_path
                self.file_rotated.emit(current_path)

            self.sample_ready.emit(sample)

            next_deadline += self.interval
            remaining = next_deadline - time.perf_counter()
            if remaining > 0:
                # 可中断休眠：用户点"停止"时立即返回，不必等满一个间隔。
                if self._stop_event.wait(remaining):
                    break
            else:
                # 落后过多（系统休眠、磁盘长时间阻塞等）时重置基准，
                # 避免连续补采造成 CPU/IO 尖峰。
                if remaining < -self.interval * 2:
                    next_deadline = time.perf_counter()


__all__ = ["Sampler"]
