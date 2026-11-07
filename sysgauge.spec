# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：单文件、无控制台窗口。

用法：pyinstaller --clean --noconfirm sysgauge.spec
产物：dist/SysGauge.exe
"""

# 明确排除用不到的重型 Qt 模块与科学计算库，避免 exe 无谓膨胀
EXCLUDES = [
    # 本程序只用 QtCore / QtGui / QtWidgets / QtNetwork
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel",
    "PySide6.QtWebSockets",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuickWidgets",
    "PySide6.QtQuick3D",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtDesigner",
    "PySide6.QtUiTools",
    "PySide6.QtSql",
    "PySide6.QtBluetooth",
    "PySide6.QtNfc",
    "PySide6.QtPositioning",
    "PySide6.QtLocation",
    "PySide6.QtRemoteObjects",
    "PySide6.QtScxml",
    "PySide6.QtSensors",
    "PySide6.QtSerialPort",
    "PySide6.QtSpatialAudio",
    "PySide6.QtStateMachine",
    "PySide6.QtTextToSpeech",
    "PySide6.QtHelp",
    "PySide6.QtHttpServer",
    "PySide6.QtGraphs",
    "PySide6.QtGraphsWidgets",
    # 注意：不要排除 QtOpenGL / QtOpenGLWidgets / QtSvg，
    # pyqtgraph 在导入阶段就会加载它们（见 pyqtgraph/Qt/OpenGLHelpers.py），
    # 排除后打包产物启动即 ModuleNotFoundError。
    # 其它无关库
    "tkinter",
    "matplotlib",
    "scipy",
    "pandas",
    "IPython",
    "pytest",
    "PyQt5",
    "PyQt6",
]

a = Analysis(
    ["run.py"],
    pathex=[],
    binaries=[],
    # 打包应用图标：既嵌入 exe 文件图标，也作为资源供窗口图标读取
    datas=[("assets/sysgauge.ico", "assets")],
    # pyqtgraph 内部存在动态导入，显式声明避免打包后运行时缺模块
    hiddenimports=["pyqtgraph", "pyqtgraph.Qt", "pyqtgraph.graphicsItems"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="SysGauge",
    icon="assets/sysgauge.ico",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,  # GUI 程序，不弹黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
