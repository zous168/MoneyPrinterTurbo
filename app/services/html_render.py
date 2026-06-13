"""
把 HTML 文件用无头浏览器（Playwright + Chromium）截图为 PNG。

为什么不用扩散文生图直接出整张图：知识卡片信息图是密集的中文图文版式，
扩散模型无法稳定渲染清晰、准确的中文文字与精确排版。用浏览器渲染 HTML/CSS
再截图，能保证中文清晰、版式 100% 可控。

Streamlit 注意点：Streamlit 的脚本线程可能与 Playwright 同步 API 的事件循环
检测冲突。这里优先在进程内调用，捕获到事件循环相关错误时自动回退到子进程，
保证在 WebUI 中也能稳定出图。
"""

import json
import os
import subprocess
import sys
import threading
from typing import Optional

from loguru import logger


def _worker_cmd(payload_json: str) -> list:
    """渲染 worker 的启动命令。冻结(PyInstaller)时 sys.executable 是 app.exe，
    用 `--pyi-render <payload>` 让同一个 exe 充当渲染 worker；否则用 python 跑本文件。"""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--pyi-render", payload_json]
    return [sys.executable, os.path.abspath(__file__), payload_json]


def run_worker(payload_json: str) -> None:
    """子进程/冻结 worker 入口：按 payload 的 mode 在进程内渲染。"""
    if sys.platform == "win32":
        import asyncio
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    args = json.loads(payload_json)
    mode = args.get("mode")
    if mode == "render_batch":
        _render_pngs_batch_in_process(args["jobs"], args.get("scale", 1))
    elif mode == "record_batch":
        _record_batch_in_process(args["jobs"], args.get("scale", 1))
    elif mode == "record":
        _record_in_process(args["html_path"], args["out_path"], args["width"],
                            args["height"], args["duration_s"], args.get("scale", 1))
    else:
        _render_in_process(args["html_path"], args["out_path"], args["width"],
                           args["full_page"], args.get("height"), args.get("scale", 2))


def _prefer_subprocess() -> bool:
    """是否跳过进程内尝试、直接用子进程渲染。

    Windows 上 Playwright 同步 API 需要 Proactor 事件循环来拉起浏览器子进程。
    Streamlit 在 tornado 的 Selector 事件循环 + 工作线程里跑页面代码，进程内尝试
    必然抛 NotImplementedError，并打印一堆 "Future exception was never retrieved"
    噪音。只有「主线程 + Proactor 策略」(如纯 CLI) 才能进程内驱动；其余情况一律
    直接走子进程（子进程 __main__ 会设好 Proactor 策略），既无噪音也更快。
    """
    if sys.platform != "win32":
        return False
    try:
        import asyncio
        on_main = threading.current_thread() is threading.main_thread()
        proactor = isinstance(
            asyncio.get_event_loop_policy(), asyncio.WindowsProactorEventLoopPolicy
        )
        return not (on_main and proactor)
    except Exception:
        return True


def _render_in_process(html_path: str, out_path: str, width: int, full_page: bool,
                       height: Optional[int] = None, scale: int = 2) -> str:
    from playwright.sync_api import sync_playwright

    file_url = "file:///" + os.path.abspath(html_path).replace("\\", "/")
    # height 指定时按精确尺寸截图（用于视频场景：固定 1080x1920 等）；
    # 否则按 full_page 截整页（用于信息图长图）。
    # full_page 截图时视口高度只是下限：设小一点，让短内容也按真实高度截，
    # 避免内容不足一屏时底部留出大片空白（Playwright 会按 max(内容, 视口) 截）。
    vp_height = height or 240
    exact = height is not None
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--disable-gpu"])
        try:
            page = browser.new_page(
                viewport={"width": width, "height": vp_height}, device_scale_factor=scale
            )
            page.goto(file_url, wait_until="networkidle")
            # 给图片/网络资源一点缓冲，避免插画还没绘制完成就截图。
            page.wait_for_timeout(400)
            page.screenshot(path=out_path, full_page=False if exact else full_page)
        finally:
            browser.close()
    return out_path


def _render_in_subprocess(html_path: str, out_path: str, width: int, full_page: bool,
                          height: Optional[int] = None, scale: int = 2) -> str:
    payload = {
        "html_path": os.path.abspath(html_path),
        "out_path": os.path.abspath(out_path),
        "width": width,
        "full_page": full_page,
        "height": height,
        "scale": scale,
    }
    proc = subprocess.run(
        _worker_cmd(json.dumps(payload)),
        capture_output=True,
        text=True,
        timeout=180,
    )
    if proc.returncode != 0 or not os.path.exists(out_path):
        raise RuntimeError(
            f"html render subprocess failed (code={proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return out_path


def render_html_to_png(
    html_path: str,
    out_path: str,
    width: int = 1080,
    full_page: bool = True,
    height: Optional[int] = None,
    scale: int = 2,
) -> str:
    """
    渲染 html_path 为 PNG 写入 out_path 并返回路径。

    - 不传 height：按整页长图截图（信息图用）。
    - 传 height：按精确 width×height 截图（视频场景用，建议 scale=1 得到精确像素）。
    若 Playwright 未安装或浏览器内核缺失，会抛出带安装指引的异常。
    """
    try:
        import playwright  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "未安装 Playwright，无法把 HTML 渲染为图片。请执行：\n"
            "  uv sync --extra playwright   （或 pip install playwright）\n"
            "  python -m playwright install chromium"
        ) from e

    if _prefer_subprocess():
        return _render_in_subprocess(html_path, out_path, width, full_page, height, scale)

    try:
        return _render_in_process(html_path, out_path, width, full_page, height, scale)
    except Exception as e:
        msg = str(e).lower()
        if "executable doesn't exist" in msg or "playwright install" in msg:
            raise RuntimeError(
                "Playwright 浏览器内核未安装，请执行：python -m playwright install chromium"
            ) from e
        # 进程内失败几乎都源于运行环境：Streamlit 子线程 + Windows 上 Playwright 同步 API
        # 需要 Proactor 事件循环，子线程拿不到时会抛 NotImplementedError（消息为空）、
        # 或事件循环/greenlet 相关错误。一律回退到全新子进程渲染（子进程主线程默认
        # Proactor 策略，能正常驱动 Chromium）。
        logger.warning(
            f"in-process render failed ({type(e).__name__}: {e}); fallback to subprocess"
        )
        try:
            return _render_in_subprocess(html_path, out_path, width, full_page, height, scale)
        except Exception as e2:
            raise RuntimeError(
                f"HTML 渲染失败。进程内错误：{type(e).__name__}: {e}；"
                f"子进程错误：{e2}"
            ) from e2


def _record_in_process(html_path: str, out_path: str, width: int, height: int,
                       duration_s: float, scale: int = 1) -> str:
    """录制网页（含 CSS 动画）为 webm 视频，时长 duration_s 秒。"""
    import shutil
    import tempfile

    from playwright.sync_api import sync_playwright

    file_url = "file:///" + os.path.abspath(html_path).replace("\\", "/")
    rec_dir = tempfile.mkdtemp(prefix="rec_")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox", "--disable-gpu"])
            try:
                context = browser.new_context(
                    viewport={"width": width, "height": height},
                    record_video_dir=rec_dir,
                    record_video_size={"width": width, "height": height},
                    device_scale_factor=scale,
                )
                page = context.new_page()
                page.goto(file_url, wait_until="load")
                # CSS 动画在加载时开始播放；保持页面到指定时长，录满整段。
                page.wait_for_timeout(int(duration_s * 1000))
                video = page.video
                page.close()
                context.close()  # 关闭 context 才会落盘视频
                src = video.path() if video else None
            finally:
                browser.close()
        if not src or not os.path.exists(src):
            raise RuntimeError("playwright did not produce a video file")
        shutil.move(src, out_path)
    finally:
        shutil.rmtree(rec_dir, ignore_errors=True)
    return out_path


def _record_in_subprocess(html_path: str, out_path: str, width: int, height: int,
                          duration_s: float, scale: int = 1) -> str:
    payload = {
        "mode": "record",
        "html_path": os.path.abspath(html_path),
        "out_path": os.path.abspath(out_path),
        "width": width, "height": height, "duration_s": duration_s, "scale": scale,
    }
    proc = subprocess.run(
        _worker_cmd(json.dumps(payload)),
        capture_output=True, text=True, timeout=max(120, int(duration_s) + 120),
    )
    if proc.returncode != 0 or not os.path.exists(out_path):
        raise RuntimeError(
            f"html record subprocess failed (code={proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return out_path


def record_html_to_video(html_path: str, out_path: str, width: int, height: int,
                         duration_s: float, scale: int = 1) -> str:
    """录制网页动画为视频（webm）。Streamlit 线程下自动回退子进程。"""
    try:
        import playwright  # noqa: F401
    except ImportError as e:
        raise RuntimeError("未安装 Playwright，无法录制视频。") from e
    if _prefer_subprocess():
        return _record_in_subprocess(html_path, out_path, width, height, duration_s, scale)
    try:
        return _record_in_process(html_path, out_path, width, height, duration_s, scale)
    except Exception as e:
        logger.warning(f"in-process record failed ({type(e).__name__}: {e}); fallback to subprocess")
        return _record_in_subprocess(html_path, out_path, width, height, duration_s, scale)


def _record_batch_in_process(jobs: list, scale: int = 1) -> list:
    """在同一个浏览器实例里依次录制多个场景（每个 job: html_path/out_path/width/height/duration_s）。"""
    import shutil
    import tempfile

    from playwright.sync_api import sync_playwright

    outs = []
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--disable-gpu"])
        try:
            for job in jobs:
                rec_dir = tempfile.mkdtemp(prefix="rec_")
                try:
                    context = browser.new_context(
                        viewport={"width": job["width"], "height": job["height"]},
                        record_video_dir=rec_dir,
                        record_video_size={"width": job["width"], "height": job["height"]},
                        device_scale_factor=scale,
                    )
                    page = context.new_page()
                    page.goto("file:///" + os.path.abspath(job["html_path"]).replace("\\", "/"),
                              wait_until="load")
                    page.wait_for_timeout(int(job["duration_s"] * 1000))
                    video = page.video
                    page.close()
                    context.close()
                    src = video.path() if video else None
                    if src and os.path.exists(src):
                        shutil.move(src, job["out_path"])
                        outs.append(job["out_path"])
                    else:
                        outs.append(None)
                finally:
                    shutil.rmtree(rec_dir, ignore_errors=True)
        finally:
            browser.close()
    return outs


def _record_batch_in_subprocess(jobs: list, scale: int = 1) -> list:
    total = sum(j["duration_s"] for j in jobs)
    payload = {"mode": "record_batch",
               "jobs": [{**j, "html_path": os.path.abspath(j["html_path"]),
                         "out_path": os.path.abspath(j["out_path"])} for j in jobs],
               "scale": scale}
    proc = subprocess.run(
        _worker_cmd(json.dumps(payload)),
        capture_output=True, text=True, timeout=int(total) + 180,
    )
    missing = [j["out_path"] for j in jobs if not os.path.exists(j["out_path"])]
    if proc.returncode != 0 or missing:
        raise RuntimeError(
            f"batch record subprocess failed (code={proc.returncode}, missing={len(missing)}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return [j["out_path"] for j in jobs]


def record_html_videos_batch(jobs: list, scale: int = 1) -> list:
    """批量录制多个场景，复用一个浏览器实例（省去逐场景启动开销）。"""
    try:
        import playwright  # noqa: F401
    except ImportError as e:
        raise RuntimeError("未安装 Playwright，无法录制视频。") from e
    if _prefer_subprocess():
        return _record_batch_in_subprocess(jobs, scale)
    try:
        return _record_batch_in_process(jobs, scale)
    except Exception as e:
        logger.warning(f"in-process batch record failed ({type(e).__name__}: {e}); fallback to subprocess")
        return _record_batch_in_subprocess(jobs, scale)


def _render_pngs_batch_in_process(jobs: list, scale: int = 1) -> list:
    """在同一个浏览器实例里依次把多个 HTML 截成 PNG（每个 job: html_path/out_path/width，
    可选 height=精确尺寸截图 / 不传则整页长图）。复用一个浏览器，省去逐张启动开销。"""
    from playwright.sync_api import sync_playwright

    outs = []
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--disable-gpu"])
        try:
            for job in jobs:
                height = job.get("height")
                exact = height is not None
                page = browser.new_page(
                    viewport={"width": job["width"], "height": height or 240},
                    device_scale_factor=job.get("scale", scale),
                )
                page.goto("file:///" + os.path.abspath(job["html_path"]).replace("\\", "/"),
                          wait_until="networkidle")
                page.wait_for_timeout(350)
                page.screenshot(path=job["out_path"],
                                full_page=False if exact else job.get("full_page", True))
                page.close()
                outs.append(job["out_path"] if os.path.exists(job["out_path"]) else None)
        finally:
            browser.close()
    return outs


def _render_pngs_batch_in_subprocess(jobs: list, scale: int = 1) -> list:
    payload = {"mode": "render_batch",
               "jobs": [{**j, "html_path": os.path.abspath(j["html_path"]),
                         "out_path": os.path.abspath(j["out_path"])} for j in jobs],
               "scale": scale}
    proc = subprocess.run(
        _worker_cmd(json.dumps(payload)),
        capture_output=True, text=True, timeout=max(180, 30 * len(jobs)),
    )
    missing = [j["out_path"] for j in jobs if not os.path.exists(j["out_path"])]
    if proc.returncode != 0 or missing:
        raise RuntimeError(
            f"batch png subprocess failed (code={proc.returncode}, missing={len(missing)}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return [j["out_path"] for j in jobs]


def render_html_pngs_batch(jobs: list, scale: int = 1) -> list:
    """批量把多个 HTML 截成 PNG，复用一个浏览器实例（用于一次性出多张分镜图）。"""
    try:
        import playwright  # noqa: F401
    except ImportError as e:
        raise RuntimeError("未安装 Playwright，无法渲染图片。") from e
    if _prefer_subprocess():
        return _render_pngs_batch_in_subprocess(jobs, scale)
    try:
        return _render_pngs_batch_in_process(jobs, scale)
    except Exception as e:
        logger.warning(f"in-process batch png failed ({type(e).__name__}: {e}); fallback to subprocess")
        return _render_pngs_batch_in_subprocess(jobs, scale)


if __name__ == "__main__":
    # 作为子进程入口：python html_render.py '<json-payload>'
    run_worker(sys.argv[1])
