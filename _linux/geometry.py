"""截图几何的纯计算：选区坐标映射、PNG 尺寸。不导入 GTK。"""
from __future__ import annotations

import struct


def selection_crop_box(
    mon_img_x: int,
    mon_img_y: int,
    sel_scale_x: float,
    sel_scale_y: float,
    sel_x: float,
    sel_y: float,
    sel_w: float,
    sel_h: float,
) -> tuple[int, int, int, int]:
    """把选区逻辑坐标映射成 PIL crop 需要的 (left, top, right, bottom)。"""
    left = mon_img_x + int(round(sel_x * sel_scale_x))
    top = mon_img_y + int(round(sel_y * sel_scale_y))
    right = left + max(1, int(round(sel_w * sel_scale_x)))
    bottom = top + max(1, int(round(sel_h * sel_scale_y)))
    return (left, top, right, bottom)


def png_dimensions(path: str) -> tuple[int, int]:
    """从 PNG 文件头读取图片宽高（无需 PIL）。"""
    with open(path, "rb") as f:
        f.read(16)  # 跳过 PNG 签名 + IHDR chunk header
        w, h = struct.unpack(">II", f.read(8))
    return w, h
