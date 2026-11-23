"""Krita docker hosting an OverviewView. Settings > Dockers > Realtime Overview.

Like any docker it can be docked, tabbed, or floated onto another monitor.
"""

from krita import DockWidget

from .overview_view import OverviewView


class OverviewDocker(DockWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Realtime Overview")
        self.view = OverviewView(self)
        self.view.double_clicked.connect(lambda: self.setFloating(not self.isFloating()))
        self.setWidget(self.view)

    def canvasChanged(self, canvas):
        # The view follows Krita's active document on its own.
        pass
