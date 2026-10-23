"""``_ContentPipelineMixin``：QThreadPool 提交、收口与动作区。

``window_view.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from barkit.plugins.recognize.recognizer import (
    describe_barcodes,
    group_text_blocks,
)

from .view_task import _RecognizeTask, _TaskSignals


class _ContentPipelineMixin:
    """识别流水线：提交 QThreadPool（OCR 与条码各一个 QRunnable）、收口、结果计数与动作区可用性。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    def _sync_result(self) -> None:
        """识别结果联动：
        - 页签名带计数（文字(n) / 条码(n)）
        - 识别后自动切到有结果的页；都有 → 文字；错误/未识别 → 保持文字
        """
        text_cnt = self._text_count
        code_cnt = self._code_count
        self._tabs.setTabText(0, f"文字 ({text_cnt})" if text_cnt else "文字")
        self._tabs.setTabText(1, f"条码 ({code_cnt})" if code_cnt else "条码")
        if text_cnt and code_cnt:
            self._tabs.setCurrentIndex(0)
        elif code_cnt and not text_cnt:
            self._tabs.setCurrentIndex(1)
        else:
            self._tabs.setCurrentIndex(0)

    @property
    def _text_count(self) -> int:
        return len(group_text_blocks(self._last_ocr or {}))

    @property
    def _code_count(self) -> int:
        return len(describe_barcodes(self._last_barcode or {}))

    def _update_actions(self) -> None:
        has_image = self._view.has_image
        self._recognize_btn.setEnabled(has_image and not self._recognizing_flag)
        self._pick_btn.setEnabled(not self._recognizing_flag)
        self._clear_btn.setEnabled(self._view.has_boxes())
        self._update_clipboard_btn()

    # ---------- 识别流水线 ----------

    @property
    def _recognizing(self) -> bool:
        return self._status.text() == "识别中…"

    def _start(self) -> None:
        if not self._view.has_image:
            return
        image = self._image_or_path()
        if image is None:
            self._show_status("图片数据不可用")
            return
        repo_cfg = self._cfg
        ocr_enabled = bool((repo_cfg.get("ocr") or {}).get("enabled", True))
        barcode_enabled = bool((repo_cfg.get("barcode") or {}).get("enabled", True))
        if not (ocr_enabled or barcode_enabled):
            self._show_status("OCR 与条码均已关闭，请到配置开启")
            return

        regions = self._view.normalized_boxes()
        self._pending_kinds = set()
        self._set_recognizing(True)
        self._reset_results()
        self._show_status("识别中…")

        signals = _TaskSignals(self)
        signals.done.connect(self._on_task_done)
        if ocr_enabled:
            self._pending_kinds.add("ocr")
            self._pool.start(_RecognizeTask.make("ocr", signals, image, regions, self._cfg.get("ocr", {})))
        if barcode_enabled:
            self._pending_kinds.add("barcode")
            self._pool.start(_RecognizeTask.make("barcode", signals, image, regions, self._cfg.get("barcode", {})))

    def _set_recognizing(self, on: bool) -> None:
        self._recognizing_flag = on
        self._update_actions()
        if not on:
            # 识别完成立即释放剪贴板来源的大 QImage，只留结果文本（§3.4）；
            # 视图预览 pixmap 保留，再次识别可从 pixmap 恢复数据（见 _image_or_path）
            self._image_qimage = None

    def _reset_results(self) -> None:
        self._list_text.clear()
        self._list_code.clear()
        self._total_label.setText("未识别")
        self._code_total_label.setText("未识别")
        self._copy_all_btn.setEnabled(False)
        self._last_ocr = {}
        self._last_barcode = {}
        self._sync_result()

    def _on_task_done(self, kind: str, result: dict) -> None:
        if kind == "ocr":
            self._last_ocr = result
            self._render_text(result)
        else:
            self._last_barcode = result
            self._render_codes(result)
        self._pending_kinds.discard(kind)
        self._sync_result()
        if not self._pending_kinds:
            self._set_recognizing(False)
            self._update_actions()
            self._show_status("识别完成")
