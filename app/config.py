"""应用配置：读写 %APPDATA%\\SysGauge\\config.json。

设计要点：
- 默认值集中在 AppConfig 里，读取失败/字段缺失一律回落到默认值，避免因配置文件损坏而无法启动。
- 写入采用"先写临时文件再 os.replace"的原子替换，防止写一半断电导致配置损坏。
"""

from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields

from .i18n import normalize_language

logger = logging.getLogger(__name__)

APP_DIR_NAME = "SysGauge"

# 采样间隔的可调范围（秒）
MIN_INTERVAL = 0.1
MAX_INTERVAL = 60.0

# 不允许文件持续膨胀：0 表示不切分
MIN_ROTATE_MB = 0
MAX_ROTATE_MB = 10240


def appdata_dir() -> str:
    """%APPDATA%\\SysGauge"""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_DIR_NAME)


def config_path() -> str:
    return os.path.join(appdata_dir(), "config.json")


def log_dir() -> str:
    return os.path.join(appdata_dir(), "logs")


def default_save_dir() -> str:
    """默认数据目录：%USERPROFILE%\\Documents\\SysGauge"""
    docs = os.path.join(os.path.expanduser("~"), "Documents")
    if not os.path.isdir(docs):
        docs = os.path.expanduser("~")
    return os.path.join(docs, APP_DIR_NAME)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass
class AppConfig:
    # 采样
    interval_sec: float = 1.0
    enable_cores: bool = False
    enable_freq: bool = True
    enable_swap: bool = False

    # 存储
    save_dir: str = field(default_factory=default_save_dir)
    rotate_mb: int = 100

    # 界面
    minimize_to_tray: bool = True
    auto_scale: bool = False  # 图表纵轴自适应（贴谷 / 贴峰）
    language: str = ""  # 界面语言，空字符串表示跟随系统
    window_geometry: str | None = None  # base64(winId 序列化后的 QByteArray)

    # 运行时状态（不落盘）
    _loaded_from: str = field(default="", repr=False, compare=False)

    # ---------- 校验 ----------

    def normalize(self) -> "AppConfig":
        """把越界/非法值夹紧到安全范围。"""
        try:
            self.interval_sec = _clamp(float(self.interval_sec), MIN_INTERVAL, MAX_INTERVAL)
        except (TypeError, ValueError):
            self.interval_sec = 1.0

        try:
            self.rotate_mb = int(_clamp(int(self.rotate_mb), MIN_ROTATE_MB, MAX_ROTATE_MB))
        except (TypeError, ValueError):
            self.rotate_mb = 100

        for name in ("enable_cores", "enable_freq", "enable_swap", "minimize_to_tray", "auto_scale"):
            setattr(self, name, bool(getattr(self, name)))

        if not isinstance(self.save_dir, str) or not self.save_dir.strip():
            self.save_dir = default_save_dir()
        if not isinstance(self.language, str) or not self.language:
            self.language = ""
        else:
            self.language = normalize_language(self.language)
        if not isinstance(self.window_geometry, str):
            self.window_geometry = None
        return self

    # ---------- 读写 ----------

    @classmethod
    def load(cls) -> "AppConfig":
        path = config_path()
        cfg = cls()
        existed = os.path.isfile(path)
        if existed:
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                if isinstance(raw, dict):
                    known = {f.name for f in fields(cls) if not f.name.startswith("_")}
                    for key, value in raw.items():
                        if key in known:
                            setattr(cfg, key, value)
                cfg._loaded_from = path
            except (OSError, ValueError) as exc:
                logger.warning("配置文件读取失败，使用默认配置: %s", exc)
        cfg.normalize()
        if not existed:
            cfg.save()
        return cfg

    def save(self) -> bool:
        """原子写入配置文件，失败不抛出。"""
        path = config_path()
        data = asdict(self)
        data.pop("_loaded_from", None)
        tmp = path + ".tmp"
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
            return True
        except OSError as exc:
            logger.warning("配置文件写入失败: %s", exc)
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            return False

    def ensure_save_dir(self) -> bool:
        """确保保存目录可用；不可用时回落到默认目录。返回是否可用。"""
        if _is_writable_dir(self.save_dir):
            return True
        fallback = default_save_dir()
        logger.warning("保存目录不可用，回落到: %s", fallback)
        self.save_dir = fallback
        return _is_writable_dir(fallback)


def _is_writable_dir(path: str) -> bool:
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".sysgauge_write_test")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe)
        return True
    except OSError:
        return False


__all__ = [
    "AppConfig",
    "MIN_INTERVAL",
    "MAX_INTERVAL",
    "appdata_dir",
    "config_path",
    "log_dir",
    "default_save_dir",
    "encode_geometry",
    "decode_geometry",
]


def encode_geometry(qbytearray) -> str | None:
    """把 QByteArray 形式的窗口几何编码为 base64 字符串。"""
    try:
        data = bytes(qbytearray)
        if not data:
            return None
        return base64.b64encode(data).decode("ascii")
    except (TypeError, ValueError):
        return None


def decode_geometry(text: str | None):
    """解码为 bytes，供 QMainWindow.restoreGeometry 使用。"""
    if not text:
        return None
    try:
        return base64.b64decode(text.encode("ascii"))
    except (ValueError, TypeError):
        return None
