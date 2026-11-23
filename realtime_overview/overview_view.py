"""A standalone top-level window that shows a live, scaled view of the active document.

How it stays realtime
---------------------
Krita's Overview docker renders its thumbnail as an *idle task stroke*. Strokes run
sequentially, so while a long stroke is active (e.g. the Transform tool, which keeps
its stroke open until you apply) the docker cannot refresh.

This window instead polls ``doc.rootNode().thumbnail(w, h)`` on a timer from the GUI
thread. That call:
  * takes no image lock and does not go through the stroke queue,
  * reads the root layer's composited paint device - the same data the canvas
    displays, including the Transform tool's in-stack preview,
  * is cached per paint device and only recomputed after the image changed
    (cheap nearest-neighbour sampling), so polling an idle image costs ~nothing.

The thumbnail covers the root's *exact bounds* (content bounds), not the canvas
rect, so it is positioned using ``rootNode().bounds()``.
"""

import math
import os
import time

from krita import Krita

from . import settings
from .qt_compat import QAction, QtCore, QtGui, QtWidgets

Qt = QtCore.Qt

MARGIN = 0            # logical px around the image
MIN_INTERVAL_MS = 8
FPS_CHOICES = (10, 20, 30, 60)
TEAR_EPS = 0.00002       # transparent-fraction increase that counts as "new holes" (~13 px of 650k)
OPAQUE_FRAC = 0.0005     # clean frames this transparent or less get their holes patched
CONFIRM_MATCHES = 2      # same-size fresh reads that must agree before new transparency is accepted
TEAR_RETRY_MS = 40       # fresh re-read delay while holes persist
SETTLE_MS = 80           # fresh re-read delay after a change
DEBUG_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug.log")


class OverviewView(QtWidgets.QWidget):
    """The live preview itself. Hosted by OverviewWindow or OverviewDocker."""

    title_changed = QtCore.pyqtSignal(str)
    double_clicked = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setMinimumSize(60, 45)
        self.host_actions = []      # extra context-menu actions supplied by the container

        self._mirror = settings.read_bool("mirror", False)
        self._gray = settings.read_bool("grayscale", False)
        self._smooth = settings.read_bool("smooth", True)
        self._checker = settings.read_bool("checker", True)
        self._fps = settings.read_int("fps", 30)
        if self._fps not in FPS_CHOICES:
            self._fps = 30

        self._frame = None          # composed QImage, widget-sized
        self._last_key = None
        self._message = ""
        self._doc_id = None
        self._stable_bounds = None  # last bounds believed to be at full resolution
        self._error_logged = False
        self._debug = False
        self._bypass_cache = False
        self._variant = 0           # 0/1: which of two thumbnail sizes we request
        self._base_frac = [None, None]     # per variant: transparent fraction of last clean frame
        self._pending_frac = [None, None]  # per variant: elevated fraction of the previous fresh read
        self._confirmations = 0            # fresh reads matching their predecessor of the same size
        self._shown_image = None    # last displayed thumbnail (possibly patched)
        self._shown_bounds = None

        self._settle_timer = QtCore.QTimer(self)
        self._settle_timer.setSingleShot(True)
        self._settle_timer.timeout.connect(lambda: self._tick(fresh=True))

        self._timer = QtCore.QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._tick)
        self._timer.setInterval(self._base_interval())

        self._checker_brush = self._make_checker_brush()
        self._build_actions()

    # ------------------------------------------------------------------ actions

    def _build_actions(self):
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

        self._act_mirror = make("Mirror horizontally", "M", self._mirror, self._set_mirror)
        self._act_gray = make("Grayscale (value check)", "G", self._gray, self._set_gray)
        self._act_smooth = make("Smooth sampling (2x supersample)", "S", self._smooth, self._set_smooth)
        self._act_checker = make("Checkerboard for transparency", None, self._checker, self._set_checker)

        self._fps_group = QtGui.QActionGroup(self) if hasattr(QtGui, "QActionGroup") \
            else QtWidgets.QActionGroup(self)
        self._fps_actions = []
        for fps in FPS_CHOICES:
            action = QAction(f"{fps} fps", self)
            action.setCheckable(True)
            action.setChecked(fps == self._fps)
            action.triggered.connect(lambda _checked=False, f=fps: self._set_fps(f))
            self._fps_group.addAction(action)
            self._fps_actions.append(action)

        self._act_debug = make("Debug: write log", None, False, self._set_debug)
        self._act_bypass = make("Debug: bypass thumbnail cache", None, False, self._set_bypass)

    def contextMenuEvent(self, event):
        menu = QtWidgets.QMenu(self)
        menu.addAction(self._act_mirror)
        menu.addAction(self._act_gray)
        menu.addAction(self._act_checker)
        menu.addSeparator()
        menu.addAction(self._act_smooth)
        fps_menu = menu.addMenu("Max refresh rate")
        for action in self._fps_actions:
            fps_menu.addAction(action)
        menu.addSeparator()
        menu.addAction(self._act_debug)
        menu.addAction(self._act_bypass)
        if self.host_actions:
            menu.addSeparator()
            for action in self.host_actions:
                menu.addAction(action)
        menu.exec(event.globalPos())

    def mouseDoubleClickEvent(self, event):
        self.double_clicked.emit()

    def _set_mirror(self, on):
        self._mirror = on
        settings.write_bool("mirror", on)
        self._invalidate()

    def _set_gray(self, on):
        self._gray = on
        settings.write_bool("grayscale", on)
        self._invalidate()

    def _set_smooth(self, on):
        self._smooth = on
        settings.write_bool("smooth", on)
        self._invalidate()

    def _set_checker(self, on):
        self._checker = on
        settings.write_bool("checker", on)
        self._invalidate()

    def _set_fps(self, fps):
        self._fps = fps
        settings.write("fps", fps)
        self._timer.setInterval(self._base_interval())

    # --------------------------------------------------------------- lifecycle

    def _base_interval(self):
        return max(MIN_INTERVAL_MS, int(1000 / self._fps))

    def _set_title(self, title):
        if title != getattr(self, "_title", None):
            self._title = title
            self.title_changed.emit(title)

    def showEvent(self, event):
        super().showEvent(event)
        self._invalidate()
        self._timer.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._timer.stop()
        self._settle_timer.stop()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._invalidate()

    def _invalidate(self):
        self._last_key = None
        if self.isVisible():
            self._tick()

    # ---------------------------------------------------------------- refresh

    def _tick(self, fresh=False):
        if self.window().isMinimized() or not self.isVisible():
            return
        start = time.perf_counter()
        try:
            changed = self._refresh(fresh)
        except Exception as exc:  # never let a timer callback spam tracebacks
            if not self._error_logged:
                self._error_logged = True
                import traceback
                traceback.print_exc()
            changed = self._set_message(f"Error: {exc}")
        if changed:
            self.update()
            # Adaptive throttle: never spend more than ~1/3 of the GUI thread on this.
            cost_ms = (time.perf_counter() - start) * 1000.0
            interval = max(self._base_interval(), int(cost_ms * 3))
            if interval != self._timer.interval():
                self._timer.setInterval(interval)

    def _set_message(self, text):
        if self._message == text and self._frame is None:
            return False
        self._message = text
        self._frame = None
        self._last_key = None
        return True

    def _refresh(self, fresh=False):
        doc = Krita.instance().activeDocument()
        if doc is None:
            self._set_title("Realtime Overview")
            return self._set_message("No document open")

        img_w, img_h = doc.width(), doc.height()
        root = doc.rootNode()
        if img_w <= 0 or img_h <= 0 or root is None:
            return self._set_message("Empty document")

        doc_id = (doc.fileName(), doc.name(), img_w, img_h)
        if doc_id != self._doc_id:
            self._doc_id = doc_id
            self._stable_bounds = None
            self._base_frac = [None, None]
            self._pending_frac = [None, None]
            self._shown_image = None
            self._set_title(f"Realtime Overview - {doc.name() or 'Untitled'}")

        dpr = self.devicePixelRatioF()
        area = QtCore.QRectF(self.rect()).adjusted(MARGIN, MARGIN, -MARGIN, -MARGIN)
        if area.width() < 2 or area.height() < 2:
            return False
        scale = min(area.width() / img_w, area.height() / img_h)   # logical px per image px
        target = QtCore.QRectF(0, 0, img_w * scale, img_h * scale)
        target.moveCenter(area.center())

        bounds = root.bounds()
        image = None
        full_bounds = QtCore.QRect()
        if not bounds.isEmpty():
            lod = self._detect_lod(bounds, img_w, img_h)
            full_bounds = QtCore.QRect(bounds.x() * lod, bounds.y() * lod,
                                       bounds.width() * lod, bounds.height() * lod)
            sample = scale * dpr * (2.0 if self._smooth else 1.0)
            sample = min(sample, 1.0)
            tw = max(1, min(bounds.width(), math.ceil(full_bounds.width() * sample)))
            th = max(1, min(bounds.height(), math.ceil(full_bounds.height() * sample)))
            if fresh or self._bypass_cache:
                # Krita's thumbnail cache is keyed by size, so switching between two
                # sizes (1px apart) forces a fresh sample of the current pixels.
                self._variant ^= 1
            if tw > 1:
                tw -= self._variant
            image = root.thumbnail(tw, th)
            if image.isNull():
                image = None
        else:
            lod = 1

        if self._debug:
            self._debug_log(doc, root, bounds, lod, full_bounds, image, img_w, img_h)

        key = (
            image.cacheKey() if image is not None else 0,
            full_bounds.getRect(), img_w, img_h, self.width(), self.height(), dpr,
            self._mirror, self._gray, self._smooth, self._checker,
        )
        if key == self._last_key:
            return False

        # Tear guard. Krita clears a patch before re-compositing it, so a read that
        # lands in between shows transparent holes; during a Transform drag most
        # reads are like that. Any transparency beyond the last known-clean frame
        # is treated as a tear until several fresh reads, taken after the image
        # stopped changing, agree on it.
        # Fractions are tracked per requested size ("variant"): the two sizes are
        # 1px apart and sample thin features differently, but the same size on
        # the same pixels gives identical results.
        v = self._variant
        frac = self._transparent_fraction(image)
        base = self._base_frac[v]
        tolerance = 0.0
        if base is None:
            base, tolerance = self._base_frac[1 - v], 0.25
        elevated = base is not None and frac > base + max(TEAR_EPS, tolerance * base)
        if elevated and fresh:
            pending = self._pending_frac[v]
            if pending is not None and abs(frac - pending) <= TEAR_EPS:
                self._confirmations += 1
            elif pending is not None:
                self._confirmations = 0     # holes moved: still tearing
            self._pending_frac[v] = frac
            if self._confirmations >= CONFIRM_MATCHES:
                elevated = False            # persistent -> real transparency
                self._base_frac[1 - v] = None
        if elevated:
            self._settle_timer.start(TEAR_RETRY_MS if fresh else SETTLE_MS)
            patched = self._patch_holes(image, full_bounds)
            if patched is None:
                return False                # can't patch: keep showing the last frame
            image = patched
        else:
            self._base_frac[v] = frac
            self._pending_frac = [None, None]
            self._confirmations = 0
            if not fresh:
                # The cached thumbnail may have been sampled mid-update (the cache is
                # invalidated when a write *starts*), so re-read once things calm down.
                self._settle_timer.start(SETTLE_MS)

        self._shown_image = image
        self._shown_bounds = full_bounds.getRect()
        self._last_key = key
        self._message = ""
        self._frame = self._compose(image, full_bounds, target, scale, dpr)
        return True

    def _patch_holes(self, image, full_bounds):
        """Fill fully transparent pixels of `image` from the previously shown frame.

        Only used when the clean image is (almost) opaque, i.e. every hole is a tear.
        Returns None when patching isn't safe.
        """
        prev = self._shown_image
        known = [b for b in self._base_frac if b is not None]
        if (image is None or prev is None or not known or max(known) > OPAQUE_FRAC
                or self._shown_bounds != full_bounds.getRect()):
            return None
        out = QtGui.QImage(image.size(), QtGui.QImage.Format.Format_ARGB32_Premultiplied)
        out.fill(0)
        p = QtGui.QPainter(out)
        p.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, prev.size() != image.size())
        p.drawImage(QtCore.QRectF(out.rect()), prev)
        p.drawImage(0, 0, image)            # source-over: holes (alpha 0) show prev
        p.end()
        return out

    @staticmethod
    def _transparent_fraction(image):
        """Fraction of fully transparent pixels (alpha == 0)."""
        if image is None or image.width() == 0 or image.height() == 0:
            return 0.0
        if image.depth() != 32:
            image = image.convertToFormat(QtGui.QImage.Format.Format_ARGB32)
        size = image.sizeInBytes() if hasattr(image, "sizeInBytes") else image.byteCount()
        alpha = image.constBits().asstring(size)[3::4]   # B,G,R,A / R,G,B,A: alpha is byte 3
        return alpha.count(0) / len(alpha)

    # ------------------------------------------------------------------ debug

    def _set_debug(self, on):
        self._debug = on
        if on:
            self._debug_t0 = time.perf_counter()
            self._debug_last = 0.0
            with open(DEBUG_LOG, "w", encoding="utf-8") as f:
                f.write("# Realtime Overview debug log\n")
            self._log_line("debug on, bypass_cache=%s smooth=%s fps=%s" %
                           (self._bypass_cache, self._smooth, self._fps))

    def _set_bypass(self, on):
        self._bypass_cache = on
        if self._debug:
            self._log_line("bypass_cache=%s" % on)

    def _log_line(self, text):
        t = time.perf_counter() - self._debug_t0
        with open(DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write("%8.3f %s\n" % (t, text))

    def _debug_log(self, doc, root, bounds, lod, full_bounds, image, img_w, img_h):
        now = time.perf_counter()
        if now - self._debug_last < 0.2:
            return
        self._debug_last = now
        # Probe points (full-res image coords). Compare: thumbnail vs direct projection reads.
        pts = [(img_w // 2, img_h // 2), (img_w // 4, img_h // 4), (3 * img_w // 4, 3 * img_h // 4)]
        parts = []
        for (px, py) in pts:
            def proj(x, y):
                try:
                    q = doc.projection(x, y, 1, 1)
                    return q.pixelColor(0, 0).name(QtGui.QColor.NameFormat.HexArgb) if not q.isNull() else "null"
                except Exception as exc:
                    return "err:%s" % exc
            direct = proj(px, py)
            direct_lod = proj(px // lod, py // lod) if lod > 1 else "-"
            thumb = "-"
            if image is not None and full_bounds.width() > 0 and full_bounds.height() > 0:
                u = int((px - full_bounds.x()) / full_bounds.width() * image.width())
                v = int((py - full_bounds.y()) / full_bounds.height() * image.height())
                if 0 <= u < image.width() and 0 <= v < image.height():
                    thumb = image.pixelColor(u, v).name(QtGui.QColor.NameFormat.HexArgb)
            parts.append("(%d,%d) thumb=%s proj=%s projlod=%s" % (px, py, thumb, direct, direct_lod))
        self._log_line("img=%dx%d bounds=%s lod=%d thumb=%s key=%s | %s" % (
            img_w, img_h, bounds.getRect(), lod,
            "%dx%d" % (image.width(), image.height()) if image is not None else "none",
            image.cacheKey() if image is not None else 0, " ; ".join(parts)))

    def _detect_lod(self, bounds, img_w, img_h):
        """Return the level-of-detail factor (1, 2, 4, 8) of the data we just read.

        With Instant Preview, Krita temporarily computes strokes on a downscaled
        copy of the image, and paint devices report coordinates in that scaled
        space while such a stroke runs. Detect it so the preview doesn't shrink.
        """
        def close(rect, x, y, w, h):
            return (abs(rect.x() - x) <= 2 and abs(rect.y() - y) <= 2 and
                    abs(rect.width() - w) <= 2 and abs(rect.height() - h) <= 2)

        full_canvas = close(bounds, 0, 0, img_w, img_h)
        if not full_canvas:
            for f in (2, 4, 8):
                if img_w >= 64 * f and close(bounds, 0, 0, img_w / f, img_h / f):
                    return f
            sb = self._stable_bounds
            if sb is not None and sb.width() >= 64 and sb.height() >= 64:
                for f in (2, 4, 8):
                    if close(bounds, sb.x() / f, sb.y() / f, sb.width() / f, sb.height() / f):
                        return f
        self._stable_bounds = QtCore.QRect(bounds)
        return 1

    def _compose(self, image, full_bounds, target, scale, dpr):
        frame = QtGui.QImage(max(1, math.ceil(self.width() * dpr)),
                             max(1, math.ceil(self.height() * dpr)),
                             QtGui.QImage.Format.Format_ARGB32_Premultiplied)
        frame.setDevicePixelRatio(dpr)
        frame.fill(self.palette().color(QtGui.QPalette.ColorRole.Window).darker(160))

        p = QtGui.QPainter(frame)
        if self._mirror:
            cx = target.center().x()
            p.translate(cx, 0)
            p.scale(-1, 1)
            p.translate(-cx, 0)
        p.setClipRect(target)
        if self._checker:
            p.fillRect(target, self._checker_brush)
        else:
            p.fillRect(target, QtGui.QColor(255, 255, 255))
        if image is not None:
            dest = QtCore.QRectF(target.x() + full_bounds.x() * scale,
                                 target.y() + full_bounds.y() * scale,
                                 full_bounds.width() * scale,
                                 full_bounds.height() * scale)
            # Smooth when shrinking; crisp pixels when the image is smaller than the window.
            p.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform,
                            image.width() > dest.width() * dpr + 0.5)
            p.drawImage(dest, image)
        p.end()

        if self._gray:
            frame = frame.convertToFormat(QtGui.QImage.Format.Format_Grayscale8)
            frame.setDevicePixelRatio(dpr)
        return frame

    @staticmethod
    def _make_checker_brush():
        tile = QtGui.QPixmap(16, 16)
        tile.fill(QtGui.QColor(200, 200, 200))
        p = QtGui.QPainter(tile)
        p.fillRect(0, 0, 8, 8, QtGui.QColor(150, 150, 150))
        p.fillRect(8, 8, 8, 8, QtGui.QColor(150, 150, 150))
        p.end()
        return QtGui.QBrush(tile)

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        if self._frame is not None:
            p.drawImage(0, 0, self._frame)
        else:
            p.fillRect(self.rect(), self.palette().color(QtGui.QPalette.ColorRole.Window).darker(160))
        if self._message:
            p.setPen(QtGui.QColor(220, 220, 220))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                       self._message + "\n\nRight-click for options, double-click for fullscreen")
        p.end()
