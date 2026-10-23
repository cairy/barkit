"""``RecognizeContent`` **本体**：类声明（走 CLASS_DECL_BY_MOD 替换，基类就是那五枚 mixin ＋ QWidget）、``__init__`` 与控件树。⚠️ 它**不在 MIXIN 里** —— 那张表是「本模块自己养一枚**混合类**」，而本模块养的是**主类**。

``window_view.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from PySide6.QtCore import (
    Qt,
    QThreadPool,
)
from PySide6.QtGui import QImage
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from barkit.shell.ui_kit import (
    apply_text_role,
    tool_button,
)
from barkit.plugins.recognize.selview import SelectionView

from .content_clipboard import _ContentClipboardMixin
from .content_drag import _ContentDragMixin
from .content_image import _ContentImageMixin
from .content_pipeline import _ContentPipelineMixin
from .content_results import _ContentResultsMixin
from .view_rows import _ResultList


# ---------- 识别内容面 ----------


class RecognizeContent(
    _ContentClipboardMixin,
    _ContentImageMixin,
    _ContentPipelineMixin,
    _ContentResultsMixin,
    _ContentDragMixin,
    QWidget,
):
    # 混合类按**职责**分开在同目录的兄弟模块，方法经 ``self`` 取用实例状态，
    # 故 MRO 顺序不影响变量解析。``QWidget`` 排在**最后**：PySide6 的元类按最靠右
    # 的 Qt 基类登记虚函数 —— ``_ContentDragMixin`` 整族都是虚函数重写。

    """识别内容面：左图右结果，**全部 UI 与识别逻辑**都在这里（运行期面，§7.3）。

    作为独立 QWidget，它有两种承载方式：

    - 交给框架 ``DetailHost``：``DetailEntrySpec(presentation="window",
      build=lambda: RecognizeContent(...))`` 的返回值，Dock 显隐 / 居中 / 关闭全归框架；
    - 被 ``RecognizeWindow`` 包成独立窗口：热键复用 ``open_window`` 那条旧路径仍直接
      new 它（壳负责标题 / 初尺寸 / Dock 激活策略 / 整窗拖放转发）。

    内容面**不碰**窗口级事务（标题、激活策略、``WA_DeleteOnClose``）；它只管自己的
    业务与一块画布，并自接整窗拖放（这样经 ``DetailHost`` 承载时拖放也照样可用——
    ``DetailForm`` 的原生窗没有多余标题栏，内容铺满整窗）。
    """


    def __init__(self, plugin_cfg: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._cfg = plugin_cfg or {}
        self._image_qimage: QImage | None = None   # 剪贴板来源的原图（识别后释放）
        self._image_path: str | None = None        # 文件来源的路径
        self._pool = QThreadPool(self)             # 跟随内容销毁
        self._recognizing_flag = False             # 显式识别状态（不用 UI 文本推断）
        self._last_ocr: dict = {}
        self._last_barcode: dict = {}
        self._pending_kinds: set[str] = set()

        self._auto_recognize_pending = False  # 剪贴板载入后待自动识别（showEvent 触发）
        self._last_clipboard_key: int | None = None  # 防重复载入同一张剪贴板图
        # 整窗启用拖放：拖到内容任意处都接受文件（§4.1 拾取态提示"拖入图片文件"）。
        # QMainWindow 默认不接受拖放，但本内容是 QWidget，直接 setAcceptDrops 即可；
        # 独立窗口壳 RecognizeWindow 也另挂一份同款转发，覆盖其标题栏区域。
        self.setAcceptDrops(True)
        self._build_ui()
        self._start_clipboard_watch()


        # 不在构造期同步读剪贴板大图：主线程 clipboard().image() 解码会卡 UI，
        # 且构造期发生在窗口 show 之前（激活策略尚未切 Regular），与后续
        # showEvent 的激活切换叠加会死锁——实机表现为「窗体弹出即卡死」。
        # 剪贴板载入改由用户显式动作触发：点「剪贴板」按钮 / 重按热键（见
        # _pick_from_clipboard / open_window 重触发）。1 秒轮询只更新按钮可用态。

    # ---------- UI ----------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(4)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.setChildrenCollapsible(False)
        root.addWidget(splitter)  # 必须加入根布局，否则 splitter 不参与布局直接钉在左上角

        # 左：图片区
        self._view = SelectionView(splitter)
        self._view.message.connect(self._show_status)
        self._view.boxes_changed.connect(lambda: self._update_actions())
        # 文件载入统一上提到内容面层：空串=请求"选择文件"对话框，非空=按路径载入。
        # 这样来源语义（存路径/存 QImage/清旧状态）只在一处管理，避免子控件
        # 直接加载导致 _image_qimage 残留而识别错乱（见 _set_image_from_path）。
        self._view.file_dropped.connect(self._on_file_dropped)
        splitter.addWidget(self._view)

        # 右：结果区
        right = QWidget(splitter)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(2)

        # 操作行（统一高度：识别=主操作填充，其余=描边；状态右对齐 muted）
        ops = QHBoxLayout()
        ops.setSpacing(6)
        self._recognize_btn = tool_button("识别", "primary")
        self._recognize_btn.clicked.connect(self._start)
        self._clipboard_btn = tool_button("剪贴板", "pill-ghost")
        self._clipboard_btn.setToolTip("剪贴板有图片时可用：载入/替换为剪贴板图片")
        self._clipboard_btn.clicked.connect(self._pick_from_clipboard)
        self._pick_btn = tool_button("换图", "pill-ghost")
        self._pick_btn.clicked.connect(self._pick_image)
        self._clear_btn = tool_button("清空选区", "pill-ghost")
        self._clear_btn.clicked.connect(self._view.clear_boxes)
        ops.addWidget(self._recognize_btn)
        ops.addWidget(self._clipboard_btn)
        ops.addWidget(self._pick_btn)
        ops.addWidget(self._clear_btn)
        ops.addStretch(1)
        self._status = QLabel("就绪")
        apply_text_role(self._status, "caption")
        ops.addWidget(self._status)
        right_layout.addLayout(ops)

        # 结果区：原生 QTabWidget（文字(n) / 条码(n)），每个页内是列表 + 悬停复制
        # （设计稿 §5.2 的"复制入口深"用逐行悬停「复制」解决，而非 Tab 本身）
        self._tabs = QTabWidget(right)
        right_layout.addWidget(self._tabs, stretch=1)

        # 文字页：内容贴边（与 pane 同一矩形，无内外边距错位）；仅顶部按钮区留白
        text_page = QWidget()
        text_layout = QVBoxLayout(text_page)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(2)
        text_top = QHBoxLayout()
        text_top.setContentsMargins(10, 10, 10, 6)
        text_top.setSpacing(4)
        self._copy_all_btn = tool_button("复制全部", "pill-ghost")
        self._copy_all_btn.clicked.connect(self._copy_all_text)
        self._total_label = QLabel("未识别")
        apply_text_role(self._total_label, "caption")
        text_top.addWidget(self._copy_all_btn)
        text_top.addStretch(1)
        text_top.addWidget(self._total_label)
        text_layout.addLayout(text_top)
        self._list_text = _ResultList(text_page)
        text_layout.addWidget(self._list_text, stretch=1)
        self._tabs.addTab(text_page, "文字")

        # 条码页：内容贴边，仅顶部按钮区留白
        code_page = QWidget()
        code_layout = QVBoxLayout(code_page)
        code_layout.setContentsMargins(0, 0, 0, 0)
        code_layout.setSpacing(2)
        code_top = QHBoxLayout()
        code_top.setContentsMargins(10, 10, 10, 6)
        self._code_total_label = QLabel("未识别")
        apply_text_role(self._code_total_label, "caption")
        code_top.addWidget(self._code_total_label)
        code_top.addStretch(1)
        code_layout.addLayout(code_top)
        self._list_code = _ResultList(code_page)
        code_layout.addWidget(self._list_code, stretch=1)
        self._tabs.addTab(code_page, "条码")

        splitter.addWidget(right)
        # 给文字更多横向空间（左图 60% : 右侧 40%）
        splitter.setSizes([620, 420])
        self._update_actions()
        self._sync_result()
