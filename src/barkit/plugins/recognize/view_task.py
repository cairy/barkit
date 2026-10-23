"""跑在 ``QThreadPool`` 上的识别任务：``_TaskSignals`` / ``_RecognizeTask`` ＋ QImage→PIL 的转换。

``window_view.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

import logging

from PySide6 import QtGui
from PySide6.QtCore import (
    QObject,
    QRunnable,
    Signal,
)
from PySide6.QtGui import QImage

from typing import TYPE_CHECKING

from barkit.plugins.recognize.recognizer import (
    run_barcode,
    run_ocr,
)

if TYPE_CHECKING:
    # 只在注解里出现的名字（`_qimage_to_pil` 的返回类型）。`from __future__ import
    # annotations` 已生效 ⇒ 注解不求值，运行期不必真 import PIL。
    from PIL import Image


#: 识别窗口全族共用的日志名（`logging_setup` 的 `%(name)s` 会写进日志文件）。
#: 写死**入口模块名**而不是 `__name__`：平铺没有父包，而本模块是兄弟之一
#: （这一行随它的唯一使用者 `_RecognizeTask.run` 一起搬过来，理由见 plan 的注释）。
log = logging.getLogger("barkit.plugins.recognize.window_view")


# ---------- 并行识别任务 ----------

class _TaskSignals(QObject):
    """QRunnable 无信号能力，借 QObject 载体把结果跨线程带回主线程。"""

    done = Signal(str, object)  # (kind, result)  kind in {"ocr", "barcode"}


def _qimage_to_pil(qimage: QtGui.QImage) -> "Image.Image":
    """QImage → PIL.Image（纯字节拷贝，无 numpy 依赖）。

    注意：convertToFormat 会把 dpr=2 的 QImage 数据压回逻辑像素存储
    （constBits 实际为逻辑 w*h 字节，dpr 只是元数据），故按 width()/height()
    直接 frombytes 即可，无需按设备像素放大。
    """
    from PIL import Image

    img = qimage.convertToFormat(QImage.Format.Format_RGB888)
    return Image.frombytes("RGB", (img.width(), img.height()), bytes(img.constBits()))


class _RecognizeTask(QRunnable):
    """单个识别任务（OCR 或条码），跑在 QThreadPool worker 线程。

    懒加载也发生在这里：首次识别时才 import mac-ocr-text / mac-barcode-read，
    不阻塞主线程，也不会把 Vision/PIL 拉进启动内存（§3.4）。
    """

    def __init__(self, kind: str, runner, *, signals: _TaskSignals, **kwargs) -> None:
        super().__init__()
        self._kind = kind
        self._runner = runner
        self._kwargs = kwargs
        self._signals = signals

    def run(self) -> None:
        try:
            kwargs = dict(self._kwargs)
            # QImage → PIL.Image 的转换放 worker 线程（PIL/Vision 懒加载不占主线程，§3.4）
            image = kwargs.get("image")
            if isinstance(image, QtGui.QImage):
                kwargs["image"] = _qimage_to_pil(image)
            result = self._runner(**kwargs)
            self._signals.done.emit(self._kind, result)
        except Exception as e:  # noqa: BLE001 - worker 线程兜底，绝不静默
            log.exception("识别失败（%s）", self._kind)
            self._signals.done.emit(self._kind, {"error": str(e)})

    @staticmethod
    def make(kind: str, signals: _TaskSignals, image, regions, cfg: dict) -> "_RecognizeTask":
        if kind == "ocr":
            return _RecognizeTask("ocr", run_ocr, signals=signals, image=image, regions=regions, ocr_cfg=cfg)
        return _RecognizeTask("barcode", run_barcode, signals=signals, image=image, regions=regions, barcode_cfg=cfg)
