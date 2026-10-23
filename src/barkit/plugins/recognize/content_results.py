"""``_ContentResultsMixin``：结果渲染、复制与状态行。

``window_view.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from PySide6.QtWidgets import QApplication

from barkit.plugins.recognize.recognizer import (
    describe_barcodes,
    describe_text,
    group_text_blocks,
)

from .view_rows import ResultRow


class _ContentResultsMixin:
    """结果渲染与复制：文字段、条码段、复制与状态行。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    def _render_text(self, result: dict) -> None:
        self._list_text.clear()
        if result.get("error"):
            self._list_text.add_row(ResultRow(f"识别出错：{result['error']}", "", self._copy))
            return
        blocks = group_text_blocks(result)
        if blocks:
            self._copy_all_btn.setEnabled(True)
            self._total_label.setText(f"{len(blocks)} 段")
            for idx, (text, _) in enumerate(blocks, 1):
                tag = f"段落 {idx}" if len(blocks) > 1 else None
                self._list_text.add_row(
                    ResultRow(text, text, self._copy, tag=tag, mono=False, tip="已复制文字")
                )
        else:
            self._list_text.add_row(ResultRow("（未识别到文字）", "", self._copy))
            self._total_label.setText("无文字")

    def _render_codes(self, result: dict) -> None:
        self._list_code.clear()
        if result.get("error"):
            self._list_code.add_row(ResultRow(f"识别出错：{result['error']}", "", self._copy))
            return
        codes = describe_barcodes(result)
        if codes:
            self._code_total_label.setText(f"{len(codes)} 条")
            for code in codes:
                text = f"[{code['barcode_type']}] {code['value']}"
                self._list_code.add_row(
                    ResultRow(text, code["value"], self._copy, mono=True, tip="已复制条码")
                )
        else:
            self._list_code.add_row(ResultRow("（未识别到条码）", "", self._copy))
            self._code_total_label.setText("无条码")

    # ---------- 结果复制 ----------

    def _copy_all_text(self) -> None:
        text = describe_text(self._last_ocr).strip()
        if text:
            QApplication.clipboard().setText(text)
            self._show_status("已复制全部文字")

    def _copy(self, text: str, tip: str) -> None:
        if text:
            QApplication.clipboard().setText(text)
            self._show_status(tip)

    # ---------- 杂项 ----------

    def _show_status(self, text: str) -> None:
        self._status.setText(text)
