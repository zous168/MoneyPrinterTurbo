"""
文生图后端。

复用项目现有的集成与配置：
  - pollinations：llm.py 中已集成其文本接口，这里使用同源的 image.pollinations.ai
    图像接口，免 API key，作为默认后端，接入成本最低；
  - siliconflow：config.toml 已有 [siliconflow] api_key，支持 Kolors / Flux 等
    对中文主题友好的模型。

对外只暴露 `text_to_image(...)`，返回落盘后的本地图片路径。
"""

import time
from typing import Optional
from urllib.parse import quote

import requests
from loguru import logger

from app.config import config

_POLLINATIONS_IMAGE_BASE = "https://image.pollinations.ai/prompt"
_SILICONFLOW_IMAGE_URL = "https://api.siliconflow.cn/v1/images/generations"


def _tls_verify() -> bool:
    return bool(config.app.get("tls_verify", True))


def _proxies():
    p = getattr(config, "proxy", None)
    if not p:
        return None
    proxies = {}
    if p.get("http"):
        proxies["http"] = p["http"]
    if p.get("https"):
        proxies["https"] = p["https"]
    return proxies or None


def image_provider() -> str:
    return config.app.get("image_provider", "pollinations")


def _model_for(provider: str) -> str:
    """返回该生图后端实际使用的模型名（供日志显示）。"""
    if provider == "siliconflow":
        sf = getattr(config, "siliconflow", {}) or {}
        return sf.get("image_model_name", "Kwai-Kolors/Kolors")
    if provider in ("openai", "openai_compatible"):
        return config.app.get("openai_image_model", "") or "(unset)"
    return config.app.get("pollinations_image_model", "flux")


def _save_bytes(data: bytes, out_path: str) -> str:
    with open(out_path, "wb") as f:
        f.write(data)
    return out_path


def _generate_pollinations(prompt: str, width: int, height: int, out_path: str, seed: Optional[int]) -> str:
    # image.pollinations.ai 直接以 GET 返回图片二进制，prompt 放在 URL path 中。
    url = f"{_POLLINATIONS_IMAGE_BASE}/{quote(prompt)}"
    params = {
        "width": width,
        "height": height,
        "nologo": "true",
        "model": config.app.get("pollinations_image_model", "flux"),
    }
    if seed is not None:
        params["seed"] = seed
    api_key = config.app.get("pollinations_api_key", "")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    resp = requests.get(
        url,
        params=params,
        headers=headers,
        timeout=120,
        verify=_tls_verify(),
        proxies=_proxies(),
    )
    resp.raise_for_status()
    if not resp.content or not resp.headers.get("Content-Type", "").startswith("image"):
        raise RuntimeError(f"pollinations returned non-image content: {resp.text[:200]}")
    return _save_bytes(resp.content, out_path)


def _generate_siliconflow(prompt: str, width: int, height: int, out_path: str, seed: Optional[int]) -> str:
    api_key = config.siliconflow.get("api_key", "") if hasattr(config, "siliconflow") else ""
    if not api_key:
        raise ValueError("siliconflow: api_key 未配置，请在 config.toml 的 [siliconflow] 段设置。")
    model = config.siliconflow.get("image_model_name", "Kwai-Kolors/Kolors")
    payload = {
        "model": model,
        "prompt": prompt,
        "image_size": f"{width}x{height}",
        "batch_size": 1,
        "num_inference_steps": config.siliconflow.get("image_steps", 20),
    }
    if seed is not None:
        payload["seed"] = seed
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    resp = requests.post(
        _SILICONFLOW_IMAGE_URL,
        json=payload,
        headers=headers,
        timeout=180,
        verify=_tls_verify(),
        proxies=_proxies(),
    )
    resp.raise_for_status()
    result = resp.json()
    images = result.get("images") or result.get("data") or []
    if not images:
        raise RuntimeError(f"siliconflow returned no image: {result}")
    img_url = images[0].get("url")
    if not img_url:
        raise RuntimeError(f"siliconflow image entry has no url: {images[0]}")
    img_resp = requests.get(img_url, timeout=120, verify=_tls_verify(), proxies=_proxies())
    img_resp.raise_for_status()
    return _save_bytes(img_resp.content, out_path)


def _generate_openai_compatible(prompt: str, width: int, height: int, out_path: str, seed: Optional[int]) -> str:
    # 复用现有 openai 兼容端点（openai_api_key / openai_base_url）的
    # /images/generations 接口。很多聚合网关（如 OpenRouter、各类中转站）都用同一把
    # key 同时提供文本与文生图（FLUX / Kolors / SD 等），这样无需再单独配置生图服务。
    api_key = config.app.get("openai_api_key")
    base_url = (config.app.get("openai_base_url", "") or "https://api.openai.com/v1").rstrip("/")
    model = config.app.get("openai_image_model", "")
    if not api_key:
        raise ValueError("openai 文生图：openai_api_key 未配置。")
    if not model:
        raise ValueError("openai 文生图：openai_image_model 未配置（如 flux-1-schnell / dall-e-3）。")
    payload = {
        "model": model,
        "prompt": prompt,
        "n": 1,
        "size": f"{width}x{height}",
    }
    if seed is not None:
        payload["seed"] = seed
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    resp = requests.post(
        f"{base_url}/images/generations",
        json=payload,
        headers=headers,
        timeout=180,
        verify=_tls_verify(),
        proxies=_proxies(),
    )
    resp.raise_for_status()
    data = (resp.json() or {}).get("data") or []
    if not data:
        raise RuntimeError(f"openai image endpoint returned no data: {resp.text[:200]}")
    entry = data[0]
    if entry.get("b64_json"):
        import base64

        return _save_bytes(base64.b64decode(entry["b64_json"]), out_path)
    if entry.get("url"):
        img_resp = requests.get(entry["url"], timeout=120, verify=_tls_verify(), proxies=_proxies())
        img_resp.raise_for_status()
        return _save_bytes(img_resp.content, out_path)
    raise RuntimeError(f"openai image entry has neither b64_json nor url: {entry}")


def text_to_image(
    prompt: str,
    out_path: str,
    width: int = 768,
    height: int = 768,
    seed: Optional[int] = None,
    provider: Optional[str] = None,
    retries: int = 2,
) -> str:
    """
    根据 prompt 生成一张图片并保存到 out_path，返回该路径。

    provider 为空时使用 config.app.image_provider（默认 pollinations）。
    失败会按 retries 重试。
    """
    provider = provider or image_provider()
    last_err: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            if provider == "siliconflow":
                return _generate_siliconflow(prompt, width, height, out_path, seed)
            if provider in ("openai", "openai_compatible"):
                return _generate_openai_compatible(prompt, width, height, out_path, seed)
            return _generate_pollinations(prompt, width, height, out_path, seed)
        except Exception as e:
            last_err = e
            logger.warning(
                f"text_to_image failed (provider={provider}, model={_model_for(provider)}, "
                f"attempt {attempt + 1}/{retries + 1}): {e}"
            )
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"text_to_image failed after retries: {last_err}")
