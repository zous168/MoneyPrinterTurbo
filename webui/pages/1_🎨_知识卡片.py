"""
WebUI 页面：知识卡片信息图生成器

Streamlit 多页应用会自动发现 webui/pages/ 下的脚本。
本页面实现需求的 5 步流程：
  1. 输入主题 + 上传模板图片
  2. 反推模板版式结构（视觉大模型）
  3. 基于主题组织知识数据（结构化内容 + 文案）
  4. 审核矫正
  5. 渲染出成品图
"""

import json
import os
import re
import sys
import time

import streamlit as st

# 把项目根目录加入 sys.path，使本页面能直接 import app 包。
root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
if root_dir not in sys.path:
    sys.path.append(root_dir)

from app.config import config
from app.services import image_studio
from app.utils import utils

# 视频功能为可选项：便携「只出图」包可不带视频依赖(moviepy/edge-tts 等)，
# 导入失败时自动隐藏视频区块，出图功能不受影响。
try:
    from app.services import card_video
    HAS_VIDEO = True
except Exception:
    card_video = None
    HAS_VIDEO = False

st.set_page_config(page_title="知识卡片生成器", page_icon="🎨", layout="wide")
st.title("🎨 知识卡片信息图生成器")
st.caption(
    "输入主题 + 上传一张模板信息图，自动反推版式 → 组织知识 → 审核矫正 → 生成同款卡片图。"
    "文字渲染走 HTML + 无头浏览器截图，中文清晰；卡片插画走文生图。"
)


# ---------------------------------------------------------------- 历史记录
def _history_tasks():
    """扫描已生成的任务目录，返回按时间倒序的任务列表。"""
    base = utils.storage_dir("image_studio")
    out = []
    if not os.path.isdir(base):
        return out
    for name in os.listdir(base):
        if name.startswith("_"):  # 跳过 _thumbs / _uploads
            continue
        cj = os.path.join(base, name, "content.json")
        if not os.path.isfile(cj):
            continue
        try:
            content = json.load(open(cj, encoding="utf-8"))
        except Exception:
            continue
        meta = {}
        mj = os.path.join(base, name, "meta.json")
        if os.path.isfile(mj):
            try:
                meta = json.load(open(mj, encoding="utf-8"))
            except Exception:
                pass
        img = os.path.join(base, name, "infographic.png")
        out.append({
            "dir": os.path.join(base, name), "id": name,
            "title": content.get("title") or meta.get("subject") or name[:8],
            "layout": content.get("_layout", "grid"),
            "image": img if os.path.isfile(img) else None,
            "mtime": os.path.getmtime(cj), "content": content, "meta": meta,
        })
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def _load_task(t):
    """把历史任务加载成 result 结构 + 已有视频。"""
    d = t["dir"]
    illus = {}
    cards_dir = os.path.join(d, "cards")
    if os.path.isdir(cards_dir):
        for fn in os.listdir(cards_dir):
            m = re.match(r"(?:item|card)_(\d+)\.png$", fn)
            if m:
                illus[int(m.group(1)) - 1] = os.path.join(cards_dir, fn)
    # 复原反推规格与底图（若该任务是「跟随参考图」生成的），以便重渲染/出视频风格一致。
    tpl = None
    tpl_path = os.path.join(d, "template.json")
    if os.path.exists(tpl_path):
        try:
            with open(tpl_path, encoding="utf-8") as f:
                tpl = json.load(f)
        except Exception:
            tpl = None
    skin = os.path.join(d, "skin.png")
    skin = skin if os.path.exists(skin) else None
    # 复原分镜图（二级图片）
    scenes_dir = os.path.join(d, "scenes")
    scene_imgs = []
    if os.path.isdir(scenes_dir):
        scene_imgs = sorted(
            os.path.join(scenes_dir, fn) for fn in os.listdir(scenes_dir)
            if re.match(r"scene_\d+\.png$", fn)
        )
    promo = None
    promo_path = os.path.join(d, "promo.json")
    if os.path.exists(promo_path):
        try:
            with open(promo_path, encoding="utf-8") as f:
                promo = json.load(f)
        except Exception:
            promo = None
    # 恢复模板图（参考图）：meta 记录的文件名优先，兼容旧任务的 template_image.*
    tpl_img = None
    _ti = t["meta"].get("template_image")
    if _ti and os.path.exists(os.path.join(d, _ti)):
        tpl_img = os.path.join(d, _ti)
    else:
        for fn in os.listdir(d):
            if fn.startswith("template_image."):
                tpl_img = os.path.join(d, fn)
                break
    if tpl_img is None:
        # 旧任务没存模板图：从 _uploads 里按时间就近回找（最接近、且不晚于任务完成时间的上传）。
        _up = os.path.join(os.path.dirname(d), "_uploads")
        if os.path.isdir(_up):
            _ref = t.get("mtime") or os.path.getmtime(d)
            _best, _best_dt = None, None
            for fn in os.listdir(_up):
                fp = os.path.join(_up, fn)
                if not os.path.isfile(fp):
                    continue
                _dt = _ref - os.path.getmtime(fp)  # 上传应早于任务完成 → _dt>0 且较小
                if -120 < _dt < 7200 and (_best_dt is None or abs(_dt) < abs(_best_dt)):
                    _best, _best_dt = fp, _dt
            tpl_img = _best
    # 恢复二维码
    qr_img = None
    _qi = t["meta"].get("qr_image")
    if _qi and os.path.exists(os.path.join(d, _qi)):
        qr_img = os.path.join(d, _qi)
    result = {
        "out_dir": d, "content": t["content"], "illustrations": illus,
        "image": t["image"], "template": tpl, "review_log": [], "error": None,
        "style": t["meta"].get("style") or image_studio.STYLE_NAMES[0],
        "font": t["meta"].get("font"), "skin_path": skin, "scene_images": scene_imgs,
        "promo": promo, "template_path": tpl_img,
        "brand": t["meta"].get("brand", ""), "follow_text": t["meta"].get("follow_text", ""),
        "qr_path": qr_img, "scene_aspect": t["meta"].get("scene_aspect", "9:16"),
    }
    videos = {}
    vdir = os.path.join(d, "videos")
    if os.path.isdir(vdir):
        for fn in os.listdir(vdir):
            m = re.match(r"video_(\d+x\d+)_(\w+)\.mp4$", fn)
            if m:
                videos[f"{m.group(1).replace('x', ':')}-{m.group(2)}"] = os.path.join(vdir, fn)
    return result, (videos or None)


with st.expander("📂 历史记录（左侧点选 → 右侧预览并加载）", expanded=st.session_state.get("hist_open", False)):
    _tasks = _history_tasks()
    if not _tasks:
        st.caption("暂无历史记录。生成一次后会出现在这里。")
    else:
        _ids = [t["id"] for t in _tasks]
        if st.session_state.get("hist_sel") not in _ids:
            st.session_state.hist_sel = _ids[0]
        _left, _right = st.columns([1, 2])
        with _left:
            st.caption(f"共 {len(_tasks)} 条")
            for _tk in _tasks[:40]:
                _sel = st.session_state.hist_sel == _tk["id"]
                _lbl = (f"{'✅ ' if _sel else ''}{_tk['title'][:14]} · "
                        f"{time.strftime('%m-%d %H:%M', time.localtime(_tk['mtime']))}")
                if st.button(_lbl, key=f"hsel_{_tk['id']}", use_container_width=True,
                             type="primary" if _sel else "secondary"):
                    st.session_state.hist_sel = _tk["id"]
                    st.session_state.hist_open = True  # 选择时保持展开
                    st.rerun()
        with _right:
            _cur = next((t for t in _tasks if t["id"] == st.session_state.hist_sel), _tasks[0])
            if _cur["image"]:
                st.image(_cur["image"], width=300)
            st.write(f"**{_cur['title']}**")
            st.caption(
                f"{'步骤流程' if _cur['layout'] == 'steps' else '宫格'} · "
                f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(_cur['mtime']))}"
            )
            st.caption(f"目录：{_cur['dir']}")
            if st.button("📥 加载此任务", type="primary", use_container_width=True):
                _res, _vids = _load_task(_cur)
                st.session_state.ks_result = _res
                st.session_state.ks_videos = _vids
                # 回填主题/模板图/品牌信息，方便基于历史直接再次生成。
                st.session_state.ks_subj = _cur["meta"].get("subject") or _cur["title"]
                st.session_state.ks_template_path = _res.get("template_path")
                st.session_state.ks_brand = _res.get("brand", "")
                st.session_state.ks_follow = _res.get("follow_text", "")
                st.session_state.ks_qr_path = _res.get("qr_path")
                st.session_state.hist_open = False  # 加载后自动收起历史
                st.rerun()

# ---------------------------------------------------------------- 输入区
col_left, col_right = st.columns([1, 1])
with col_left:
    subject = st.text_input("① 主题", placeholder="例如：自媒体新人如何起步", key="ks_subj")
    extra = st.text_area("额外要求（可选）", placeholder="语气、受众、侧重点等", height=80)
    layout_label = st.selectbox(
        "🧱 版式", list(image_studio.LAYOUT_CHOICES.keys()), index=0,
        help="跟随模板=按上传图反推；自动按主题=让模型判断流程/清单后选版式；也可强制宫格或步骤流程。",
    )
    card_count = st.number_input(
        "卡片/步骤数量（0 = 跟随模板）", min_value=0, max_value=24, value=0, step=1,
        help="留 0 则使用从模板图反推出的数量；填具体数字则强制生成该数量（宫格=卡片数，流程=步骤数）。",
    )
    review_rounds = st.slider("审核矫正轮数", 0, 3, 1)
    gen_illus = st.checkbox("生成卡片插画（文生图，较耗时）", value=True)
    gen_promo = st.checkbox(
        "生成小红书种草文案（含关键词标签）", value=True,
        help="按小红书种草风格，为主题生成爆款标题+口语化正文+话题关键词，可直接复制发布。",
    )
    if HAS_VIDEO:
        gc1, gc2 = st.columns([2, 1])
        with gc1:
            gen_scenes = st.checkbox(
                "顺带生成每张分镜图（图集/视频素材）", value=True,
                help="把视频用的每个分镜（片头+每条+片尾）也渲染成独立竖图，可直接做小红书图集或视频素材。",
            )
        with gc2:
            scene_aspect = st.selectbox("分镜比例", ["9:16", "16:9"], index=0, disabled=not gen_scenes)
    else:
        gen_scenes, scene_aspect = False, "9:16"
    style = st.selectbox("🎨 成品风格", image_studio.STYLE_NAMES, index=0)
    font_label = st.selectbox(
        "✍️ 文字字体", list(image_studio.FONT_CHOICES.keys()), index=0,
        help="手绘/Q版字体让标题与小标题更贴近小红书手账风的原图观感。",
    )
    provider_options = ["（默认）", "openai", "pollinations", "siliconflow"]
    image_provider = st.selectbox("文生图后端", provider_options, index=0)
    with st.expander("🏷️ 版权 / 品牌（分镜图与视频，可选）"):
        brand = st.text_input("右上角品牌水印", placeholder="@某某公众号", key="ks_brand")
        follow_text = st.text_input("最后一页引导语", placeholder="关注 某某公众号，获取更多干货", key="ks_follow")
        qr_file = st.file_uploader("最后一页二维码（可选）", type=["png", "jpg", "jpeg", "webp"], key="qr_up")
        if qr_file is None and st.session_state.get("ks_qr_path") and os.path.exists(st.session_state["ks_qr_path"]):
            st.image(st.session_state["ks_qr_path"], caption="已载入历史二维码（可直接用；要换请上传新的）", width=120)

with col_right:
    uploaded = st.file_uploader("② 上传模板图片", type=["png", "jpg", "jpeg", "webp"])
    if uploaded is not None:
        # 缩略预览，避免占位过大。
        st.image(uploaded, caption="模板预览（缩略）", width=240)
    elif st.session_state.get("ks_template_path") and os.path.exists(st.session_state["ks_template_path"]):
        # 加载历史后回填的模板图：可直接预览、不重新上传也能再次生成。
        st.image(st.session_state["ks_template_path"], caption="已载入历史模板图（可直接生成；要换图请上传新的）", width=240)

st.info(
    f"当前视觉/文案模型 provider：`{config.app.get('vision_provider') or config.app.get('llm_provider', 'openai')}`"
    "（需为支持视觉的模型，如 openai gpt-4o、gemini、qwen-vl）。"
    f"文生图后端：`{config.app.get('image_provider', 'pollinations')}`。"
)

# 风格预览：左侧当前选中风格的缩略图，右侧可展开对比全部风格。
prev_col, gallery_col = st.columns([1, 3])
with prev_col:
    if style == image_studio.REFERENCE_STYLE:
        st.info("🪄 跟随参考图：成品风格将自动识别你上传的参考图（背景/配色/字体/边框/卡片样式），无固定预览。")
    else:
        try:
            st.image(image_studio.style_thumbnail(style), caption=f"当前风格：{style}", use_container_width=True)
        except Exception as e:
            st.caption(f"风格预览暂不可用：{e}")
with gallery_col:
    with st.expander("📚 全部风格对比（首次展开会生成各风格缩略图）"):
        gcols = st.columns(4)
        _preset_names = [s for s in image_studio.STYLE_NAMES if s != image_studio.REFERENCE_STYLE]
        for i, sname in enumerate(_preset_names):
            with gcols[i % 4]:
                try:
                    st.image(image_studio.style_thumbnail(sname), caption=sname, use_container_width=True)
                except Exception:
                    st.caption(f"{sname}（预览失败）")

def _save_upload(file) -> str:
    """把上传的模板图片落盘，返回路径。"""
    up_dir = utils.storage_dir(os.path.join("image_studio", "_uploads"), create=True)
    path = os.path.join(up_dir, f"{utils.get_uuid()}_{file.name}")
    with open(path, "wb") as f:
        f.write(file.getbuffer())
    return path


# 运行态守卫：任务进行中禁用按钮，避免重复点击造成请求突发（429 限流）。
running = st.session_state.get("ks_running", False)
start = st.button("🚀 一键生成", type="primary", use_container_width=True, disabled=running)
if running:
    st.warning("⏳ 任务进行中，按钮已禁用，请等待完成后再生成（避免触发限流）。")

# 阶段一：接收点击 → 置运行态并落盘参数 → 立即 rerun，让按钮先渲染成禁用。
if start and not running:
    if not subject.strip():
        st.error("请先填写主题。")
        st.stop()
    # 模板图：优先用本次新上传的；否则用从历史加载回填的模板图。
    _tpl_path = _save_upload(uploaded) if uploaded is not None else st.session_state.get("ks_template_path")
    if not _tpl_path or not os.path.exists(_tpl_path):
        st.error("请先上传模板图片（或从历史记录加载一个带模板图的任务）。")
        st.stop()
    st.session_state.ks_running = True
    st.session_state.ks_result = None
    st.session_state.ks_params = {
        "subject": subject.strip(),
        "template_path": _tpl_path,
        "extra": extra.strip(),
        "provider": None if image_provider == "（默认）" else image_provider,
        "gen_illus": gen_illus,
        "review_rounds": review_rounds,
        "card_count": int(card_count),
        "style": style,
        "font": image_studio.FONT_CHOICES.get(font_label),
        "layout": image_studio.LAYOUT_CHOICES.get(layout_label, "template"),
        "gen_scenes": gen_scenes,
        "scene_aspect": scene_aspect,
        "gen_promo": gen_promo,
        "brand": brand.strip(),
        "follow_text": follow_text.strip(),
        "qr_path": _save_upload(qr_file) if qr_file is not None else st.session_state.get("ks_qr_path"),
    }
    st.rerun()

# 阶段二：分步执行，每个阶段完成即时展示其结果；中途失败也保留已完成阶段。
if st.session_state.get("ks_running") and st.session_state.get("ks_params"):
    p = st.session_state.ks_params
    status_box = st.status("处理中…", expanded=True)
    live = st.container()
    # result 同时存进 session_state，逐阶段累积，刷新/失败都不丢。
    result = {"out_dir": None, "template": None, "content": None,
              "review_log": [], "illustrations": {}, "image": None, "error": None,
              "style": p.get("style"), "font": p.get("font")}
    st.session_state.ks_result = result

    def cb(stage: str, msg: str):
        status_box.write(f"**[{stage}]** {msg}")

    try:
        # 断点续跑：复用上次任务目录（保留已落盘的中间结果）；否则新建目录。
        _resume_dir = p.get("resume_dir")
        if _resume_dir and os.path.isdir(_resume_dir):
            out_dir = _resume_dir
            cb("resume", f"断点续跑：复用目录 {out_dir}，跳过已完成步骤")
        else:
            out_dir = utils.storage_dir(os.path.join("image_studio", utils.get_uuid()), create=True)
        result["out_dir"] = out_dir

        # 步骤2：反推模板结构（带缓存的服务层 helper，续跑时复用 template.json）
        template = image_studio.reverse_template_cached(
            out_dir, p["template_path"], cb,
            layout_choice=p.get("layout", "template"),
            subject=p["subject"], card_count=int(p.get("card_count") or 0),
        )
        result["template"] = template
        with live.expander("🧩 步骤2 · 模板结构反推", expanded=False):
            st.json(template)

        # 步骤3：组织知识数据（带缓存，续跑时复用 content.json）
        content = image_studio.generate_knowledge_cached(
            out_dir, p["subject"], template, p["extra"], cb,
            aspect=p.get("scene_aspect", "9:16"),
        )
        result["content"] = content
        with live.expander("📝 步骤3 · 知识数据", expanded=True):
            st.json(content)

        # 步骤4：审核矫正（带缓存，content 带 _reviewed 或 rounds=0 时跳过）
        content, review_log = image_studio.review_and_correct_cached(
            out_dir, p["template_path"], p["subject"], content, template,
            p["review_rounds"], cb,
        )
        result["content"] = content
        if review_log:
            result["review_log"] = review_log
            with live.expander("🔍 步骤4 · 审核日志", expanded=False):
                st.json(review_log)

        # 步骤4b：小红书种草文案（带缓存，续跑时复用 promo.json）
        if p.get("gen_promo"):
            try:
                promo = image_studio.generate_promo_copy_cached(out_dir, p["subject"], content, cb)
                if promo.get("title") or promo.get("body"):
                    result["promo"] = promo
                    with open(os.path.join(out_dir, "promo.md"), "w", encoding="utf-8") as f:
                        f.write(image_studio.promo_to_markdown(promo))
                    with live.expander("📣 步骤4b · 小红书种草文案", expanded=True):
                        st.markdown(f"**{promo.get('title','')}**")
                        st.text(promo.get("body", ""))
                        st.caption(" ".join(promo.get("tags", [])))
            except Exception as e:
                cb("promo", f"种草文案生成失败（不影响出图）：{e}")

        # 步骤5a：卡片插画
        illus = {}
        if p["gen_illus"]:
            illus = image_studio.generate_card_illustrations(
                content, os.path.join(out_dir, "cards"), p["provider"], cb
            )
            result["illustrations"] = illus

        # 背景改用确定性 SVG 涂鸦底纹（按参考图配色），不再依赖不稳定的文生图底图。
        skin_path = None
        result["skin_path"] = skin_path

        # 步骤5b：渲染成品图（卡片一律由 LLM 生成自包含 HTML，见 image_studio.build_html）
        rendered = image_studio.render(content, illus, out_dir, cb, style=p.get("style"),
                                       font=p.get("font"), template=template, skin_path=skin_path)
        result["image"] = rendered["image"]

        # 步骤5c：顺带生成每张分镜图（二级图片，可做图集/视频素材）
        # 版权/品牌 + 分镜比例：存进 result 供视频/重渲复用
        result["brand"] = p.get("brand", "")
        result["follow_text"] = p.get("follow_text", "")
        result["qr_path"] = p.get("qr_path")
        result["scene_aspect"] = p.get("scene_aspect", "9:16")
        if p.get("gen_scenes") and HAS_VIDEO:
            try:
                result["scene_images"] = card_video.render_scene_images(
                    content, illus, os.path.join(out_dir, "scenes"),
                    style=p.get("style"), font=p.get("font"),
                    aspect=p.get("scene_aspect", "9:16"), lang="zh",
                    template=template, skin_path=skin_path, cb=cb,
                    brand=p.get("brand", ""), follow_text=p.get("follow_text", ""),
                    qr_path=p.get("qr_path"),
                )
            except Exception as e:
                cb("scene_img", f"分镜图生成失败（不影响成品图）：{e}")
        with open(os.path.join(out_dir, "content.json"), "w", encoding="utf-8") as f:
            json.dump(content, f, ensure_ascii=False, indent=2)
        # 把模板图复制进任务目录，便于历史记录恢复（预览 + 不重新上传直接再生成）。
        _tpl_name = ""
        try:
            _src = p.get("template_path")
            if _src and os.path.exists(_src):
                _ext = os.path.splitext(_src)[1] or ".png"
                _tpl_name = f"template_image{_ext}"
                import shutil as _sh
                _sh.copyfile(_src, os.path.join(out_dir, _tpl_name))
        except Exception:
            _tpl_name = ""
        # 复制二维码进任务目录，便于历史恢复
        _qr_name = ""
        try:
            _qsrc = p.get("qr_path")
            if _qsrc and os.path.exists(_qsrc):
                _qext = os.path.splitext(_qsrc)[1] or ".png"
                _qr_name = f"qr_image{_qext}"
                import shutil as _sh2
                _sh2.copyfile(_qsrc, os.path.join(out_dir, _qr_name))
        except Exception:
            _qr_name = ""
        # meta.json：保存主题/风格/字体/模板图名/品牌信息，供历史记录恢复
        with open(os.path.join(out_dir, "meta.json"), "w", encoding="utf-8") as f:
            json.dump({"subject": p["subject"], "style": p.get("style"),
                       "font": p.get("font"), "layout": content.get("_layout", "grid"),
                       "template_image": _tpl_name,
                       "brand": p.get("brand", ""), "follow_text": p.get("follow_text", ""),
                       "qr_image": _qr_name, "scene_aspect": p.get("scene_aspect", "9:16")},
                      f, ensure_ascii=False, indent=2)
        # template.json：保存反推出的风格规格，便于排查“为什么背景/边框是这样”
        try:
            with open(os.path.join(out_dir, "template.json"), "w", encoding="utf-8") as f:
                json.dump(template, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

        status_box.update(label="完成 ✅", state="complete", expanded=False)
    except Exception as e:
        result["error"] = str(e)
        status_box.update(label="失败 ❌（已完成的阶段结果已保留，见下方）", state="error")
    finally:
        st.session_state.ks_result = result
        st.session_state.ks_running = False
        # 暂存本次参数，供失败后「断点续跑」复用（只换 resume_dir，跳过已完成步骤）。
        st.session_state.ks_last_params = p
        st.session_state.ks_params = None
    st.rerun()

# 结果展示：从 session_state 渲染，刷新 / 点下载按钮都不会丢失或重跑。
result = st.session_state.get("ks_result")
if result:
    if result.get("error"):
        st.error(f"生成中断：{result['error']}")
        # 断点续跑：复用上次目录与已完成步骤（反推/知识/审核/文案/已生成的插画），只补做失败部分。
        _can_resume = (result.get("out_dir") and os.path.isdir(result["out_dir"])
                       and st.session_state.get("ks_last_params") and not running)
        if _can_resume and st.button(
            "🔁 断点续跑（复用已完成步骤，只补做失败部分）",
            type="primary", use_container_width=True,
        ):
            _rp = dict(st.session_state.ks_last_params)
            _rp["resume_dir"] = result["out_dir"]
            st.session_state.ks_params = _rp
            st.session_state.ks_running = True
            st.session_state.ks_result = None
            st.rerun()
    if result.get("image") and os.path.exists(result["image"]):
        st.success("生成完成！")

    # ✨ 标题候选（带钩子·去AI味）：可换标题并重渲成品图/分镜
    _content0 = result.get("content") or {}
    _topts = _content0.get("title_options") or []
    if _topts:
        with st.expander("✨ 标题候选（带钩子·去 AI 味，可换标题重渲）", expanded=False):
            _curt = _content0.get("title", "")
            _choices = _topts if _curt in _topts else ([_curt] + _topts if _curt else _topts)
            _pick = st.radio("选择标题", _choices,
                             index=_choices.index(_curt) if _curt in _choices else 0, key="title_pick")
            if st.button("✅ 用所选标题重新渲染成品图 + 分镜", use_container_width=True):
                _c = result.get("content") or {}
                _c["title"] = _pick
                if _c.get("title_highlight") and _c["title_highlight"] not in _pick:
                    _c["title_highlight"] = ""
                _od, _tpl, _sk = result.get("out_dir"), result.get("template"), result.get("skin_path")
                _ill = result.get("illustrations") or {}
                try:
                    with st.spinner("重新渲染中…"):
                        _rr = image_studio.render(_c, _ill, _od, None, style=result.get("style"),
                                                  font=result.get("font"), template=_tpl, skin_path=_sk)
                        result["image"] = _rr["image"]
                        if result.get("scene_images") and HAS_VIDEO:
                            result["scene_images"] = card_video.render_scene_images(
                                _c, _ill, os.path.join(_od, "scenes"),
                                style=result.get("style"), font=result.get("font"),
                                aspect=result.get("scene_aspect", "9:16"), lang="zh",
                                template=_tpl, skin_path=_sk,
                                brand=result.get("brand", ""), follow_text=result.get("follow_text", ""),
                                qr_path=result.get("qr_path"),
                            )
                        with open(os.path.join(_od, "content.json"), "w", encoding="utf-8") as f:
                            json.dump(_c, f, ensure_ascii=False, indent=2)
                    st.session_state.ks_result = result
                    st.rerun()
                except Exception as e:
                    st.error(f"重渲失败：{e}")

    tab_img, tab_content, tab_template, tab_review = st.tabs(
        ["成品图", "知识数据", "模板结构", "审核日志"]
    )
    with tab_img:
        if result.get("image") and os.path.exists(result["image"]):
            # 长图预览限制宽度，避免撑满页面；原图通过下载获取。
            st.image(result["image"], width=380)
            with open(result["image"], "rb") as f:
                st.download_button("下载成品图 PNG", f, file_name="knowledge_card.png", mime="image/png")
        else:
            st.caption("尚未生成成品图（流程未走到渲染步骤）。")
        if result.get("out_dir"):
            st.caption(f"输出目录：{result['out_dir']}")
    with tab_content:
        st.json(result.get("content") or {"提示": "尚未生成知识数据"})
    with tab_template:
        st.json(result.get("template") or {"提示": "尚未反推模板结构"})
    with tab_review:
        st.json(result.get("review_log") or [])

    # ------------------------------------------------------------------ 小红书种草文案
    _promo = result.get("promo") or {}
    if _promo.get("title") or _promo.get("body"):
        st.divider()
        st.subheader("📣 小红书种草文案（含关键词）")
        _md = image_studio.promo_to_markdown(_promo)
        st.markdown(f"**{_promo.get('title','')}**")
        st.text(_promo.get("body", ""))
        if _promo.get("tags"):
            st.caption("关键词： " + " ".join(_promo["tags"]))
        st.download_button("📄 下载种草文案 (txt)", _md, file_name="promo.txt", mime="text/plain")
        with st.expander("📋 一键复制（全文）"):
            st.code(_md, language="markdown")

    # ------------------------------------------------------------------ 分镜图（二级图片）
    _scenes = [s for s in (result.get("scene_images") or []) if s and os.path.exists(s)]
    if _scenes:
        st.divider()
        st.subheader(f"🖼️ 分镜图（{len(_scenes)} 张 · 可做小红书图集 / 视频素材）")
        import io
        import zipfile
        _buf = io.BytesIO()
        with zipfile.ZipFile(_buf, "w") as _zf:
            for _i, _sp in enumerate(_scenes):
                _zf.write(_sp, arcname=f"scene_{_i:02d}.png")
        st.download_button("📦 打包下载全部分镜图 (zip)", _buf.getvalue(),
                           file_name="scene_images.zip", mime="application/zip")
        _gc = st.columns(4)
        for _i, _sp in enumerate(_scenes):
            with _gc[_i % 4]:
                st.image(_sp, use_container_width=True)
                with open(_sp, "rb") as _f:
                    st.download_button(f"下载 #{_i + 1}", _f, file_name=f"scene_{_i:02d}.png",
                                       mime="image/png", key=f"scene_dl_{_i}")

    # ------------------------------------------------------------------ 视频
    _content = result.get("content") or {}
    _items = _content.get("cards") or _content.get("steps") or []
    if _items and HAS_VIDEO:
        st.divider()
        st.subheader("🎬 生成短视频（抖音 / YouTube）")
        st.caption("把上面的知识卡片逐张做成动画，配 AI 旁白 + 字幕 + 背景音乐，输出可直接发布的短视频。")

        vrun = st.session_state.get("ks_vid_running", False)
        vc1, vc2, vc3 = st.columns(3)
        with vc1:
            aspects = st.multiselect("比例", ["9:16", "16:9"], default=["9:16"], disabled=vrun)
        with vc2:
            lang_labels = st.multiselect("配音语言", ["中文", "English"], default=["中文"], disabled=vrun)
        with vc3:
            with_bgm = st.checkbox("加背景音乐", value=True, disabled=vrun)
            captions = st.checkbox("逐字高亮字幕", value=True, disabled=vrun)

        lang_map = {"中文": "zh", "English": "en"}
        langs = [lang_map[x] for x in lang_labels]

        # 音色选择（按所选语言显示对应音色）
        voices = {}
        vv1, vv2 = st.columns(2)
        if "zh" in langs:
            with vv1:
                zlbl = st.selectbox("中文音色", list(card_video.ZH_VOICES.keys()), index=0, disabled=vrun)
                voices["zh"] = card_video.ZH_VOICES[zlbl]
        if "en" in langs:
            with vv2:
                elbl = st.selectbox("英文音色", list(card_video.EN_VOICES.keys()), index=0, disabled=vrun)
                voices["en"] = card_video.EN_VOICES[elbl]

        trans_label = st.selectbox("✨ 转场效果", list(card_video.TRANSITIONS.keys()), index=0, disabled=vrun)
        transition = card_video.TRANSITIONS[trans_label]

        vbtn = st.button("🎬 生成视频", type="primary", disabled=vrun, use_container_width=True)
        if vrun:
            st.warning("⏳ 视频生成中（较耗时，请耐心等待）…")

        if vbtn and not vrun:
            if not aspects or not langs:
                st.error("请至少选择一个比例和一种语言。")
            else:
                st.session_state.ks_vid_running = True
                st.session_state.ks_vid_params = {
                    "aspects": aspects, "langs": langs, "with_bgm": with_bgm,
                    "captions": captions, "voices": voices, "transition": transition,
                    "out_dir": os.path.join(result["out_dir"], "videos"),
                    "style": result.get("style"), "font": result.get("font"),
                    "template": result.get("template"), "skin_path": result.get("skin_path"),
                    "brand": result.get("brand", ""), "follow_text": result.get("follow_text", ""),
                    "qr_path": result.get("qr_path"),
                    # 复用已生成的分镜图 HTML（同语言+同比例时视频直接拿来录制，不重复生成）
                    "scene_html_dir": os.path.join(result["out_dir"], "scenes"),
                    "scene_html_aspect": result.get("scene_aspect", "9:16"),
                }
                st.session_state.ks_videos = None
                st.rerun()

        if st.session_state.get("ks_vid_running") and st.session_state.get("ks_vid_params"):
            vp = st.session_state.ks_vid_params
            vstatus = st.status("视频生成中…", expanded=True)
            try:
                videos = card_video.generate_videos(
                    _content, result.get("illustrations") or {}, vp["out_dir"],
                    style=vp["style"], font=vp["font"],
                    aspects=vp["aspects"], langs=vp["langs"], with_bgm=vp["with_bgm"],
                    captions=vp.get("captions", True), voices=vp.get("voices"),
                    transition=vp.get("transition", "smoothleft"),
                    cb=lambda s, m: vstatus.write(f"**[{s}]** {m}"),
                    template=vp.get("template"), skin_path=vp.get("skin_path"),
                    brand=vp.get("brand", ""), follow_text=vp.get("follow_text", ""),
                    qr_path=vp.get("qr_path"),
                    scene_html_dir=vp.get("scene_html_dir"),
                    scene_html_aspect=vp.get("scene_html_aspect"),
                )
                st.session_state.ks_videos = videos
                vstatus.update(label="视频完成 ✅", state="complete", expanded=False)
            except Exception as e:
                st.session_state.ks_videos = {"error": str(e)}
                vstatus.update(label="视频失败 ❌", state="error")
            finally:
                st.session_state.ks_vid_running = False
                st.session_state.ks_vid_params = None
            st.rerun()

        videos = st.session_state.get("ks_videos")
        if videos:
            if videos.get("error"):
                st.error(f"视频生成失败：{videos['error']}")
            else:
                st.success(f"已生成 {len(videos)} 个视频")
                for key, path in videos.items():
                    if not os.path.exists(path):
                        continue
                    st.markdown(f"**🎬 {key}**")
                    if key.startswith("9:16"):
                        # 竖屏放窄列，避免占满整宽被拉得超大
                        pc = st.columns([1, 1, 2])
                        with pc[0]:
                            st.video(path)
                    else:
                        # 横屏约束到约一半宽度
                        pc = st.columns([3, 1])
                        with pc[0]:
                            st.video(path)
                    with open(path, "rb") as f:
                        st.download_button(f"下载 {key}.mp4", f, file_name=f"card_video_{key}.mp4",
                                           mime="video/mp4", key=f"dl_{key}")
