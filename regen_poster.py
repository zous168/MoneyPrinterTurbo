"""海报成品图再生成（LLM 自包含卡片 HTML 验证用）。
用法: python regen_poster.py <src_dir> [out_subdir]
"""
import json
import os
import re
import sys

from app.services import image_studio

src = sys.argv[1] if len(sys.argv) > 1 else r"storage\_verify_keep2"
out_sub = sys.argv[2] if len(sys.argv) > 2 else "poster_llm"

content = json.load(open(os.path.join(src, "content.json"), encoding="utf-8"))
content["_llm_card_html"] = True  # 开启 LLM 自包含卡片 HTML

cards_dir = os.path.join(src, "cards")
illus = {}
for fn in os.listdir(cards_dir):
    m = re.search(r"item_(\d+)", fn)
    if m:
        illus[int(m.group(1)) - 1] = os.path.join(cards_dir, fn)

out_dir = os.path.join(src, out_sub)
res = image_studio.render(content, illus, out_dir, cb=lambda *a: print("  ", *a))
print("OK ->", res["image"])
