"""CSV 记录器：追加写 + 每次采样立即 flush + fsync。

崩溃/断电安全性的落点在这个文件里：
1. 只追加、不修改：断电最坏情况只是最后一行不完整，之前所有行完好。
2. 每写一行就 flush() + os.fsync()：Windows 上 fsync 会调用 _commit，
   把 Python 缓冲和操作系统内核缓冲一起刷进磁盘，保证"已记录的行不丢"。
3. 文件操作失败（磁盘满、路径不可写）抛出 RecorderError，由上层提示用户，不静默丢数据。
"""

from __future__ import annotations

import csv
import logging
import os
from datetime import datetime

from .metrics import COLUMNS, MetricSample

logger = logging.getLogger(__name__)

BYTES_PER_MB = 1024 * 1024


class RecorderError(Exception):
    """写盘失败（磁盘满、路径不可写、句柄异常等）。"""


class CsvRecorder:
    """把 MetricSample 逐行追加写入 CSV 文件。"""

    def __init__(self, save_dir: str, rotate_mb: int = 100) -> None:
        self.save_dir = save_dir
        self.rotate_mb = max(0, int(rotate_mb))
        self._fh = None
        self._writer: csv.writer | None = None
        self._path: str | None = None
        self._part = 1
        self._row_count = 0
        self._session_stamp: str | None = None
        self.bytes_written = 0

    # ---------- 状态 ----------

    @property
    def is_open(self) -> bool:
        return self._fh is not None

    @property
    def current_path(self) -> str | None:
        return self._path

    @property
    def row_count(self) -> int:
        return self._row_count

    # ---------- 生命周期 ----------

    def start(self, save_dir: str | None = None) -> str:
        """创建新文件并写入表头，返回文件路径。"""
        if self._fh is not None:
            self.stop()

        if save_dir is not None:
            self.save_dir = save_dir
        try:
            os.makedirs(self.save_dir, exist_ok=True)
        except OSError as exc:
            raise RecorderError(f"无法创建数据目录 {self.save_dir}: {exc}") from exc

        self._session_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._part = 1
        self._row_count = 0
        self.bytes_written = 0
        self._open_file()
        return self._path

    def stop(self) -> str | None:
        """刷盘并关闭文件；返回最后写入的文件路径。"""
        path = self._path
        self._close()
        return path

    def _open_file(self) -> None:
        if self._part == 1:
            name = f"sysgauge_{self._session_stamp}.csv"
        else:
            name = f"sysgauge_{self._session_stamp}_part{self._part}.csv"
        self._path = os.path.join(self.save_dir, name)
        try:
            # newline="" 是 csv 模块的硬性要求；一行一行写，由 _flush 负责落盘。
            self._fh = open(self._path, "a", newline="", encoding="utf-8")
            self._writer = csv.writer(self._fh, lineterminator="\n")
            if os.path.getsize(self._path) == 0:
                self._writer.writerow(COLUMNS)
                self._flush()
        except OSError as exc:
            self._close()
            raise RecorderError(f"无法写入文件 {self._path}: {exc}") from exc

    def _close(self) -> None:
        if self._fh is None:
            return
        try:
            self._flush()
        except RecorderError:
            logger.warning("关闭文件时刷盘失败: %s", self._path, exc_info=True)
        finally:
            try:
                self._fh.close()
            except OSError:
                logger.warning("关闭文件句柄失败: %s", self._path, exc_info=True)
            self._fh = None
            self._writer = None

    def _flush(self) -> None:
        """把缓冲写穿到磁盘。这是"断电不丢数据"的关键调用。"""
        if self._fh is None:
            return
        try:
            self._fh.flush()
            os.fsync(self._fh.fileno())
        except OSError as exc:
            raise RecorderError(f"数据落盘失败（磁盘可能已满）: {self._path}: {exc}") from exc

    # ---------- 写入 ----------

    def append(self, sample: MetricSample) -> None:
        """追加一行并立即落盘。"""
        if self._fh is None or self._writer is None:
            raise RecorderError("记录器未启动")

        if self._need_rotate():
            self._close()
            self._part += 1
            self._open_file()
            logger.info("文件达到切分阈值，新文件: %s", self._path)

        row = sample.to_row()
        try:
            self._writer.writerow(row)
            self._flush()
        except OSError as exc:
            raise RecorderError(f"写入失败（磁盘可能已满）: {self._path}: {exc}") from exc
        except ValueError as exc:  # csv 编码等异常
            raise RecorderError(f"写入失败: {self._path}: {exc}") from exc

        self._row_count += 1
        self.bytes_written = os.path.getsize(self._path) if self._path else self.bytes_written

    def _need_rotate(self) -> bool:
        if self.rotate_mb <= 0 or self._fh is None:
            return False
        try:
            size = os.path.getsize(self._path)
        except OSError:
            return False
        return size >= self.rotate_mb * BYTES_PER_MB


__all__ = ["CsvRecorder", "RecorderError"]
