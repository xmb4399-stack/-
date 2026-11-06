# -*- coding: utf-8 -*-
"""
make_assets.py —— 生成应用图标和启动图

不想为了两张图去装 Pillow，所以这里用纯 Python 手写 PNG（zlib + struct）。
图形也很简单，用解析几何直接算每个像素在不在形状里，再按 2x2 降采样获得抗锯齿。

生成：
    assets/icon.png        512x512  蓝底圆角方块 + 白色播放三角
    assets/presplash.png   512x512  浅蓝底 + 同款圆形图标

运行：python tools/make_assets.py
"""

import os
import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / 'assets'

SS = 2                 # 2x2 超采样
BLUE_TOP = (0.357, 0.608, 0.835)      # #5b9bd5
BLUE_BOTTOM = (0.165, 0.420, 0.620)   # #2a6b9e
LIGHT_TOP = (0.929, 0.953, 1.000)     # #edf3ff
LIGHT_BOTTOM = (0.831, 0.878, 0.941)  # #d4e0f0


def write_png(path, width, height, pixels):
    """pixels 是 RGBA 字节串，逐行从上到下。"""
    stride = width * 4
    raw = bytearray()
    for y in range(height):
        raw.append(0)                       # 每行的 filter 类型：0 = None
        raw += pixels[y * stride:(y + 1) * stride]

    def chunk(tag, data):
        body = tag + data
        return (struct.pack('>I', len(data)) + body +
                struct.pack('>I', zlib.crc32(body) & 0xffffffff))

    png = b'\x89PNG\r\n\x1a\n'
    png += chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0))
    png += chunk(b'IDAT', zlib.compress(bytes(raw), 9))
    png += chunk(b'IEND', b'')
    with open(str(path), 'wb') as fp:
        fp.write(png)


def in_round_rect(x, y, x0, y0, x1, y1, r):
    """点 (x, y) 在不在圆角矩形里。"""
    if x < x0 or x > x1 or y < y0 or y > y1:
        return False
    # 把点夹到「内缩 r 的矩形」上，再比较到它的距离
    cx = min(max(x, x0 + r), x1 - r)
    cy = min(max(y, y0 + r), y1 - r)
    dx = x - cx
    dy = y - cy
    return dx * dx + dy * dy <= r * r


def in_triangle(x, y, a, b, c):
    """点 (x, y) 在不在三角形 abc 里（用叉积同号判断）。"""

    def cross(p, q, pt):
        return (q[0] - p[0]) * (pt[1] - p[1]) - (q[1] - p[1]) * (pt[0] - p[0])

    d1 = cross(a, b, (x, y))
    d2 = cross(b, c, (x, y))
    d3 = cross(c, a, (x, y))
    has_neg = (d1 < 0) or (d2 < 0) or (d3 < 0)
    has_pos = (d1 > 0) or (d2 > 0) or (d3 > 0)
    return not (has_neg and has_pos)


def render(size, bg_round_rect, bg_colors, mark_top, mark_bottom, circle_mark):
    """
    渲染一张图，返回降采样之后的 RGBA 字节串。

    bg_round_rect: (x0, y0, x1, y1, r) 背景圆角矩形，单位是 0~1 的相对值
    bg_colors:     (top_rgb, bottom_rgb)
    mark_*:        白色标记的纵向范围（相对值），只用来算渐变
    circle_mark:   True 表示白色标记画成圆形+三角，False 表示只画三角
    """
    w = h = size * SS
    buf = bytearray(w * h * 4)

    x0, y0, x1, y1, rr = [v * w for v in bg_round_rect]
    top_rgb, bottom_rgb = bg_colors

    # 白色播放三角的三个顶点
    cx, cy = w / 2.0, h / 2.0
    span = w * 0.30
    tri = ((cx - span * 0.40, cy - span * 0.66),
           (cx - span * 0.40, cy + span * 0.66),
           (cx + span * 0.68, cy))

    circle_r = w * 0.30

    for py in range(h):
        t = py / float(h - 1)
        base = (top_rgb[0] + (bottom_rgb[0] - top_rgb[0]) * t,
                top_rgb[1] + (bottom_rgb[1] - top_rgb[1]) * t,
                top_rgb[2] + (bottom_rgb[2] - top_rgb[2]) * t)
        row = py * w * 4
        for px in range(w):
            if not in_round_rect(px + 0.5, py + 0.5, x0, y0, x1, y1, rr):
                continue
            idx = row + px * 4
            if circle_mark:
                dx = px + 0.5 - cx
                dy = py + 0.5 - cy
                if dx * dx + dy * dy <= circle_r * circle_r:
                    buf[idx:idx + 3] = b'\xff\xff\xff'
                    buf[idx + 3] = 255
                    continue
            if (not circle_mark) and in_triangle(px + 0.5, py + 0.5, *tri):
                buf[idx:idx + 3] = b'\xff\xff\xff'
                buf[idx + 3] = 255
                continue
            buf[idx] = int(round(base[0] * 255))
            buf[idx + 1] = int(round(base[1] * 255))
            buf[idx + 2] = int(round(base[2] * 255))
            buf[idx + 3] = 255

    # 三角形要在圆形之内才好看，所以圆形那一版把三角形叠上去
    if circle_mark:
        for py in range(h):
            for px in range(w):
                idx = (py * w + px) * 4
                if buf[idx + 3] == 0:
                    continue
                if in_triangle(px + 0.5, py + 0.5, *tri):
                    dx = px + 0.5 - cx
                    dy = py + 0.5 - cy
                    if dx * dx + dy * dy <= circle_r * circle_r:
                        buf[idx] = int(BLUE_TOP[0] * 255)
                        buf[idx + 1] = int(BLUE_TOP[1] * 255)
                        buf[idx + 2] = int(BLUE_TOP[2] * 255)

    # 降采样
    out = bytearray(size * size * 4)
    for oy in range(size):
        for ox in range(size):
            r = g = b = a = 0
            for dy in range(SS):
                srow = ((oy * SS + dy) * w + ox * SS) * 4
                for dx in range(SS):
                    s = srow + dx * 4
                    r += buf[s]
                    g += buf[s + 1]
                    b += buf[s + 2]
                    a += buf[s + 3]
            n = SS * SS
            d = (oy * size + ox) * 4
            out[d] = r // n
            out[d + 1] = g // n
            out[d + 2] = b // n
            out[d + 3] = a // n
    return out


def main():
    ASSETS.mkdir(parents=True, exist_ok=True)

    print('生成 icon.png ...')
    icon = render(
        size=512,
        bg_round_rect=(0.034, 0.034, 0.966, 0.966, 0.20),
        bg_colors=(BLUE_TOP, BLUE_BOTTOM),
        mark_top=0.0, mark_bottom=1.0,
        circle_mark=False,
    )
    write_png(ASSETS / 'icon.png', 512, 512, icon)

    print('生成 presplash.png ...')
    splash = render(
        size=512,
        bg_round_rect=(0.0, 0.0, 1.0, 1.0, 0.0),
        bg_colors=(LIGHT_TOP, LIGHT_BOTTOM),
        mark_top=0.0, mark_bottom=1.0,
        circle_mark=True,
    )
    write_png(ASSETS / 'presplash.png', 512, 512, splash)

    for name in ('icon.png', 'presplash.png'):
        p = ASSETS / name
        print('  %s  %d 字节' % (p, os.path.getsize(str(p))))


if __name__ == '__main__':
    main()
