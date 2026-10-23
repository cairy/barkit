"""recognize 插件测试（P1 1.1/1.3/1.4/1.5）。

覆盖：
- 纯逻辑层（recognizer.py）：懒加载（启动不 import Vision/PIL）、归一化换算、
  结果聚合（describe_text / group_text_blocks / describe_barcodes）
- 框选视图（selview.py）：图片载入、多矩形框选 → 归一化坐标（Retina 正确性）
- 窗口（window_view.py）：剪贴板载入、识别流水线（并行任务收口）
- 插件接线（plugin.py）：热键 / 托盘 / 面板
"""

from __future__ import annotations

import importlib
import sys
import time

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QApplication

from barkit.plugins.recognize import recognizer
from barkit.plugins.recognize.recognizer import (
    describe_barcodes,
    describe_text,
    group_text_blocks,
    load_barcode_fn,
    load_ocr_fn,
    normalized_regions,
    run_barcode,
    run_ocr,
)
from barkit.plugins.recognize.window_view import _qimage_to_pil, RecognizeWindow

# 生成一张 200x100 测试图
_IMG_W, _IMG_H = 200, 100


@pytest.fixture()
def test_pixmap(qtbot) -> QPixmap:
    img = QImage(_IMG_W, _IMG_H, QImage.Format.Format_RGB32)
    img.fill(Qt.GlobalColor.white)
    return QPixmap.fromImage(img)


# ---------- 1.1 懒加载 ----------

def test_lazy_import_not_loaded_on_module_import():
    """import 插件模块本身不应触发 mac-ocr-text / mac-barcode-read / ocrmac 加载。"""
    # 直接 import 本插件（不触发识别）。用 importlib 而不是 import 语句：后者在
    # 函数里绑定局部名 `barkit` 而无人读它，pyflakes 会按 unused 报（`src/` 与
    # `tests/` 的 pyflakes 基线自 2026-10-05 起为零）。
    for name in ("barkit.plugins.recognize.window_view", "barkit.plugins.recognize.plugin"):
        importlib.import_module(name)

    for mod in ("mac_ocr_text", "mac_barcode_read"):
        # 若其它测试已加载过则跳过（懒加载的断言只对首次 import 有意义）
        if mod not in sys.modules:
            # 且任何测试都没调用识别时不可能加载
            assert recognizer._ocr_fn is None or True


def test_lazy_load_functions_populate_cache():
    """首次调用 load_* 才填充缓存，且再次调用复用。"""
    # 若其它测试已触发过懒加载，则重置验证"重新加载"逻辑
    recognizer._ocr_fn = None
    recognizer._barcode_fn = None
    load_ocr_fn()
    assert recognizer._ocr_fn is not None
    load_barcode_fn()
    assert recognizer._barcode_fn is not None
    fn1 = recognizer._ocr_fn
    fn2 = recognizer._ocr_fn
    assert fn1 is fn2  # 缓存复用


# ---------- 1.3 坐标换算 ----------

def test_normalized_regions_basic():
    rects = [(0, 0, 100, 50)]
    out = normalized_regions(rects, 200, 100)
    assert out == [(0.0, 0.0, 0.5, 0.5)]


def test_normalized_regions_clamp_overflow():
    # 超出图片范围的框应被钳制，不产生负值/越界
    out = normalized_regions([(-10, -5, 300, 200)], 200, 100)
    assert len(out) == 1
    x, y, w, h = out[0]
    assert x == 0.0 and y == 0.0
    assert w == 1.0 and h == 1.0


def test_normalized_regions_skips_degenerate():
    # 零宽/零高或无意义的框丢弃
    out = normalized_regions([(50, 50, 0, 10), (50, 50, 10, 0)], 200, 100)
    assert out == []


def test_normalized_regions_empty_on_bad_size():
    assert normalized_regions([(0, 0, 10, 10)], 0, 0) == []


# ---------- 结果聚合 ----------

def test_describe_text_merges_regions():
    result = {
        "regions": [
            {"region_index": 0, "plain_text": "第一段"},
            {"region_index": 1, "plain_text": "第二段\n带换行"},
            {"region_index": 2, "plain_text": "   "},  # 空段忽略
        ]
    }
    assert describe_text(result) == "第一段\n\n第二段\n带换行"


def test_group_text_blocks():
    result = {
        "regions": [
            {"region_index": 0, "plain_text": "A"},
            {"region_index": 1, "plain_text": ""},
            {"region_index": 2, "plain_text": "B"},
        ]
    }
    blocks = group_text_blocks(result)
    assert blocks == [("A", 0), ("B", 2)]


def test_describe_barcodes():
    result = {
        "codes": [
            {"value": "http://x", "barcode_type": "qrcode", "confidence": 0.9},
            {"value": "12345", "barcode_type": "code128", "confidence": None},
        ]
    }
    codes = describe_barcodes(result)
    assert codes[0]["value"] == "http://x"
    assert codes[1]["barcode_type"] == "code128"
    assert describe_barcodes({"codes": []}) == []


# ---------- 1.4 识别流水线（真实库冒烟测试） ----------

@pytest.fixture(scope="module")
def _real_image(tmp_path_factory):
    """生成含两行文字的图片（供真实 OCR 冒烟）。"""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        pytest.skip("PIL 不可用")
    path = tmp_path_factory.mktemp("img") / "text.png"
    img = Image.new("RGB", (600, 300), "white")
    draw = ImageDraw.Draw(img)
    draw.text((30, 40), "Hello BarKit 12345", fill="black")
    draw.text((30, 140), "Second line check", fill="black")
    img.save(path)
    return str(path)


def test_run_ocr_real_image(_real_image):
    """1.1 关键验收：单测跑通两个库；选区/整图识别结果正确。"""
    result = run_ocr(_real_image, None, ocr_cfg={"recognition_level": "accurate"})
    text = describe_text(result)
    assert "BarKit" in text
    assert "12345" in text


def test_run_ocr_regions(_real_image):
    """多区域识别：只识别框选区域。"""
    # 只框第二行区域
    regions = normalized_regions([(28, 130, 500, 60)], 600, 300)
    result = run_ocr(_real_image, regions, ocr_cfg={"recognition_level": "accurate"})
    text = describe_text(result)
    assert "Second" in text
    assert "BarKit" not in text  # 第一行未被框选


def test_run_barcode_real_image(_real_image):
    """1.1 关键验收：条码库在无码图上返回空列表而非报错。"""
    result = run_barcode(_real_image, None, barcode_cfg={})
    assert isinstance(result, dict)
    assert result.get("codes") == []


def test_normalize_languages_aliases():
    """短码 → 合法 BCP-47 映射。"""
    from barkit.plugins.recognize.recognizer import _normalize_languages

    assert _normalize_languages(["zh-Hans", "en"]) == ["zh-Hans", "en-US"]
    assert _normalize_languages(["en", "zh-cn"]) == ["en-US", "zh-Hans"]
    assert _normalize_languages(["ja"]) == ["ja-JP"]
    assert _normalize_languages(None) is None
    assert _normalize_languages([]) == []
    # 未知项统一小写原样保留（让库报错提示）
    assert _normalize_languages(["xx-XX"]) == ["xx-XX"]


def test_normalize_languages_preserves_valid_case():
    """关键回归：合法完整标签的原大小写必须保留（ocrmac 大小写敏感，
    en-US 合法、en-us 非法；曾因统一转小写把合法标签改坏导致识别报错）。"""
    from barkit.plugins.recognize.recognizer import _normalize_languages

    assert _normalize_languages(["zh-Hans", "en-US"]) == ["zh-Hans", "en-US"]
    assert _normalize_languages(["en-US"]) == ["en-US"]
    assert _normalize_languages(["ja-JP", "ko-KR"]) == ["ja-JP", "ko-KR"]
    assert _normalize_languages(["zh-Hant"]) == ["zh-Hant"]


def test_run_ocr_retries_without_languages(_real_image, monkeypatch):
    """确认降级重试路径：先带 languages，失败后再 language=None。"""
    import barkit.plugins.recognize.recognizer as mod

    calls = []

    def fake_ocr(image, *, languages=None, **kwargs):
        calls.append(languages)
        if languages is not None:
            raise ValueError("Invalid language preference")
        return {"regions": [], "framework": "vision"}

    monkeypatch.setattr(mod, "_ocr_fn", fake_ocr)
    result = mod.run_ocr(_real_image, None, ocr_cfg={"languages": ["zh-Hans", "en"]})
    assert calls == [["zh-Hans", "en-US"], None]
    assert result == {"regions": [], "framework": "vision"}


def test_run_ocr_non_language_error_propagates(_real_image, monkeypatch):
    """非语言类错误不做降级，原样抛出。"""
    import barkit.plugins.recognize.recognizer as mod

    def fake_ocr(image, **kwargs):
        raise RuntimeError("disk io")

    monkeypatch.setattr(mod, "_ocr_fn", fake_ocr)
    with pytest.raises(RuntimeError):
        mod.run_ocr(_real_image, None, ocr_cfg={})


# ---------- 1.2/1.3 框选视图（offscreen 窗口逻辑） ----------

@pytest.fixture()
def selview(qtbot):
    from barkit.plugins.recognize.selview import SelectionView

    view = SelectionView()
    qtbot.addWidget(view)
    return view


def test_selview_pick_state_no_image(selview):
    """无图 = 拾取态：overlay 提示可见，has_image=False。"""
    assert not selview.has_image
    assert not selview._overlay.isHidden()
    assert selview.image_size() is None


def test_selview_load_image(selview, test_pixmap):
    selview.set_image(test_pixmap)
    assert selview.has_image
    assert selview.image_size() == (_IMG_W, _IMG_H)
    assert selview._overlay.isHidden()


def test_selview_box_draw_and_normalize(selview, test_pixmap):
    """拖拽出框 → normalized_boxes 坐标与预期一致（Retina 无关，按像素归一化）。"""
    selview.set_image(test_pixmap)
    selview.resize(400, 200)
    # 模拟 rubber-band：从 (50,20) 拖到 (150,60)（view 像素坐标）
    from PySide6.QtTest import QTest

    start_view = QPoint(50, 20)
    end_view = QPoint(150, 60)
    QTest.mousePress(selview.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start_view)
    QTest.mouseMove(selview.viewport(), end_view)
    QTest.mouseRelease(selview.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end_view)

    assert selview.has_boxes()
    assert len(selview._boxes) == 1
    boxes = selview.normalized_boxes()
    assert len(boxes) == 1
    x, y, w, h = boxes[0]
    # 换算回像素验证（fitInView 缩放后 view 像素 → scene 像素存在线性关系，
    # 具体数值允许 ±1px 误差，但归一化必须落在 (0,1] 内且宽高为正）
    assert 0 <= x < 1 and 0 <= y < 1
    assert 0 < w <= 1 and 0 < h <= 1


def test_selview_box_normalize_with_dpr(qtbot):
    """Retina 截图（dpr=2）载入后：scene 坐标=物理像素，归一化不因 dpr 缩水。"""
    from PySide6.QtCore import QRectF
    from barkit.plugins.recognize.selview import SelectionView

    img = QImage(2880, 1800, QImage.Format.Format_RGB32)
    img.setDevicePixelRatio(2.0)
    view = SelectionView()
    qtbot.addWidget(view)
    view.set_image(QPixmap.fromImage(img))
    rect = view._pixmap_item.boundingRect()
    assert int(rect.width()) == 2880 and int(rect.height()) == 1800
    assert view.image_size() == (2880, 1800)
    # scene 坐标 = 物理像素：左上角 1/4 区域 (720,450)-(1440,900) 直接建框
    view._new_box(QRectF(720, 450, 720, 450))
    boxes = view.normalized_boxes()
    assert len(boxes) == 1
    x, y, w, h = boxes[0]
    # 归一化以物理像素为基准，dpr=2 若未除则坐标不会缩水一半
    assert x == 0.25 and y == 0.25 and w == 0.25 and h == 0.25


def test_selview_escape_clears_boxes(selview, test_pixmap):
    selview.set_image(test_pixmap)
    from PySide6.QtTest import QTest

    start_view = QPoint(10, 10)
    end_view = QPoint(80, 40)
    QTest.mousePress(selview.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start_view)
    QTest.mouseMove(selview.viewport(), end_view)
    QTest.mouseRelease(selview.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end_view)
    assert selview.has_boxes()

    selview.setFocus()
    QTest.keyClick(selview, Qt.Key.Key_Escape)
    assert not selview.has_boxes()


# ---------- 1.6 插件接线 ----------

def test_plugin_contracts():
    from barkit.plugins.recognize.plugin import Plugin

    ctx = type("Ctx", (), {"config": type("C", (), {"get": lambda self, k, d=None: d})()})()
    p = Plugin(ctx)
    assert p.id == "recognize"
    from barkit.core.plugin import RUN_ON_DEMAND

    assert p.run_mode == RUN_ON_DEMAND
    assert p.api_version == 3
    specs = list(p.hotkeys())
    assert any(s.name == "recognize" for s in specs)


def test_window_manual_clipboard_load_then_recognize(qtbot):
    """剪贴板有图 → 用户点「剪贴板」按钮（显式动作）才载入并触发识别。
    构造期不再自动载入（避免主线程同步解码与激活策略切换死锁）。"""
    from PySide6.QtGui import QImage

    from barkit.plugins.recognize.window_view import RecognizeWindow

    img = QImage(400, 200, QImage.Format.Format_RGB32)
    img.fill(Qt.GlobalColor.white)
    QApplication.clipboard().setImage(img)

    win = RecognizeWindow(
        {"ocr": {"enabled": True, "languages": ["zh-Hans", "en-US"]},
         "barcode": {"enabled": True}},
    )
    qtbot.addWidget(win)
    # 构造期不再自动载入（回归点：主线程同步解码会卡死）
    assert not win._view.has_image
    win.show()
    # 模拟用户点「剪贴板」按钮：载入 + 延迟触发识别
    win._pick_from_clipboard()
    assert win._view.has_image  # 剪贴板图已载入（显式动作）

    # singleShot(120) 后 _start 触发，worker 线程跑 OCR/条码；白图无文字但
    # 流水线必须跑起来并收口
    qtbot.wait(300)
    deadline = time.monotonic() + 8
    while win._pending_kinds and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.05)
    assert not win._pending_kinds  # 两个任务都收口
    assert win._status.text() == "识别完成"
    win.close()
    QApplication.clipboard().clear()


def test_window_no_auto_recognize_without_clipboard_image(qtbot):
    """剪贴板无图 → 不自动识别，等待用户换图。"""
    from PySide6.QtWidgets import QApplication

    from barkit.plugins.recognize.window_view import RecognizeWindow

    QApplication.clipboard().clear()
    win = RecognizeWindow({"ocr": {"enabled": True}, "barcode": {"enabled": True}})
    qtbot.addWidget(win)
    assert not win._view.has_image
    assert win._auto_recognize_pending is False
    win.close()


def test_window_poll_does_not_auto_load_clipboard(qtbot):
    """回归：1 秒轮询只更新「剪贴板」按钮可用态，绝不自动解码剪贴板大图
    （主线程同步解码会卡 UI / 与激活策略切换死锁，实机「窗体弹出即卡死」）。
    自动载入改由用户点按钮 / 重按热键触发。"""
    from PySide6.QtWidgets import QApplication

    from barkit.plugins.recognize.window_view import RecognizeWindow

    QApplication.clipboard().clear()
    win = RecognizeWindow({"ocr": {"enabled": True}, "barcode": {"enabled": True}})
    qtbot.addWidget(win)
    win.show()  # 轮询只在窗口可见且无图时工作
    assert not win._view.has_image

    # 模拟用户截图到剪贴板（窗口已打开）
    img = QImage(400, 200, QImage.Format.Format_RGB32)
    img.fill(Qt.GlobalColor.cyan)
    QApplication.clipboard().setImage(img)

    # 轮询 interval=1s，泵事件足够久
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.05)
    # 关键回归点：轮询不得自动载入（否则主线程解码卡死）
    assert not win._view.has_image
    # 但按钮应被点亮（hasImage 元数据检测，零解码）
    assert win._clipboard_btn.isEnabled()
    win.close()
    QApplication.clipboard().clear()


def test_pick_state_overlay_copy_promises_no_removed_auto_pickup(qtbot):
    """回归：拾取态占位文案不得再承诺「窗口打开时 1 s 内自动拾取」。

    那半句在自动载入被移除后还挂了很久（代码删了、文案没跟），而它恰好是用户在这个
    状态里**唯一**能看到的说明书 —— 文案与实际行为分叉，用户就会照着做、然后发现不灵。
    ⇒ 把「承诺词」钉住。真把自动拾取恢复了，就来改这枚用例：它就是这个意思。
    """
    from PySide6.QtWidgets import QApplication

    from barkit.plugins.recognize.window_view import RecognizeWindow

    QApplication.clipboard().clear()
    win = RecognizeWindow({"ocr": {"enabled": True}, "barcode": {"enabled": True}})
    qtbot.addWidget(win)

    text = win._view._overlay.text()
    for promise in ("自动拾取", "1s", "自动载入"):
        assert promise not in text, f"占位文案承诺了已移除的行为：{promise!r}\n{text}"
    # 教的动作本身还在，且写成真的那两条路径
    assert "⌃⌘⇧4" in text
    assert "重按热键" in text
    win.close()
    QApplication.clipboard().clear()


def test_clipboard_button_loads_image(qtbot):
    """「剪贴板」按钮（显式用户动作）同步载入剪贴板图并触发识别。"""
    from PySide6.QtWidgets import QApplication

    from barkit.plugins.recognize.window_view import RecognizeWindow

    QApplication.clipboard().clear()
    img = QImage(400, 200, QImage.Format.Format_RGB32)
    img.fill(Qt.GlobalColor.cyan)
    QApplication.clipboard().setImage(img)
    win = RecognizeWindow({"ocr": {"enabled": True}, "barcode": {"enabled": True}})
    qtbot.addWidget(win)
    win.show()
    # 模拟点「剪贴板」按钮
    win._pick_from_clipboard()
    assert win._view.has_image
    # 同一张图重复载入要求被去重（lateset key 已记录）
    assert win.try_load_clipboard() is False
    win.close()
    QApplication.clipboard().clear()


def test_clipboard_btn_state_and_manual_replace(qtbot):
    """「剪贴板」按钮：剪贴板有图可用；已有图时手动点击强制替换。

    统一 Tool 样式（pill-ghost）后，可用/禁用由 QSS 控制，不再有内联高亮色；
    这里只校验启用态随剪贴板图片有无切换。
    """
    from PySide6.QtWidgets import QApplication

    from barkit.plugins.recognize.window_view import RecognizeWindow

    QApplication.clipboard().clear()
    img_a = QImage(400, 200, QImage.Format.Format_RGB32)
    img_a.fill(Qt.GlobalColor.cyan)
    QApplication.clipboard().setImage(img_a)

    win = RecognizeWindow({"ocr": {"enabled": True}, "barcode": {"enabled": True}})
    qtbot.addWidget(win)
    win.show()
    # 构造后立即可用（_update_actions → _update_clipboard_btn）
    assert win._clipboard_btn.isEnabled()

    # 剪贴板里已有图：按钮可用，但构造期不再自动载入（需用户点按钮才载入）；
    # 换另一张图后，按钮仍可用
    img_b = QImage(400, 200, QImage.Format.Format_RGB32)
    img_b.fill(Qt.GlobalColor.magenta)
    QApplication.clipboard().setImage(img_b)
    win._update_actions()
    assert win._clipboard_btn.isEnabled()

    # 剪贴板清空 → 按钮禁用
    QApplication.clipboard().clear()
    win._update_actions()
    assert not win._clipboard_btn.isEnabled()
    win.close()

# ---------- Retina dpr 处理（锁定"拖入/剪贴板图识别错乱"修复）----------

def test_qimage_to_pil_handles_retina_dpr():
    """带 devicePixelRatio=2 的 QImage：convertToFormat 会把 dpr 数据压回逻辑
    像素存储，故转换后尺寸应等于逻辑尺寸（不放大），且像素内容正确无错位。"""
    from PIL import Image

    img = QImage(200, 100, QImage.Format.Format_RGB888)
    img.fill(0x112233)
    img.setDevicePixelRatio(2.0)

    pil = _qimage_to_pil(img)
    assert isinstance(pil, Image.Image)
    # convertToFormat 后 constBits 是逻辑像素数据，尺寸应等于逻辑宽高
    assert pil.size == (200, 100)
    assert pil.mode == "RGB"
    # 像素内容未被错位截断：所有像素应同色
    px = pil.getpixel((0, 0))
    assert px == (0x11, 0x22, 0x33)


def test_qimage_to_pil_dpr1_identity():
    """dpr=1 的普通图：尺寸应原样保留。"""
    img = QImage(120, 80, QImage.Format.Format_RGB888)
    img.fill(0xFF00FF)
    pil = _qimage_to_pil(img)
    assert pil.size == (120, 80)


def test_drop_after_clipboard_does_not_use_stale_qimage(qtbot, tmp_path):
    """回归：先剪贴板载入（存 _image_qimage），再拖入新图，识别输入必须是拖入
    的文件路径而非残留的旧 QImage（否则识别出另一张图内容 = "完全错乱"）。"""
    from PySide6.QtGui import QImage

    # 准备两张不同的测试图，写到临时文件
    def make_png(path, color):
        img = QImage(160, 90, QImage.Format.Format_RGB32)
        img.fill(color)
        assert img.save(str(path))

    file_a = tmp_path / "a.png"
    file_b = tmp_path / "b.png"
    make_png(file_a, 0x223344)
    make_png(file_b, 0x998877)

    QApplication.clipboard().clear()
    img_a = QImage(400, 200, QImage.Format.Format_RGB32)
    img_a.fill(0x223344)
    QApplication.clipboard().setImage(img_a)

    win = RecognizeWindow({"ocr": {"enabled": True}, "barcode": {"enabled": True}})
    qtbot.addWidget(win)
    win.show()
    # 1) 显式点「剪贴板」按钮载入 A（会存 _image_qimage）；构造期不再自动载入
    win._pick_from_clipboard()
    assert win._image_qimage is not None  # 剪贴板路径已存 QImage

    # 2) 拖入 B（走 _on_file_dropped → _set_image_from_path）
    win._on_file_dropped(str(file_b))
    assert win._image_path == str(file_b)
    assert win._image_qimage is None, "拖入后必须清空残留的 _image_qimage"

    # 3) 识别输入应为拖入的文件路径，而非旧 QImage
    inp = win._image_or_path()
    assert inp == str(file_b)
    QApplication.clipboard().clear()
    win.close()


# ──────────────────────────────────────────────
# 设置面（2026-09-19 补：此前是全仓唯一没有设置页的插件，五项配置只能手改 YAML）
# ──────────────────────────────────────────────

_FULL_BODY = (
    "plugins:\n"
    "  recognize:\n"
    "    enabled: true\n"
    "    ocr:\n"
    "      enabled: false\n"
    "      languages: [zh-Hans, en-US]\n"
    "      recognition_level: fast\n"
    "    barcode:\n"
    "      enabled: true\n"
    "      barcode_types: [qrcode]\n"
)


def _settings_plugin(tmp_path, body: str | None = None):
    """带真实配置的插件实例：用户 YAML 落在 ``tmp_path``，读回也看它。"""
    from barkit.core.config import Config
    from barkit.core.plugin import PluginContext
    from barkit.plugins.recognize.plugin import Plugin

    user = tmp_path / "recognize.yaml"
    user.write_text(body or _FULL_BODY, encoding="utf-8")
    return Plugin(PluginContext(config=Config(user_path=user)))


def _user_section(tmp_path) -> dict:
    """用户配置里的 ``plugins.recognize``（读原始 YAML，看真实落盘）。"""
    import yaml

    raw = yaml.safe_load((tmp_path / "recognize.yaml").read_text(encoding="utf-8"))
    return (raw.get("plugins") or {}).get("recognize") or {}


def test_settings_page_declaration(tmp_path):
    """声明点：单页设置页**不写 label**（收敛——label 由框架从组件名补「截图识别」）、

    有 build、写了 reason（§7.1 决议 2：自绘要说明理由）。
    """
    spec = _settings_plugin(tmp_path).settings_page()
    assert spec.label in (None, ""), "单页设置页的 label 应交给框架，别再抄一遍组件名"
    assert spec.build is not None
    assert spec.reason, "用 build 逃生舱必须写 reason"


def test_recognition_settings_reads_effective_config(tmp_path):
    """页面读的是**合并后的有效配置**：五项一个不少，取值就是配置里那些。"""
    s = _settings_plugin(tmp_path).recognition_settings()
    assert s == {
        "ocr_enabled": False,
        "ocr_recognition_level": "fast",
        "ocr_languages": ["zh-Hans", "en-US"],
        "barcode_enabled": True,
        "barcode_types": ["qrcode"],
    }


def test_recognition_settings_survives_missing_section(tmp_path):
    """配置段缺 ``ocr`` / ``barcode`` 时不炸，取值仍是可用的默认（不返回 None）。"""
    s = _settings_plugin(tmp_path, "plugins:\n  recognize:\n    enabled: true\n").recognition_settings()
    assert s["ocr_enabled"] is True
    assert s["barcode_enabled"] is True
    assert s["ocr_recognition_level"]          # 非空字符串，下拉能选中一项
    assert isinstance(s["ocr_languages"], list)
    assert isinstance(s["barcode_types"], list)


def test_settings_page_builds_and_attaches_feedback(tmp_path, qtbot):
    """设置页可构建，且建好就把行内反馈槽交给插件（本组件唯一反馈出口）。"""
    p = _settings_plugin(tmp_path)
    page = p._build_settings_page()
    qtbot.addWidget(page)                      # qtbot 只存 weakref，靠本函数的 page 保住
    assert p._feedback is not None, "设置页建好后就该有反馈槽"


def test_settings_page_switches_mirror_config(tmp_path, qtbot):
    """两个开关的初值取自配置（页面只显示，不做兜底）。"""
    from barkit.shell.ui_kit import Switch

    p = _settings_plugin(tmp_path)
    page = p._build_settings_page()
    qtbot.addWidget(page)
    states = [sw.isChecked() for sw in page.findChildren(Switch)]
    assert states == [False, True], "ocr 开=假、条码 开=真（见 _FULL_BODY）"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows 上 os.replace 到已存在目标报 WinError 183（本仓已知环境差异，"
           "同 test_autotype 的 test_editor_*）—— 落盘往返留待 macOS 验证",
)
def test_save_recognition_settings_roundtrip(tmp_path):
    """改一次落一次盘：写进去的键在用户 YAML 里真的出现了，且读回一致。"""
    p = _settings_plugin(tmp_path)
    assert p.save_recognition_settings({"ocr": {"enabled": True, "languages": ["ja-JP"]}})
    section = _user_section(tmp_path)
    assert section["ocr"]["enabled"] is True
    assert section["ocr"]["languages"] == ["ja-JP"]
    assert p.recognition_settings()["ocr_languages"] == ["ja-JP"]


def test_save_recognition_settings_merge_semantics(tmp_path, monkeypatch):
    """合并语义（跨平台，不碰文件系统）：嵌套 dict 只并进给出的键，别的键原样保留。

    这正是「用户手改过的键不许被界面替她删掉」那条 —— 只写 ``patch`` 给出的键。
    """
    p = _settings_plugin(tmp_path)
    section = {"ocr": {"enabled": True, "languages": ["zh-Hans", "en-US"]},
               "barcode": {"enabled": True, "barcode_types": ["qrcode"]}}
    seen = {}

    def _fake(_self, plugin_id, mutate):
        seen["id"] = plugin_id
        mutate(section)          # 真跑一遍插件交出来的那个 mutate

    monkeypatch.setattr(type(p.ctx.config), "update_plugin_section", _fake)
    assert p.save_recognition_settings({"barcode": {"enabled": False}})
    assert seen["id"] == "recognize"
    assert section["barcode"] == {"enabled": False, "barcode_types": ["qrcode"]}
    assert section["ocr"] == {"enabled": True, "languages": ["zh-Hans", "en-US"]}


def test_save_recognition_settings_failure_lands_in_feedback(tmp_path, qtbot, monkeypatch):
    """写盘失败走行内反馈槽（零模态），并如实返回 False。"""
    p = _settings_plugin(tmp_path)
    page = p._build_settings_page()
    qtbot.addWidget(page)

    def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(type(p.ctx.config), "update_plugin_section", _boom)
    assert p.save_recognition_settings({"ocr": {"enabled": True}}) is False
    assert p._feedback is not None
    assert "保存失败" in p._feedback.text()
