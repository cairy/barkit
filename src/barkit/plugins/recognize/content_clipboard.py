"""``_ContentClipboardMixin``：剪贴板监听与载入。

``window_view.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtGui import QGuiApplication


class _ContentClipboardMixin:
    """剪贴板那一路：监听 QTimer、轮询、载入按钮与统一载入路径。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 图片来源 ----------

    def _start_clipboard_watch(self) -> None:
        """秒级轮询剪贴板，**只更新「剪贴板」按钮的可用态**（有图即点亮）。

        不做系统剪贴板监听（无官方 API），秒级轮询开销可忽略。
        这里**不**自动载入图片：那条路已移除，改由「重按热键」/ 点「剪贴板」按钮触发
        —— 原因见 :meth:`_poll_clipboard` 的注释。
        """
        self._clipboard_timer = QTimer(self)
        self._clipboard_timer.setInterval(1000)
        self._clipboard_timer.timeout.connect(self._poll_clipboard)
        self._clipboard_timer.start()

    def _poll_clipboard(self) -> None:
        # 只更新「剪贴板」按钮可用性：剪贴板有图即点亮（可手动载入/替换）。
        # 注意：【不】在此自动解码剪贴板大图——clipboard().image() 在主线程同步
        # 解码，叠加窗口激活策略切换（showEvent）会死锁，实机表现为「窗体弹出即
        # 卡死」。自动载入已移除，统一由用户点「剪贴板」/ 重按热键触发。
        #
        # 原先「可见且无图才往下走」那个分支随自动载入一起删掉了：它后面已无任何代码，
        # 留着的只会是一句**看起来还在做事的空 return**（`isVisible` / `has_image`
        # 判断谁都不敢动，因为看不出删掉会不会丢行为）。
        self._update_clipboard_btn()

    def _update_clipboard_btn(self) -> None:
        """剪贴板有图 → 「剪贴板」按钮可用（pill-ghost 样式自带可用/禁用状态）。"""
        mime = QGuiApplication.clipboard().mimeData()
        has_image = mime is not None and mime.hasImage()
        self._clipboard_btn.setEnabled(has_image and not self._recognizing_flag)

    def _pick_from_clipboard(self) -> None:
        """「剪贴板」按钮：强制从剪贴板载入（已有图则替换），载入后自动识别。"""
        mime = QGuiApplication.clipboard().mimeData()
        if mime is None or not mime.hasImage():
            self._show_status("剪贴板没有图片")
            return
        image = QGuiApplication.clipboard().image()
        if image.isNull():
            self._show_status("剪贴板图片数据无效")
            return
        self._last_clipboard_key = image.cacheKey()
        self._set_image_from_qimage(image)
        self._show_status("已从剪贴板载入图片")
        QTimer.singleShot(120, self._start)

    def try_load_clipboard(self) -> bool:
        """剪贴板有图且与上次载入不同 → 载入预览并（若已显示）自动识别。

        返回是否载入。窗口已显示时立即调度自动识别；未显示（构造期）则
        置 pending 由 showEvent 统一触发。也可由热键唤起路径直接调用。
        仅自动拾取路径用（无图才载入）；手动替换走 _pick_from_clipboard。
        """
        if self._view.has_image:
            return False
        mime = QGuiApplication.clipboard().mimeData()
        if mime is None or not mime.hasImage():
            return False
        image = QGuiApplication.clipboard().image()
        if image.isNull():
            return False
        if image.cacheKey() == self._last_clipboard_key:
            return False  # 同一张图不重复载入
        self._last_clipboard_key = image.cacheKey()
        self._set_image_from_qimage(image)
        self._show_status("已从剪贴板载入图片")
        if self.isVisible():
            QTimer.singleShot(120, self._start)  # 等 UI 渲染完再识别
        else:
            self._auto_recognize_pending = True
        return True
