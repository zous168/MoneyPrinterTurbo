# -*- coding: utf-8 -*-
"""
PyInstaller 入口（知识卡片 Studio）。

一个 exe 两用：
  1. 渲染 worker：被 html_render 以 `KnowledgeCard.exe --pyi-render <json>` 调用，
     跑 Playwright 截图/录制（worker 分支前置，只导入 html_render，启动快）。
  2. 正常启动：拉起 Streamlit 跑知识卡片页面。

资源约定（onedir）：代码/页面在 _internal（datas，加入 sys.path）；
resource/ config.toml ms-playwright storage 放在 exe 同级（用户可见可写）。
"""
import os
import sys
import glob

_MEIPASS = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
_EXEDIR = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else _MEIPASS

for _p in (_MEIPASS, _EXEDIR):
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.path.join(_EXEDIR, "ms-playwright")

# ---- 渲染 worker 分支：尽量少导入、尽早返回（每次截图/录制都会拉起一个 worker）----
if len(sys.argv) >= 3 and sys.argv[1] == "--pyi-render":
    from app.services import html_render
    html_render.run_worker(sys.argv[2])
    sys.exit(0)

# ---- 主程序路径：eager import 让 PyInstaller 收齐 app.* 用到的第三方依赖 ----
import app.config.config            # noqa: F401,E402
import app.services.html_render     # noqa: F401,E402
import app.services.image_studio    # noqa: F401,E402
import app.services.card_video      # noqa: F401,E402
import app.services.image_gen       # noqa: F401,E402
import app.services.vision          # noqa: F401,E402
import app.services.voice           # noqa: F401,E402
import app.services.video           # noqa: F401,E402
import app.services.llm             # noqa: F401,E402


def _find_page() -> str:
    pages_dir = os.path.join(_MEIPASS, "webui", "pages")
    clean = os.path.join(pages_dir, "KnowledgeCard.py")
    if os.path.exists(clean):
        return clean
    cands = [f for f in glob.glob(os.path.join(pages_dir, "*.py"))
             if not os.path.basename(f).startswith("__")]
    return cands[0] if cands else clean


def _run_streamlit() -> None:
    os.environ.setdefault("STREAMLIT_BROWSER_GATHER_USAGE_STATS", "false")
    os.environ.setdefault("STREAMLIT_GLOBAL_DEVELOPMENT_MODE", "false")
    from streamlit.web import cli as stcli

    sys.argv = [
        "streamlit", "run", _find_page(),
        "--server.port", "8501",
        "--server.address", "127.0.0.1",
        "--browser.gatherUsageStats", "false",
        "--global.developmentMode", "false",
    ]
    sys.exit(stcli.main())


if __name__ == "__main__":
    _run_streamlit()
