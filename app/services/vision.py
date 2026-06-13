"""
多模态视觉理解服务。

本模块复用 config.toml 里现有的 `llm_provider` 及其凭证配置，给视觉能力的
大模型发送「文字 + 图片」，用于：
  - 反推模板图片的版式结构（image_studio 步骤 2）
  - 审核生成内容与原图是否一致（image_studio 步骤 4）

设计原则：
  - 不重复造 provider 体系，直接读取与 app/services/llm.py 相同的配置键；
  - 只覆盖具备视觉能力的主流 provider（OpenAI 兼容协议 + Gemini + Azure +
    LiteLLM）。对不支持视觉的 provider 给出明确报错，提示用户切换。
"""

import base64
import json
import mimetypes
import re
import time
from typing import List, Optional

from loguru import logger

from app.config import config

# OpenAI Chat Completions 多模态协议（image_url）兼容的 provider。
# 这些 provider 在 llm.py 里都走标准 OpenAI SDK，凭证键名一致，可统一处理。
_OPENAI_COMPATIBLE = {
    "openai": ("openai_api_key", "openai_model_name", "openai_base_url", "https://api.openai.com/v1"),
    "oneapi": ("oneapi_api_key", "oneapi_model_name", "oneapi_base_url", ""),
    "moonshot": ("moonshot_api_key", "moonshot_model_name", "moonshot_base_url", "https://api.moonshot.cn/v1"),
    "grok": ("grok_api_key", "grok_model_name", "grok_base_url", "https://api.x.ai/v1"),
    "minimax": ("minimax_api_key", "minimax_model_name", "minimax_base_url", "https://api.minimax.io/v1"),
    "mimo": ("mimo_api_key", "mimo_model_name", "mimo_base_url", "https://api.xiaomimimo.com/v1"),
    "modelscope": ("modelscope_api_key", "modelscope_model_name", "modelscope_base_url", "https://api-inference.modelscope.cn/v1/"),
    "deepseek": ("deepseek_api_key", "deepseek_model_name", "deepseek_base_url", "https://api.deepseek.com"),
    "qwen": ("qwen_api_key", "qwen_model_name", "qwen_base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
}


def _encode_image(image_path: str) -> tuple[str, bytes]:
    """读取图片并返回 (mime_type, raw_bytes)。"""
    mime, _ = mimetypes.guess_type(image_path)
    if not mime or not mime.startswith("image/"):
        mime = "image/png"
    with open(image_path, "rb") as f:
        data = f.read()
    if not data:
        raise ValueError(f"image is empty: {image_path}")
    return mime, data


def _data_url(image_path: str) -> str:
    mime, data = _encode_image(image_path)
    b64 = base64.b64encode(data).decode("utf-8")
    return f"data:{mime};base64,{b64}"


def _vision_provider() -> str:
    return config.app.get("vision_provider") or config.app.get("llm_provider", "openai")


def _openai_compatible_messages(prompt: str, image_paths: List[str]):
    content = [{"type": "text", "text": prompt}]
    for p in image_paths:
        content.append({"type": "image_url", "image_url": {"url": _data_url(p)}})
    return [{"role": "user", "content": content}]


def analyze_images(prompt: str, image_paths: List[str]) -> str:
    """
    给视觉大模型发送文字 prompt + 一张或多张图片，返回模型的文本回复。

    复用 llm_provider 配置；provider 不支持视觉时抛出带说明的异常。
    """
    if not image_paths:
        raise ValueError("analyze_images requires at least one image path")

    provider = _vision_provider()
    logger.info(f"vision provider: {provider}, images: {len(image_paths)}")

    if provider in _OPENAI_COMPATIBLE:
        from openai import OpenAI

        key_name, model_name_key, base_url_key, default_base = _OPENAI_COMPATIBLE[provider]
        api_key = config.app.get(key_name)
        model_name = config.app.get("vision_model_name") or config.app.get(model_name_key)
        base_url = config.app.get(base_url_key, "") or default_base
        if not api_key:
            raise ValueError(f"{provider}: api_key 未配置，请在 config.toml 中设置后再使用图片反推。")
        if not model_name:
            raise ValueError(f"{provider}: model_name 未配置。")
        client = OpenAI(api_key=api_key, base_url=base_url)
        response = client.chat.completions.create(
            model=model_name,
            messages=_openai_compatible_messages(prompt, image_paths),
        )
        return _extract_openai_text(response, provider)

    if provider == "azure":
        from openai import AzureOpenAI

        api_key = config.app.get("azure_api_key")
        model_name = config.app.get("vision_model_name") or config.app.get("azure_model_name")
        base_url = config.app.get("azure_base_url", "")
        api_version = config.app.get("azure_api_version", "2024-02-15-preview")
        if not api_key:
            raise ValueError("azure: api_key 未配置。")
        client = AzureOpenAI(api_key=api_key, api_version=api_version, azure_endpoint=base_url)
        response = client.chat.completions.create(
            model=model_name,
            messages=_openai_compatible_messages(prompt, image_paths),
        )
        return _extract_openai_text(response, provider)

    if provider == "gemini":
        import google.generativeai as genai

        api_key = config.app.get("gemini_api_key")
        model_name = config.app.get("vision_model_name") or config.app.get("gemini_model_name") or "gemini-2.5-flash"
        base_url = config.app.get("gemini_base_url", "")
        if not api_key:
            raise ValueError("gemini: api_key 未配置。")
        if base_url:
            genai.configure(api_key=api_key, transport="rest", client_options={"api_endpoint": base_url})
        else:
            genai.configure(api_key=api_key, transport="rest")
        model = genai.GenerativeModel(model_name=model_name)
        parts = [prompt]
        for p in image_paths:
            mime, data = _encode_image(p)
            parts.append({"mime_type": mime, "data": data})
        response = model.generate_content(parts)
        try:
            return response.candidates[0].content.parts[0].text.strip()
        except (AttributeError, IndexError) as e:
            raise ValueError(f"[gemini] returned invalid vision response: {e}")

    if provider == "ollama":
        from openai import OpenAI

        api_key = "ollama"
        model_name = config.app.get("vision_model_name") or config.app.get("ollama_model_name")
        base_url = config.app.get("ollama_base_url", "") or config.get_default_ollama_base_url()
        if not model_name:
            raise ValueError("ollama: model_name 未配置（视觉需使用 llava 等多模态模型）。")
        client = OpenAI(api_key=api_key, base_url=base_url)
        response = client.chat.completions.create(
            model=model_name,
            messages=_openai_compatible_messages(prompt, image_paths),
        )
        return _extract_openai_text(response, provider)

    if provider == "litellm":
        import litellm

        model_name = config.app.get("vision_model_name") or config.app.get("litellm_model_name")
        if not model_name:
            raise ValueError("litellm: model_name 未配置。")
        response = litellm.completion(
            model=model_name,
            messages=_openai_compatible_messages(prompt, image_paths),
            drop_params=True,
        )
        return _extract_openai_text(response, provider)

    raise ValueError(
        f"provider '{provider}' 暂不支持图片视觉理解。请在 config.toml 中将 llm_provider "
        f"（或单独设置 vision_provider）切换为支持视觉的 provider，例如 openai(gpt-4o)、"
        f"gemini、qwen(qwen-vl) 等。"
    )


def _extract_openai_text(response, provider: str) -> str:
    choices = getattr(response, "choices", None)
    if not choices:
        raise ValueError(f"[{provider}] vision returned empty choices")
    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", None) if message else None
    if not content or not isinstance(content, str) or not content.strip():
        raise ValueError(f"[{provider}] vision returned empty content")
    return content.strip()


def analyze_image_json(prompt: str, image_paths: List[str], retries: int = 3) -> dict:
    """
    要求模型基于图片返回 JSON，并稳健解析。

    会在 prompt 末尾追加“只返回 JSON”的约束，并对返回内容做代码块剥离、
    花括号截取等兜底，最大程度避免模型偶发的格式噪声导致解析失败。
    """
    json_prompt = (
        prompt
        + "\n\n严格要求：只返回一个合法的 JSON 对象，不要包含任何解释文字、"
        "不要使用 Markdown 代码块标记。"
    )
    last_err: Optional[Exception] = None
    for i in range(retries):
        try:
            raw = analyze_images(json_prompt, image_paths)
            return _parse_json(raw)
        except Exception as e:
            last_err = e
            logger.warning(f"vision json parse failed (attempt {i + 1}/{retries}): {e}")
            if i < retries - 1:
                # 限流（429/burst）退避更久，普通解析失败也稍等，平滑请求速率。
                msg = str(e).lower()
                wait = min(2 ** i * 3, 24) if ("429" in msg or "rate" in msg or "limit_burst" in msg) else 1.0
                time.sleep(wait)
    raise ValueError(f"failed to get valid JSON from vision model: {last_err}")


def _parse_json(raw: str) -> dict:
    text = raw.strip()
    # 去掉 ```json ... ``` 代码块包裹
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    # 截取第一个 { 到最后一个 } 之间的内容（去掉前后多余文字）
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json.loads(_repair_json(text))


def _repair_json(text: str) -> str:
    """对模型偶发的非法 JSON 做轻量修复：去尾逗号、补对象/数组元素间缺失的逗号。"""
    # 去掉对象/数组里多余的尾逗号： ,} 或 ,]
    text = re.sub(r",\s*([}\]])", r"\1", text)
    # 在相邻值之间补缺失的逗号：  } { -> },{   ] [ -> ],[   " " -> ","   等
    text = re.sub(r"([}\]\"\d])(\s*\n\s*)([{\[\"])", r"\1,\2\3", text)
    # 紧贴的 }{ / ]" / "{ 等也补逗号
    text = re.sub(r"([}\]])\s*([{\[])", r"\1,\2", text)
    text = re.sub(r"(\")\s*([{\[])", r"\1,\2", text)
    return text
