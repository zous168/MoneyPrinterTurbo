# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec：知识卡片 Studio（Streamlit + Playwright）。onedir 打包。
# 用法：  .venv\Scripts\pyinstaller knowledgecard.spec --noconfirm
from PyInstaller.utils.hooks import collect_all, copy_metadata

datas, binaries, hiddenimports = [], [], []

# 需要完整收集（子模块 + 数据文件 + 二进制）的包。
_COLLECT = [
    "streamlit", "playwright", "moviepy", "edge_tts", "imageio_ffmpeg",
    "altair", "pyarrow", "narwhals", "pydeck", "tornado", "watchdog",
    "blinker", "click", "toml", "loguru", "openai", "PIL", "tenacity",
    "rich", "validators", "tiktoken", "tiktoken_ext",
]
for _pkg in _COLLECT:
    try:
        d, b, h = collect_all(_pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception as _e:
        print(f"[spec] collect_all skip {_pkg}: {_e}")

# 部分包通过 importlib.metadata 查版本，需带上 *.dist-info（recursive 连依赖链一起带）。
for _md in ["streamlit", "altair", "pyarrow", "openai", "playwright",
            "moviepy", "edge_tts", "numpy", "pandas", "imageio", "imageio_ffmpeg",
            "pillow", "tqdm", "decorator", "proglog", "requests", "tenacity",
            "tiktoken", "loguru", "click", "rich", "packaging"]:
    try:
        datas += copy_metadata(_md, recursive=True)
    except Exception as _e:
        print(f"[spec] copy_metadata skip {_md}: {_e}")

# 页面/国际化作为数据（Streamlit 以脚本方式读取页面文件）。
datas += [("webui", "webui")]

# app.* 由 pyi_app.py 里 eager import 带入；这里再兜底收集子模块。
hiddenimports += [
    "streamlit.web.cli",
    "streamlit.runtime.scriptrunner.magic_funcs",
    "app", "app.services", "app.utils", "app.config", "app.models",
    "app.services.image_studio", "app.services.card_video",
    "app.services.html_render", "app.services.image_gen",
    "app.services.vision", "app.services.voice", "app.services.video",
    "app.services.llm",
]

a = Analysis(
    ["pyi_app.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="KnowledgeCard",
    console=True,
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False, upx=False,
    name="KnowledgeCard",
)
