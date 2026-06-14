"""
知识卡片 → 短视频。

把 image_studio 生成的结构化内容（宫格卡片 / 步骤流程）逐张做成动画场景，
配 AI 旁白(TTS) + 屏幕文字(兼作字幕) + 背景音乐，合成可直接发布的短视频。

复用现有能力：
  - app/services/voice.py    —— TTS 配音（edge-tts，中/英）
  - app/services/video.py    —— BGM 选取（get_bgm_file）
  - app/services/html_render —— 把每个场景渲染成固定分辨率 PNG
  - app/services/image_studio—— 复用风格主题(_STYLES)与文案生成(_generate_text_with_retry)

对外主入口：generate_videos(...)，按「比例 × 语言」批量出片。
"""

import os
import shutil
import subprocess
from typing import Callable, Optional

from loguru import logger

from app.services import image_studio, video as video_svc, voice
from app.utils import utils
from app.services.html_render import (record_html_to_video, record_html_videos_batch,
                                      render_html_to_png)
from app.services.image_studio import _generate_text_with_retry, vision

ProgressCb = Optional[Callable[[str, str], None]]

# 比例 → (宽, 高)
RESOLUTIONS = {"9:16": (1080, 1920), "16:9": (1920, 1080)}
# 语言 → edge-tts 默认音色
VOICES = {
    "zh": "zh-CN-XiaoxiaoNeural-Female",
    "en": "en-US-JennyNeural-Female",
}

# 界面可选的精选音色（edge-tts，免 key）。显示名 → voice_name。
ZH_VOICES = {
    "晓晓 · 温柔女声": "zh-CN-XiaoxiaoNeural-Female",
    "晓伊 · 活泼女声": "zh-CN-XiaoyiNeural-Female",
    "云希 · 阳光男声": "zh-CN-YunxiNeural-Male",
    "云扬 · 专业男声": "zh-CN-YunyangNeural-Male",
    "云健 · 浑厚男声": "zh-CN-YunjianNeural-Male",
    "晓辰 · 知性女声": "zh-CN-XiaochenNeural-Female",
    "东北 · 晓北女声": "zh-CN-liaoning-XiaobeiNeural-Female",
    "粤语 · 曉曼女声": "zh-HK-HiuMaanNeural-Female",
    "台湾 · 曉臻女声": "zh-TW-HsiaoChenNeural-Female",
}
EN_VOICES = {
    "Jenny · US Female": "en-US-JennyNeural-Female",
    "Aria · US Female": "en-US-AriaNeural-Female",
    "Guy · US Male": "en-US-GuyNeural-Male",
    "Christopher · US Male": "en-US-ChristopherNeural-Male",
    "Sonia · UK Female": "en-GB-SoniaNeural-Female",
    "Ryan · UK Male": "en-GB-RyanNeural-Male",
}
VOICE_OPTIONS = {"zh": ZH_VOICES, "en": EN_VOICES}
LANG_LABELS = {"zh": "中文", "en": "English"}
_CTA = {
    "zh": "觉得有用就点赞收藏，关注我了解更多干货～",
    "en": "If you found this helpful, like and follow for more!",
}


def _notify(cb: ProgressCb, stage: str, msg: str):
    logger.info(f"[card_video][{stage}] {msg}")
    if cb:
        try:
            cb(stage, msg)
        except Exception as e:
            logger.warning(f"progress cb error: {e}")


# --------------------------------------------------------------------------- #
# 英文版内容（双语出片用）
# --------------------------------------------------------------------------- #
def translate_content(content: dict, cb: ProgressCb = None) -> dict:
    """把中文内容整体翻译成英文，保持同样的 JSON 结构（用于英文配音版）。"""
    import json

    _notify(cb, "translate", "正在翻译为英文版…")
    prompt = (
        "把下面这份知识卡片信息图的 JSON 内容翻译成自然、地道的英文，"
        "保持完全相同的 JSON 结构与字段；数字、illustration_prompt 字段原样保留；"
        "title_highlight 改成 title(英文) 中最适合高亮的一个英文单词或短语（必须是 title 的子串）。"
        "只返回翻译后的 JSON。\n\n" + json.dumps(content, ensure_ascii=False)
    )
    raw = _generate_text_with_retry(prompt, cb)
    en = vision._parse_json(raw)
    # 关键结构字段沿用原值，避免翻译时被改坏。
    en["_layout"] = content.get("_layout", "grid")
    en["_grid_columns"] = content.get("_grid_columns", 4)
    en.setdefault("accent_color", content.get("accent_color", "#2e9e54"))
    return en


# --------------------------------------------------------------------------- #
# 旁白文本 + 场景列表
# --------------------------------------------------------------------------- #
def _join_zh(parts):
    return "。".join([p for p in parts if p]).replace("。。", "。")


def generate_narration(content: dict, lang: str, cb: ProgressCb = None) -> tuple:
    """
    生成口语化、有感情的口播文案（区别于画面文字）。
    返回 (intro 文案, [每个条目的文案...], outro 文案)。失败则返回 (None, [], None) 让上层回退。
    """
    items = content.get("cards") or content.get("steps") or []
    layout = content.get("_layout", "grid")
    title = content.get("title", "")
    lines_desc = []
    for i, it in enumerate(items, 1):
        head = it.get("heading") or it.get("category", "")
        body = it.get("text") or it.get("summary", "")
        lines_desc.append(f"{i}. {head}：{body}")
    items_block = "\n".join(lines_desc)
    kind = "步骤教程" if layout == "steps" else "清单盘点"

    if lang == "zh":
        prompt = (
            "你是百万粉丝短视频口播文案高手。下面是一条知识卡片视频的内容（主题 + "
            f"{len(items)} 个{kind}要点）。请把它改写成【口语化、有感情、有节奏】的配音旁白，"
            "像真人对着镜头热情讲解，**不要照读卡片原文**。\n"
            "要求：\n"
            "- intro：一句抓人的开场钩子（结合主题，制造好奇/痛点/利益，可用'你知道吗''别再''其实'等）\n"
            f"- items：{len(items)} 条，每条 1~2 句自然口语讲解，可用'第一''接下来''划重点''记住''最后'"
            "等连接词与'超实用''绝了''亲测有效''真的会谢'等情绪词，简洁有力\n"
            "- outro：一句引导点赞收藏关注的结尾\n"
            f"主题：{title}\n要点：\n{items_block}\n\n"
            '只返回 JSON：{"intro":"...","items":["...",...],"outro":"..."}'
        )
    else:
        prompt = (
            "You are a top short-video voiceover scriptwriter. Below is the content of a "
            f"knowledge-card video (topic + {len(items)} points). Rewrite it into a casual, "
            "emotional, punchy voiceover — like a real person talking to camera. Do NOT just "
            "read the card text.\n"
            "- intro: one catchy hook line\n"
            f"- items: {len(items)} lines, each 1-2 natural spoken sentences with connectors "
            "(first, next, here's the key, remember, finally) and light emotion\n"
            "- outro: one line asking to like and follow\n"
            f"Topic: {title}\nPoints:\n{items_block}\n\n"
            'Return only JSON: {"intro":"...","items":["...",...],"outro":"..."}'
        )

    _notify(cb, "script", f"[{LANG_LABELS.get(lang, lang)}] 生成口播文案…")
    try:
        raw = _generate_text_with_retry(prompt, cb)
        data = vision._parse_json(raw)
        intro = str(data.get("intro", "")).strip()
        item_lines = [str(x).strip() for x in (data.get("items") or [])]
        outro = str(data.get("outro", "")).strip() or _CTA[lang]
        return intro, item_lines, outro
    except Exception as e:
        logger.warning(f"narration generation failed, fallback to reading card text: {e}")
        return None, [], None


def _build_scenes(content: dict, lang: str, narration: tuple = None) -> list:
    """把内容拆成场景列表：片头 + 每个条目 + 片尾。narration=(intro, [items], outro) 提供口播文案时优先用它。"""
    title = content.get("title", "")
    subtitle = content.get("subtitle", "")
    layout = content.get("_layout", "grid")
    items = content.get("cards") or content.get("steps") or []
    nar_intro, nar_items, nar_outro = (narration or (None, [], None))

    scenes = []
    # 片头
    intro_fallback = _join_zh([title, subtitle]) if lang == "zh" else ". ".join([t for t in [title, subtitle] if t])
    scenes.append({
        "kind": "intro", "badge": "", "heading": title,
        "lines": [subtitle] if subtitle else [],
        "item_idx": None,
        "narration": nar_intro or intro_fallback,
    })
    # 条目
    total = len(items)
    for idx, it in enumerate(items):
        num = it.get("number", idx + 1)
        if layout == "steps":
            heading = it.get("heading", "")
            text = it.get("text", "")
            lines = [text] if text else []
            fallback = f"第{num}步，{heading}。{text}" if lang == "zh" else f"Step {num}. {heading}. {text}"
        else:
            heading = it.get("category", "")
            summary = it.get("summary", "")
            bullets = it.get("bullets", [])
            lines = ([summary] if summary else []) + list(bullets)
            fallback = _join_zh([heading, summary]) if lang == "zh" else ". ".join([p for p in [heading, summary] if p])
        narr = nar_items[idx] if idx < len(nar_items) and nar_items[idx] else fallback
        scenes.append({
            "kind": "item", "badge": str(num), "heading": heading,
            "lines": lines, "item_idx": idx, "narration": narr,
            "progress": f"{idx + 1}/{total}",
            "layout": it.get("layout") or "",   # 兜底用的版式枚举（空则规划器决定）
            "elements": it.get("elements") or [],  # LLM 二维网格定位（最高优先级）
            "blocks": it.get("blocks") or [],   # LLM 设计的内容块结构（次优先）
            "arrange": it.get("arrange") or "",  # LLM 决定的排布：row 左右 / column 上下
        })
    # 片尾
    cta = nar_outro or _CTA[lang]
    scenes.append({
        "kind": "outro", "badge": "", "heading": "🎬", "lines": [],
        "item_idx": None, "narration": cta, "cta": cta,
    })
    return scenes


# --------------------------------------------------------------------------- #
# 分镜 HTML —— 由 LLM 生成自包含内层 HTML（内联样式、不依赖外部 CSS）
# --------------------------------------------------------------------------- #
# 预置入场动画关键帧（由骨架统一注入，供 LLM 内联 animation: 引用；LLM 不可自写 @keyframes）。
# fill-mode both → 截静图时停在终态、不会拍到 opacity:0 的空帧；录视频时正常播放。
_SCENE_KEYFRAMES = (
    "@keyframes fadeUp{from{opacity:0;transform:translateY(48px)}to{opacity:1;transform:translateY(0)}}"
    "@keyframes fadeIn{from{opacity:0}to{opacity:1}}"
    "@keyframes pop{0%{opacity:0;transform:scale(.3)}70%{transform:scale(1.12)}100%{opacity:1;transform:scale(1)}}"
    "@keyframes popImg{0%{opacity:0;transform:scale(.85)}100%{opacity:1;transform:scale(1)}}"
    "@keyframes kenburns{from{transform:scale(1)}to{transform:scale(1.08)}}"
)


def _scene_page(inner: str, w: int, h: int) -> str:
    """把 LLM 生成的单屏内层 HTML 包进固定骨架：只注入字体加载 + 画布尺寸 + reset + 预置动画关键帧。
    布局/视觉全由内层内联样式决定（@font-face/@keyframes 是字体加载与动画基元，不是布局样式）。"""
    return (
        '<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8"><style>\n'
        f'{image_studio._fontface_css()}\n'
        '*{box-sizing:border-box;margin:0;padding:0}\n'
        f'html,body{{width:{w}px;height:{h}px;overflow:hidden}}\n'
        f'{_SCENE_KEYFRAMES}\n'
        f'</style></head><body>{inner}</body></html>'
    )


def _qr_html(qr_uri: str) -> str:
    if not qr_uri:
        return ""
    return (f"<div style='width:240px;height:240px;background:#fff;border-radius:18px;"
            f"padding:14px;box-shadow:0 6px 20px rgba(0,0,0,0.18);margin:24px auto'>"
            f"<img src='{qr_uri}' style='width:100%;height:100%;object-fit:contain'/></div>")


def _build_scene_html_prompt(data: list, w: int, h: int, safe: int, portrait: bool) -> str:
    import json
    top = h - safe
    return (
        "你是竖屏短视频信息卡（单屏一张）的资深设计师+前端工程师。下面是一条视频若干「分镜」"
        "（intro 片头 / item 正文 / outro 片尾）的数据。请为**每一个分镜**生成一段**自包含内层 HTML**。\n"
        f"【画布尺寸（务必按此精确排版）】整屏 {w}×{h}px；底部 {safe}px 是字幕安全区不可用；"
        f"**实际可放内容的区域 = {w}×{top}px（宽×高）**。根 div 已含 padding，你要在这个精确尺寸内把版面排满、排稳，"
        "既不留大片空白、也不溢出/裁切。\n"
        "【质量基准：对标专业 PPT / 优质小红书图文】每页都要像精心设计过：\n"
        " - **根据本页内容选最合适的版式**（文字多→图小或纯文字分点；有关键数字→大数字强调；要点少→大字陈述；有插画→图文主次分明），不要所有页一个模子；\n"
        " - **每页必须有一个明确的「重点 / 视觉焦点」**：把本页最核心的一句结论 / 一个关键数字 / 标题，用**最大字号或 accent 高亮色块**做成第一眼就看到的焦点，其余要点作支撑、字号明显更小，形成**主次层级**，不要一页全是同样大小的文字平铺。\n\n"
        "硬性规则（必须全部遵守）：\n"
        f"1. 每个分镜是**单个根 <div>**，根样式：width:100%;height:100%;display:flex;flex-direction:column;"
        f"padding:80px 60px {safe + 40}px;overflow:hidden;position:relative。靠 **flex 布局**排版。\n"
        "2. **严禁用 position:absolute 排正文/标题/图片**（只有角落水印这类角标可 absolute）。"
        "正文走正常流 / flex，**绝不允许文字和图片重叠**。\n"
        f"3. 底部 {safe}px 是**字幕安全区**（已用 padding-bottom 预留）：任何内容都不许进入。\n"
        f"4. **内容必须铺满整个 {top}px 可用高度、不许下半屏留大片空白**（这是最常见的低质量问题）：根 div 用 "
        "`justify-content:space-between`（或把内容分成 顶部标题区 / 中部主体 / 底部 三组并让它们分散），"
        "主体区 `flex:1` 撑开、内部再 `justify-content:space-around` 把要点拉开。**生成后自检：最底部元素应接近安全区上沿，若下面空了一大块就是不合格。**\n"
        "5. **只用内联 style**：禁止 class、<style>、外部 CSS、<script>、@import；**不许自己写 @keyframes**。\n"
        "6. **动画**：骨架已预置关键帧 fadeUp / fadeIn / pop / popImg / kenburns，请给标题、要点、插画等**内联**加 animation 做入场，"
        "并用 animation-delay 错峰依次出现（例：标题 style='...;animation:fadeUp .6s ease both'，"
        "要点逐条 'animation:fadeUp .5s ease both;animation-delay:.2s/.35s/.5s'，插画 'animation:popImg .6s ease both'，"
        "**务必带 both**（截图停终态）。每个分镜都要有动画。\n"
        "7. HTML 属性一律用**单引号**。\n"
        "8. 整屏有自己的浅色背景（该分镜 accent 的极浅色调，干净像小红书知识卡）；正文深灰 #2b2b2b，大标题用 accent。\n"
        "9. 字号要大（竖屏远看）且**有下限**：主标题 80~110px、分类标题**≥64px**、焦点句 48~52px、正文要点**≥38px**、序号徽标 ~46px。"
        "**强调焦点 ≠ 把标题/要点缩小**——标题和要点绝不能因为突出焦点而被压到很小（违反就是不合格）。行距舒展，不溢出不裁切。\n"
        "10. 字体：标题用 font-family:'ZCOOL KuaiLe'（内置手写体）；正文用 \"Microsoft YaHei\",sans-serif。\n"
        "11. img 非空就放插画，**必须完整显示不裁切、且不能上下/左右大留白**。插画是**正方形**，规则：\n"
        "    a) img 样式固定写 `width:100%;height:auto;aspect-ratio:1/1;object-fit:contain;border-radius:28px`，"
        "**绝不要写 height:100%**（会被拉成长条留白）。\n"
        "    b) 给 img 套一个**宽度受限的方形容器**（如 `width:560px` 或 `max-width:58%`），容器 `align-self:center` 居中，"
        f"且**限制图片高度**：方形容器 `max-height:{int(top * 0.42)}px`，**禁止图片占满整屏把文字挤到一角**；"
        "浅色底衬(background)**只加在 img 或这个方形容器上**，不要加在会被拉伸的整列/整行上（否则一大块灰底）。\n"
        "    c) src **原样照抄**含 file:/// 前缀。把图做成醒目主体、占合理大区域但保持正方形。img 为空则不放图、文字撑满。\n"
        "12. **构图要逐页轮换、不要每页都一样**（像真正的 PPT/图集每页有设计感）：在下列版式间循环——"
        "①图在上+要点在下 ②要点在上+图在下 ③图文左右分栏（文字栏 flex:1、图片栏放第 11 条的方形容器、两栏 align-items:center、不重叠）"
        "④大序号/大标题强调页(要点少时) ⑤无图时居中大字陈述。"
        "**对齐也随之变化**（有的居中、有的靠左），不要所有页都靠左。但**配色、字体、徽标、组件风格保持统一**，整体是一套设计系统。\n"
        "13. 各 kind：\n"
        "   - intro：超大主标题（topic；highlight 词非空就用 accent 高亮）+ 副标题（lines[0]）+ 引导行（如「↓ 一起来看 ↓」），整屏居中、有气势。\n"
        "   - item：弱化的 topic 小字 + 序号圆形徽标(accent 底白字) + 分类大标题(heading)；右上角 watermark(小字半透明)；按第 12 条轮换构图。**不要页码/进度**。\n"
        "     **重点分层（务必做出主次，不要所有要点一样大）**：把 **lines[0]**（本页核心结论）做成一句**焦点句**"
        "（48~52px、加粗、accent 色或加一条 accent 左边框/浅色块底），放在标题下方显眼位置；**lines[1:]** 是 38~40px 的支撑要点列表。"
        "若句中含关键数字/比例（如 3~6个月、50%、10%），把那个数字再放大加粗。但**焦点句也要参与整页铺满**，别让它和图挤在上半屏。\n"
        "   - outro：整屏居中。点赞收藏引导(cta) + 关注(follow，accent 胶囊按钮)；若数据 qr 为 true，**原样保留占位符** {{QR}}。\n"
        "14. 若某条数据带 **_critique** 字段，那是上一版渲染出来被审核挑出的问题，**必须针对性彻底修正**（别再犯）。\n\n"
        "数据（JSON 数组，顺序即分镜顺序）：\n" + json.dumps(data, ensure_ascii=False) + "\n\n"
        '**只返回 JSON**：{"scenes":["<div ...>...</div>", ...]}，数组长度与顺序和输入一一对应。'
        "不要解释、不要 markdown 代码围栏。"
    )


def _llm_scene_html(content, scenes, illustrations, style, aspect, brand,
                    follow_text, qr_uri, cb) -> list | None:
    """一次 LLM 调用，为全部分镜生成自包含内层 HTML（与 scenes 等长）。
    传入每个分镜的数据 + 插画 file:// 路径 + 主题色 + 画布/安全区，LLM 填充返回。
    多次失败返回 None，由调用方回退到内置模板 _scene_html（兜底防空白）。"""
    if not scenes:
        return None
    data, w, h, safe, portrait = _scene_payload(
        content, scenes, illustrations, style, aspect, brand, follow_text, qr_uri)
    return _gen_scene_html_batch(data, w, h, safe, portrait, cb)


def _scene_payload(content, scenes, illustrations, style, aspect, brand, follow_text, qr_uri):
    """把每个分镜整理成喂给 LLM 的数据 + 画布参数。返回 (data, w, h, safe, portrait)。"""
    # 品牌自动补 @ 前缀（用户没打 @ 时），用于水印与片尾关注。
    brand = (brand or "").strip()
    if brand and not brand.startswith(("@", "＠")):
        brand = "@" + brand
    spec = (image_studio._STYLES.get(style or "")
            or image_studio._STYLES[next(iter(image_studio._STYLES))])
    palette = spec["palette"] or image_studio._PALETTE_XHS
    base_accent = image_studio.readable_accent(content.get("accent_color") or spec["default_accent"])
    topic = content.get("title", "")
    w, h = RESOLUTIONS.get(aspect, RESOLUTIONS["9:16"])
    portrait = aspect == "9:16"
    safe = int(h * (0.16 if portrait else 0.10))

    data = []
    for sc in scenes:
        idx = sc.get("item_idx")
        c_accent = (image_studio.readable_accent(palette[idx % len(palette)][1])
                    if idx is not None else base_accent)
        img = illustrations.get(idx) if idx is not None else None
        d = {
            "kind": sc["kind"],
            "number": sc.get("badge", ""),
            "heading": sc.get("heading", ""),
            "lines": [x for x in (sc.get("lines") or []) if x],
            "accent": c_accent,
            "img": image_studio._img_file_uri(img) if img and os.path.exists(img) else "",
        }
        if sc["kind"] == "intro":
            d["topic"] = topic
            d["highlight"] = content.get("title_highlight", "")
        elif sc["kind"] == "outro":
            d["cta"] = follow_text or sc.get("cta", "")
            d["follow"] = brand
            d["qr"] = bool(qr_uri)
        else:
            d["topic"] = topic
            d["watermark"] = brand
        data.append(d)
    return data, w, h, safe, portrait


def _gen_scene_html_batch(data, w, h, safe, portrait, cb) -> list | None:
    """一次 LLM 调用为 data 里每个分镜生成自包含内层 HTML（与 data 等长）。
    data 项若带 _critique（上一版渲染问题），提示词会要求针对性修正。失败返回 None。"""
    if not data:
        return None
    prompt = _build_scene_html_prompt(data, w, h, safe, portrait)
    for attempt in range(3):
        try:
            raw = _generate_text_with_retry(prompt, cb)
            obj = raw if isinstance(raw, dict) else vision._parse_json(raw)
            arr = obj.get("scenes") if isinstance(obj, dict) else None
            if isinstance(arr, list) and len(arr) == len(data):
                out = [str(x).strip() for x in arr]
                if all(s.startswith("<") for s in out):
                    return out
            _notify(cb, "scene_img", f"LLM 分镜 HTML 不合规，重试（{attempt + 1}/3）…")
        except Exception as e:
            _notify(cb, "scene_img", f"LLM 分镜 HTML 出错，重试（{attempt + 1}/3）：{e}")
    _notify(cb, "scene_img", "LLM 分镜 HTML 多次失败，临时回退内置模板渲染")
    return None


def _review_scene_pngs(png_paths: list, safe: int, cb) -> dict:
    """用视觉模型审核已渲染的分镜图，挑出版面有明显问题的页。
    返回 {下标: 问题描述}；视觉模型不可用/解析失败则返回 {}（视为全部通过，不阻塞出图）。"""
    if not png_paths:
        return {}
    prompt = (
        "这些是一条竖屏短视频的分镜图（按我给的顺序，下标从 0 开始）。"
        "请逐张审核**版面质量**，挑出有以下**明显问题**的页（宁严勿松）：\n"
        "- 文字和图片重叠、文字被图片/边缘裁切、文字溢出屏幕；\n"
        "- **下半屏或某一大块区域大片空白**、内容挤在顶部/一角没铺满；\n"
        "- **正文/标题字号过小**（远看费劲）、或主次混乱没有重点；\n"
        "- 图片显示不全/被裁、或图片被拉成长条、图占满屏把文字挤走；\n"
        "- 内容明显不平衡、不像设计好的成品。\n"
        "只有排版饱满、主次分明、协调美观的页才算通过。\n"
        '只返回 JSON：{"bad":[{"index":下标,"problem":"一句话说清问题"}]}，没有问题就返回 {"bad":[]}。'
        "不要解释、不要代码围栏。"
    )
    bad = {}
    chunk = 4  # 视觉接口单次最多 5 张图，分块审核后按全局下标合并
    try:
        for base in range(0, len(png_paths), chunk):
            group = png_paths[base:base + chunk]
            raw = vision.analyze_images(prompt, group)
            obj = vision._parse_json(raw)
            for it in (obj.get("bad") or []):
                i = base + int(it.get("index", -1))
                if base <= i < base + len(group):
                    bad[i] = str(it.get("problem", "版面有问题")).strip()
    except Exception as e:
        _notify(cb, "scene_img", f"分镜审核跳过（视觉模型不可用）：{e}")
        return {}
    if bad:
        _notify(cb, "scene_img", f"分镜审核：{len(bad)} 页需返工 → {sorted(bad)}")
    else:
        _notify(cb, "scene_img", "分镜审核：全部通过")
    return bad


# --------------------------------------------------------------------------- #
# 内置模板分镜（兜底：LLM 不可用时用）
# --------------------------------------------------------------------------- #
def _plan_item_layout(scene, order_idx, has_illus) -> str:
    """
    像 PPT 一样为每个条目分镜规划版式，避免每页千篇一律：
      - image_top：插画在上 + 标题 + 要点（主力版式）
      - big_number：超大序号强调 + 标题 + 一句话（适合要点很少的页）
      - statement：居中大字陈述（节奏页，无插画）
    按固定节奏轮换 + 依内容守卫，整体观感有设计感且不破版。
    """
    i = order_idx or 0
    # 相邻页版式必不同，整体像 PPT：图上要点 / 图下要点 / 大数字 / 居中陈述 轮换。
    rotation = ["image_top", "image_bottom", "big_number", "statement"]
    base = rotation[i % len(rotation)]
    # 需要插画的版式在没有插画时退化为居中陈述。
    if base in ("image_top", "image_bottom") and not has_illus:
        base = "statement"
    return base


def _scene_html(content, scene, illus_path, style, font, aspect, template=None, skin_path=None,
                brand="", follow_text="", qr_uri=None):
    # 品牌自动补 @ 前缀（用户没打 @ 时）。
    brand = (brand or "").strip()
    if brand and not brand.startswith(("@", "＠")):
        brand = "@" + brand
    # 「跟随参考图」：用反推规格(背景底图/手写字体/配色)渲染每个场景，与卡片风格统一。
    if style == image_studio.REFERENCE_STYLE and template:
        spec = image_studio._reference_spec(template, skin_path)
    else:
        spec = (image_studio._STYLES.get(style or "")
                or image_studio._STYLES[next(iter(image_studio._STYLES))])
    palette = spec["palette"] or image_studio._PALETTE_XHS
    accent = image_studio.readable_accent(content.get("accent_color") or spec["default_accent"])
    bg, fg, box, muted, tfont = spec["bg"], spec["fg"], spec["box"], spec["muted"], spec["tfont"]
    w, h = RESOLUTIONS[aspect]
    portrait = aspect == "9:16"

    idx = scene.get("item_idx")
    c_accent = palette[idx % len(palette)][1] if idx is not None else accent
    c_accent = image_studio.readable_accent(c_accent)
    topic = content.get("title", "")

    illus_html = ""
    if illus_path and os.path.exists(illus_path):
        illus_html = f'<div class="illus"><img src="{image_studio._img_data_uri(illus_path)}"/></div>'

    if scene["kind"] == "intro":
        title_html = image_studio._highlight_title(scene["heading"], content.get("title_highlight", ""), accent)
        body = f'<div class="intro-spark">✨</div><div class="intro-title">{title_html}</div>'
        if scene["lines"]:
            body += f'<div class="intro-sub">{scene["lines"][0]}</div>'
        body += '<div class="intro-hint">↓ 一起来看 ↓</div>'
    elif scene["kind"] == "outro":
        _ftext = follow_text or scene.get("cta", "")
        _qr = f'<div class="qr"><img src="{qr_uri}"/></div>' if qr_uri else ""
        _follow = f"➕ {brand}" if brand else "➕ 关注 · 学更多"
        body = (
            '<div class="outro-icons"><span>👍</span><span>💖</span><span>⭐</span></div>'
            f'<div class="outro">{_ftext}</div>'
            f'{_qr}'
            f'<div class="outro-follow">{_follow}</div>'
        )
    else:
        # 【PPT 式正文页】统一版式：主题(小) → 序号+分类大标题 → 插画 → 焦点句(核心结论) → 要点卡片。
        # 用普通文档流 + flex 居中，**绝不用 overflow:hidden 的固定格子**（杜绝大数字/长文被裁），无页码。
        _all = [ln for ln in (scene.get("lines") or []) if ln]
        badge = f'<span class="badge" style="background:{c_accent}">{scene["badge"]}</span>'
        head_div = (f'<div class="s-head">{badge}'
                    f'<span class="s-title" style="color:{c_accent}">{scene["heading"]}</span></div>')
        brand_div = f'<div class="brand">{topic}</div>'
        focus_html, rest = "", _all
        if _all:  # 第一条通常是核心结论 → 做成焦点句强调
            focus_html = (f'<div class="focus" style="border-color:{c_accent};color:{c_accent}">'
                          f'{_all[0]}</div>')
            rest = _all[1:]
        bullets_html = ""
        if rest:  # 其余做成 PPT 式要点卡片（带主题色圆点、卡片底，不是纯文本列表）
            lis = "".join(
                f'<li style="animation-delay:{0.45 + i * 0.12:.2f}s">'
                f'<span class="dot" style="background:{c_accent}"></span>'
                f'<span class="btxt">{ln}</span></li>'
                for i, ln in enumerate(rest[:4]))
            bullets_html = f'<ul class="bullets">{lis}</ul>'
        if portrait:
            # 竖版：自上而下堆叠（图在标题下方）。
            body = f'{brand_div}{head_div}{illus_html}{focus_html}{bullets_html}'
        elif illus_html:
            # 横版：左图右文，充分利用宽度（短高画布不适合纵向堆叠）。
            body = (f'{brand_div}<div class="scene-row"><div class="row-img">{illus_html}</div>'
                    f'<div class="row-txt">{head_div}{focus_html}{bullets_html}</div></div>')
        else:
            body = f'{brand_div}{head_div}{focus_html}{bullets_html}'
        scene["layout"] = "ppt_item"

    # 竖屏纵向堆叠、横屏也纵向（图在上）；尺寸随比例放大。
    head_sz = 84 if portrait else 70
    line_sz = 46 if portrait else 40
    # 插画尺寸随要点条数自适应收缩，确保「主题+插画+标题+要点」整屏放得下、不裁切。
    _nl = min(5, len(scene.get("lines") or []))
    if portrait:
        illus_sz = 480 if _nl <= 2 else (400 if _nl == 3 else (350 if _nl == 4 else 300))
    else:
        illus_sz = 300 if _nl <= 2 else (260 if _nl == 3 else 220)  # 横版高度小，插画更小
    # 内容整体垂直居中，避免顶部堆叠、底部大片留白；仅老回退版式(image_top/bottom)保持顶对齐防裁切。
    justify = "flex-start" if scene.get("layout") in ("image_top", "image_bottom") else "center"
    # 每页底部品牌水印（片尾页已突出展示品牌，不再重复加水印）。
    _wm = f'<div class="watermark">{brand}</div>' if (brand and scene["kind"] != "outro") else ""

    return f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8"><style>
{image_studio._fontface_css()}
* {{ box-sizing:border-box; margin:0; padding:0; }}
@keyframes fadeUp {{ from {{ opacity:0; transform:translateY(48px); }} to {{ opacity:1; transform:translateY(0); }} }}
@keyframes fadeIn {{ from {{ opacity:0; }} to {{ opacity:1; }} }}
@keyframes pop {{ 0% {{ opacity:0; transform:scale(0.3); }} 70% {{ transform:scale(1.12); }} 100% {{ opacity:1; transform:scale(1); }} }}
@keyframes kenburns {{ from {{ transform:scale(1.0); }} to {{ transform:scale(1.10); }} }}
@keyframes popImg {{ 0% {{ opacity:0; transform:scale(0.85); }} 100% {{ opacity:1; transform:scale(1); }} }}
html,body {{ width:{w}px; height:{h}px; overflow:hidden; }}
/* 底部预留字幕安全区（约 23% 高度），内容在其上方居中，避免与字幕重叠 */
body {{ font-family:{image_studio._FONT_SANS}; background:{bg}; color:{fg}; position:relative;
  display:flex; flex-direction:column; align-items:center; justify-content:{justify};
  text-align:center; padding:{84 if portrait else 50}px {70 if portrait else 90}px {int(h * (0.16 if portrait else 0.10))}px; }}
.watermark {{ position:absolute; top:{int(h * 0.018)}px; right:{40 if portrait else 56}px;
  font-family:{tfont}; font-size:{30 if portrait else 26}px; color:{muted}; opacity:0.7; letter-spacing:1px; }}
.qr {{ width:{260 if portrait else 220}px; height:{260 if portrait else 220}px; background:#fff; border-radius:18px;
  padding:14px; box-shadow:0 6px 20px rgba(0,0,0,0.18); margin:30px 0 10px; flex-shrink:0; animation:pop .6s .3s ease both; }}
.qr img {{ width:100%; height:100%; object-fit:contain; image-rendering:pixelated; }}
/* 左右布局（arrange=row，多用于横版）：左图右文 */
.row {{ display:flex; flex-direction:row; align-items:center; justify-content:center; gap:56px; width:100%; }}
.col-img {{ flex:0 0 42%; display:flex; justify-content:center; align-items:center; }}
/* 横版图片自适应左列宽度(保持方形)，不再固定小尺寸强塞 */
.col-img .illus {{ width:100%; max-width:560px; height:auto; aspect-ratio:1/1; margin-bottom:0; }}
.col-txt {{ flex:1; text-align:left; }}
.col-txt .s-head {{ justify-content:flex-start; }}
.col-txt .lines {{ max-width:100%; }}
.col-txt .statement {{ margin-left:0; max-width:100%; }}
.col-txt .statval, .col-txt .bigsum {{ text-align:left; }}
/* 【二维网格版面】LLM 指定每个元素 col/row/w/h，用 CSS Grid 精确定位（横向+纵向）。
   横竖各自的网格(竖 9×16 / 横 16×9)，格子近似正方形，与 image_studio._grid_dims 对齐 */
.grid {{ display:grid; width:100%; flex:1 1 0%; min-height:0; align-self:stretch;
  grid-template-columns:repeat({9 if portrait else 16},1fr); grid-template-rows:repeat({16 if portrait else 9},1fr); gap:{18 if portrait else 16}px; }}
.cell {{ display:flex; flex-direction:column; justify-content:center; overflow:hidden; min-width:0; min-height:0; }}
.grid .s-head {{ margin:0; flex-wrap:wrap; }}
.grid .lines {{ max-width:100%; }}
.grid .lines li {{ margin:6px 0; font-size:{40 if portrait else 34}px; line-height:1.4; }}
.grid .statement, .grid .note {{ max-width:100%; margin:0; }}
/* 网格内的大数字/陈述要收住字号，避免撑爆格子 */
.grid .statval {{ font-size:{120 if portrait else 96}px; max-width:100%; margin:4px 0; line-height:1.0; }}
.grid .statement {{ font-size:{56 if portrait else 48}px; line-height:1.35; }}
.grid .bigsum {{ max-width:100%; margin:4px 0; }}
.gimg {{ width:100%; height:100%; display:flex; align-items:center; justify-content:center; }}
.gimg img {{ max-width:100%; max-height:100%; width:auto; height:auto; object-fit:contain;
  border-radius:28px; background:{box}; box-shadow:0 12px 40px rgba(0,0,0,0.18); animation:popImg .6s ease both; }}
/* 正文页主题小字：固定在左上角，与右上水印对成一条页眉，单行省略、不随内容上下浮动 */
.brand {{ position:absolute; top:{int(h * 0.018)}px; left:{70 if portrait else 90}px;
  font-family:{image_studio._FONT_SANS}; font-size:{30 if portrait else 26}px; font-weight:600;
  color:{muted}; opacity:0.8; max-width:{w - 420}px; text-align:left; line-height:1.3;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis; animation:fadeIn .5s ease both; }}
/* 插画：方形容器 + contain，整张完整显示、绝不裁切 */
.illus {{ width:{illus_sz}px; height:{illus_sz}px; flex-shrink:0; border-radius:32px; overflow:hidden; background:{box};
  box-shadow:0 12px 40px rgba(0,0,0,0.18); margin:{8 if portrait else 6}px 0 {30 if portrait else 18}px;
  animation:popImg .6s ease both; }}
.illus img {{ width:100%; height:100%; object-fit:contain; transform-origin:center;
  animation:kenburns 9s ease-out both; }}
.s-head {{ display:flex; align-items:center; justify-content:center; gap:20px; margin-bottom:{18 if portrait else 14}px; flex-wrap:wrap; }}
.badge {{ color:#fff; min-width:72px; height:72px; border-radius:50%; display:inline-flex; align-items:center;
  justify-content:center; font-weight:900; font-size:44px; box-shadow:0 4px 12px rgba(0,0,0,0.25);
  animation:pop .55s .15s ease both; }}
.s-title {{ font-family:{tfont}; font-size:{head_sz}px; font-weight:900; animation:fadeUp .6s .25s ease both; }}
/* 焦点句：本页核心结论，accent 色块 + 左边框，第一眼焦点 */
.focus {{ font-family:{tfont}; font-weight:900; font-size:{56 if portrait else 46}px; line-height:1.35;
  max-width:{w - 150}px; text-align:left; padding:{18 if portrait else 14}px {28 if portrait else 22}px;
  border-left:{14 if portrait else 10}px solid; border-radius:14px; background:rgba(255,255,255,0.55);
  margin:{8 if portrait else 6}px 0 {24 if portrait else 16}px; animation:fadeUp .6s .2s ease both; }}
/* PPT 式要点卡片（圆点 + 卡片底，非纯文本列表） */
.bullets {{ list-style:none; max-width:{w - 150}px; width:100%; display:flex; flex-direction:column;
  gap:{16 if portrait else 12}px; }}
.bullets li {{ display:flex; align-items:flex-start; gap:18px; text-align:left;
  font-size:{line_sz}px; line-height:1.4; color:{fg}; background:rgba(255,255,255,0.5);
  border-radius:16px; padding:{18 if portrait else 14}px {24 if portrait else 20}px;
  box-shadow:0 3px 12px rgba(0,0,0,0.06); opacity:0; animation:fadeUp .55s ease both; }}
.bullets .dot {{ flex-shrink:0; width:18px; height:18px; border-radius:50%; margin-top:{14 if portrait else 12}px; }}
.bullets .btxt {{ flex:1; }}
/* 横版正文页：左图右文两栏，充分利用宽度 */
.scene-row {{ display:flex; flex-direction:row; align-items:center; justify-content:center; gap:70px; width:100%; }}
.scene-row .row-img {{ flex:0 0 40%; display:flex; justify-content:center; align-items:center; }}
.scene-row .row-img .illus {{ width:100%; max-width:480px; height:auto; aspect-ratio:1/1; margin:0; }}
.scene-row .row-txt {{ flex:1; display:flex; flex-direction:column; align-items:flex-start; gap:16px; text-align:left; }}
.scene-row .row-txt .s-head {{ justify-content:flex-start; margin-bottom:0; }}
.scene-row .row-txt .focus {{ margin:0; }}
.scene-row .row-txt .bullets {{ max-width:100%; }}
.lines {{ list-style:none; max-width:{w - 160}px; }}
.lines li {{ font-size:{line_sz}px; line-height:1.55; margin:14px 0; color:{fg}; opacity:0;
  animation:fadeUp .55s ease both; }}
.progress {{ margin-top:40px; font-size:40px; font-weight:800; opacity:0.8; animation:fadeIn .6s .9s ease both; }}
/* PPT 式版式：大数字强调页 / 居中陈述页 */
.s-head.center {{ justify-content:center; }}
.bignum {{ font-family:{tfont}; font-size:{420 if portrait else 300}px; font-weight:900; line-height:0.95;
  flex-shrink:0; margin-bottom:6px; animation:pop .6s ease both; }}
.s-title.big {{ font-size:{head_sz + 14}px; margin-bottom:24px; }}
.bigsum {{ font-size:{line_sz + 8}px; color:{fg}; max-width:{w - 200}px; line-height:1.5; opacity:0.92;
  animation:fadeUp .6s .25s ease both; }}
.statval {{ font-family:{tfont}; font-size:{210 if portrait else 130}px; font-weight:900; line-height:1.0;
  flex-shrink:0; letter-spacing:1px; margin:6px 0; max-width:{w - 80}px; animation:pop .6s ease both; }}
.statement {{ font-family:{tfont}; font-size:{head_sz - 2}px; font-weight:900; color:{fg}; line-height:1.45;
  max-width:{w - 200}px; margin:30px 0; padding:8px 0 8px 36px; border-left:12px solid; text-align:left;
  animation:fadeUp .6s .2s ease both; }}
.note {{ font-size:{line_sz - 2}px; color:{fg}; opacity:0.92; background:rgba(255,255,255,0.55);
  border-left:8px solid; border-radius:12px; padding:16px 22px; max-width:{w - 200}px; margin:18px 0;
  text-align:left; animation:fadeUp .55s .3s ease both; }}
.intro-spark {{ font-size:90px; margin-bottom:14px; animation:pop .6s ease both; }}
.intro-title {{ font-family:{tfont}; font-size:{head_sz + 24}px; font-weight:900; line-height:1.2;
  animation:fadeUp .7s .1s ease both; }}
.intro-title .hl {{ color:{accent}; background:linear-gradient(transparent 60%, {accent}33 60%); padding:0 8px; }}
.intro-sub {{ font-size:{line_sz + 6}px; color:{muted}; font-weight:600; margin-top:34px;
  animation:fadeUp .7s .35s ease both; }}
.intro-hint {{ margin-top:54px; font-size:{line_sz}px; font-weight:800; color:{accent};
  animation:fadeUp .7s .7s ease both; }}
.outro-icons {{ font-size:110px; margin-bottom:30px; }}
.outro-icons span {{ display:inline-block; margin:0 16px; animation:pop .6s ease both; }}
.outro-icons span:nth-child(2) {{ animation-delay:.18s; }}
.outro-icons span:nth-child(3) {{ animation-delay:.36s; }}
.outro {{ font-family:{tfont}; font-size:{head_sz}px; font-weight:900; color:{fg}; max-width:{w - 200}px;
  line-height:1.4; animation:fadeUp .7s .3s ease both; }}
.outro-follow {{ margin-top:48px; font-size:{line_sz + 4}px; font-weight:900; color:#fff; background:{accent};
  padding:20px 52px; border-radius:60px; box-shadow:0 8px 24px {accent}66; animation:pop .6s .5s ease both; }}
{image_studio._font_override_css(font)}
</style></head><body>{body}{_wm}</body></html>"""


# --------------------------------------------------------------------------- #
# TTS + 合成
# --------------------------------------------------------------------------- #
def _tts_scene(text: str, voice_name: str, out_mp3: str) -> tuple[float, list]:
    """
    对一句旁白做 TTS，写入 out_mp3。
    返回 (时长秒, 词级时间戳列表[(文字, start_s, end_s)])，用于卡拉OK字幕。
    """
    from html import unescape

    cues = []
    try:
        sm = voice.tts(text=text, voice_name=voice_name, voice_rate=1.0,
                       voice_file=out_mp3, voice_volume=1.0)
        if os.path.exists(out_mp3) and os.path.getsize(out_mp3) > 0:
            dur = max(1.5, voice.get_audio_duration(out_mp3))
            if sm is not None and getattr(sm, "cues", None):
                for cue in sm.cues:
                    try:
                        cues.append((unescape(cue.content),
                                     cue.start.total_seconds(), cue.end.total_seconds()))
                    except Exception:
                        pass
            return dur, cues
    except Exception as e:
        logger.warning(f"tts failed, fallback to silent: {e}")
    return max(2.0, len(text) / 5.0), cues


# --------------------------------------------------------------------------- #
# 逐字高亮字幕（ASS karaoke + ffmpeg 烧录）
# --------------------------------------------------------------------------- #
def _ass_color(hex_color: str) -> str:
    h = (hex_color or "#ffffff").lstrip("#")
    if len(h) != 6:
        h = "ffffff"
    return f"&H00{h[4:6]}{h[2:4]}{h[0:2]}".upper()  # ASS 是 &HAABBGGRR


def _ass_time(t: float) -> str:
    t = max(0.0, t)
    hh = int(t // 3600)
    mm = int((t % 3600) // 60)
    ss = t % 60
    return f"{hh}:{mm:02d}:{ss:05.2f}"


def _build_ass(cues_list: list, scene_durs: list, scene_starts: list, width: int, height: int,
               accent: str, out_ass: str) -> str:
    """根据每个场景的词级时间戳生成 ASS karaoke 字幕（当前词高亮）。scene_starts 为各场景在成片时间轴上的起始秒。"""
    # 字幕字色随主题 accent（过浅则压暗保证可读），配深色描边，像电影字幕又有品牌感。
    primary = _ass_color(image_studio.readable_accent(accent))
    secondary = "&H00FFFFFF"
    fs = max(26, int(height * 0.036))
    marginv = int(height * 0.13)
    marginh = int(width * 0.10)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,ZCOOL KuaiLe,{fs},{primary},{secondary},&H00202020,&H88000000,-1,0,0,0,100,100,1,0,1,6,3,2,{marginh},{marginh},{marginv},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, Effect, Text
"""
    # 电影字幕：每行宽度收窄(更易读)，一条最多 2 行；优先在停顿处断句。
    per_line = max(8, int((width - 2 * marginh) / fs * 0.78))
    hard_units = per_line * 2          # 一条字幕的字宽上限（≈2 行）
    GAP = 0.26                         # 词间停顿≥0.26s 视为标点/分句边界

    def _disp(s):  # 清洗显示文本：去花括号/反斜杠/句末标点（中间停顿靠 gap 断句）
        s = str(s).replace("{", "").replace("}", "").replace("\\", "")
        return s.translate(str.maketrans("", "", "，。、！？；：,.!?;:　 "))

    def _units(s):
        return sum(1.0 if ord(ch) > 0x2E80 else 0.5 for ch in s)

    events = []
    for i, dur in enumerate(scene_durs):
        base = scene_starts[i] if i < len(scene_starts) else 0.0
        next_scene = scene_starts[i + 1] if i + 1 < len(scene_starts) else (base + dur)
        cues = cues_list[i] if i < len(cues_list) else []
        if not cues:
            continue

        # 1) 断句：遇到停顿(gap)或超过整条上限就开新一条，模拟电影字幕的自然分句
        groups, cur, cur_u, prev_end = [], [], 0.0, None
        for c in cues:
            u = _units(_disp(c[0]))
            gap = (c[1] - prev_end) if prev_end is not None else 0.0
            if cur and (gap >= GAP or cur_u + u > hard_units):
                groups.append(cur)
                cur, cur_u = [], 0.0
            cur.append(c)
            cur_u += u
            prev_end = c[2]
        if cur:
            groups.append(cur)

        # 2) 每条整句显示(白字+描边)；按 per_line 折行；裁到下一条/下一场景，留最短停留
        for gi, g in enumerate(groups):
            g_start = base + g[0][1]
            boundary = base + groups[gi + 1][0][1] if gi + 1 < len(groups) else next_scene
            g_end = min(base + g[-1][2] + 0.15, boundary)
            if g_end - g_start < 0.6:                     # 太短会一闪而过
                g_end = min(g_start + 0.8, boundary)
            line_u, parts = 0.0, []
            for content, cs, ce in g:
                d = _disp(content)
                u = _units(d)
                if parts and line_u + u > per_line:        # 折到第二行
                    parts.append("\\N")
                    line_u = 0.0
                parts.append(d)
                line_u += u
            text = "".join(parts)
            events.append(f"Dialogue: 0,{_ass_time(g_start)},{_ass_time(g_end)},Cap,,0,0,,{text}")
    with open(out_ass, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(events) + "\n")
    return out_ass


def _burn_ass(in_mp4: str, ass_path: str, out_mp4: str) -> str:
    """用 ffmpeg 把 ASS 字幕烧录进视频（字幕用项目内置的站酷快乐体）。"""
    ffmpeg = video_svc.get_ffmpeg_binary()
    d = os.path.dirname(os.path.abspath(ass_path))
    name = os.path.basename(ass_path)  # 用相对名 + cwd 规避 Windows 路径转义问题
    # 把品牌字体拷到 ass 同目录，用 fontsdir=. 让 libass 找到（避免 Windows 路径转义）。
    try:
        src_font = os.path.join(utils.font_dir(), "ZCOOLKuaiLe-Regular.ttf")
        if os.path.exists(src_font):
            shutil.copyfile(src_font, os.path.join(d, "ZCOOLKuaiLe-Regular.ttf"))
    except Exception:
        pass
    cmd = [ffmpeg, "-y", "-i", os.path.abspath(in_mp4), "-vf", f"ass={name}:fontsdir=.",
           "-c:a", "copy", os.path.abspath(out_mp4)]
    r = subprocess.run(cmd, cwd=d, capture_output=True, text=True, timeout=600)
    if r.returncode != 0 or not os.path.exists(out_mp4):
        raise RuntimeError(f"ffmpeg ass burn failed: {(r.stderr or '')[-400:]}")
    return out_mp4


def _run_ffmpeg(cmd, timeout=900):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {(r.stderr or '')[-400:]}")
    return r


# 场景间转场（ffmpeg xfade）。显示名 → xfade 名（random=每个切换轮换不同效果）。
TRANSITION_DUR = 0.45
TRANSITIONS = {
    "平滑滑动": "smoothleft",
    "横向滑入": "slideleft",
    "向上推": "slideup",
    "放大": "zoomin",
    "溶解": "dissolve",
    "圆形展开": "circleopen",
    "淡入淡出": "fade",
    "随机混合": "random",
}
_RANDOM_POOL = ["smoothleft", "smoothright", "slideup", "zoomin", "dissolve",
                "circleopen", "fadeblack", "wiperight", "smoothdown"]


def _transition_dur(durations):
    return min(TRANSITION_DUR, (min(durations) if durations else 1.0) * 0.4)


def _timeline(durations, td):
    """各场景在成片时间轴上的起始秒（相邻场景交叠 td），及总时长。"""
    starts, acc = [], 0.0
    for d in durations:
        starts.append(acc)
        acc += d - td
    total = acc + td if durations else 0.0
    return starts, total


def _pick_transition(transition, cut_index):
    if transition == "random":
        return _RANDOM_POOL[(cut_index - 1) % len(_RANDOM_POOL)]
    return transition or "fade"


def _assemble(scene_videos, scene_audios, durations, aspect, out_path,
              with_bgm=True, transition="smoothleft"):
    """
    用 ffmpeg 拼接，场景间用 xfade 转场：
      1) 每场景编码成统一参数的 mp4 分段（裁到目标时长 + 配音，无配音补静音）；
      2) xfade 链式转场拼视频 + 各场景音频按起始时间 adelay 后混合（一遍编码）；
      3) 叠加循环 BGM（视频流拷贝、仅混音频）。
    """
    ff = video_svc.get_ffmpeg_binary()
    w, h = RESOLUTIONS[aspect]
    work = os.path.dirname(os.path.abspath(out_path))
    seg_dir = os.path.join(work, "_seg_" + os.path.splitext(os.path.basename(out_path))[0])
    os.makedirs(seg_dir, exist_ok=True)

    # 1) 分段（不在分段内做淡入淡出，转场交给 xfade；整体首尾再加淡入淡出）
    # tpad 克隆末帧把视频补到至少 dur 秒，再用 -t dur 裁到精确长度：保证 segment 时长
    # 恰好等于配音时长，避免录制偏短导致 xfade 偏移错位、画面提前定格。
    segs = []
    for i, (vid, mp3, dur) in enumerate(zip(scene_videos, scene_audios, durations)):
        seg = os.path.join(seg_dir, f"seg{i}.mp4")
        vf = (f"scale={w}:{h},fps=30,format=yuv420p,setsar=1,"
              f"tpad=stop_mode=clone:stop_duration=20")
        has_audio = bool(mp3 and os.path.exists(mp3) and os.path.getsize(mp3) > 0)
        cmd = [ff, "-y", "-loglevel", "error", "-i", os.path.abspath(vid)]
        if has_audio:
            cmd += ["-i", os.path.abspath(mp3)]
        else:
            cmd += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
        cmd += ["-t", f"{dur:.2f}", "-vf", vf, "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                "-af", "apad", "-c:a", "aac", "-ar", "44100", "-ac", "2",
                "-video_track_timescale", "30000", seg]
        _run_ffmpeg(cmd)
        segs.append(seg)

    n = len(segs)
    td = _transition_dur(durations)
    starts, total = _timeline(durations, td)

    # 2) xfade 视频链 + adelay 音频混合，一遍编码到 assembled.mp4
    inputs = []
    for s in segs:
        inputs += ["-i", os.path.abspath(s)]

    vparts = []
    if n == 1:
        vlast = "[0:v]"
    else:
        prev = "[0:v]"
        for i in range(1, n):
            t = _pick_transition(transition, i)
            lab = f"[vx{i}]"
            vparts.append(
                f"{prev}[{i}:v]xfade=transition={t}:duration={td:.3f}:offset={starts[i]:.3f}{lab}")
            prev = lab
        vlast = prev
    fout_st = max(0.1, total - 0.45)
    vparts.append(f"{vlast}fade=t=in:st=0:d=0.3,fade=t=out:st={fout_st:.3f}:d=0.4[vout]")

    aparts = []
    for i in range(n):
        ms = int(round(starts[i] * 1000))
        aparts.append(f"[{i}:a]adelay={ms}|{ms}[a{i}]")
    amix_in = "".join(f"[a{i}]" for i in range(n))
    aparts.append(f"{amix_in}amix=inputs={n}:normalize=0:dropout_transition=0[aout]")

    filt = ";".join(vparts + aparts)
    assembled = os.path.join(seg_dir, "assembled.mp4")
    _run_ffmpeg([ff, "-y", "-loglevel", "error", *inputs, "-filter_complex", filt,
                 "-map", "[vout]", "-map", "[aout]", "-t", f"{total:.2f}",
                 "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                 "-r", "30", "-c:a", "aac", "-ar", "44100", assembled])

    # 3) BGM（视频流拷贝、仅混音频）
    bgm_file = video_svc.get_bgm_file(bgm_type="random", bgm_file="") if with_bgm else ""
    if bgm_file and os.path.exists(bgm_file):
        fade_st = max(0.0, total - 2)
        filt2 = (f"[1:a]volume=0.12,afade=t=out:st={fade_st:.2f}:d=2[bg];"
                 f"[0:a][bg]amix=inputs=2:duration=first:dropout_transition=0[ao]")
        try:
            # 用 -t 显式定长；不能用 -shortest：它与 -stream_loop -1 的无限 BGM 组合时
            # 会在第一段循环边界就结束，把视频截断到 BGM 单次长度附近。
            _run_ffmpeg([ff, "-y", "-loglevel", "error", "-i", assembled,
                         "-stream_loop", "-1", "-i", os.path.abspath(bgm_file),
                         "-filter_complex", filt2, "-map", "0:v", "-map", "[ao]",
                         "-c:v", "copy", "-c:a", "aac", "-t", f"{total:.2f}",
                         os.path.abspath(out_path)])
        except Exception as e:
            logger.warning(f"bgm mux failed, output without bgm: {e}")
            shutil.copyfile(assembled, out_path)
    else:
        shutil.copyfile(assembled, out_path)

    shutil.rmtree(seg_dir, ignore_errors=True)
    return out_path


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def generate_videos(
    content: dict,
    illustrations: dict,
    out_dir: str,
    style: Optional[str] = None,
    font: Optional[str] = None,
    aspects: Optional[list] = None,
    langs: Optional[list] = None,
    with_bgm: bool = True,
    captions: bool = True,
    voices: Optional[dict] = None,
    transition: str = "smoothleft",
    cb: ProgressCb = None,
    template: Optional[dict] = None,
    skin_path: Optional[str] = None,
    brand: str = "",
    follow_text: str = "",
    qr_path: Optional[str] = None,
    scene_html_dir: Optional[str] = None,
    scene_html_aspect: Optional[str] = None,
) -> dict:
    """
    按「比例 × 语言」批量出片。返回 {f"{aspect}-{lang}": video_path}。

    scene_html_dir/scene_html_aspect：上一步 render_scene_images 已生成的分镜 HTML 目录与其比例。
    若视频的「基准语言(zh) + 相同比例」与之匹配，直接复用那批 scene_NN.html 录制，**不再重复生成**，
    保证视频画面与分镜图完全一致。

    同一语言的旁白只配音一次，在不同比例间复用；场景图按比例分别渲染。
    captions=True 时叠加逐字高亮(卡拉OK)字幕。
    """
    aspects = aspects or ["9:16"]
    langs = langs or ["zh"]
    os.makedirs(out_dir, exist_ok=True)
    qr_uri = (image_studio._img_data_uri(qr_path)
              if qr_path and os.path.exists(qr_path) else None)
    results = {}

    for lang in langs:
        c = content if lang == "zh" else translate_content(content, cb)
        narration = generate_narration(c, lang, cb)  # 口语化口播文案（区别于画面文字）
        scenes = _build_scenes(c, lang, narration)
        voice_name = (voices or {}).get(lang) or VOICES.get(lang, VOICES["zh"])
        accent = c.get("accent_color") or "#2e9e54"

        # 1) 配音（每语言一次，跨比例复用），同时收集词级时间戳用于字幕
        audio_dir = os.path.join(out_dir, f"audio_{lang}")
        os.makedirs(audio_dir, exist_ok=True)
        audios, durations, cues_list = [], [], []
        for si, sc in enumerate(scenes):
            _notify(cb, "tts", f"[{LANG_LABELS[lang]}] 配音 {si + 1}/{len(scenes)}")
            mp3 = os.path.join(audio_dir, f"s{si}.mp3")
            dur, cues = _tts_scene(sc["narration"], voice_name, mp3)
            durations.append(dur + 0.5)
            cues_list.append(cues)
            audios.append(mp3)

        # 2) 每个比例：录制动画场景 + 合成
        for aspect in aspects:
            w, h = RESOLUTIONS[aspect]
            scene_dir = os.path.join(out_dir, f"scenes_{lang}_{aspect.replace(':', 'x')}")
            os.makedirs(scene_dir, exist_ok=True)
            # 优先复用「上方已生成的分镜图」的 HTML：基准语言(zh)+相同比例且文件齐全时直接拿来录制，不重复生成。
            reuse = (scene_html_dir and lang == "zh" and aspect == scene_html_aspect
                     and all(os.path.exists(os.path.join(scene_html_dir, f"scene_{i:02d}.html"))
                             for i in range(len(scenes))))
            if reuse:
                _notify(cb, "scene", f"[{LANG_LABELS[lang]} {aspect}] 复用已生成的分镜 HTML，不再重复生成")
                llm_inner = None
            else:
                # 分镜默认走稳定的确定性 PPT 卡片模板；仅 _llm_scene_html=True 才用 LLM 自由生成。
                llm_inner = (_llm_scene_html(c, scenes, illustrations, style, aspect,
                                             brand, follow_text, qr_uri, cb)
                             if c.get("_llm_scene_html") else None)
            # 先写好（或复用）每个场景 HTML，再用同一个浏览器批量录制（省去逐场景启动开销）。
            jobs, scene_vids = [], []
            for si, sc in enumerate(scenes):
                if reuse:
                    hp = os.path.join(scene_html_dir, f"scene_{si:02d}.html")
                else:
                    if llm_inner is not None:
                        html = _scene_page(llm_inner[si].replace("{{QR}}", _qr_html(qr_uri)), w, h)
                    else:
                        illus = illustrations.get(sc["item_idx"]) if sc["item_idx"] is not None else None
                        html = _scene_html(c, sc, illus, style, font, aspect, template, skin_path,
                                           brand, follow_text, qr_uri)
                    hp = os.path.join(scene_dir, f"s{si}.html")
                    with open(hp, "w", encoding="utf-8") as f:
                        f.write(html)
                webm = os.path.join(scene_dir, f"s{si}.webm")
                # 录制比旁白多 0.6s 缓冲，保证 webm ≥ 音频时长，合成裁剪时不会切掉语音。
                jobs.append({"html_path": hp, "out_path": webm, "width": w, "height": h,
                             "duration_s": durations[si] + 0.6})
                scene_vids.append(webm)
            _notify(cb, "scene", f"[{LANG_LABELS[lang]} {aspect}] 批量录制 {len(jobs)} 个动画场景…")
            record_html_videos_batch(jobs, scale=1)

            out_path = os.path.join(out_dir, f"video_{aspect.replace(':', 'x')}_{lang}.mp4")
            _notify(cb, "compose", f"[{LANG_LABELS[lang]} {aspect}] 合成视频中…")
            _assemble(scene_vids, audios, durations, aspect, out_path, with_bgm, transition)

            # 烧录逐字高亮字幕（失败则保留无字幕版，不阻断）
            if captions and any(cues_list):
                try:
                    _notify(cb, "caption", f"[{LANG_LABELS[lang]} {aspect}] 烧录逐字字幕…")
                    ass = os.path.join(scene_dir, "caption.ass")
                    # 字幕时间轴需与 xfade 交叠后的场景起始一致
                    _starts, _ = _timeline(durations, _transition_dur(durations))
                    _build_ass(cues_list, durations, _starts, w, h, accent, ass)
                    capped = out_path + ".cap.mp4"
                    _burn_ass(out_path, ass, capped)
                    os.replace(capped, out_path)
                except Exception as e:
                    logger.warning(f"caption burn failed, keep no-caption video: {e}")

            results[f"{aspect}-{lang}"] = out_path
            _notify(cb, "compose", f"完成：{os.path.basename(out_path)}")

    return results


def render_scene_images(
    content: dict,
    illustrations: dict,
    out_dir: str,
    style: Optional[str] = None,
    font: Optional[str] = None,
    aspect: str = "9:16",
    lang: str = "zh",
    template: Optional[dict] = None,
    skin_path: Optional[str] = None,
    cb: ProgressCb = None,
    brand: str = "",
    follow_text: str = "",
    qr_path: Optional[str] = None,
) -> list:
    """
    把视频用的每个分镜(片头/每个条目/片尾)渲染成独立静图(二级图片)，与出图步骤一并完成，
    可直接做小红书图集 / 视频素材。返回 PNG 路径列表(按场景顺序)。
    """
    from app.services.html_render import render_html_pngs_batch

    os.makedirs(out_dir, exist_ok=True)
    qr_uri = (image_studio._img_data_uri(qr_path)
              if qr_path and os.path.exists(qr_path) else None)
    scenes = _build_scenes(content, lang, None)  # 静图无需旁白文案
    # 分镜默认走稳定的确定性 PPT 卡片模板（_scene_html，效果好不翻车）；
    # 仅 content["_llm_scene_html"]=True 时才用 LLM 自由生成+视觉审核（方差大，慎用）。
    data, w, h, safe, portrait = _scene_payload(
        content, scenes, illustrations, style, aspect, brand, follow_text, qr_uri)
    inner = (_gen_scene_html_batch(data, w, h, safe, portrait, cb)
             if content.get("_llm_scene_html") else None)

    def _page_html(i):
        if inner is not None:
            return _scene_page(inner[i].replace("{{QR}}", _qr_html(qr_uri)), w, h)
        sc = scenes[i]
        illus = illustrations.get(sc["item_idx"]) if sc["item_idx"] is not None else None
        return _scene_html(content, sc, illus, style, font, aspect, template, skin_path,
                           brand, follow_text, qr_uri)

    def _render(indices):
        jobs = []
        for i in indices:
            hp = os.path.join(out_dir, f"scene_{i:02d}.html")
            with open(hp, "w", encoding="utf-8") as f:
                f.write(_page_html(i))
            jobs.append({"html_path": hp, "out_path": os.path.join(out_dir, f"scene_{i:02d}.png"),
                         "width": w, "height": h, "scale": 1})
        render_html_pngs_batch(jobs, scale=1)

    pngs = [os.path.join(out_dir, f"scene_{i:02d}.png") for i in range(len(scenes))]
    _notify(cb, "scene_img", f"渲染 {len(scenes)} 张分镜图（{aspect}）…")
    _render(range(len(scenes)))

    # 审核矫正：渲染后用视觉模型挑出版面坏页 → 带着问题针对性重生成 → 只重渲这些页（最多 2 轮）。
    if inner is not None:
        for _round in range(2):
            bad = _review_scene_pngs(pngs, safe, cb)
            if not bad:
                break
            sub = [dict(data[i], _critique=bad[i]) for i in bad]
            fixed = _gen_scene_html_batch(sub, w, h, safe, portrait, cb)
            if not fixed:
                break
            for k, i in enumerate(bad):
                inner[i] = fixed[k]
            _notify(cb, "scene_img", f"重渲 {len(bad)} 张返工分镜…")
            _render(list(bad))

    return [p for p in pngs if os.path.exists(p)]
