"""Separate top-level window hosting an OverviewView (for a second monitor / fullscreen)."""

from . import settings
from .overview_view import OverviewView
from .qt_compat import QAction, QtCore, QtGui, QtWidgets

Qt = QtCore.Qt


class OverviewWindow(QtWidgets.QWidget):
    closed = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle("Realtime Overview")
        self.setMinimumSize(120, 90)
        self.resize(640, 480)

        self._on_top = settings.read_bool("on_top", False)
        self._no_focus = settings.read_bool("no_focus", True)

        self.view = OverviewView(self)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.view)
        self.view.title_changed.connect(self.setWindowTitle)
        self.view.double_clicked.connect(self.toggle_fullscreen)

        def make(text, shortcut, checked, slot):
            action = QAction(text, self)
            if shortcut:
                action.setShortcut(QtGui.QKeySequence(shortcut))
                action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            if checked is not None:
                action.setCheckable(True)
                action.setChecked(checked)
                action.toggled.connect(slot)
            else:
                action.triggered.connect(slot)
            self.addAction(action)
            return action

        self.view.host_actions = [
            make("Fullscreen", "F11", None, self.toggle_fullscreen),
            make("Always on top", "T", self._on_top, self._set_on_top),
            make("Don't take keyboard focus", None, self._no_focus, self._set_no_focus),
            make("Close", None, None, self.close),
        ]
        self._apply_window_flags(show=False)

        geometry = settings.read("geometry", "")
        if geometry:
            self.restoreGeometry(QtCore.QByteArray.fromBase64(geometry.encode("ascii")))

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape and self.isFullScreen():
            self.toggle_fullscreen()
        else:
            super().keyPressEvent(event)

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def _set_on_top(self, on):
        self._on_top = on
        settings.write_bool("on_top", on)
        self._apply_window_flags(show=True)

    def _set_no_focus(self, on):
        self._no_focus = on
        settings.write_bool("no_focus", on)
        self._apply_window_flags(show=True)

    def _apply_window_flags(self, show):
        was_visible = self.isVisible()
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, self._on_top)
        # Keeps Krita's main window focused, so clicking the overview never
        # swallows your keyboard shortcuts.
        self.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus, self._no_focus)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, self._no_focus)
        if show and was_visible:
            self.show()  # changing flags hides the window

    def save_state(self):
        settings.write("geometry", bytes(self.saveGeometry().toBase64()).decode("ascii"))

    def closeEvent(self, event):
        self.save_state()
        super().closeEvent(event)
        self.closed.emit()
