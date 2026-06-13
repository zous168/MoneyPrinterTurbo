"""分镜图再生成（布局迭代用）。从已有 content.json + cards/ 重渲 scenes，不跑 LLM/出图。
用法: python regen_scenes.py <src_dir> [out_subdir]
"""
import json
import os
import re
import sys

from app.services import card_video, image_studio

src = sys.argv[1] if len(sys.argv) > 1 else r"storage\_verify_keep2"
out_sub = sys.argv[2] if len(sys.argv) > 2 else "scenes_regen"

content = json.load(open(os.path.join(src, "content.json"), encoding="utf-8"))
cards_dir = os.path.join(src, "cards")
illus = {}
for fn in os.listdir(cards_dir):
    m = re.search(r"item_(\d+)", fn)
    if m:
        illus[int(m.group(1)) - 1] = os.path.join(cards_dir, fn)

# content.json 里的 elements 是「已 reflow 的最终座标」，重跑 _norm_elements 让布局修复生效。
aspect = content.get("_aspect", "9:16")
gcols, grows = image_studio._grid_dims(aspect)
for c in content.get("cards", []):
    if c.get("elements"):
        c["elements"] = image_studio._norm_elements(c["elements"], gcols, grows)

out_dir = os.path.join(src, out_sub)
paths = card_video.render_scene_images(
    content, illus, out_dir,
    style=content.get("_style"), aspect=content.get("_aspect", "9:16"), lang="zh",
    brand="睡眠研究所",
)
print(f"OK -> {out_dir}")
for p in paths:
    print("  ", os.path.basename(p))
