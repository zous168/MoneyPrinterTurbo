"""
知识卡片信息图生成 pipeline（5 步）。

输入：一个主题 + 一张模板信息图。

流程：
  1. 输入主题并上传模板图片（由调用方完成，传入 subject 与 template_image_path）
  2. 反推模板：用视觉大模型理解模板图片的版式结构，提取结构化的 schema
  3. 组织知识数据：基于模板 schema 与新主题，生成填充用的结构化内容 + 文案
  4. 审核矫正：把生成内容与原模板图一起交给视觉模型审核，不通过则按建议矫正回填，循环若干轮
  5. 生成图片：为每张卡片文生图插画 → 渲染 HTML 模板 → 无头浏览器截图出成品

对外主入口：run_pipeline(...)；各步骤也单独暴露，供 WebUI 分步调用。
"""

import base64
import json
import os
import time
from typing import Callable, Optional

from loguru import logger

from app.config import config
from app.models.schema import ImageStudioRequest
from app.services import image_gen, vision
from app.services.html_render import render_html_to_png
from app.utils import utils

ProgressCb = Optional[Callable[[str, str], None]]


def _notify(cb: ProgressCb, stage: str, msg: str):
    logger.info(f"[image_studio][{stage}] {msg}")
    if cb:
        try:
            cb(stage, msg)
        except Exception as e:  # 进度回调不应影响主流程
            logger.warning(f"progress callback error: {e}")


# --------------------------------------------------------------------------- #
# 步骤 2：反推模板结构
# --------------------------------------------------------------------------- #
_REVERSE_PROMPT = """你是一名资深的信息图（infographic）版式分析师。
请仔细观察这张知识卡片信息图，反推出它的**版式结构模板**（注意：只分析版式结构与风格，不要照抄它的具体文字内容）。

请用 JSON 返回以下字段：
{
  "layout_kind": "二选一：'grid' 或 'steps'。grid=多张卡片并排成网格（如 N 宫格清单）；steps=自上而下的步骤/流程列表，每一步是一个小标题+一段说明文字，通常右侧或下方配一张插图（如食谱、教程、操作流程）。",
  "layout_type": "对整体版式的简述，如 '顶部标题 + N宫格知识卡片 + 底部总结区' 或 '顶部标题 + 竖向N步骤流程 + 底部贴士'",
  "title": {"has_highlight": true/false, "highlight_color": "高亮关键词的大致颜色十六进制", "style_note": "标题风格，如手写体/加粗"},
  "subtitle_present": true/false,
  "card_count": 卡片数量(整数，grid 版式用),
  "step_count": 步骤数量(整数，steps 版式用),
  "has_tips": true/false,
  "grid_columns": 每行卡片数(整数),
  "card_structure": {
    "has_number_badge": true/false,
    "has_category_title": true/false,
    "has_illustration": true/false,
    "has_one_line_summary": true/false,
    "bullets_per_card": 每张卡片的要点条数(整数)
  },
  "bottom_sections": [
    {"title": "底部分区标题，如 成功的底层思维", "type": "icons_grid / roadmap / other", "item_count": 条目数}
  ],
  "palette": ["主色调若干个十六进制"],
  "illustration_style": "插画风格描述，如 '柔和水彩 / 扁平卡通 / 手绘'",
  "overall_style_note": "整体观感，如 小红书风格、清新治愈、手账感",

  "background": {
    "kind": "四选一：'solid'(纯色) / 'gradient'(渐变) / 'textured'(有纹理或手绘底纹) / 'photo'(照片或插画铺底)",
    "css": "一段可直接用于 CSS background 的值。solid 给十六进制如 '#fffdf7'；gradient 给 'linear-gradient(135deg,#fff0f6,#eafaff)' 这类。textured/photo 可留空字符串。",
    "prompt": "仅当 kind 是 textured 或 photo 时填写：用【英文】描述这张底图(背景纹理+外边框+四散装饰元素+整体艺术风格)，以便文生图重绘。务必强调 no text, no words, blank center。其它情况留空字符串。"
  },
  "border": {
    "present": true/false,
    "css": "整图外边框的 CSS 值，如 '4px solid #3b5bdb' 或 '3px dashed #c1272d'；无边框给 'none'",
    "radius": "外框圆角像素数(整数)，直角给 0"
  },
  "title_style": "标题字体风格，四选一：'handwriting'(手写/手绘/卡通圆体/毛笔感) / 'bold-sans'(标准粗黑体) / 'serif'(衬线/宋体) / 'script'(花体)。判断：若整体是手账/手绘/小红书/涂鸦/治愈风，标题通常是手写圆润体——优先选 handwriting；只有明显是规整无衬线黑体时才选 bold-sans。",
  "text_color": "正文文字主色十六进制(需与背景对比清晰)",
  "card_style": "卡片容器风格，三选一。判断依据=每条内容是否有清晰的独立矩形卡片(白色或纯色色块背景+圆角/阴影)：有独立实心色块卡片→'boxed'；只有描边线框、内部透出背景→'outlined'；内容直接压在整图背景上、条目之间没有任何卡片色块(常见于海报/手账/涂鸦/手绘风)→'open'。仔细看：背景纹理是否在每条内容下方连续可见？连续可见说明没有卡片色块，应判 'open'。",
  "decorations": ["画面里出现的装饰元素英文关键词，如 stars, vines, arrows, sparkles, leaves, shooting-star；没有则空数组"]
}
"""


_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _cn_to_int(s: str):
    """简易中文数字转整数（支持 一~九十九）。失败返回 None。"""
    if not s:
        return None
    if "十" in s:
        left, _, right = s.partition("十")
        l = _CN_DIGITS.get(left, 1) if left else 1
        r = _CN_DIGITS.get(right, 0) if right else 0
        return l * 10 + r
    if len(s) == 1:
        return _CN_DIGITS.get(s)
    return None


def count_from_subject(subject: str):
    """
    从主题里识别明确的数量（如「12个理财常识」「五种方法」「3步」）。
    命中且在 2~24 之间则返回该整数，供覆盖模板的卡片/步骤数，避免标题数字与卡片数不符。
    """
    import re
    m = re.search(
        r"(\d{1,2}|[一二三四五六七八九十两]{1,3})\s*"
        r"(?:个|种|条|步|点|招|式|项|方面|方法|要点|技巧|习惯|误区|秘诀|原则|阶段|大)",
        subject or "",
    )
    if not m:
        return None
    tok = m.group(1)
    n = int(tok) if tok.isdigit() else _cn_to_int(tok)
    return n if n and 2 <= n <= 24 else None


def reverse_template(image_path: str, cb: ProgressCb = None) -> dict:
    """步骤 2：反推模板版式结构，返回结构化 schema。"""
    _notify(cb, "reverse", "正在反推模板版式结构…")
    schema = vision.analyze_image_json(_REVERSE_PROMPT, [image_path])
    # 归一化版式类型：只认 grid / steps，其余一律按 grid 处理。
    kind = str(schema.get("layout_kind", "grid")).strip().lower()
    schema["layout_kind"] = "steps" if "step" in kind else "grid"
    # 兜底默认值，避免后续步骤因缺字段报错。
    schema.setdefault("card_count", 12)
    schema.setdefault("step_count", 5)
    schema.setdefault("grid_columns", 4)
    schema.setdefault("card_structure", {})
    schema["card_structure"].setdefault("bullets_per_card", 3)
    schema.setdefault("bottom_sections", [])
    schema.setdefault("has_tips", schema["layout_kind"] == "steps")
    schema.setdefault("palette", ["#2e7d32", "#ef6c00", "#1565c0", "#6a1b9a"])
    schema.setdefault("illustration_style", "soft watercolor illustration")
    # 视觉风格规格兜底（供「跟随参考图」动态渲染用）。
    bg = schema.get("background")
    if not isinstance(bg, dict):
        bg = {}
    bg.setdefault("kind", "solid")
    bg.setdefault("css", "")
    bg.setdefault("prompt", "")
    schema["background"] = bg
    border = schema.get("border")
    if not isinstance(border, dict):
        border = {}
    border.setdefault("present", False)
    border.setdefault("css", "none")
    border.setdefault("radius", 0)
    schema["border"] = border
    schema.setdefault("title_style", "bold-sans")
    schema.setdefault("text_color", "#2b2b2b")
    schema.setdefault("card_style", "boxed")
    if not isinstance(schema.get("decorations"), list):
        schema["decorations"] = []
    if schema["layout_kind"] == "steps":
        _notify(cb, "reverse", f"反推完成：步骤流程版式 / {schema.get('step_count')} 步")
    else:
        _notify(cb, "reverse", f"反推完成：宫格版式 / {schema.get('card_count')} 张卡片 / {schema.get('grid_columns')} 列")
    return schema


# --------------------------------------------------------------------------- #
# 步骤 3：基于主题组织知识数据
# --------------------------------------------------------------------------- #
def _build_content_prompt(subject: str, template: dict, extra: str, aspect: str = "9:16") -> str:
    card_count = template.get("card_count", 12)
    bullets = max(2, template.get("card_structure", {}).get("bullets_per_card", 3))
    orient = "竖版 9:16 全屏（高 > 宽，纵向空间大）" if aspect == "9:16" else "横版 16:9 全屏（宽 > 高）"
    gcols, grows = _grid_dims(aspect)  # 横竖各自的网格（格子近似正方形）
    if aspect == "9:16":  # 竖版示例：图占上、要点铺底（纵向错落）
        elem_example = (
            '{"role":"heading","col":1,"row":1,"w":9,"h":2,"align":"left"},\n'
            '        {"role":"image","col":1,"row":3,"w":9,"h":8,"align":"center"},\n'
            '        {"role":"bullets","items":["要点1","要点2"],"col":1,"row":11,"w":9,"h":5,"align":"left"}'
        )
    else:  # 横版示例：左图右文（横向错落）
        elem_example = (
            '{"role":"heading","col":1,"row":1,"w":16,"h":2,"align":"left"},\n'
            '        {"role":"image","col":1,"row":3,"w":8,"h":7,"align":"center"},\n'
            '        {"role":"bullets","items":["要点1","要点2"],"col":9,"row":3,"w":8,"h":7,"align":"left"}'
        )
    sections = template.get("bottom_sections", [])
    section_titles = [s.get("title", "") for s in sections] if sections else ["核心思维", "行动路线图"]
    illus_style = template.get("illustration_style", "soft watercolor illustration")

    return f"""你是一名小红书知识博主 + 信息图文案策划。
请围绕主题「{subject}」，产出一张知识卡片信息图所需的全部内容，**严格套用给定的版式结构**。

版式要求：
- 共需要 {card_count} 张知识卡片；
- 每张卡片包含：分类标题、一句话概括、{bullets} 条要点；
- 底部需要 {len(section_titles)} 个总结分区，分别对应：{', '.join(section_titles)}；
- 内容必须真实、可操作、信息充实具体，**全部文字使用简体中文（严禁出现繁体字）**，符合中文表达习惯。
{f'- 额外要求：{extra}' if extra.strip() else ''}

每张卡片还要给出一段**英文插画描述**(illustration_prompt)，用于文生图，
风格统一为：{illus_style}, clean, minimal, consistent style, no text in image。

【二维版面设计：用网格给每个元素定位（横向 col + 纵向 row）】
每张卡片渲染成**一页 {orient}**。把页面看作一个 **{gcols} 列 × {grows} 行的网格**
（col 1→{gcols} 从左到右，row 1→{grows} 从上到下；该网格已贴合本比例，格子近似正方形）。
请你作为版面设计师，为这一页设计若干「元素」(elements)，并**逐个指定它在网格中的位置与大小**：
col(起始列 1~{gcols})、row(起始行 1~{grows})、w(占几列)、h(占几行)、align(left/center/right 文字对齐)。
强制要求：
- **每页必须含 role:"heading"（分类标题）与 role:"image"（该卡插画）两个元素**，再加 1~2 个文字元素；
- 同时考虑**横向与纵向位置**：图可在上/下/左/右，文字与图错落排布，整页填充饱满、不挤不空；
- 元素之间**不要重叠**，合计大致铺满 {gcols}×{grows} 网格（别只占一角留大片空白）；
- **role:"image" 要分到接近正方形的区域**（w 与 h 相近，插画是方形的）；与文字并排时图至少占约一半宽度，别又细又长；
- **相邻卡片版面要有变化**（有的图大占上半、有的图靠左半、有的数字超大居中…），像一套设计精良的 PPT。
可用 role：
- "heading" —— 分类标题（渲染时自动带序号徽标）
- "image" —— 该卡插画（每页必含一次）
- "stat"(value,label) —— 数字/时间/比例超大突出
- "statement"(text) —— 一句有力的话/金句/警示（≤15字）
- "bullets"(items 2~3 条) —— 并列要点，每条用完整一句话把要点说清楚（信息具体、可成句）
- "note"(text) —— 次要补充提示（一句即可）
设计原则：重点突出，靠大字号、插画与充实的文字把版面撑满；横向纵向都利用起来；相邻卡片版面尽量不同；条理清晰、不拥挤。
（{orient}：{'宽 > 高，适合左右错落、图占一侧' if aspect != '9:16' else '高 > 宽，适合上下错落、纵向铺满'}）

【标题】请先想 3 个**带钩子、去 AI 味**的标题候选（title_options），再从中选最好的一个作为主标题 title。
钩子角度可用：痛点共鸣 / 好奇悬念 / 利益承诺 / 数字清单 / 反差打破常规；写得像真人发小红书，
口语自然有情绪，**禁用 AI 套路词**（如"必看""收藏""划重点""赶紧马住""绝绝子""家人们"等浮夸词）。
若标题含数量数字，必须正好等于 {card_count}。

只返回如下结构的 JSON：
{{
  "title_options": ["钩子标题1", "钩子标题2", "钩子标题3"],
  "title": "从 title_options 里选最好的一个作为主标题",
  "title_highlight": "主标题 title 中最适合高亮的一个关键词（必须是 title 的子串）",
  "subtitle": "副标题（一句话点题）",
  "accent_color": "主题色十六进制（贴合主题情绪）",
  "cards": [
    {{
      "number": 1,
      "category": "分类标题",
      "summary": "一句话概括（用于配音旁白）",
      "bullets": ["要点1", "要点2", "要点3"],
      "elements": [
        {elem_example}
      ],
      "illustration_prompt": "english illustration description"
    }}
    // 共 {card_count} 张
  ],
  "bottom_left": {{
    "title": "{section_titles[0] if section_titles else '核心思维'}",
    "items": [{{"label": "短标题", "desc": "一句话说明"}}]
  }},
  "bottom_right": {{
    "title": "{section_titles[1] if len(section_titles) > 1 else '行动路线图'}",
    "steps": [{{"label": "步骤名", "desc": "一句话说明"}}]
  }}
}}
"""


def classify_layout_by_subject(subject: str, cb: ProgressCb = None) -> str:
    """让模型判断主题更适合 steps（有先后顺序的流程）还是 grid（并列清单），返回其一。"""
    prompt = (
        f"判断主题「{subject}」更适合哪种信息图版式，只回答一个英文单词：\n"
        "- steps：有明确先后顺序的流程、教程、步骤（如食谱、操作教程、报名/申请流程、安装步骤）\n"
        "- grid：并列的清单、要点、盘点（如 N 个方法、N 个常识、N 种选择、N 个赛道）\n"
        "只返回 steps 或 grid，不要其他内容。"
    )
    try:
        r = _generate_text_with_retry(prompt, cb, retries=2).strip().lower()
    except Exception as e:
        logger.warning(f"classify_layout failed, fallback to grid: {e}")
        return "grid"
    return "steps" if "step" in r else "grid"


def _build_steps_prompt(subject: str, template: dict, extra: str) -> str:
    step_count = template.get("step_count", 5)
    has_tips = template.get("has_tips", True)
    illus_style = template.get("illustration_style", "soft watercolor illustration")

    tips_field = (
        '"tips": "底部小贴士（一句实用提醒，没有可留空）",' if has_tips else '"tips": "",'
    )
    return f"""你是一名小红书博主 + 流程图文案策划。
请围绕主题「{subject}」，产出一张「步骤流程」信息图所需的全部内容（**自上而下的步骤列表版式**，不是宫格卡片）。

要求：
- 拆成 {step_count} 个清晰的步骤，顺序合理、可直接照做；
- 每个步骤包含：一个小标题(heading) + 一段说明文字(text，2~3句，把这一步的关键动作与要点讲清楚)；
- 内容紧扣主题「{subject}」，**全部文字使用简体中文（严禁出现繁体字）**，符合中文表达习惯，具体充实、避免空话。
{f'- 额外要求：{extra}' if extra.strip() else ''}

每个步骤还要给出一段**英文插画描述**(illustration_prompt)用于文生图，
风格统一为：{illus_style}, clean, minimal, consistent style, no text in image。

只返回如下结构的 JSON：
{{
  "title": "主标题（紧扣主题、有吸引力）",
  "title_highlight": "标题中最适合高亮的一个关键词（必须是 title 的子串）",
  "subtitle": "副标题（一句点题，可留空）",
  "accent_color": "主题色十六进制（贴合主题情绪）",
  "steps": [
    {{
      "number": 1,
      "heading": "步骤小标题",
      "text": "这一步的具体说明文字",
      "illustration_prompt": "english illustration description"
    }}
    // 共 {step_count} 步
  ],
  {tips_field}
}}
"""


def _is_rate_limited(text: str) -> bool:
    t = (text or "").lower()
    return "429" in t or "rate" in t or "limit_burst" in t or "too many requests" in t


def _generate_text_with_retry(prompt: str, cb: ProgressCb, retries: int = 4) -> str:
    """
    调用文本 LLM 并对限流（429 / burst rate）做指数退避重试。

    _generate_response 出错时会返回 "Error: ..." 字符串而非抛异常，
    这里据此判断是否为限流并退避重试，平滑请求速率。
    """
    from app.services.llm import _generate_response

    last = ""
    for attempt in range(retries + 1):
        last = _generate_response(prompt)
        if not last.startswith("Error:"):
            return last
        if _is_rate_limited(last) and attempt < retries:
            wait = min(2 ** attempt * 3, 30)  # 3s, 6s, 12s, 24s…（上限 30s）
            _notify(cb, "knowledge", f"触发限流，{wait}s 后重试（{attempt + 1}/{retries}）…")
            time.sleep(wait)
            continue
        break
    raise RuntimeError(f"LLM 生成知识数据失败：{last}")


# --------------------------------------------------------------------------- #
# 小红书种草文案（含关键词标签）
# --------------------------------------------------------------------------- #
# 去 AI 味 / 拟人化的统一约束（生成与审核共用）。
_HUMAN_STYLE = (
    "【像真人随手分享，不是营销稿】：\n"
    "- 口吻自然真诚，像跟朋友聊天；句子长短不一，可有口语、语气词、一点点不完美的真实感；\n"
    "- 多写具体细节和个人体验（'我之前…''踩过的坑'），少写空泛大词；\n"
    "- 严禁 AI 套路腔与烂大街词：如 '赶紧马住''直接开窍''划重点了''家人们''绝绝子''巨''YYDS'"
    "'看完直接''建议收藏反复观看''废话不多说' 等；不要每条都用 '💡数字.' 的整齐模板；\n"
    "- emoji 克制自然（全文几个即可），不要每行都堆；\n"
    "- 不要浮夸承诺和绝对化用词。"
)


def generate_promo_copy(subject: str, content: dict = None, cb: ProgressCb = None,
                        review: bool = True) -> dict:
    """
    针对主题，按小红书「种草」方式生成发布文案：爆款标题 + 拟人化正文 + 关键词话题标签。
    review=True 时再过一道「编辑审核」改写，进一步去 AI 味、增强真实感。
    content 可选（传入已生成的卡片内容作为事实依据，避免空谈）。
    返回 {"title","body","tags":[...]}；失败返回空结构。
    """
    items = (content or {}).get("cards") or (content or {}).get("steps") or []
    points = "\n".join(
        f"- {it.get('category') or it.get('heading', '')}："
        f"{it.get('summary') or it.get('text', '')}"
        for it in items[:12]
    )
    points_block = f"\n可参考的要点（保证内容真实，不要编造）：\n{points}\n" if points else ""
    prompt = (
        f"你是一个爱分享的真实小红书用户（不是营销号）。围绕主题「{subject}」写一篇你自己的种草笔记。\n"
        f"{_HUMAN_STYLE}\n"
        "结构：\n"
        "- title：能戳中人的标题，≤20 字，最多 1 个 emoji，别太用力；\n"
        "- body：开头一句真实的钩子（你的经历/困惑），中间把干货讲清楚（可分点但别套模板），"
        "结尾自然收个尾、轻轻引导互动（别硬喊点赞收藏关注）；\n"
        "- tags：8~12 个精准话题标签，每个以 # 开头，覆盖主题词/人群词/场景词。\n"
        f"{points_block}\n"
        '只返回 JSON：{"title":"...","body":"...","tags":["#...","#..."]}'
    )
    _notify(cb, "promo", "生成小红书种草文案（含关键词）…")
    try:
        raw = _generate_text_with_retry(prompt, cb)
        data = vision._parse_json(raw)
        promo = {
            "title": str(data.get("title", "")).strip(),
            "body": str(data.get("body", "")).strip(),
            "tags": [str(t).strip() for t in (data.get("tags") or []) if str(t).strip()],
        }
    except Exception as e:
        logger.warning(f"promo copy generation failed: {e}")
        return {"title": "", "body": "", "tags": []}

    if review and (promo.get("title") or promo.get("body")):
        promo = review_promo_copy(promo, subject, cb)
    return promo


def review_promo_copy(promo: dict, subject: str, cb: ProgressCb = None) -> dict:
    """编辑审核：把文案改写得更像真人、去掉 AI 味与套路腔；保持 JSON 结构与关键词主旨。"""
    import json as _json

    prompt = (
        "你是资深小红书编辑。审核下面这篇笔记文案，判断它是否有 AI 味/套路腔/营销腔，"
        "然后**改写**成更像真人随手分享的版本（主题不变、信息真实）。\n"
        f"{_HUMAN_STYLE}\n"
        "要求：保持 JSON 结构；标题更自然不浮夸；正文去掉套话与模板感、补一点真实细节；"
        "tags 保留并可微调为更精准的关键词。\n\n"
        f"主题：{subject}\n原文案 JSON：\n{_json.dumps(promo, ensure_ascii=False)}\n\n"
        '只返回改写后的 JSON：{"title":"...","body":"...","tags":["#...","#..."]}'
    )
    _notify(cb, "promo", "审核并润色文案（去 AI 味）…")
    try:
        raw = _generate_text_with_retry(prompt, cb)
        data = vision._parse_json(raw)
        revised = {
            "title": str(data.get("title", "")).strip() or promo.get("title", ""),
            "body": str(data.get("body", "")).strip() or promo.get("body", ""),
            "tags": [str(t).strip() for t in (data.get("tags") or []) if str(t).strip()] or promo.get("tags", []),
        }
        return revised
    except Exception as e:
        logger.warning(f"promo copy review failed, keep draft: {e}")
        return promo


def promo_to_markdown(promo: dict) -> str:
    """把种草文案拼成可直接复制发布的文本。"""
    title = promo.get("title", "")
    body = promo.get("body", "")
    tags = " ".join(promo.get("tags", []))
    return f"{title}\n\n{body}\n\n{tags}\n".strip() + "\n"


def generate_knowledge(subject: str, template: dict, extra: str = "", cb: ProgressCb = None,
                       aspect: str = "9:16") -> dict:
    """步骤 3：基于主题与模板 schema 生成知识数据（结构化内容 + 文案）。aspect 指导分镜按比例组织。"""
    layout = template.get("layout_kind", "grid")
    _notify(cb, "knowledge", f"正在围绕「{subject}」组织知识数据（{('步骤流程' if layout == 'steps' else '宫格')}版式）…")
    if layout == "steps":
        prompt = _build_steps_prompt(subject, template, extra)
    else:
        prompt = _build_content_prompt(subject, template, extra, aspect)
    # 模型偶发吐非法 JSON（缺逗号等）：解析失败则重新生成，最多 3 次。
    content, last_err = None, None
    for attempt in range(3):
        raw = _generate_text_with_retry(prompt, cb)
        if isinstance(raw, dict):
            content = raw
            break
        try:
            content = vision._parse_json(raw)
            break
        except Exception as e:
            last_err = e
            _notify(cb, "knowledge", f"返回内容 JSON 解析失败，重新生成（{attempt + 1}/3）…")
    if content is None:
        raise RuntimeError(f"知识数据 JSON 解析失败（已重试）：{last_err}")
    content = _normalize_content(content, subject, template, aspect)
    if content.get("_layout") == "steps":
        _notify(cb, "knowledge", f"已生成 {len(content['steps'])} 个步骤内容")
    else:
        _notify(cb, "knowledge", f"已生成 {len(content['cards'])} 张卡片内容")
    return content


# LLM 可为每页分镜指定的版式（无效/缺省则留空，由确定性规划器兜底）。
_SCENE_LAYOUTS = {"image_top", "image_bottom", "big_number", "statement"}
# LLM 为每页自由组合的内容块类型。
_BLOCK_TYPES = {"stat", "statement", "bullets", "image", "note"}
# LLM 在 12×12 网格上为每页定位的元素角色（二维版面）。
_ELEMENT_ROLES = {"heading", "image", "stat", "statement", "bullets", "note"}


def _grid_dims(aspect: str) -> tuple:
    """横竖各自的网格（格子近似正方形）：竖版 9×16，横版 16×9。返回 (cols, rows)。"""
    return (9, 16) if aspect == "9:16" else (16, 9)


def _norm_elements(v, cols: int = 12, rows: int = 12) -> list:
    """校验 LLM 设计的二维网格元素：clamp 到 cols×rows，过滤非法/空元素。
    需同时含 image + heading 且 ≥2 个有效元素才视为有效版面，否则返回 []（回退旧 blocks 流）。"""
    if not isinstance(v, list):
        return []
    out = []
    for e in v[:6]:
        if not isinstance(e, dict):
            continue
        role = str(e.get("role", "")).strip().lower()
        if role not in _ELEMENT_ROLES:
            continue
        try:
            col, row = int(e.get("col", 0)), int(e.get("row", 0))
            w, h = int(e.get("w", 0)), int(e.get("h", 0))
        except (TypeError, ValueError):
            continue
        col = min(max(col, 1), cols)
        row = min(max(row, 1), rows)
        w = min(max(w, 1), cols - col + 1)
        h = min(max(h, 1), rows - row + 1)
        align = str(e.get("align", "center")).strip().lower()
        if align not in ("left", "center", "right"):
            align = "center"
        el = {"role": role, "col": col, "row": row, "w": w, "h": h, "align": align}
        if role == "bullets":
            items = [str(x).strip() for x in (e.get("items") or []) if str(x).strip()][:4]
            if not items:
                continue
            el["items"] = items
        elif role == "stat":
            val = str(e.get("value", "")).strip()
            if not val:
                continue
            # stat 只配真·数字/比例/时间；文字过长（如"需要vs想要"）会被超大字撑爆 → 转成 statement
            if len(val) > 6 or not any(ch.isdigit() for ch in val):
                el["role"] = "statement"
                lbl = str(e.get("label", "")).strip()
                el["text"] = f"{val}（{lbl}）" if lbl else val
            else:
                el["value"], el["label"] = val, str(e.get("label", "")).strip()
        elif role in ("statement", "note"):
            t = str(e.get("text", "")).strip()
            if not t:
                continue
            el["text"] = t
        elif role == "heading":
            el["text"] = str(e.get("text", "")).strip()  # 可选覆盖；空则用 category
        out.append(el)
    has_img = any(x["role"] == "image" for x in out)
    has_head = any(x["role"] == "heading" for x in out)
    if not (has_img and has_head and len(out) >= 2):
        return []
    return _reflow_fill(out, cols, rows)


def _el_weight(el: dict, cols: int, rows: int = 16) -> int:
    """元素需要的行高权重：决定该元素在整页里分到多少行高。
    - 文字(bullets/statement/note)：按估算行数，防长文被压扁裁切。
    - stat：超大数字(≈120px)+小标签，至少 3 行才不被裁/不顶到下方图。
    - image：插画是正方形，行高应≈宽度对应的正方形高度，否则窄图塞进高带会上下大留白。
    """
    if el["role"] == "bullets":
        units = el.get("items", [])
    elif el["role"] in ("statement", "note"):
        units = [el.get("text", "")]
    elif el["role"] == "stat":
        return max(el["h"], 3)
    elif el["role"] == "image":
        # 插画是正方形：等效行高 = 列宽 × 单元格宽高比。竖版底部留字幕安全区，
        # 单元格偏扁(cellW/cellH≈1.25)，横版相反(≈0.4)。直接用正方形换算，
        # 不沿用 LLM 的 h（窄图给大 h 会在高带里上下大片留白）。
        coeff = 1.25 if cols < rows else 0.4
        return max(1, round(el["w"] * coeff))
    else:
        return max(1, el["h"])
    cpl = max(6, 2 * el["w"])  # 每行约 2 字/列（竖 9 列≈18 字、横 16 列≈32 字一行）
    lines = sum(max(1, -(-len(u) // cpl)) for u in units)  # ceil 除法
    return max(el["h"], lines + 1)  # +1 留呼吸空间


def _reflow_fill(out: list, cols: int, rows: int) -> list:
    """纵向铺满整页：保留 LLM 的横向(col/w)、相对顺序与左右并排分组，
    但把元素重排成自上而下的「行带」并按权重撑满整个网格高度，杜绝底部大片留白。"""
    def _cols_overlap(a, b):
        return not (a["col"] + a["w"] <= b["col"] or b["col"] + b["w"] <= a["col"])

    out.sort(key=lambda e: (e["row"], e["col"]))
    bands = []
    for e in out:
        b = bands[-1] if bands else None
        # 同一带=左右并排：要求纵向重叠 **且** 与带内已有元素列不相交（否则会文字压文字）。
        if b and e["row"] < b["end"] and not any(_cols_overlap(e, x) for x in b["els"]):
            b["els"].append(e)
            b["end"] = max(b["end"], e["row"] + e["h"])
        else:
            bands.append({"els": [e], "end": e["row"] + e["h"]})
    # 独占一带的元素横向铺满整宽：杜绝「底部数字只占左半、右半大片空白」这类横向留白。
    # （左右并排的多元素带保留各自 col/w，不动。）
    for b in bands:
        if len(b["els"]) == 1 and b["els"][0]["w"] < cols:
            b["els"][0]["col"], b["els"][0]["w"] = 1, cols
    # 每带权重 = 带内元素需要行高的最大值（文字按估算行数）；按权重分配整页行高。
    weights = [max(_el_weight(el, cols, rows) for el in b["els"]) for b in bands]
    tot = sum(weights) or 1
    n = len(bands)
    bhs = []
    rem = rows
    for i, wt in enumerate(weights):
        bh = rem if i == n - 1 else max(1, round(wt / tot * rows))
        bh = max(1, min(bh, rem - (n - 1 - i)))  # 给后面每带至少留 1 行
        bhs.append(bh)
        rem -= bh
    # 保底：stat 带 ≥3 行（超大数字+标签否则被裁、标签顶到下图）、文字带 ≥估算行数。
    # 缺的行从有余量的图片带借（图片可压缩，文字/数字不行）。
    def _min_rows(b):
        roles = {el["role"] for el in b["els"]}
        if "stat" in roles:
            return 3
        if roles & {"bullets", "statement", "note"} and "image" not in roles:
            return max(_el_weight(el, cols, rows) for el in b["els"])
        return 1  # 图片/标题带可被压
    mins = [_min_rows(b) for b in bands]
    img_idx = [i for i, b in enumerate(bands) if any(el["role"] == "image" for el in b["els"])]
    for i in range(n):
        while bhs[i] < mins[i]:
            donor = max((j for j in img_idx if bhs[j] > mins[j]), default=None,
                        key=lambda j: bhs[j])
            if donor is None:
                break
            bhs[donor] -= 1
            bhs[i] += 1
    cur = 1
    for b, bh in zip(bands, bhs):
        for el in b["els"]:
            el["row"], el["h"] = cur, bh
        cur += bh
        cur += bh
    return out


def _norm_layout(v) -> str:
    s = str(v or "").strip().lower()
    return s if s in _SCENE_LAYOUTS else ""


def _norm_arrange(v) -> str:
    s = str(v or "").strip().lower()
    return s if s in ("row", "column") else ""


def _norm_blocks(v) -> list:
    """校验 LLM 设计的内容块结构；过滤非法类型/空块，最多保留 4 块。"""
    if not isinstance(v, list):
        return []
    out = []
    for b in v[:4]:
        if not isinstance(b, dict):
            continue
        bt = str(b.get("type", "")).strip().lower()
        if bt not in _BLOCK_TYPES:
            continue
        if bt == "stat":
            val = str(b.get("value", "")).strip()
            if val:
                out.append({"type": "stat", "value": val, "label": str(b.get("label", "")).strip()})
        elif bt == "statement":
            t = str(b.get("text", "")).strip()
            if t:
                out.append({"type": "statement", "text": t})
        elif bt == "bullets":
            items = [str(x).strip() for x in (b.get("items") or []) if str(x).strip()][:3]
            if items:
                out.append({"type": "bullets", "items": items})
        elif bt == "image":
            out.append({"type": "image"})
        elif bt == "note":
            t = str(b.get("text", "")).strip()
            if t:
                out.append({"type": "note", "text": t})
    return out


def _normalize_content(content: dict, subject: str, template: dict, aspect: str = None) -> dict:
    """校验并补全 LLM 返回的内容，保证下游渲染不缺字段。aspect 决定网格维度(竖9×16/横16×9)。"""
    if aspect is None:
        aspect = content.get("_aspect") or "9:16"
    content["_aspect"] = aspect
    g_cols, g_rows = _grid_dims(aspect)
    # 标题候选（带钩子·去AI味）；主标题缺省时取第一个候选。
    opts = [str(x).strip() for x in (content.get("title_options") or []) if str(x).strip()][:3]
    content["title_options"] = opts
    if not str(content.get("title", "")).strip() and opts:
        content["title"] = opts[0]
    content.setdefault("title", subject)
    content.setdefault("subtitle", "")
    if not content.get("title_highlight") or content["title_highlight"] not in content["title"]:
        content["title_highlight"] = ""
    content.setdefault("accent_color", (template.get("palette") or ["#2e7d32"])[0])

    layout = template.get("layout_kind", "grid")
    # LLM 实际返回了 steps 也按步骤处理（容错模板判断与生成不一致的情况）。
    if layout == "steps" or (content.get("steps") and not content.get("cards")):
        steps = content.get("steps") or []
        norm_steps = []
        for i, s in enumerate(steps, start=1):
            if not isinstance(s, dict):
                continue
            norm_steps.append(
                {
                    "number": s.get("number", i),
                    "heading": str(s.get("heading") or s.get("category", "")).strip(),
                    "text": str(s.get("text") or s.get("summary", "")).strip(),
                    "layout": _norm_layout(s.get("layout")),
                    "blocks": _norm_blocks(s.get("blocks")),
                    "illustration_prompt": str(s.get("illustration_prompt", "")).strip(),
                }
            )
        content["steps"] = norm_steps
        content["tips"] = str(content.get("tips", "")).strip()
        content["_layout"] = "steps"
        content.pop("cards", None)
        return content

    bullets_per_card = template.get("card_structure", {}).get("bullets_per_card", 3)
    cards = content.get("cards") or []
    norm_cards = []
    for i, c in enumerate(cards, start=1):
        if not isinstance(c, dict):
            continue
        bl = c.get("bullets") or []
        if isinstance(bl, str):
            bl = [bl]
        bl = [str(b).strip() for b in bl if str(b).strip()]
        elements = _norm_elements(c.get("elements"), g_cols, g_rows)
        # 旁白用要点：卡片没给 bullets 时，从网格元素里抽取文本兜底。
        if not bl and elements:
            for el in elements:
                if el["role"] == "bullets":
                    bl = list(el["items"])
                    break
                if el["role"] in ("statement", "note") and el.get("text"):
                    bl = [el["text"]]
                elif el["role"] == "stat" and el.get("label"):
                    bl = [el["label"]]
        norm_cards.append(
            {
                "number": c.get("number", i),
                "category": str(c.get("category", "")).strip(),
                "summary": str(c.get("summary", "")).strip(),
                "bullets": bl[: max(bullets_per_card, len(bl))],
                "layout": _norm_layout(c.get("layout")),
                "blocks": _norm_blocks(c.get("blocks")),
                "elements": elements,
                "arrange": _norm_arrange(c.get("arrange")),
                "illustration_prompt": str(c.get("illustration_prompt", "")).strip(),
            }
        )
    content["cards"] = norm_cards
    content["_layout"] = "grid"
    content.setdefault("bottom_left", {})
    content.setdefault("bottom_right", {})
    return content


# --------------------------------------------------------------------------- #
# 步骤 4：审核矫正
# --------------------------------------------------------------------------- #
_REVIEW_PROMPT_TMPL = """你是信息图质量审核员。下面是基于某模板版式、围绕主题「{subject}」生成的内容（JSON）。
请对照所附的**原始模板图片**，审核生成内容是否：
1. 条目数量（卡片数 或 步骤数）与模板一致；
2. 每个条目结构完整（宫格卡片需分类/概括/要点齐全；步骤流程需小标题/说明齐全）；
3. 内容紧扣主题「{subject}」、真实可信、无重复无空话；
4. 顺序/分区合理（步骤流程顺序正确；宫格的底部总结分区完整）。

待审核内容：
{content_json}

只返回如下 JSON：
{{
  "approved": true/false,
  "issues": ["问题点1", "问题点2"],
  "corrected_content": {{ 若 approved 为 false，则给出完整修正后的同结构 JSON；若 approved 为 true 则为 null }}
}}
"""


def review_and_correct(
    image_path: str,
    subject: str,
    content: dict,
    template: dict,
    max_rounds: int = 1,
    cb: ProgressCb = None,
) -> tuple[dict, list]:
    """步骤 4：视觉审核 + 矫正回填，最多 max_rounds 轮。返回 (最终内容, 审核日志)。"""
    log = []
    current = content
    for rnd in range(1, max_rounds + 1):
        _notify(cb, "review", f"第 {rnd}/{max_rounds} 轮审核中…")
        prompt = _REVIEW_PROMPT_TMPL.format(
            subject=subject,
            content_json=json.dumps(current, ensure_ascii=False),
        )
        try:
            result = vision.analyze_image_json(prompt, [image_path])
        except Exception as e:
            logger.warning(f"review round {rnd} failed: {e}")
            log.append({"round": rnd, "error": str(e)})
            break

        approved = bool(result.get("approved"))
        issues = result.get("issues") or []
        log.append({"round": rnd, "approved": approved, "issues": issues})
        if approved:
            _notify(cb, "review", "审核通过")
            break

        corrected = result.get("corrected_content")
        if isinstance(corrected, dict) and (corrected.get("cards") or corrected.get("steps")):
            corrected["_aspect"] = current.get("_aspect")  # 沿用首轮的比例网格
            current = _normalize_content(corrected, subject, template)
            _notify(cb, "review", f"已按 {len(issues)} 条建议矫正，重新审核")
        else:
            _notify(cb, "review", "审核未通过但未提供可用修正，保留当前内容")
            break
    return current, log


# --------------------------------------------------------------------------- #
# 步骤 5a：生成卡片插画
# --------------------------------------------------------------------------- #
def generate_card_illustrations(
    content: dict,
    out_dir: str,
    provider: Optional[str] = None,
    cb: ProgressCb = None,
    workers: Optional[int] = None,
) -> dict:
    """为每张卡片/步骤生成插画，返回 {index: image_path}。失败项跳过（不阻断出图）。

    各插画相互独立，按 workers 路并发生成（默认取 config.app.illustration_concurrency，
    缺省 4）；单张慢/超时不再拖累其余。已存在的直接复用（断点续跑）。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    os.makedirs(out_dir, exist_ok=True)
    illustrations = {}
    # 同时兼容宫格(cards)与步骤流程(steps)两种内容结构。
    items = content.get("cards") or content.get("steps") or []
    total = len(items)

    # 先扫一遍：已存在的直接复用，缺失/失败的收集成待生成任务并发处理。
    todo = []  # [(idx, prompt, label, out_path), ...]
    for idx, item in enumerate(items):
        prompt = item.get("illustration_prompt") or item.get("category") or item.get("heading", "")
        if not prompt:
            continue
        label = item.get("category") or item.get("heading", "")
        out_path = os.path.join(out_dir, f"item_{idx + 1}.png")
        # 断点续跑：已生成的插画直接复用，只补做缺失/失败的那几张，省时省 token。
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            illustrations[idx] = out_path
            _notify(cb, "illustration", f"插画 {idx + 1}/{total} 已存在，复用：{label}")
            continue
        todo.append((idx, prompt, label, out_path))

    if not todo:
        return illustrations

    if workers is None:
        try:
            workers = int(config.app.get("illustration_concurrency", 4))
        except (TypeError, ValueError):
            workers = 4
    workers = max(1, min(workers, len(todo)))

    def _one(task):
        idx, prompt, label, out_path = task
        # 1024 方图：清晰填充分镜/卡片；用端点支持的尺寸(512/1024)，768 等会被拒(400)。
        image_gen.text_to_image(prompt, out_path, width=1024, height=1024, provider=provider)
        return idx, out_path

    _notify(cb, "illustration", f"并发生成 {len(todo)} 张插画（{workers} 路并行）…")
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_one, t): t for t in todo}
        for fut in as_completed(futs):
            idx, prompt, label, out_path = futs[fut]
            done += 1
            try:
                _, saved = fut.result()
                illustrations[idx] = saved
                _notify(cb, "illustration", f"插画完成 {done}/{len(todo)}：{label}")
            except Exception as e:
                logger.warning(f"item {idx + 1} illustration failed, skip: {e}")
                _notify(cb, "illustration", f"插画失败跳过 {done}/{len(todo)}：{label}（{e}）")
    return illustrations


# --------------------------------------------------------------------------- #
# 步骤 5b：构建 HTML 并渲染
# --------------------------------------------------------------------------- #
def _img_data_uri(path: str) -> str:
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    return f"data:image/png;base64,{b64}"


def _highlight_title(title: str, highlight: str, accent: str) -> str:
    if highlight and highlight in title:
        return title.replace(highlight, f'<span class="hl">{highlight}</span>', 1)
    return title


# 各风格的卡片配色板 (背景, 强调色)，按卡片序号循环。
_PALETTE_XHS = [
    ("#e9f6ec", "#2e9e54"), ("#fff1e3", "#ef7a1a"), ("#e6f1fd", "#2378d8"),
    ("#f5eafc", "#8d3bc4"), ("#fde9f0", "#d83a78"), ("#e3f6f3", "#13a394"),
    ("#fff7df", "#e0a40c"), ("#ece8fb", "#6147c9"), ("#fdeae6", "#dd5436"),
    ("#eef7e2", "#69a52a"), ("#e4f4fd", "#0c93c9"), ("#f0ecfb", "#5b54cf"),
]
_PALETTE_MACARON = [
    ("#ffe0ec", "#ff7aa2"), ("#fff0d9", "#ffb04d"), ("#dcf5e6", "#4cc38a"),
    ("#dbeafe", "#5b9bd5"), ("#ede3ff", "#a78bfa"), ("#ffe4d6", "#fb8c5a"),
    ("#d6f5f5", "#3fc4c4"), ("#fde2f0", "#ec6fb0"), ("#e6f0ff", "#6f9bef"),
    ("#eafbe0", "#79c34a"), ("#fff3cc", "#eab308"), ("#e9e2ff", "#8b7cf0"),
]
_PALETTE_TECH = [
    ("#1d2530", "#36e0c8"), ("#251d33", "#b06cff"), ("#1d2e26", "#3ee07a"),
    ("#2e1d28", "#ff5c8a"), ("#1d2733", "#3aa0ff"), ("#2e2a1d", "#ffcf3a"),
    ("#1d3330", "#2bdce0"), ("#281d33", "#9d6bff"), ("#33271d", "#ff8a3a"),
    ("#1d2630", "#5b9bff"), ("#2a1d33", "#d06cff"), ("#1d3326", "#4ade80"),
]
# 国潮：宣纸底 + 传统色（朱红/黛绿/黛蓝/藤黄金/黛紫/赭石）循环。
_PALETTE_GUOCHAO = [
    ("#fbeee0", "#c1272d"), ("#e9f1ec", "#1f7a5c"), ("#e7eef6", "#27568e"),
    ("#f7eed5", "#b8860b"), ("#f2e8f0", "#7a2f63"), ("#f6e9e0", "#a8451e"),
    ("#fbeee0", "#c1272d"), ("#e9f1ec", "#1f7a5c"), ("#e7eef6", "#27568e"),
    ("#f7eed5", "#b8860b"), ("#f2e8f0", "#7a2f63"), ("#f6e9e0", "#a8451e"),
]
# ins 杂志风 / 极简黑白：单色卡片（白底 + 深色描述），强调色只用于标题点缀。
_PALETTE_INS = [("#ffffff", "#1a1a1a")]
_PALETTE_MONO = [("#ffffff", "#111111")]

# 衬线 / 楷体字体栈（用于杂志风、国潮风）。
_FONT_SERIF = '"Songti SC","SimSun","宋体","Georgia","Times New Roman",serif'
_FONT_KAI = '"华文楷体","STKaiti","KaiTi","楷体","华文行楷","STXingkai",serif'

# 标题手写字体栈：优先项目内置的「站酷快乐体」(Q版圆润)/「马善政毛笔楷书」(手绘)，
# 回退到 Windows 自带的华文行楷/隶书，最后雅黑加粗。
_FONT_HAND = ('"ZCOOL KuaiLe","站酷快乐体","Ma Shan Zheng","华文行楷","STXingkai",'
              '"隶书","LiSu","Microsoft YaHei",sans-serif')

# 项目内置的手绘/Q版中文字体（resource/fonts 下的 ttf 文件）。
_BUNDLED_FONTS = {
    "ZCOOL KuaiLe": "ZCOOLKuaiLe-Regular.ttf",   # 站酷快乐体，Q版圆润
    "Ma Shan Zheng": "MaShanZheng-Regular.ttf",  # 马善政毛笔楷书，手绘毛笔
}

# 字体选项：界面「字体」下拉的可选项。None=跟随风格默认。
FONT_CHOICES = {
    "跟随风格": None,
    "Q版圆润（站酷快乐体）": '"ZCOOL KuaiLe","Microsoft YaHei",sans-serif',
    "手绘毛笔（马善政）": '"Ma Shan Zheng","Microsoft YaHei",sans-serif',
}


def _fontface_css() -> str:
    """生成 @font-face 规则，用 file:// 本地加载内置手绘字体（离线可靠）。"""
    fdir = utils.font_dir()
    rules = []
    for family, fname in _BUNDLED_FONTS.items():
        fpath = os.path.join(fdir, fname)
        if not os.path.exists(fpath):
            continue
        url = "file:///" + fpath.replace("\\", "/")
        rules.append(
            f'@font-face {{ font-family:"{family}"; src:url("{url}"); '
            f'font-weight:normal; font-style:normal; font-display:swap; }}'
        )
    return "\n".join(rules)


def _font_override_css(font: str | None) -> str:
    """用户指定手绘/Q版字体时，覆盖标题与各级小标题的字体（正文保持可读）。"""
    if not font:
        return ""
    return (
        f'.title,.cat,.s-title,.bottom-title,.summary,.sec-label,.subtitle-wrap '
        f'{{ font-family:{font} !important; }}'
    )
_FONT_SANS = '"Microsoft YaHei","PingFang SC","Noto Sans CJK SC",sans-serif'
_FONT_TECH = '"Bahnschrift","DIN","Microsoft YaHei",sans-serif'


def _wave_svg(color: str) -> str:
    """手绘波浪下划线（SVG），放在标题下方做点缀。"""
    return (
        '<svg class="wave" width="360" height="16" viewBox="0 0 360 16" '
        'preserveAspectRatio="none"><path d="M3 11 Q 25 2 47 9 T 91 9 T 135 9 '
        'T 179 9 T 223 9 T 267 9 T 311 9 T 357 9" stroke="' + color + '" '
        'stroke-width="5" fill="none" stroke-linecap="round"/></svg>'
    )


def _css_xhs(accent: str, cols: int) -> str:
    return f"""
body {{ width:1080px; padding:44px 36px 52px; font-family:{_FONT_SANS};
  background: radial-gradient(circle at 12% 8%, #fff6e6 0, transparent 38%),
    radial-gradient(circle at 88% 4%, #eafaf0 0, transparent 34%), #fffdf7; color:#2b2b2b; }}
.header {{ text-align:center; margin-bottom:30px; }}
.title {{ font-family:{_FONT_HAND}; font-size:70px; font-weight:900; line-height:1.18; letter-spacing:2px; color:#1f1f1f; }}
.title .sun {{ font-size:40px; vertical-align:18px; margin-left:6px; }}
.hl {{ color:{accent}; background:linear-gradient(transparent 58%, {accent}30 58%, {accent}30 92%, transparent 92%); padding:0 4px; }}
.wave {{ display:block; margin:-2px auto 0; }}
.subtitle-wrap {{ margin-top:14px; font-size:25px; color:#5a5a5a; font-weight:600; }}
.subtitle-wrap .leaf {{ color:{accent}; margin:0 10px; font-size:20px; }}
.grid {{ display:grid; grid-template-columns:repeat({cols},1fr); gap:18px; }}
.card {{ border-radius:20px; padding:16px 16px 18px; background:#fff; border-top:5px solid {accent}; box-shadow:0 6px 18px rgba(60,50,30,0.10); }}
.card-head {{ display:flex; align-items:center; gap:9px; margin-bottom:12px; }}
.badge {{ color:#fff; width:30px; height:30px; border-radius:50%; display:inline-flex; align-items:center; justify-content:center; font-weight:800; font-size:17px; box-shadow:0 2px 5px rgba(0,0,0,0.18); }}
.cat {{ font-family:{_FONT_HAND}; font-size:22px; font-weight:800; letter-spacing:0.5px; }}
.illus {{ width:100%; aspect-ratio:1/1; border-radius:14px; overflow:hidden; margin-bottom:11px; background:#fff; }}
.illus img {{ width:100%; height:100%; object-fit:cover; }}
.summary {{ font-size:15.5px; font-weight:800; margin-bottom:9px; line-height:1.4; }}
.bullets {{ list-style:none; }}
.bullets li {{ font-size:14px; color:#4a4a4a; line-height:1.75; padding-left:16px; position:relative; }}
.bullets li::before {{ content:"▪"; position:absolute; left:0; color:var(--dot); font-size:13px; }}
.bottom {{ display:grid; grid-template-columns:1fr 1fr; gap:18px; margin-top:26px; }}
.bottom-box {{ background:#fffaf0; border:2px dashed #e7d9bf; border-radius:20px; padding:22px; }}
.bottom-title {{ font-size:24px; font-weight:900; margin-bottom:16px; color:{accent}; }}
.sec-grid {{ display:grid; grid-template-columns:repeat(2,1fr); gap:14px; }}
.sec-item {{ background:#fff; border-radius:12px; padding:10px 12px; box-shadow:0 2px 6px rgba(0,0,0,0.05); }}
.sec-label {{ font-size:16px; font-weight:800; color:#333; }}
.sec-desc {{ font-size:13px; color:#777; margin-top:4px; line-height:1.5; }}
"""


def _css_business(accent: str, cols: int) -> str:
    return f"""
body {{ width:1080px; padding:48px 40px 54px; font-family:{_FONT_SANS}; background:#f4f6f9; color:#1f2733; }}
.header {{ text-align:center; margin-bottom:34px; }}
.title {{ font-family:{_FONT_SANS}; font-size:58px; font-weight:800; line-height:1.2; color:#16202e; }}
.title .sun {{ display:none; }}
.hl {{ color:{accent}; }}
.wave {{ display:none; }}
.subtitle-wrap {{ margin-top:12px; font-size:21px; color:#6b7686; font-weight:500; }}
.subtitle-wrap .leaf {{ display:none; }}
.grid {{ display:grid; grid-template-columns:repeat({cols},1fr); gap:16px; }}
.card {{ border-radius:12px; padding:18px 16px; background:#fff; border:1px solid #e5e9ef; border-left:4px solid {accent}; box-shadow:0 1px 3px rgba(20,30,50,0.06); }}
.card-head {{ display:flex; align-items:center; gap:9px; margin-bottom:12px; }}
.badge {{ color:#fff; width:28px; height:28px; border-radius:7px; display:inline-flex; align-items:center; justify-content:center; font-weight:700; font-size:15px; }}
.cat {{ font-size:19px; font-weight:700; color:#1f2733 !important; }}
.illus {{ width:100%; aspect-ratio:1/1; border-radius:8px; overflow:hidden; margin-bottom:11px; background:#f0f2f5; }}
.illus img {{ width:100%; height:100%; object-fit:cover; }}
.summary {{ font-size:15px; font-weight:700; margin-bottom:8px; line-height:1.4; }}
.bullets {{ list-style:none; }}
.bullets li {{ font-size:13.5px; color:#55606e; line-height:1.7; padding-left:15px; position:relative; }}
.bullets li::before {{ content:"•"; position:absolute; left:0; color:var(--dot); }}
.bottom {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-top:24px; }}
.bottom-box {{ background:#fff; border:1px solid #e5e9ef; border-radius:12px; padding:22px; }}
.bottom-title {{ font-size:21px; font-weight:800; margin-bottom:14px; color:{accent}; }}
.sec-grid {{ display:grid; grid-template-columns:repeat(2,1fr); gap:12px; }}
.sec-item {{ background:#f7f9fb; border-radius:8px; padding:10px 12px; }}
.sec-label {{ font-size:15px; font-weight:700; color:#1f2733; }}
.sec-desc {{ font-size:12.5px; color:#828d9b; margin-top:3px; line-height:1.5; }}
"""


def _css_macaron(accent: str, cols: int) -> str:
    return f"""
body {{ width:1080px; padding:44px 36px 52px; font-family:{_FONT_SANS};
  background: linear-gradient(135deg, #fff0f6 0%, #f3f0ff 50%, #eafaff 100%); color:#4a4458; }}
.header {{ text-align:center; margin-bottom:30px; }}
.title {{ font-family:{_FONT_HAND}; font-size:66px; font-weight:900; line-height:1.2; color:#6b5b95; }}
.title .sun {{ font-size:38px; vertical-align:16px; margin-left:6px; }}
.hl {{ color:{accent}; background:linear-gradient(transparent 60%, {accent}33 60%); padding:0 4px; border-radius:4px; }}
.wave {{ display:block; margin:-2px auto 0; }}
.subtitle-wrap {{ margin-top:14px; font-size:24px; color:#8b80a0; font-weight:600; }}
.subtitle-wrap .leaf {{ color:{accent}; margin:0 10px; font-size:19px; }}
.grid {{ display:grid; grid-template-columns:repeat({cols},1fr); gap:18px; }}
.card {{ border-radius:24px; padding:17px; border:none; box-shadow:0 8px 20px rgba(150,120,180,0.14); }}
.card-head {{ display:flex; align-items:center; gap:9px; margin-bottom:12px; }}
.badge {{ color:#fff; width:30px; height:30px; border-radius:50%; display:inline-flex; align-items:center; justify-content:center; font-weight:800; font-size:16px; }}
.cat {{ font-family:{_FONT_HAND}; font-size:21px; font-weight:800; }}
.illus {{ width:100%; aspect-ratio:1/1; border-radius:18px; overflow:hidden; margin-bottom:11px; background:#fff; }}
.illus img {{ width:100%; height:100%; object-fit:cover; }}
.summary {{ font-size:15px; font-weight:800; margin-bottom:9px; line-height:1.4; }}
.bullets {{ list-style:none; }}
.bullets li {{ font-size:14px; color:#6a6478; line-height:1.75; padding-left:17px; position:relative; }}
.bullets li::before {{ content:"♡"; position:absolute; left:0; color:var(--dot); font-size:12px; }}
.bottom {{ display:grid; grid-template-columns:1fr 1fr; gap:18px; margin-top:26px; }}
.bottom-box {{ background:rgba(255,255,255,0.7); border-radius:24px; padding:22px; box-shadow:0 6px 16px rgba(150,120,180,0.12); }}
.bottom-title {{ font-size:23px; font-weight:900; margin-bottom:16px; color:{accent}; }}
.sec-grid {{ display:grid; grid-template-columns:repeat(2,1fr); gap:14px; }}
.sec-item {{ background:#fff; border-radius:16px; padding:11px 13px; }}
.sec-label {{ font-size:16px; font-weight:800; color:#5a5468; }}
.sec-desc {{ font-size:13px; color:#9a92aa; margin-top:4px; line-height:1.5; }}
"""


def _css_tech(accent: str, cols: int) -> str:
    return f"""
body {{ width:1080px; padding:46px 38px 54px; font-family:{_FONT_SANS};
  background: radial-gradient(circle at 15% 0%, #1c2b3a 0, transparent 45%),
    radial-gradient(circle at 85% 5%, #2a1c3a 0, transparent 42%), #11131a; color:#d7dce6; }}
.header {{ text-align:center; margin-bottom:32px; }}
.title {{ font-family:{_FONT_TECH}; font-size:64px; font-weight:800; line-height:1.18; letter-spacing:1px; color:#fff; text-shadow:0 0 18px {accent}88; }}
.title .sun {{ display:none; }}
.hl {{ color:{accent}; text-shadow:0 0 14px {accent}; }}
.wave {{ display:block; margin:2px auto 0; filter:drop-shadow(0 0 6px {accent}); }}
.subtitle-wrap {{ margin-top:14px; font-size:22px; color:#8a93a6; font-weight:500; letter-spacing:2px; }}
.subtitle-wrap .leaf {{ display:none; }}
.grid {{ display:grid; grid-template-columns:repeat({cols},1fr); gap:16px; }}
.card {{ border-radius:14px; padding:16px; background:#1a1e28; border:1px solid var(--dot); box-shadow:0 0 14px -4px var(--dot); }}
.card-head {{ display:flex; align-items:center; gap:9px; margin-bottom:12px; }}
.badge {{ color:#0d0f14; width:28px; height:28px; border-radius:8px; display:inline-flex; align-items:center; justify-content:center; font-weight:800; font-size:15px; box-shadow:0 0 10px var(--dot); }}
.cat {{ font-size:20px; font-weight:800; }}
.illus {{ width:100%; aspect-ratio:1/1; border-radius:10px; overflow:hidden; margin-bottom:11px; background:#0e1016; }}
.illus img {{ width:100%; height:100%; object-fit:cover; }}
.summary {{ font-size:15px; font-weight:700; margin-bottom:9px; line-height:1.4; }}
.bullets {{ list-style:none; }}
.bullets li {{ font-size:13.5px; color:#9aa4b4; line-height:1.75; padding-left:16px; position:relative; }}
.bullets li::before {{ content:"›"; position:absolute; left:0; color:var(--dot); font-weight:800; }}
.bottom {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-top:26px; }}
.bottom-box {{ background:#161a23; border:1px solid #2a3140; border-radius:14px; padding:22px; }}
.bottom-title {{ font-size:22px; font-weight:800; margin-bottom:15px; color:{accent}; text-shadow:0 0 10px {accent}66; }}
.sec-grid {{ display:grid; grid-template-columns:repeat(2,1fr); gap:13px; }}
.sec-item {{ background:#1d222d; border-radius:10px; padding:10px 12px; }}
.sec-label {{ font-size:15px; font-weight:700; color:#e2e7ef; }}
.sec-desc {{ font-size:12.5px; color:#7e8799; margin-top:3px; line-height:1.5; }}
"""


def _css_ins(accent: str, cols: int) -> str:
    return f"""
body {{ width:1080px; padding:52px 48px 56px; font-family:{_FONT_SERIF}; background:#fff; color:#1a1a1a; }}
.header {{ text-align:center; margin-bottom:36px; border-bottom:3px double #1a1a1a; padding-bottom:22px; }}
.title {{ font-family:{_FONT_SERIF}; font-size:62px; font-weight:700; letter-spacing:3px; line-height:1.2; color:#111; }}
.title .sun {{ display:none; }}
.hl {{ color:{accent}; font-style:italic; }}
.wave {{ display:none; }}
.subtitle-wrap {{ margin-top:14px; font-size:18px; color:#888; letter-spacing:6px; }}
.subtitle-wrap .leaf {{ display:none; }}
.grid {{ display:grid; grid-template-columns:repeat({cols},1fr); gap:22px; }}
.card {{ border-radius:0; padding:8px 14px 16px; background:#fff; border-top:2px solid #1a1a1a; box-shadow:none; }}
.card-head {{ display:flex; align-items:baseline; gap:10px; margin-bottom:10px; padding-top:8px; }}
.badge {{ background:transparent !important; color:{accent} !important; width:auto; height:auto; font-family:{_FONT_SERIF}; font-weight:700; font-size:28px; box-shadow:none; }}
.cat {{ font-size:21px; font-weight:700; color:#111 !important; letter-spacing:1px; }}
.illus {{ width:100%; aspect-ratio:4/3; border-radius:0; overflow:hidden; margin:6px 0 10px; background:#f2f2f2; }}
.illus img {{ width:100%; height:100%; object-fit:cover; }}
.summary {{ font-size:15px; font-weight:600; font-style:italic; margin-bottom:8px; color:#333 !important; }}
.bullets {{ list-style:none; }}
.bullets li {{ font-size:13.5px; color:#555; line-height:1.7; padding-left:16px; position:relative; }}
.bullets li::before {{ content:"—"; position:absolute; left:0; color:#999; }}
.bottom {{ display:grid; grid-template-columns:1fr 1fr; gap:22px; margin-top:28px; border-top:1px solid #ddd; padding-top:24px; }}
.bottom-box {{ background:#fff; border:none; border-radius:0; padding:0 6px; }}
.bottom-title {{ font-size:22px; font-weight:700; margin-bottom:14px; color:#111; letter-spacing:2px; }}
.sec-grid {{ display:grid; grid-template-columns:repeat(2,1fr); gap:14px; }}
.sec-item {{ background:#fff; border-top:1px solid #ccc; border-radius:0; padding:8px 4px; box-shadow:none; }}
.sec-label {{ font-size:15px; font-weight:700; color:#222; }}
.sec-desc {{ font-size:12.5px; color:#888; margin-top:3px; line-height:1.5; }}
"""


def _css_guochao(accent: str, cols: int) -> str:
    return f"""
body {{ width:1080px; padding:46px 38px 54px; font-family:{_FONT_SERIF};
  background: radial-gradient(circle at 50% 0%, #f9f1dd 0, transparent 60%), #f1e4c9; color:#3a2a1e; }}
.header {{ text-align:center; margin-bottom:30px; }}
.title {{ font-family:{_FONT_KAI}; font-size:74px; font-weight:900; line-height:1.18; color:{accent}; letter-spacing:5px; }}
.title .sun {{ display:none; }}
.hl {{ color:#b8860b; background:linear-gradient(transparent 62%, #b8860b30 62%); padding:0 4px; }}
.wave {{ display:block; margin:2px auto 0; }}
.subtitle-wrap {{ margin-top:12px; font-size:23px; color:#6b5236; font-weight:600; letter-spacing:5px; }}
.subtitle-wrap .leaf {{ display:none; }}
.grid {{ display:grid; grid-template-columns:repeat({cols},1fr); gap:16px; }}
.card {{ border-radius:8px; padding:15px; background:#fffdf6; border:1px solid #d8c3a0; border-top:4px solid {accent}; box-shadow:0 3px 10px rgba(120,90,50,0.12); }}
.card-head {{ display:flex; align-items:center; gap:9px; margin-bottom:11px; }}
.badge {{ color:#fff5e6; width:30px; height:30px; border-radius:6px; display:inline-flex; align-items:center; justify-content:center; font-family:{_FONT_KAI}; font-weight:800; font-size:18px; }}
.cat {{ font-size:21px; font-weight:800; font-family:{_FONT_KAI}; }}
.illus {{ width:100%; aspect-ratio:1/1; border-radius:6px; overflow:hidden; margin-bottom:10px; background:#fff; }}
.illus img {{ width:100%; height:100%; object-fit:cover; }}
.summary {{ font-size:15.5px; font-weight:800; margin-bottom:9px; line-height:1.4; }}
.bullets {{ list-style:none; }}
.bullets li {{ font-size:14px; color:#5a4632; line-height:1.75; padding-left:17px; position:relative; }}
.bullets li::before {{ content:"❖"; position:absolute; left:0; color:var(--dot); font-size:11px; }}
.bottom {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-top:24px; }}
.bottom-box {{ background:rgba(255,250,238,0.85); border:1.5px solid #cdb892; border-radius:8px; padding:22px; }}
.bottom-title {{ font-size:23px; font-weight:900; font-family:{_FONT_KAI}; margin-bottom:15px; color:{accent}; }}
.sec-grid {{ display:grid; grid-template-columns:repeat(2,1fr); gap:14px; }}
.sec-item {{ background:#fff; border-radius:6px; padding:10px 12px; box-shadow:0 1px 4px rgba(120,90,50,0.1); }}
.sec-label {{ font-size:16px; font-weight:800; color:#3a2a1e; }}
.sec-desc {{ font-size:13px; color:#8a7355; margin-top:4px; line-height:1.5; }}
"""


def _css_minimal(accent: str, cols: int) -> str:
    return f"""
body {{ width:1080px; padding:50px 44px 56px; font-family:{_FONT_SANS}; background:#fff; color:#111; }}
.header {{ text-align:center; margin-bottom:38px; }}
.title {{ font-family:{_FONT_SANS}; font-size:60px; font-weight:800; letter-spacing:-1px; line-height:1.15; color:#000; }}
.title .sun {{ display:none; }}
.hl {{ color:#000; background:linear-gradient(transparent 70%, #00000018 70%); padding:0 2px; }}
.wave {{ display:none; }}
.subtitle-wrap {{ margin-top:14px; font-size:18px; color:#999; letter-spacing:3px; }}
.subtitle-wrap .leaf {{ display:none; }}
.grid {{ display:grid; grid-template-columns:repeat({cols},1fr); gap:14px; }}
.card {{ border-radius:0; padding:16px; background:#fff; border:1px solid #e2e2e2; box-shadow:none; }}
.card-head {{ display:flex; align-items:center; gap:9px; margin-bottom:11px; }}
.badge {{ color:#fff; width:26px; height:26px; border-radius:0; display:inline-flex; align-items:center; justify-content:center; font-weight:700; font-size:15px; }}
.cat {{ font-size:20px; font-weight:800; color:#111 !important; }}
.illus {{ width:100%; aspect-ratio:1/1; border-radius:0; overflow:hidden; margin-bottom:10px; background:#f4f4f4; }}
.illus img {{ width:100%; height:100%; object-fit:cover; filter:grayscale(100%); }}
.summary {{ font-size:15px; font-weight:700; margin-bottom:8px; color:#222 !important; }}
.bullets {{ list-style:none; }}
.bullets li {{ font-size:13.5px; color:#666; line-height:1.7; padding-left:16px; position:relative; }}
.bullets li::before {{ content:"—"; position:absolute; left:0; color:#bbb; }}
.bottom {{ display:grid; grid-template-columns:1fr 1fr; gap:14px; margin-top:26px; }}
.bottom-box {{ background:#fafafa; border:1px solid #e2e2e2; border-radius:0; padding:22px; }}
.bottom-title {{ font-size:21px; font-weight:800; margin-bottom:14px; color:#000; }}
.sec-grid {{ display:grid; grid-template-columns:repeat(2,1fr); gap:12px; }}
.sec-item {{ background:#fff; border:1px solid #eee; border-radius:0; padding:10px 12px; }}
.sec-label {{ font-size:15px; font-weight:800; color:#111; }}
.sec-desc {{ font-size:12.5px; color:#999; margin-top:3px; line-height:1.5; }}
"""


# 风格注册表：界面下拉的可选项即此处的 key。
# bg/fg/box/muted/tfont/sep 供「步骤流程」版式复用，使两种版式风格一致。
_STYLES = {
    "小红书手账风": dict(palette=_PALETTE_XHS, mono=False, css=_css_xhs,
                    deco=dict(sun=True, leaf=True, wave=True), icons=("💡", "🚀"),
                    default_accent="#2e9e54", tfont=_FONT_HAND, sep="wave",
                    bg="radial-gradient(circle at 12% 8%, #fff6e6 0, transparent 38%), radial-gradient(circle at 88% 4%, #eafaf0 0, transparent 34%), #fffdf7",
                    fg="#2b2b2b", box="#ffffff", muted="#666666"),
    "简约商务风": dict(palette=None, mono_bg="#ffffff", css=_css_business,
                   deco=dict(sun=False, leaf=False, wave=False), icons=("", ""),
                   default_accent="#2563eb", tfont=_FONT_SANS, sep="line",
                   bg="#f4f6f9", fg="#1f2733", box="#ffffff", muted="#6b7686"),
    "马卡龙清新风": dict(palette=_PALETTE_MACARON, mono=False, css=_css_macaron,
                    deco=dict(sun=True, leaf=True, wave=True), icons=("🍬", "🌈"),
                    default_accent="#ff7aa2", tfont=_FONT_HAND, sep="wave",
                    bg="linear-gradient(135deg, #fff0f6 0%, #f3f0ff 50%, #eafaff 100%)",
                    fg="#4a4458", box="#ffffff", muted="#8b80a0"),
    "暗黑科技风": dict(palette=_PALETTE_TECH, mono=False, css=_css_tech,
                   deco=dict(sun=False, leaf=False, wave=True), icons=("◆", "▶"),
                   default_accent="#36e0c8", tfont=_FONT_TECH, sep="line",
                   bg="radial-gradient(circle at 15% 0%, #1c2b3a 0, transparent 45%), radial-gradient(circle at 85% 5%, #2a1c3a 0, transparent 42%), #11131a",
                   fg="#d7dce6", box="#1a1e28", muted="#8a93a6"),
    "ins杂志风": dict(palette=_PALETTE_INS, mono=False, css=_css_ins,
                   deco=dict(sun=False, leaf=False, wave=False), icons=("", ""),
                   default_accent="#b5654a", tfont=_FONT_SERIF, sep="line",
                   bg="#ffffff", fg="#1a1a1a", box="#ffffff", muted="#888888"),
    "国潮风": dict(palette=_PALETTE_GUOCHAO, mono=False, css=_css_guochao,
                 deco=dict(sun=False, leaf=False, wave=True), icons=("❖", "➤"),
                 default_accent="#c1272d", tfont=_FONT_KAI, sep="wave",
                 bg="radial-gradient(circle at 50% 0%, #f9f1dd 0, transparent 60%), #f1e4c9",
                 fg="#3a2a1e", box="#fffdf6", muted="#8a7355"),
    "极简黑白": dict(palette=_PALETTE_MONO, mono=False, css=_css_minimal,
                 deco=dict(sun=False, leaf=False, wave=False), icons=("", ""),
                 default_accent="#111111", tfont=_FONT_SANS, sep="line",
                 bg="#ffffff", fg="#111111", box="#ffffff", muted="#999999"),
}
STYLE_NAMES = list(_STYLES.keys())

# 「跟随参考图」：不走固定预设，而是用反推出的视觉风格规格动态生成 CSS（+ 必要时
# 文生图重绘底图），让成品尽量贴近上传的参考图。放在下拉最前，作为默认推荐项。
REFERENCE_STYLE = "跟随参考图（动态）"
STYLE_NAMES = [REFERENCE_STYLE] + STYLE_NAMES


def _palette_hexes(template: dict) -> list:
    """从反推规格里提取合法的十六进制颜色列表。"""
    out = []
    for h in (template.get("palette") or []):
        if isinstance(h, str) and h.strip().startswith("#") and len(h.strip()) in (4, 7):
            out.append(h.strip())
    return out


def _hex_to_rgba(hex_color: str, alpha: float) -> str:
    """#RRGGBB / #RGB → rgba(r,g,b,alpha)。非法输入回退到淡纸色。"""
    h = (hex_color or "").strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        return f"rgba(252,247,236,{alpha})"
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return f"rgba(252,247,236,{alpha})"
    return f"rgba({r},{g},{b},{alpha})"


def readable_accent(hex_color: str, target: float = 150.0) -> str:
    """保证强调色在浅底上可读：颜色太亮(亮度>175)就按比例压暗到目标亮度，保留色相。"""
    s = str(hex_color or "").strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        return hex_color or "#333333"
    try:
        r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    except ValueError:
        return hex_color
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    if lum <= 175:
        return "#" + s
    f = target / max(lum, 1.0)
    return "#%02x%02x%02x" % (int(r * f), int(g * f), int(b * f))


def _lightest_hex(hexes: list) -> Optional[str]:
    """取一组颜色里最亮的那个（当作纸色/底色）。"""
    best, best_lum = None, -1.0
    for h in hexes:
        s = h.lstrip("#")
        if len(s) == 3:
            s = "".join(c * 2 for c in s)
        if len(s) != 6:
            continue
        try:
            r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
        except ValueError:
            continue
        lum = 0.299 * r + 0.587 * g + 0.114 * b
        if lum > best_lum:
            best, best_lum = h, lum
    return best


def _paper_color(template: dict) -> str:
    """
    选一个中性纸色作底色：优先 background.css 的浅色；否则从 palette 里挑【高亮度+低饱和】
    的近白/米色；都没有就回退暖白。避免把 palette 里鲜艳的黄/蓝当成底色导致整张刺眼。
    """
    import re

    def _ok(hexv):
        s = hexv.lstrip("#")
        if len(s) == 3:
            s = "".join(c * 2 for c in s)
        if len(s) != 6:
            return None
        try:
            r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
        except ValueError:
            return None
        lum = 0.299 * r + 0.587 * g + 0.114 * b
        mx, mn = max(r, g, b), min(r, g, b)
        sat = (mx - mn) / mx if mx else 0.0
        return (lum, sat)

    # 把 background.css 里的所有 hex（含渐变里的多个色）+ palette 一起作为候选。
    css = str((template.get("background") or {}).get("css", "")).strip()
    candidates = re.findall(r"#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})", css) + _palette_hexes(template)

    best, best_lum = None, -1.0
    for h in candidates:
        m = _ok(h)
        if not m:
            continue
        lum, sat = m
        if lum >= 224 and sat <= 0.22 and lum > best_lum:
            best, best_lum = h, lum
    return best or "#fbf7ef"


def _title_font(title_style: str) -> str:
    """把标题风格关键字映射到字体栈。"""
    s = (title_style or "").lower()
    if "hand" in s or "script" in s or "毛笔" in s or "手" in s:
        return _FONT_HAND
    if "serif" in s or "宋" in s or "衬线" in s:
        return _FONT_SERIF
    return _FONT_SANS


def _doodle_bg_svg(template: dict) -> str:
    """
    生成一张可平铺的【SVG 手绘涂鸦底纹】data URI：按参考图反推出的配色，用很淡的
    星星/圆点/加号/波浪/三角等手账元素铺满背景。确定性、可控、免文生图，稳定有"背景效果"。
    """
    import urllib.parse

    hexes = _palette_hexes(template)
    col = readable_accent(hexes[0]) if hexes else "#9aa0a6"
    g_stroke = (
        f'<g stroke="{col}" stroke-width="2.2" fill="none" stroke-linecap="round" opacity="0.16">'
        '<path d="M42 22 L48 38 L65 38 L51 48 L56 65 L42 55 L28 65 L33 48 L19 38 L36 38 Z"/>'  # 星星
        '<circle cx="158" cy="44" r="9"/>'                                                      # 圆
        '<path d="M185 96 v22 M174 107 h22"/>'                                                   # 加号
        '<path d="M22 158 q10 -12 20 0 t20 0 t20 0"/>'                                           # 波浪
        '<path d="M128 158 l16 26 h-32 Z"/>'                                                     # 三角
        '</g>'
    )
    g_fill = (
        f'<g fill="{col}" opacity="0.14">'
        '<circle cx="100" cy="100" r="3.5"/><circle cx="208" cy="190" r="3.5"/>'
        '<circle cx="64" cy="208" r="3"/><circle cx="195" cy="150" r="2.6"/>'
        '</g>'
    )
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="230" height="230" '
        f'viewBox="0 0 230 230">{g_stroke}{g_fill}</svg>'
    )
    return "data:image/svg+xml," + urllib.parse.quote(svg)


def _bg_value(template: dict, skin_uri: str | None = None) -> str:
    """
    计算 body 背景：在反推出的底色/渐变之上平铺一层很淡的 SVG 涂鸦底纹（确定性、稳定）。
    底色优先用 background.css（纯色/渐变），否则用中性纸色。
    """
    paper = _paper_color(template)
    css = str((template.get("background") or {}).get("css", "")).strip()
    base = css if css else paper
    return f'url("{_doodle_bg_svg(template)}") repeat, {base}'


def generate_reference_skin(
    template: dict, out_dir: str, provider: Optional[str] = None,
    cb: ProgressCb = None, width: int = 1024, height: int = 1024,
) -> Optional[str]:
    """
    当参考图背景为 textured/photo 时，按反推出的 bg prompt 文生图重绘一张【无文字】
    底图，作为成品的背景皮肤。失败或不适用时返回 None（上层回退到 CSS 背景）。
    """
    bg = template.get("background") or {}
    kind = str(bg.get("kind", "solid")).lower()
    decorations = [str(x) for x in (template.get("decorations") or []) if str(x).strip()]
    note = template.get("overall_style_note", "") or ""
    illus = template.get("illustration_style", "") or ""
    # 触发条件放宽：textured/photo、或检测到装饰元素、或整体是手绘/手账/插画/海报风，
    # 都生成装饰底图——不再只依赖不稳定的 kind 判断。只有纯净极简风才走纯色背景。
    hand_words = ("手账", "手绘", "涂鸦", "插画", "海报", "治愈", "可爱", "卡通",
                  "doodle", "hand", "sketch", "poster", "illustrat", "watercolor", "cartoon")
    is_hand = any(w in note.lower() or w in illus.lower() for w in hand_words)
    if kind not in ("textured", "photo") and not decorations and not is_hand:
        return None
    base = (bg.get("prompt") or "").strip()
    if not base:
        deco_txt = ", ".join(decorations) if decorations else "small doodles, stars, leaves, sparkles"
        base = (f"decorative infographic background, {note} style, {illus} art style, "
                f"hand-drawn {deco_txt} scattered along the edges")
    hexes = _palette_hexes(template)
    palette_txt = (", soft color palette " + ", ".join(hexes[:5])) if hexes else ""
    paper = _paper_color(template)
    # 强约束：只在四周边缘点缀、中间整片留白、克制/细线/低对比，避免抢正文。
    prompt = (
        base + palette_txt +
        ", clean simple cute line-art decorations arranged ONLY along the four edges and corners, "
        "clear and visible but tasteful, the entire large central area MUST be completely empty "
        "for content, absolutely no decorations or objects in the middle, "
        "NO TEXT, NO WORDS, NO LETTERS, NO NUMBERS, flat lay"
    )
    out = os.path.join(out_dir, "skin.png")
    _notify(cb, "skin", "按参考图风格生成背景底图（文生图）…")
    try:
        os.makedirs(out_dir, exist_ok=True)
        image_gen.text_to_image(prompt, out, width=width, height=height, provider=provider)
        _soften_skin(out, paper)  # 确定性后处理：压淡 + 中央留白，保证不抢正文
        return out
    except Exception as e:
        logger.warning(f"reference skin generation failed, fallback to CSS background: {e}")
        return None


def _soften_skin(path: str, paper_hex: str) -> None:
    """
    对生成的底图做确定性约束，不依赖模型自觉：
      1) 整体向纸色混合 → 压淡装饰、统一基调；
      2) 在中央内容区叠一层半透明纸色 → 保证正文区域干净，装饰只留在四周。
    处理失败不抛错（保留原图）。
    """
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return
    try:
        h = paper_hex.strip().lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        paper = (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)) if len(h) == 6 else (252, 247, 236)
        img = Image.open(path).convert("RGB")
        w, hh = img.size
        # 1) 压淡：48% 底图 + 52% 纸色（装饰清晰可见但不刺眼）
        img = Image.blend(Image.new("RGB", (w, hh), paper), img, 0.48)
        # 2) 中央留白：边缘 13% 之内保留装饰，其余盖一层纸色（alpha 150/255）
        rgba = img.convert("RGBA")
        overlay = Image.new("RGBA", (w, hh), (0, 0, 0, 0))
        m = int(min(w, hh) * 0.13)
        ImageDraw.Draw(overlay).rounded_rectangle(
            [m, m, w - m, hh - m], radius=int(min(w, hh) * 0.05), fill=paper + (150,))
        Image.alpha_composite(rgba, overlay).convert("RGB").save(path)
    except Exception as e:
        logger.warning(f"_soften_skin skipped: {e}")


def _css_reference(template: dict, accent: str, cols: int, skin_uri: str | None) -> str:
    """「跟随参考图」动态 CSS：背景/边框/字体/卡片样式/配色都由反推规格驱动。"""
    hexes = _palette_hexes(template)
    text_color = template.get("text_color") or "#2b2b2b"
    tfont = _title_font(template.get("title_style"))
    card_style = (template.get("card_style") or "boxed").lower()
    border = template.get("border") or {}
    border_css = border.get("css", "none") if border.get("present") else "none"
    radius = border.get("radius", 0) or 0
    bg_value = _bg_value(template, skin_uri)
    textured = bool(skin_uri) or str((template.get("background") or {}).get("kind", "")).lower() in ("textured", "photo")
    # 有底图却没识别到边框时，补一条干净描边，保证画面有清晰边界。
    if textured and (not border_css or border_css == "none"):
        border_css = f"3px solid {accent}"
        radius = radius or 22

    # 卡片容器：open=透明铺底图上；outlined=半透明+描边；boxed=白色实心卡。
    if card_style == "open":
        card_bg, card_extra = "transparent", ""
        halo = "text-shadow:0 1px 2px rgba(255,255,255,0.85),0 0 6px rgba(255,255,255,0.6);" if textured else ""
    elif card_style == "outlined":
        card_bg = "rgba(255,255,255,0.62)"
        card_extra = f"border:2px solid {accent}; box-shadow:0 4px 14px rgba(0,0,0,0.08);"
        halo = ""
    else:  # boxed
        card_bg = "rgba(255,255,255,0.92)" if textured else "#ffffff"
        card_extra = "box-shadow:0 6px 18px rgba(60,50,30,0.12);"
        halo = ""
    sec_bg = "rgba(255,255,255,0.6)" if textured else "#ffffff"
    # 有纹理底图时撑到固定高度让底图铺满；纯色/渐变则随内容自适应，避免大片空白。
    min_h = "min-height:1240px;" if textured else ""

    return f"""
body {{ width:1080px; {min_h} padding:80px 64px 86px; font-family:{_FONT_SANS};
  color:{text_color}; background:{bg_value};
  border:{border_css}; border-radius:{radius}px; }}
.header {{ text-align:center; margin-bottom:30px; }}
.title {{ font-family:{tfont}; font-size:72px; font-weight:900; line-height:1.18; letter-spacing:1px; color:{text_color}; {halo} }}
.title .sun {{ display:none; }}
.hl {{ color:{accent}; background:linear-gradient(transparent 60%, {accent}33 60%, {accent}33 92%, transparent 92%); padding:0 6px; }}
.wave {{ display:block; margin:2px auto 0; }}
/* 副标题与标题强调色呼应：accent 色文字 + 淡 accent 底的圆角标签，整体协调 */
.subtitle-wrap {{ display:inline-block; margin-top:18px; font-size:25px; font-weight:700;
  font-family:{tfont}; color:{accent}; background:{accent}1f; padding:8px 26px; border-radius:32px; }}
.subtitle-wrap .leaf {{ display:none; }}
.grid {{ display:grid; grid-template-columns:repeat({cols},1fr); gap:20px; }}
.card {{ border-radius:18px; padding:18px 16px 20px; background:{card_bg}; border-top:5px solid {accent}; {card_extra} }}
.card-head {{ display:flex; align-items:center; gap:10px; margin-bottom:12px; }}
.badge {{ color:#fff; width:34px; height:34px; border-radius:50%; display:inline-flex; align-items:center; justify-content:center; font-weight:800; font-size:18px; box-shadow:0 2px 6px rgba(0,0,0,0.22); }}
.cat {{ font-family:{tfont}; font-size:25px; font-weight:800; {halo} }}
.illus {{ width:100%; aspect-ratio:1/1; border-radius:14px; overflow:hidden; margin-bottom:12px; background:rgba(255,255,255,0.45); }}
.illus img {{ width:100%; height:100%; object-fit:cover; }}
.summary {{ font-size:17px; font-weight:800; margin-bottom:10px; line-height:1.45; {halo} }}
.bullets {{ list-style:none; }}
.bullets li {{ font-size:15px; line-height:1.7; padding-left:18px; position:relative; {halo} }}
.bullets li::before {{ content:"▪"; position:absolute; left:0; color:var(--dot); }}
.bottom {{ display:grid; grid-template-columns:1fr 1fr; gap:20px; margin-top:28px; }}
.bottom-box {{ background:{sec_bg}; border:2px dashed {accent}66; border-radius:18px; padding:22px; }}
.bottom-title {{ font-family:{tfont}; font-size:27px; font-weight:900; margin-bottom:14px; color:{accent}; }}
.sec-grid {{ display:grid; grid-template-columns:repeat(2,1fr); gap:14px; }}
.sec-item {{ background:{sec_bg}; border-radius:12px; padding:10px 12px; }}
.sec-label {{ font-size:16px; font-weight:800; color:{text_color}; }}
.sec-desc {{ font-size:13.5px; color:{text_color}; opacity:0.7; margin-top:4px; line-height:1.5; }}
"""


def _reference_spec(template: dict, skin_path: str | None) -> dict:
    """把反推出的视觉规格包成一个与 _STYLES 同构的 spec，供 build_html 复用。"""
    skin_uri = _img_data_uri(skin_path) if skin_path and os.path.exists(skin_path) else None
    hexes = _palette_hexes(template) or ["#2e9e54", "#ef6c00", "#1565c0", "#c2185b"]
    card_style = (template.get("card_style") or "boxed").lower()
    textured = bool(skin_uri) or str((template.get("background") or {}).get("kind", "")).lower() in ("textured", "photo")
    if card_style == "open":
        card_bg = "transparent"
    elif card_style == "outlined":
        card_bg = "rgba(255,255,255,0.62)"
    else:
        card_bg = "rgba(255,255,255,0.92)" if textured else "#ffffff"
    return dict(
        palette=[(card_bg, readable_accent(h)) for h in hexes],
        mono=False,
        css=lambda accent, cols: _css_reference(template, accent, cols, skin_uri),
        deco=dict(sun=False, leaf=False, wave=True),
        icons=("", ""),
        default_accent=readable_accent(hexes[0]),
        tfont=_title_font(template.get("title_style")),
        sep="wave",
        bg=_bg_value(template, skin_uri),
        fg=template.get("text_color") or "#2b2b2b",
        box="rgba(255,255,255,0.6)" if textured else "#ffffff",
        muted=template.get("text_color") or "#666666",
        _skin_uri=skin_uri,
    )


# 版式选择：界面「版式」下拉的可选项。
#   template=跟随上传模板反推；auto=按主题智能判定；grid=强制宫格；steps=强制步骤流程。
LAYOUT_CHOICES = {
    "跟随模板": "template",
    "自动按主题": "auto",
    "宫格卡片": "grid",
    "步骤流程": "steps",
}


def _img_file_uri(path: str) -> str:
    """本地图片的 file:// URI（给 LLM/无头浏览器直接用 src，不走 base64）。"""
    return "file:///" + os.path.abspath(path).replace("\\", "/")


def _build_cards_html_prompt(data: list, card_w: int) -> str:
    return (
        "你是信息卡片前端工程师。下面是一张海报里若干并列小卡片的数据，"
        "请为**每一张**生成一段**自包含 HTML 片段**，用于拼进 CSS Grid 的格子。\n\n"
        "硬性规则（必须全部遵守）：\n"
        "1. 每张卡片是**单个根 <div>**，根样式含 height:100%;box-sizing:border-box;overflow:hidden;"
        "display:flex;flex-direction:column;background:用数据给的 bg;border-radius:18px;padding:16px;"
        "border-top:5px solid 数据的 accent。\n"
        "2. **只用内联 style**：禁止 class、<style>、外部 CSS、<script>。\n"
        "3. HTML 属性一律用**单引号**。\n"
        f"4. 卡片宽约 {card_w}px、高度自动（同行等高）。字号：分类标题 21~23px、焦点句 16~17px、正文要点 14~15px、序号徽标 16px。\n"
        "5. 配色**必须**用每条数据给的 accent / bg，不要自创。\n"
        "6. **统一结构（每张完全一样，只换内容/主题色）**：① 顶部一行 = 序号圆形徽标(accent 底白字) + 分类标题(category，accent 色、手写体)；"
        "② 若 img 非空，放一张方形插画；③ **焦点句** = summary，做成加粗、accent 色、第一眼重点（可加 accent 左边框）；"
        "④ 要点 = bullets，每条前面一个 accent 小圆点，行距适中。\n"
        "7. img：<img src='给定路径' style='width:100%;aspect-ratio:1/1;object-fit:cover;border-radius:12px;margin:8px 0'/>，"
        "src **原样照抄**（含 file:/// 前缀）；img 为空则不放图、文字撑满。\n"
        "8. 标题/焦点句用 font-family:'ZCOOL KuaiLe'；正文用 \"Microsoft YaHei\",sans-serif。\n"
        "9. **所有卡片版式必须完全统一**；内容要**精炼到放得下**，bullets 最多 3~4 条、宁可精简也**绝不溢出/裁切**卡片。\n\n"
        "数据(JSON 数组)：\n" + json.dumps(data, ensure_ascii=False) + "\n\n"
        '**只返回 JSON**，格式：{"cards":["<div ...>...</div>", ...]}，'
        "数组长度与顺序和输入一一对应。不要解释、不要 markdown 代码围栏。"
    )


def _llm_cards_html(cards: list, illustrations: dict, accent: str, palette,
                    mono_bg: str, cols: int, cb: ProgressCb) -> list | None:
    """让 LLM 为每张并列卡片生成自包含 HTML（内联样式、无 class、无外部 CSS）。
    传入卡片数据 + 插画 file:// 路径，LLM 填充并返回 list[str]（与 cards 等长）。
    任一环节失败返回 None，由 build_html 回退到结构化渲染（保证海报永远出得来）。"""
    n = len(cards)
    if not n:
        return None
    card_w = round((1080 - 72 - (cols - 1) * 18) / cols)  # 海报内容宽≈1008，按列均分
    data = []
    for idx, c in enumerate(cards):
        bg, c_accent = (mono_bg, accent) if not palette else palette[idx % len(palette)]
        img = illustrations.get(idx)
        data.append({
            "number": c.get("number", idx + 1),
            "category": c.get("category", ""),
            "summary": c.get("summary", ""),
            "bullets": list(c.get("bullets", [])),
            "accent": readable_accent(c_accent),
            "bg": bg,
            "img": _img_file_uri(img) if img and os.path.exists(img) else "",
        })
    prompt = _build_cards_html_prompt(data, card_w)
    for attempt in range(3):  # 卡片必须 LLM 生成，解析/数量不符就重试，尽量不退化
        try:
            raw = _generate_text_with_retry(prompt, cb)
            obj = raw if isinstance(raw, dict) else vision._parse_json(raw)
            html = obj.get("cards") if isinstance(obj, dict) else None
            if isinstance(html, list) and len(html) == n:
                out = [str(h).strip() for h in html]
                if all(h.startswith("<") for h in out):
                    return out
            _notify(cb, "render", f"LLM 卡片 HTML 不合规，重试（{attempt + 1}/3）…")
        except Exception as e:
            _notify(cb, "render", f"LLM 卡片 HTML 生成出错，重试（{attempt + 1}/3）：{e}")
    _notify(cb, "render", "LLM 卡片 HTML 多次失败，临时回退结构化渲染（兜底防空白）")
    return None


def build_html(content: dict, illustrations: dict, style: str | None = None,
               font: str | None = None, template: dict | None = None,
               skin_path: str | None = None, cb: ProgressCb = None,
               llm_cards: bool = True) -> str:
    # 按版式分流：步骤流程 → 竖向步骤渲染；其余 → 宫格卡片渲染。
    if content.get("_layout") == "steps" or (content.get("steps") and not content.get("cards")):
        return _steps_html(content, illustrations, style, font, template, skin_path)

    if style == REFERENCE_STYLE and template:
        spec = _reference_spec(template, skin_path)
    else:
        spec = _STYLES.get(style or "") or _STYLES[next(iter(_STYLES))]
    palette = spec["palette"]
    # accent：内容里指定优先，否则用该风格的默认主题色（太亮则压暗保证可读）。
    accent = readable_accent(content.get("accent_color") or spec["default_accent"])
    if spec.get("mono"):  # mono 风格统一用 accent，不用调色板
        palette = None
    title_html = _highlight_title(content.get("title", ""), content.get("title_highlight", ""), accent)
    subtitle = content.get("subtitle", "")
    cards = content.get("cards", [])
    cols = max(2, min(4, content.get("_grid_columns", 4)))

    # 卡片一律由 LLM 生成自包含 HTML（内联样式、无全局 CSS）——这是唯一正式路径。
    # 仅 llm_cards=False（如 style_thumbnail 缩略图预览）才走下面的结构化模板。
    # 极端情况下 LLM 多次失败才回退结构化兜底，避免成品图全空白。
    llm_html = None
    if llm_cards and cards:
        llm_html = _llm_cards_html(cards, illustrations, accent, palette,
                                   spec.get("mono_bg", "#ffffff"), cols, cb)

    card_blocks = []
    for idx, card in enumerate(cards):
        if llm_html is not None:
            card_blocks.append(llm_html[idx])
            continue
        if palette:
            bg, c_accent = palette[idx % len(palette)]
        else:
            bg, c_accent = spec.get("mono_bg", "#ffffff"), accent
        illus_html = ""
        if idx in illustrations and os.path.exists(illustrations[idx]):
            illus_html = (
                f'<div class="illus"><img src="{_img_data_uri(illustrations[idx])}" /></div>'
            )
        bullets = "".join(f"<li>{b}</li>" for b in card.get("bullets", []))
        card_blocks.append(
            f"""
            <div class="card" style="background:{bg};border-top-color:{c_accent};--dot:{c_accent}">
              <div class="card-head">
                <span class="badge" style="background:{c_accent}">{card.get('number', idx + 1)}</span>
                <span class="cat" style="color:{c_accent}">{card.get('category', '')}</span>
              </div>
              {illus_html}
              <div class="summary" style="color:{c_accent}">{card.get('summary', '')}</div>
              <ul class="bullets" style="--dot:{c_accent}">{bullets}</ul>
            </div>
            """
        )

    def _section(sec: dict, key: str, icon: str) -> str:
        if not sec:
            return ""
        items = sec.get(key, [])
        rows = "".join(
            f'<div class="sec-item"><div class="sec-label">{it.get("label", "")}</div>'
            f'<div class="sec-desc">{it.get("desc", "")}</div></div>'
            for it in items
        )
        prefix = f"{icon} " if icon else ""
        return (
            f'<div class="bottom-box"><div class="bottom-title">'
            f'{prefix}{sec.get("title", "")}</div><div class="sec-grid">{rows}</div></div>'
        )

    deco = spec["deco"]
    icons = spec["icons"]
    bottom_html = ""
    bl = _section(content.get("bottom_left", {}), "items", icons[0])
    br = _section(content.get("bottom_right", {}), "steps", icons[1])
    if bl or br:
        bottom_html = f'<div class="bottom">{bl}{br}</div>'

    css = spec["css"](accent, cols)

    sun = '<span class="sun">☀️</span>' if deco.get("sun") else ""
    wave = _wave_svg(accent) if deco.get("wave") else ""
    leaf = '<span class="leaf">🌿</span>' if deco.get("leaf") else ""

    return f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><style>
{_fontface_css()}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
{css}
{_font_override_css(font)}
</style></head><body>
  <div class="header">
    <div class="title">{title_html}{sun}</div>
    {wave}
    <div class="subtitle-wrap">{leaf}{subtitle}{leaf}</div>
  </div>
  <div class="grid">{''.join(card_blocks)}</div>
  {bottom_html}
</body></html>"""


def _wave_full(color: str) -> str:
    """整行宽的波浪分隔线（步骤之间）。"""
    return (
        '<svg class="sep" width="1000" height="14" viewBox="0 0 1000 14" '
        'preserveAspectRatio="none"><path d="M2 8 Q 30 1 60 8 T 120 8 T 180 8 T 240 8 '
        'T 300 8 T 360 8 T 420 8 T 480 8 T 540 8 T 600 8 T 660 8 T 720 8 T 780 8 T 840 8 '
        'T 900 8 T 960 8 T 998 8" stroke="' + color + '" stroke-width="4" fill="none" '
        'stroke-linecap="round"/></svg>'
    )


def _steps_html(content: dict, illustrations: dict, style: str | None = None,
                font: str | None = None, template: dict | None = None,
                skin_path: str | None = None) -> str:
    """步骤流程版式：竖向步骤列表，每步 = 彩色小标题 + 说明文字 + 右侧插图，底部贴士。"""
    if style == REFERENCE_STYLE and template:
        spec = _reference_spec(template, skin_path)
    else:
        spec = _STYLES.get(style or "") or _STYLES[next(iter(_STYLES))]
    palette = spec["palette"] or _PALETTE_XHS
    accent = content.get("accent_color") or spec["default_accent"]
    deco = spec["deco"]
    tfont, bg, fg, box, muted = spec["tfont"], spec["bg"], spec["fg"], spec["box"], spec["muted"]
    title_html = _highlight_title(content.get("title", ""), content.get("title_highlight", ""), accent)
    subtitle = content.get("subtitle", "")
    steps = content.get("steps", [])

    rows = []
    for idx, step in enumerate(steps):
        _, c_accent = palette[idx % len(palette)]
        illus_html = ""
        if idx in illustrations and os.path.exists(illustrations[idx]):
            illus_html = f'<div class="s-illus"><img src="{_img_data_uri(illustrations[idx])}" /></div>'
        sep = ""
        if idx > 0:
            sep = _wave_full(c_accent) if spec["sep"] == "wave" else '<div class="s-line"></div>'
        rows.append(
            f"""
            {sep}
            <div class="step">
              <div class="s-main">
                <div class="s-head">
                  <span class="s-badge" style="background:{c_accent}">{step.get('number', idx + 1)}</span>
                  <span class="s-title" style="background:linear-gradient(transparent 62%, {c_accent}33 62%)">{step.get('heading', '')}</span>
                </div>
                <div class="s-text">{step.get('text', '')}</div>
              </div>
              {illus_html}
            </div>
            """
        )

    tips = content.get("tips", "")
    tips_html = ""
    if tips:
        tips_html = f'<div class="tips"><span class="tips-tag" style="background:{accent}">小贴士</span>{tips}</div>'

    sun = "☀️" if deco.get("sun") else ""
    leaf = '<span class="leaf">🌿</span>' if deco.get("leaf") else ""
    wave = _wave_svg(accent) if deco.get("wave") else ""

    return f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><style>
{_fontface_css()}
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{ width:1080px; padding:44px 44px 52px; font-family:{_FONT_SANS}; background:{bg}; color:{fg}; }}
.header {{ text-align:center; margin-bottom:28px; }}
.title {{ font-family:{tfont}; font-size:60px; font-weight:900; line-height:1.2; letter-spacing:1px; }}
.hl {{ color:{accent}; padding:0 4px; }}
.wave {{ display:block; margin:2px auto 0; }}
.subtitle-wrap {{ margin-top:12px; font-size:22px; color:{muted}; font-weight:600; }}
.subtitle-wrap .leaf {{ color:{accent}; margin:0 10px; font-size:18px; }}
.steps {{ display:flex; flex-direction:column; gap:10px; }}
.step {{ display:flex; align-items:center; gap:20px; padding:6px 4px; }}
.s-main {{ flex:1; }}
.s-head {{ display:flex; align-items:center; gap:11px; margin-bottom:8px; }}
.s-badge {{ color:#fff; min-width:34px; height:34px; border-radius:50%; display:inline-flex; align-items:center; justify-content:center; font-weight:800; font-size:18px; box-shadow:0 2px 6px rgba(0,0,0,0.2); }}
.s-title {{ font-family:{tfont}; font-size:28px; font-weight:800; padding:1px 6px; }}
.s-text {{ font-size:18px; line-height:1.7; color:{fg}; opacity:0.9; padding-left:2px; }}
.s-illus {{ width:190px; height:190px; flex-shrink:0; border-radius:16px; overflow:hidden; background:{box}; box-shadow:0 4px 12px rgba(0,0,0,0.1); }}
.s-illus img {{ width:100%; height:100%; object-fit:cover; }}
.sep {{ display:block; width:100%; margin:2px 0; }}
.s-line {{ height:1px; background:rgba(128,128,128,0.25); margin:6px 0; }}
.tips {{ margin-top:24px; padding:18px 22px; border:2.5px dashed {accent}; border-radius:18px; font-size:19px; font-weight:600; color:{fg}; }}
.tips-tag {{ color:#fff; font-weight:800; font-size:16px; padding:3px 12px; border-radius:20px; margin-right:12px; }}
{_font_override_css(font)}
</style></head><body>
  <div class="header">
    <div class="title">{title_html} {sun}</div>
    {wave}
    <div class="subtitle-wrap">{leaf}{subtitle}{leaf}</div>
  </div>
  <div class="steps">{''.join(rows)}</div>
  {tips_html}
</body></html>"""


def render(content: dict, illustrations: dict, out_dir: str, cb: ProgressCb = None,
           style: str | None = None, font: str | None = None,
           template: dict | None = None, skin_path: str | None = None) -> dict:
    """步骤 5b：构建 HTML 并截图为 PNG。返回 {'html': path, 'image': path}。"""
    os.makedirs(out_dir, exist_ok=True)
    html = build_html(content, illustrations, style, font, template, skin_path, cb)
    html_path = os.path.join(out_dir, "infographic.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    _notify(cb, "render", "正在用无头浏览器渲染成品图…")
    image_path = os.path.join(out_dir, "infographic.png")
    render_html_to_png(html_path, image_path, width=1080, full_page=True)
    _notify(cb, "render", "成品图已生成")
    return {"html": html_path, "image": image_path}


# 缩略图用的固定示例内容（6 张卡片，足够体现风格差异）。
_THUMB_SAMPLE = {
    "title": "风格预览效果", "title_highlight": "预览",
    "subtitle": "PREVIEW 这是该风格的示例", "_grid_columns": 3,
    "cards": [
        {"number": i, "category": f"分类{i}", "summary": "一句话概括要点",
         "bullets": ["要点示例一", "要点示例二"]}
        for i in range(1, 7)
    ],
    "bottom_left": {"title": "小结", "items": [
        {"label": "要点", "desc": "简短说明"}, {"label": "要点", "desc": "简短说明"}]},
    "bottom_right": {"title": "路线", "steps": [
        {"label": "步骤", "desc": "简短说明"}, {"label": "步骤", "desc": "简短说明"}]},
}


def style_thumbnail(style: str, force: bool = False) -> str:
    """
    生成（并缓存）某风格的预览缩略图，返回 PNG 路径。

    首次调用会渲染一次（约 1~2 秒），之后命中磁盘缓存秒回。
    风格 CSS 改动后想刷新缓存，传 force=True 或删除 storage/image_studio/_thumbs。
    """
    # 「跟随参考图」没有固定预览——效果取决于上传的参考图本身。
    if style == REFERENCE_STYLE:
        raise ValueError("「跟随参考图」无固定预览：上传参考图后按其风格动态出图。")
    thumb_dir = utils.storage_dir(os.path.join("image_studio", "_thumbs"), create=True)
    out = os.path.join(thumb_dir, f"{style}.png")
    if os.path.exists(out) and not force:
        return out
    html = build_html(_THUMB_SAMPLE, {}, style, llm_cards=False)  # 缩略图用结构化模板，不调 LLM
    html_path = os.path.join(thumb_dir, f"{style}.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    render_html_to_png(html_path, out, width=820, full_page=True)
    return out


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# 断点续跑：各步骤的「带缓存」包装（WebUI 与 run_pipeline 共用同一套磁盘缓存约定）
#
# 约定：同一任务目录 out_dir 下，每步成功即把结果落盘——
#   template.json（反推+版式/数量覆盖后）、content.json（生成知识后；审核后追加
#   _reviewed=True 再重写）、promo.json（种草文案）、cards/item_N.png（插画，幂等）。
# 续跑时这些 *_cached 包装先读缓存命中即跳过，只补未完成的步骤。
# --------------------------------------------------------------------------- #
def _load_json(path: str):
    """读取 JSON，文件不存在或损坏返回 None（视作「该步未完成」）。"""
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def reverse_template_cached(out_dir: str, template_image_path: str, cb: ProgressCb = None,
                            layout_choice: str = "template", subject: str = "",
                            card_count: int = 0) -> dict:
    """反推模板（带缓存）。命中 out_dir/template.json 直接复用；否则反推并应用
    版式（template/auto/grid/steps）与数量覆盖后落盘。"""
    cache = os.path.join(out_dir, "template.json")
    cached = _load_json(cache)
    if cached is not None:
        _notify(cb, "reverse", "复用已反推的模板结构（续跑）")
        return cached
    template = reverse_template(template_image_path, cb)
    # 版式决策：跟随模板 / 自动按主题 / 强制宫格或步骤流程。
    if layout_choice == "auto":
        template["layout_kind"] = classify_layout_by_subject(subject, cb)
        _notify(cb, "reverse", f"按主题智能判定版式：{template['layout_kind']}")
    elif layout_choice in ("grid", "steps"):
        template["layout_kind"] = layout_choice
        _notify(cb, "reverse", f"按用户指定版式：{layout_choice}")
    # 数量优先级：用户手填 > 主题里写明的数量 > 模板反推值。
    if card_count:
        template["card_count"] = card_count
        template["step_count"] = card_count
        _notify(cb, "reverse", f"按用户指定数量：{card_count}")
    else:
        _n = count_from_subject(subject)
        if _n and _n != template.get("card_count"):
            template["card_count"] = _n
            template["step_count"] = _n
            _notify(cb, "reverse", f"按主题中写明的数量：{_n}（覆盖模板反推值）")
    os.makedirs(out_dir, exist_ok=True)
    with open(cache, "w", encoding="utf-8") as f:
        json.dump(template, f, ensure_ascii=False, indent=2)
    return template


def generate_knowledge_cached(out_dir: str, subject: str, template: dict, extra: str = "",
                              cb: ProgressCb = None, aspect: str = "9:16") -> dict:
    """生成知识数据（带缓存）。命中 out_dir/content.json 直接复用。"""
    cache = os.path.join(out_dir, "content.json")
    cached = _load_json(cache)
    if cached is not None:
        _notify(cb, "knowledge", "复用已生成的知识数据（续跑）")
        return cached
    content = generate_knowledge(subject, template, extra, cb, aspect=aspect)
    content["_grid_columns"] = template.get("grid_columns", 4)
    os.makedirs(out_dir, exist_ok=True)
    with open(cache, "w", encoding="utf-8") as f:
        json.dump(content, f, ensure_ascii=False, indent=2)
    return content


def review_and_correct_cached(out_dir: str, template_image_path: str, subject: str,
                              content: dict, template: dict, review_rounds: int = 1,
                              cb: ProgressCb = None) -> tuple[dict, list]:
    """审核矫正（带缓存）。content 已带 _reviewed 标记或 rounds<=0 则跳过；
    否则审核后打标记并重写 content.json，续跑时不再重审。"""
    if review_rounds <= 0 or content.get("_reviewed"):
        return content, []
    content, review_log = review_and_correct(
        template_image_path, subject, content, template, review_rounds, cb
    )
    content["_grid_columns"] = template.get("grid_columns", 4)
    content["_reviewed"] = True
    with open(os.path.join(out_dir, "content.json"), "w", encoding="utf-8") as f:
        json.dump(content, f, ensure_ascii=False, indent=2)
    return content, review_log


def generate_promo_copy_cached(out_dir: str, subject: str, content: dict = None,
                               cb: ProgressCb = None) -> dict:
    """种草文案（带缓存）。命中 out_dir/promo.json 直接复用；否则生成并落盘。"""
    cache = os.path.join(out_dir, "promo.json")
    cached = _load_json(cache)
    if cached is not None:
        _notify(cb, "promo", "复用已生成的种草文案（续跑）")
        return cached
    promo = generate_promo_copy(subject, content, cb)
    if promo.get("title") or promo.get("body"):
        os.makedirs(out_dir, exist_ok=True)
        with open(cache, "w", encoding="utf-8") as f:
            json.dump(promo, f, ensure_ascii=False, indent=2)
    return promo


def run_pipeline(req: ImageStudioRequest, cb: ProgressCb = None) -> dict:
    """
    完整执行 5 步 pipeline，返回结果字典：
      {task_id, template, content, review_log, illustrations, html, image}

    断点续跑：req.task_id 留空则新建随机目录；传入已有 task_id 则复用其目录，
    按已落盘的中间结果跳过已完成步骤、只补未完成的（与 WebUI 共用同一套缓存）。
    """
    task_id = getattr(req, "task_id", None) or utils.get_uuid()
    out_dir = utils.storage_dir(os.path.join("image_studio", task_id), create=True)
    _notify(cb, "start", f"任务 {task_id} 开始")

    template = reverse_template_cached(out_dir, req.template_image_path, cb,
                                       subject=req.subject)
    content = generate_knowledge_cached(out_dir, req.subject, template,
                                        req.extra_requirements, cb)
    content, review_log = review_and_correct_cached(
        out_dir, req.template_image_path, req.subject, content, template,
        req.review_rounds, cb
    )

    illustrations = {}
    if req.generate_illustrations:
        illustrations = generate_card_illustrations(
            content, os.path.join(out_dir, "cards"), req.image_provider, cb
        )

    style = getattr(req, "style", None)
    rendered = render(content, illustrations, out_dir, cb, style, getattr(req, "font", None),
                      template, None)

    # 落盘最终内容（审核版会带 _reviewed），便于复用/调试与续跑。
    with open(os.path.join(out_dir, "content.json"), "w", encoding="utf-8") as f:
        json.dump(content, f, ensure_ascii=False, indent=2)

    _notify(cb, "done", "全部完成")
    return {
        "task_id": task_id,
        "out_dir": out_dir,
        "template": template,
        "content": content,
        "review_log": review_log,
        "illustrations": illustrations,
        "html": rendered["html"],
        "image": rendered["image"],
    }
