"""recognize：识别流水线纯逻辑层（方案 §4.1，任务 1.1/1.4）。

不依赖 Qt，可单测。职责：
- **懒加载**：mac-ocr-text / mac-barcode-read（及其 Vision/PIL 依赖）首次识别
  触发时才 import；启动加载本插件不增加常驻内存（§3.4 内存目标）
- **并行**：OCR 与条码是独立纯函数，由 UI 层用 QThreadPool 并行调度（§4.1）
- **坐标**：regions 为 top-left 归一化 (x, y, w, h)，与两个库的对外契约对齐；
  scene 像素矩形 → 归一化的换算也在这里（Retina 天然正确，§4.1）

regions=None / [] 均表示整图识别。
"""

from __future__ import annotations

import logging
from typing import Any, Sequence

log = logging.getLogger(__name__)

# 懒加载缓存：首次调用时填充，避免重复 import 开销
_ocr_fn = None
_barcode_fn = None


# ---------- 懒加载 ----------

def load_ocr_fn():
    """懒加载 OCR 后端函数（mac-ocr-text）。"""
    global _ocr_fn
    if _ocr_fn is None:
        from mac_ocr_text import recognize_image_text  # noqa: PLC0415
        _ocr_fn = recognize_image_text
    return _ocr_fn


def load_barcode_fn():
    """懒加载条码后端函数（mac-barcode-read）。"""
    global _barcode_fn
    if _barcode_fn is None:
        from mac_barcode_read import read_barcodes_from_image  # noqa: PLC0415
        _barcode_fn = read_barcodes_from_image
    return _barcode_fn


# ---------- 识别入口（worker 线程调用） ----------

# ocrmac 合法语言是完整 BCP-47 标签（en-US/zh-Hans/...），短码（en/zh）会直接抛 ValueError。
# 这里把常见短码映射到合法标签兜底，防止用户配置短码导致识别整链路报错。
_LANG_ALIASES = {
    "en": "en-US", "zh": "zh-Hans", "zh-cn": "zh-Hans", "zh_cn": "zh-Hans",
    "zh-hans": "zh-Hans", "zh-tw": "zh-Hant", "zh-hk": "zh-Hant", "zh-hant": "zh-Hant",
    "ja": "ja-JP", "jp": "ja-JP", "ko": "ko-KR", "fr": "fr-FR", "de": "de-DE",
    "es": "es-ES", "it": "it-IT", "pt": "pt-BR", "ru": "ru-RU", "ja-jp": "ja-JP",
    "ko-kr": "ko-KR", "fr-fr": "fr-FR", "de-de": "de-DE", "es-es": "es-ES",
    "it-it": "it-IT", "pt-br": "pt-BR", "ru-ru": "ru-RU",
}


def _normalize_languages(languages: list[str] | None) -> list[str] | None:
    """语言配置规范化：仅将短码映射为合法 BCP-47 标签。

    注意：ocrmac 大小写敏感——`en-US` 合法而 `en-us` 非法。因此**不能**把
    输入统一小写后再返回；完整标签（如 en-US/zh-Hans）必须保留原始大小写，
    只有命中驼峰/短码别名表的项才按规范形式替换。
    """
    if not languages:
        return languages
    out = []
    for lang in languages:
        raw = str(lang).strip()
        key = raw.lower()
        if key in _LANG_ALIASES:
            out.append(_LANG_ALIASES[key])
        else:
            out.append(raw)  # 非别名项保留原样（含合法完整标签的原大小写）
    return out


def run_ocr(
    image,
    regions: Sequence[tuple[float, float, float, float]] | None,
    *,
    ocr_cfg: dict | None,
) -> dict[str, Any]:
    """单图 OCR。ocr_cfg 取插件配置段 plugins.recognize.ocr。

    语言偏好非法时自动降级为系统默认（不阻塞识别；可能性来源：常驻 app 的
    旧默认配置，或用户手编非法标签）。
    """
    cfg = ocr_cfg or {}
    fn = load_ocr_fn()
    try:
        return fn(
            image,
            regions=regions or None,
            recognition_level=cfg.get("recognition_level", "accurate"),
            languages=_normalize_languages(cfg.get("languages")),
        )
    except ValueError as e:
        if "language" not in str(e).lower():
            raise
        log.warning("语言偏好非法（%s），降级为系统默认语言重试", e)
        return fn(
            image,
            regions=regions or None,
            recognition_level=cfg.get("recognition_level", "accurate"),
            languages=None,
        )


def run_barcode(
    image,
    regions: Sequence[tuple[float, float, float, float]] | None,
    *,
    barcode_cfg: dict | None,
) -> dict[str, Any]:
    """单图标码读取。barcode_cfg 取插件配置段 plugins.recognize.barcode。

    不填 barcode_types = 全类型（mac-barcode-read 默认行为）。
    """
    cfg = barcode_cfg or {}
    return load_barcode_fn()(
        image,
        regions=regions or None,
        barcode_types=cfg.get("barcode_types"),
    )


# ---------- 坐标换算（框选 → 归一化） ----------

def normalized_regions(
    scene_rects: Sequence[tuple[float, float, float, float]],
    img_w: int,
    img_h: int,
) -> list[tuple[float, float, float, float]]:
    """scene 像素矩形（图片原始像素坐标，(x,y,w,h)）→ top-left 归一化。

    除法缩放恒等：原点 (0,0)=图片左上角，w/h 用原始像素宽高相除，Retina 下
    scene 与图片像素 1:1，天然正确（§4.1 框选作用于图片而非屏幕）。
    """
    if img_w <= 0 or img_h <= 0:
        return []
    out = []
    for x, y, w, h in scene_rects:
        # 钳制到图片范围内，避免浮点误差越界
        x = max(0.0, min(float(x), img_w))
        y = max(0.0, min(float(y), img_h))
        w = max(0.0, min(float(w), img_w - x))
        h = max(0.0, min(float(h), img_h - y))
        if w <= 0 or h <= 0:
            continue
        out.append((x / img_w, y / img_h, w / img_w, h / img_h))
    return out


def describe_text(ocr_result: dict[str, Any]) -> str:
    """OCR 结果 → 展示文本：按 region 顺序合并 plain_text，段间空行分隔。"""
    if not ocr_result:
        return ""
    parts = []
    for region in ocr_result.get("regions", []):
        text = (region.get("plain_text") or "").strip()
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def group_text_blocks(ocr_result: dict[str, Any]) -> list[tuple[str, int]]:
    """OCR 结果 → 逐段文本列表 [(文本, region 序号), ...]（供逐段复制）。"""
    blocks = []
    for region in ocr_result.get("regions", []):
        text = (region.get("plain_text") or "").strip()
        if text:
            blocks.append((text, region.get("region_index", 0)))
    return blocks


def describe_barcodes(barcode_result: dict[str, Any]) -> list[dict[str, Any]]:
    """条码结果 → 展示列表 [{barcode_type, value, confidence}, ...]（供逐条复制）。"""
    if not barcode_result:
        return []
    out = []
    for code in barcode_result.get("codes", []):
        out.append(
            {
                "barcode_type": code.get("barcode_type", "unknown"),
                "value": code.get("value", ""),
                "confidence": code.get("confidence"),
            }
        )
    return out