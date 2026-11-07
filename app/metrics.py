"""指标采集：把 psutil 的读数整理成与 CSV 列一一对应的 MetricSample。

单个指标读取失败时置为 None（写入 CSV 时表现为空字段），不影响整行记录。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime

import psutil

logger = logging.getLogger(__name__)

# CSV 列顺序固定，永不变化。新增字段只能追加到末尾并需要新的文件版本。
COLUMNS = [
    "timestamp_iso",
    "timestamp_unix_ms",
    "cpu_percent",
    "cpu_percent_cores",
    "cpu_freq_mhz",
    "mem_percent",
    "mem_used_bytes",
    "mem_available_bytes",
    "mem_total_bytes",
    "swap_percent",
    "swap_used_bytes",
    "swap_total_bytes",
]

CORES_SEPARATOR = "|"


def _fmt_float(value: float | None) -> str:
    return "" if value is None else f"{value:.1f}"


@dataclass
class MetricSample:
    timestamp_iso: str
    timestamp_unix_ms: int
    cpu_percent: float | None = None
    cores: list[float] | None = None
    cpu_freq_mhz: float | None = None
    mem_percent: float | None = None
    mem_used_bytes: int | None = None
    mem_available_bytes: int | None = None
    mem_total_bytes: int | None = None
    swap_percent: float | None = None
    swap_used_bytes: int | None = None
    swap_total_bytes: int | None = None

    # ---------- 派生属性 ----------

    @property
    def cpu(self) -> float | None:
        return self.cpu_percent

    @property
    def mem(self) -> float | None:
        return self.mem_percent

    def to_row(self) -> list[str]:
        """按 COLUMNS 顺序输出 CSV 行（全部为字符串，空值写空字符串）。"""
        return [
            self.timestamp_iso,
            str(self.timestamp_unix_ms),
            _fmt_float(self.cpu_percent),
            "" if self.cores is None else CORES_SEPARATOR.join(f"{c:.1f}" for c in self.cores),
            _fmt_float(self.cpu_freq_mhz),
            _fmt_float(self.mem_percent),
            "" if self.mem_used_bytes is None else str(self.mem_used_bytes),
            "" if self.mem_available_bytes is None else str(self.mem_available_bytes),
            "" if self.mem_total_bytes is None else str(self.mem_total_bytes),
            _fmt_float(self.swap_percent),
            "" if self.swap_used_bytes is None else str(self.swap_used_bytes),
            "" if self.swap_total_bytes is None else str(self.swap_total_bytes),
        ]

    def to_dict(self) -> dict:
        return {col: val for col, val in zip(COLUMNS, self.to_row())}


def warm_up() -> None:
    """预热 psutil 的 CPU 计数器。

    psutil.cpu_percent(interval=None) 的第一次调用总是返回 0.0（没有历史基线），
    因此在正式开始记录前先调一次并丢弃结果，避免首个数据点无意义。
    """
    try:
        psutil.cpu_percent(interval=None)
        psutil.cpu_percent(interval=None, percpu=True)
    except Exception:  # pragma: no cover - 极端环境下不应阻塞启动
        logger.debug("CPU 预热失败", exc_info=True)


def collect_sample(cfg) -> MetricSample:
    """采集一次数据。cfg 需提供 enable_cores / enable_freq / enable_swap。"""
    now = time.time()
    unix_ms = int(now * 1000)
    iso = datetime.fromtimestamp(now).isoformat(timespec="milliseconds")

    sample = MetricSample(timestamp_iso=iso, timestamp_unix_ms=unix_ms)

    # CPU 总体占用率
    try:
        sample.cpu_percent = float(psutil.cpu_percent(interval=None))
    except Exception:
        logger.debug("读取 CPU 占用率失败", exc_info=True)

    # 每核心占用率
    if getattr(cfg, "enable_cores", False):
        try:
            sample.cores = [float(v) for v in psutil.cpu_percent(interval=None, percpu=True)]
        except Exception:
            logger.debug("读取每核心占用率失败", exc_info=True)

    # CPU 主频
    if getattr(cfg, "enable_freq", True):
        try:
            freq = psutil.cpu_freq()
            if freq is not None:
                sample.cpu_freq_mhz = float(freq.current)
        except (NotImplementedError, AttributeError, OSError):
            logger.debug("读取 CPU 主频失败", exc_info=True)
        except Exception:
            logger.debug("读取 CPU 主频失败", exc_info=True)

    # 物理内存
    try:
        vm = psutil.virtual_memory()
        sample.mem_percent = float(vm.percent)
        sample.mem_used_bytes = int(vm.used)
        sample.mem_available_bytes = int(vm.available)
        sample.mem_total_bytes = int(vm.total)
    except Exception:
        logger.debug("读取内存信息失败", exc_info=True)

    # 交换分区
    if getattr(cfg, "enable_swap", False):
        try:
            sw = psutil.swap_memory()
            sample.swap_percent = float(sw.percent)
            sample.swap_used_bytes = int(sw.used)
            sample.swap_total_bytes = int(sw.total)
        except Exception:
            logger.debug("读取交换分区信息失败", exc_info=True)

    return sample


__all__ = ["COLUMNS", "CORES_SEPARATOR", "MetricSample", "collect_sample", "warm_up"]
