"""Import Qt from whichever PyQt the running Krita ships (Krita 5: PyQt5, Krita 6: PyQt6).

All other modules use fully scoped enum names (e.g. Qt.WindowType.Window),
which work in both PyQt5 >= 5.11 and PyQt6.
"""

try:
    from PyQt6 import QtCore, QtGui, QtWidgets, sip  # noqa: F401
    QAction = QtGui.QAction
except ImportError:
    from PyQt5 import QtCore, QtGui, QtWidgets, sip  # noqa: F401
    QAction = QtWidgets.QAction


def is_alive(obj):
    """True if obj is a wrapped Qt object whose C++ side still exists."""
    return obj is not None and not sip.isdeleted(obj)
