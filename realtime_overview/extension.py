from krita import Extension, Krita

from . import settings
from .overview_window import OverviewWindow
from .qt_compat import QtCore, is_alive


class RealtimeOverviewExtension(Extension):
    def __init__(self, parent):
        super().__init__(parent)
        self._window = None
        self._app_closing = False
        self._restored = False

    def setup(self):
        notifier = Krita.instance().notifier()
        notifier.setActive(True)
        notifier.applicationClosing.connect(self._on_app_closing)
        notifier.windowCreated.connect(self._on_window_created)

    def createActions(self, window):
        action = window.createAction("realtime_overview_toggle", "Realtime Overview",
                                     "tools/scripts")
        action.triggered.connect(self.toggle)

    # ------------------------------------------------------------------

    def toggle(self):
        if is_alive(self._window) and self._window.isVisible():
            self._window.close()
        else:
            self.show_overview()

    def show_overview(self):
        if not is_alive(self._window):
            # Parent it to Krita's main window so it closes with Krita, but keep it a
            # separate top-level window that can live on another monitor.
            main = Krita.instance().activeWindow()
            parent = main.qwindow() if main is not None else None
            self._window = OverviewWindow(parent)
            self._window.closed.connect(self._on_window_closed)
        self._window.show()
        settings.write_bool("open", True)

    def _on_window_closed(self):
        if not self._app_closing:
            settings.write_bool("open", False)

    def _on_app_closing(self):
        self._app_closing = True
        if is_alive(self._window):
            self._window.save_state()
            settings.write_bool("open", self._window.isVisible())

    def _on_window_created(self):
        if not self._restored and settings.read_bool("open", False):
            self._restored = True
            QtCore.QTimer.singleShot(0, self.show_overview)
