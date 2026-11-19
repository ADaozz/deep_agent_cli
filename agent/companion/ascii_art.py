"""Terminal image conversion from the supplied i2a reference implementation."""
import math

import numpy as np
from PIL import Image

# 与站点脚本一致：ramp 从“最空”到“最密”
# 站点原文（70 级）：Array.from("$@B%8&WM#*oahkbdpqwmZO0QLCJUYXzcvunxrjft/\\|()1{}[]?-_+~<>i!lI;:,\"^`'. ").reverse()
_DETAILED_SRC = "$@B%8&WM#*oahkbdpqwmZO0QLCJUYXzcvunxrjft/\\|()1{}[]?-_+~<>i!lI;:,\"^`'. "
DETAILED = "".join(reversed(_DETAILED_SRC))  # 70 级
PRESETS = {
    "standard": " .:-=+*#%@",
    "detailed": DETAILED,
    "blocks": " \u2591\u2592\u2593\u2588",
    "minimal": " .:oO@",
    "binary": " 01",
}
WORK_MAX = 1024  # 站点先把原图归一化到长边 <= 1024 的工作画布


def js_round(x: float) -> int:
    """JS Math.round：四舍五入 .5 时向 +∞ 取整（Python 的 round 是银行家舍入）。"""
    return int(math.floor(x + 0.5))


def effective_ramp(charset: str, custom: str, density: int) -> list[str]:
    """复刻 effectiveRamp(): 先选字符集，再按 density 抽稀成 n 级。"""
    custom_chars = [c for c in custom.replace("\r", " ").replace("\n", " ").replace("\t", " ")]
    if charset == "custom" and len(set(custom_chars)) >= 2:
        base = custom_chars
    else:
        base = list(PRESETS.get(charset) or PRESETS["standard"])
    n = js_round(len(base) * density / 100)
    n = max(2, min(n, len(base)))
    if n == len(base):
        return base
    last = len(base) - 1
    return [base[js_round(i * last / (n - 1))] for i in range(n)]


def grid_size(width: int, height: int, aspect: bool, work_w: int, work_h: int,
              char_ratio: float, spacing: float) -> tuple[int, int]:
    """复刻 dims(): 开 aspect 时行数由图片比例、字符宽高比和行距推出。"""
    w = int(width)
    h = int(height)
    if aspect:
        h = js_round(w * (work_h / work_w) * (char_ratio / spacing))
        h = max(1, min(h, 300))
    return w, h


def to_grid(img: Image.Image, w: int, h: int, resample=None) -> np.ndarray:
    """原图 -> 采样画布：黑底合成（对应 canvas fillRect('#000') + drawImage）。"""
    resample = Image.BILINEAR if resample is None else resample
    rgba = img.convert("RGBA")
    bg = Image.new("RGBA", rgba.size, (0, 0, 0, 255))
    flat = Image.alpha_composite(bg, rgba).convert("RGB")
    small = flat.resize((w, h), resample=resample)
    return np.asarray(small, dtype=np.float64)


def tone_map(rgb: np.ndarray, brightness: int, contrast: int, invert: bool):
    """复刻 render() 里的逐像素处理（顺序：对比度 -> 亮度 -> 反相 -> 亮度值）。"""
    off = brightness * 2.55
    c = contrast * 2.55
    f = (259.0 * (c + 255.0)) / (255.0 * (259.0 - c))
    v = f * (rgb - 128.0) + 128.0 + off
    v = np.clip(v, 0, 255)
    if invert:
        v = 255.0 - v
    return v


def convert(img: Image.Image, *, charset="standard", custom="", density=100,
            width=100, height=50, aspect=True, brightness=0, contrast=0,
            invert=False, gray=True, spacing=1.1, char_ratio=0.6,
            resample=None):
    """返回 dict: rows(列表[列表[字符]]), text(每行右去空白), colors(4bit RGB 数组或 None), w, h"""
    # 1) 工作画布：长边缩到 <=1024（站点 loadFile 的做法）
    nw, nh = img.size
    s = min(1.0, WORK_MAX / max(nw, nh))
    ww = max(1, js_round(nw * s))
    wh = max(1, js_round(nh * s))
    if (ww, wh) == (nw, nh):
        work = img
    else:  # 透明像素先合成到黑底，再按浏览器的做法缩到工作尺寸
        rgba = img.convert("RGBA")
        flat = Image.alpha_composite(Image.new("RGBA", rgba.size, (0, 0, 0, 255)), rgba)
        work = flat.convert("RGB").resize((ww, wh), Image.BILINEAR)

    # 2) 字符网格尺寸
    w, h = grid_size(width, height, aspect, ww, wh, char_ratio, spacing)

    # 3) 采样 + 色调
    px = to_grid(work, w, h, resample)
    v = tone_map(px, brightness, contrast, invert)
    lum = 0.2126 * v[..., 0] + 0.7152 * v[..., 1] + 0.0722 * v[..., 2]

    ramp = effective_ramp(charset, custom, density)
    last = len(ramp) - 1
    idx = np.floor(lum / 255.0 * last + 0.5).astype(int)
    idx = np.clip(idx, 0, last)
    table = np.array(ramp)
    rows = table[idx]

    text = "\n".join("".join(r).rstrip() for r in rows.tolist())

    colors = None
    if not gray:  # 彩色模式：每个字符取 4bit/通道的颜色
        q = (np.clip(v, 0, 255).astype(np.uint8) >> 4).astype(np.uint32)
        colors = (q[..., 0] << 8) | (q[..., 1] << 4) | q[..., 2]
    return {"rows": rows.tolist(), "text": text, "colors": colors, "w": w, "h": h}


def frame_iter(img: Image.Image):
    """逐帧产出 (index, RGBA 帧, 时长 ms)。PIL 在 seek 时会处理 GIF 的 disposal。"""
    n = getattr(img, "n_frames", 1)
    for i in range(n):
        img.seek(i)
        yield i, img.convert("RGBA"), (img.info.get("duration") or 100)
