"""冻结整屏截图上的框选窗口（Wayland portal 路径）。"""
from __future__ import annotations

import traceback

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk  # noqa: E402

from _linux.geometry import selection_crop_box  # noqa: E402


class SelectionOverlay(Gtk.Window):
    """冻结整屏截图上的框选窗口：只在当前显示器内拖选，返回精确图像/屏幕矩形。"""

    _CSS_READY = False

    def __init__(self, image_path: str, on_selected, on_cancel=None) -> None:
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.image_path = image_path
        self._on_selected = on_selected
        self._on_cancel = on_cancel
        self._finished = False
        self._auto_close_id = 0
        self._start: tuple[float, float] | None = None
        self._sel: tuple[float, float, float, float] | None = None

        self._display = Gdk.Display.get_default()
        self._screen = Gdk.Screen.get_default()
        seat = self._display.get_default_seat()
        pointer = seat.get_pointer()
        _, px, py = pointer.get_position()
        self._monitor_index = self._screen.get_monitor_at_point(px, py)
        monitor = self._display.get_monitor(self._monitor_index)
        self._monitor_geom = monitor.get_geometry()

        self._pixbuf = GdkPixbuf.Pixbuf.new_from_file(image_path)
        screen_w = max(1, self._screen.get_width())
        screen_h = max(1, self._screen.get_height())
        self._img_scale_x = self._pixbuf.get_width() / screen_w
        self._img_scale_y = self._pixbuf.get_height() / screen_h

        mon_x = int(round(self._monitor_geom.x * self._img_scale_x))
        mon_y = int(round(self._monitor_geom.y * self._img_scale_y))
        mon_w = int(round(self._monitor_geom.width * self._img_scale_x))
        mon_h = int(round(self._monitor_geom.height * self._img_scale_y))
        mon_x = max(0, min(mon_x, self._pixbuf.get_width() - 1))
        mon_y = max(0, min(mon_y, self._pixbuf.get_height() - 1))
        mon_w = max(1, min(mon_w, self._pixbuf.get_width() - mon_x))
        mon_h = max(1, min(mon_h, self._pixbuf.get_height() - mon_y))
        self._mon_img_x = mon_x
        self._mon_img_y = mon_y
        self._mon_pixbuf = self._pixbuf.new_subpixbuf(mon_x, mon_y, mon_w, mon_h)
        self._sel_scale_x = self._mon_pixbuf.get_width() / max(1, self._monitor_geom.width)
        self._sel_scale_y = self._mon_pixbuf.get_height() / max(1, self._monitor_geom.height)

        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_accept_focus(True)
        self.set_app_paintable(True)
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.set_default_size(self._monitor_geom.width, self._monitor_geom.height)
        self.move(self._monitor_geom.x, self._monitor_geom.y)

        self._area = Gtk.DrawingArea()
        self._area.set_size_request(self._monitor_geom.width, self._monitor_geom.height)
        self._area.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
        )
        self._area.connect("draw", self._on_draw)
        self._area.connect("button-press-event", self._on_press)
        self._area.connect("button-release-event", self._on_release)
        self._area.connect("motion-notify-event", self._on_motion)

        self._hint = Gtk.Label(label="拖拽选择区域  ·  Esc 取消")
        self._hint.set_halign(Gtk.Align.CENTER)
        self._hint.set_valign(Gtk.Align.CENTER)
        self._ensure_css()
        self._hint.get_style_context().add_class("selection-hint")

        overlay = Gtk.Overlay()
        overlay.add(self._area)
        overlay.add_overlay(self._hint)
        try:
            overlay.set_overlay_pass_through(self._hint, True)
        except Exception:
            pass
        self.add(overlay)

        self.connect("key-press-event", self._on_key)
        self.connect("destroy", self._on_destroy)

        try:
            self.fullscreen_on_monitor(self._screen, self._monitor_index)
        except Exception:
            self.fullscreen()
        self.show_all()
        self.present()
        self._area.grab_focus()
        # 全屏选区不能无限停留；30s 未操作自动取消。
        self._auto_close_id = GLib.timeout_add(30000, self._auto_cancel)

    def _auto_cancel(self) -> bool:
        self._auto_close_id = 0
        if not self._finished:
            self._cancel()
        return False

    @classmethod
    def _ensure_css(cls) -> None:
        if cls._CSS_READY:
            return
        cls._CSS_READY = True
        try:
            provider = Gtk.CssProvider()
            provider.load_from_data(
                b".selection-hint{color:#ffffff;background:rgba(10,14,18,.72);"
                b"padding:10px 18px;border-radius:8px;font-size:15px;}"
            )
            Gtk.StyleContext.add_provider_for_screen(
                Gdk.Screen.get_default(),
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )
        except Exception:
            pass

    def _on_key(self, widget, event) -> bool:
        if event.keyval == Gdk.KEY_Escape:
            self._cancel()
            return True
        return False

    def _on_press(self, widget, event) -> bool:
        if event.button == 3:
            self._cancel()
            return True
        if event.button != 1:
            return False
        self._start = (event.x, event.y)
        self._sel = (event.x, event.y, 0.0, 0.0)
        self._hint.hide()
        self._area.queue_draw()
        return True

    def _on_motion(self, widget, event) -> bool:
        if self._start is None:
            return False
        x0, y0 = self._start
        self._sel = self._normalize(x0, y0, event.x, event.y)
        self._area.queue_draw()
        return True

    def _on_release(self, widget, event) -> bool:
        if event.button != 1 or self._start is None:
            return False
        self._start = None
        if self._sel is None:
            return True
        x, y, w, h = self._sel
        if w < 5 or h < 5:
            self._sel = None
            self._hint.show()
            self._area.queue_draw()
            return True
        self._finish_selection((x, y, w, h))
        return True

    def _normalize(self, x0, y0, x1, y1):
        x = min(x0, x1)
        y = min(y0, y1)
        w = abs(x1 - x0)
        h = abs(y1 - y0)
        return (x, y, w, h)

    def _on_draw(self, widget, cr) -> bool:
        alloc = widget.get_allocation()
        width, height = alloc.width, alloc.height
        if width <= 0 or height <= 0:
            return False
        pix_w = self._mon_pixbuf.get_width()
        pix_h = self._mon_pixbuf.get_height()
        cr.save()
        cr.scale(width / pix_w, height / pix_h)
        Gdk.cairo_set_source_pixbuf(cr, self._mon_pixbuf, 0, 0)
        cr.paint()
        cr.restore()

        cr.set_source_rgba(0, 0, 0, 0.45)
        if self._sel is None:
            cr.rectangle(0, 0, width, height)
            cr.fill()
        else:
            x, y, w, h = self._sel
            x = max(0, min(x, width))
            y = max(0, min(y, height))
            w = max(0, min(w, width - x))
            h = max(0, min(h, height - y))
            cr.rectangle(0, 0, width, y)
            cr.rectangle(0, y + h, width, height - y - h)
            cr.rectangle(0, y, x, h)
            cr.rectangle(x + w, y, width - x - w, h)
            cr.fill()
            cr.set_source_rgba(0.24, 0.92, 0.84, 1.0)
            cr.set_line_width(2)
            cr.rectangle(x, y, w, h)
            cr.stroke()
            label = f"{int(round(w / self._sel_scale_x))} x {int(round(h / self._sel_scale_y))}"
            cr.set_source_rgba(0.94, 1.0, 0.98, 1.0)
            cr.select_font_face("Sans", 0, 0)
            cr.set_font_size(13)
            cr.move_to(min(x + 6, width - 120), max(y - 8, 16))
            cr.show_text(label)
        return False

    def _finish_selection(self, rect) -> None:
        if self._finished:
            return
        self._finished = True
        x, y, w, h = rect
        img_box = selection_crop_box(
            self._mon_img_x,
            self._mon_img_y,
            self._sel_scale_x,
            self._sel_scale_y,
            x, y, w, h,
        )
        screen_rect = (
            self._monitor_geom.x + int(round(x)),
            self._monitor_geom.y + int(round(y)),
            max(1, int(round(w))),
            max(1, int(round(h))),
        )
        # 先销毁选区窗口释放 pointer grab，再让结果浮窗去 grab
        self.hide()
        self.destroy()
        try:
            self._on_selected(img_box, screen_rect)
        except Exception:
            traceback.print_exc()

    def _cancel(self) -> None:
        if self._finished:
            return
        self._finished = True
        cb = self._on_cancel
        self.hide()
        self.destroy()
        if cb:
            try:
                cb()
            except Exception:
                traceback.print_exc()

    def close_silent(self) -> None:
        """不触发 on_cancel 的强制关闭（用于新截图覆盖旧选区）。"""
        self._finished = True
        self.hide()
        self.destroy()

    def _on_destroy(self, *args) -> None:
        if self._auto_close_id:
            try:
                GLib.source_remove(self._auto_close_id)
            except Exception:
                pass
            self._auto_close_id = 0
        if not self._finished:
            self._finished = True
            cb = self._on_cancel
            if cb:
                try:
                    cb()
                except Exception:
                    traceback.print_exc()
