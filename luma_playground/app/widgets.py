# SPDX-License-Identifier: Apache-2.0
"""Small widgets Playground's window and cards share.

Everything here is drawn from LumaUI tokens (`@luma_*` in playground.css). The
only literal colours are the hues of agent and project marks, which are
identity, not chrome, and are taken from the name the way LumaUI derives a
person's hue.
"""
from __future__ import annotations

import math
import zlib
from datetime import datetime, timedelta
from typing import Callable

import gi

gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Gsk", "4.0")
from gi.repository import Adw, Gdk, GLib, Graphene, Gsk, Gtk, Pango  # noqa: E402

try:
    from luma_appkit import lumaui as _lumaui
except Exception:  # noqa: BLE001 - the kit is optional for a bare GTK run
    _lumaui = None


# ── glyphs ────────────────────────────────────────────────────────────────────

def icon_name(name: str) -> str:
    """Playground's own copy of a Lucide glyph (data/icons): `pg-<name>-symbolic`."""
    if name.endswith("-symbolic") or "." in name:
        return name
    return f"pg-{name}-symbolic"


def icon(name: str, size: int = 16, *classes: str) -> Gtk.Image:
    image = Gtk.Image.new_from_icon_name(icon_name(name))
    image.set_pixel_size(size)
    image.set_accessible_role(Gtk.AccessibleRole.PRESENTATION)
    for css in classes:
        image.add_css_class(css)
    return image


def label(text: str = "", *classes: str, xalign: float = 0.0, wrap: bool = False,
          ellipsize: bool = False, selectable: bool = False, markup: bool = False, **props) -> Gtk.Label:
    widget = Gtk.Label(xalign=xalign, **props)
    if markup:
        widget.set_markup(text)
    else:
        widget.set_label(text)
    if wrap:
        widget.set_wrap(True)
        widget.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        widget.set_natural_wrap_mode(Gtk.NaturalWrapMode.WORD)
    if ellipsize:
        widget.set_ellipsize(Pango.EllipsizeMode.END)
    if selectable:
        widget.set_selectable(True)
    for css in classes:
        widget.add_css_class(css)
    return widget


def box(orientation: str = "h", spacing: int = 0, *classes: str, **props) -> Gtk.Box:
    widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL if orientation == "v" else Gtk.Orientation.HORIZONTAL,
                     spacing=spacing, **props)
    for css in classes:
        widget.add_css_class(css)
    return widget


def clear(container: Gtk.Widget) -> None:
    child = container.get_first_child()
    while child is not None:
        following = child.get_next_sibling()
        if isinstance(container, Gtk.Box):
            container.remove(child)
        elif isinstance(container, Gtk.ListBox):
            container.remove(child)
        elif isinstance(container, Adw.WrapBox):
            container.remove(child)
        else:
            child.unparent()
        child = following


def button(text: str = "", *, icon_name_: str | None = None, style: str = "fill", small: bool = False,
           on_click: Callable[[], None] | None = None, tooltip: str | None = None) -> Gtk.Button:
    """A Playground button in the kit's text-button shape (v73 `.bt`): plain, fill, key or quiet."""
    widget = Gtk.Button(valign=Gtk.Align.CENTER)
    widget.add_css_class("pg-bt")
    widget.add_css_class(style)
    if small:
        widget.add_css_class("sm")
    content = Gtk.Box(spacing=7, halign=Gtk.Align.CENTER)
    if icon_name_:
        content.append(icon(icon_name_, 15 if small else 16))
    if text:
        widget.label_widget = label(text)
        content.append(widget.label_widget)
        widget.update_property([Gtk.AccessibleProperty.LABEL], [text])
    widget.set_child(content)
    if tooltip:
        widget.set_tooltip_text(tooltip)
        if not text:
            widget.update_property([Gtk.AccessibleProperty.LABEL], [tooltip])
    if on_click is not None:
        widget.connect("clicked", lambda _b: on_click())
    return widget


def icon_button(name: str, accessible: str, *, on_click: Callable[[], None] | None = None,
                size: int = 16, css: str = "pg-ib") -> Gtk.Button:
    """A glyph alone (v73 `.ib`); `accessible` names it for screen readers and its tooltip."""
    widget = Gtk.Button(valign=Gtk.Align.CENTER, tooltip_text=accessible)
    widget.add_css_class(css)
    widget.set_child(icon(name, size))
    widget.update_property([Gtk.AccessibleProperty.LABEL], [accessible])
    if on_click is not None:
        widget.connect("clicked", lambda _b: on_click())
    return widget


# ── motion ────────────────────────────────────────────────────────────────────

def reduced_motion() -> bool:
    if _lumaui is not None:
        return _lumaui.reduced_motion()
    settings = Gtk.Settings.get_default()
    return settings is not None and not settings.get_property("gtk-enable-animations")


def arrive(widget: Gtk.Widget, *, rise: int = 6, duration: int = 380) -> None:
    """A card settling into the thread (v73 `pgin`): a short fade and a 6 px rise.

    With reduced motion it only fades, quickly; with animations off it simply appears.
    """
    settings = Gtk.Settings.get_default()
    if settings is not None and not settings.get_property("gtk-enable-animations"):
        return
    reduced = reduced_motion()
    base = widget.get_margin_top()
    widget.set_opacity(0.0)

    def step(value: float) -> None:
        widget.set_opacity(value)
        if not reduced:
            widget.set_margin_top(base + round(rise * (1.0 - value)))

    target = Adw.CallbackAnimationTarget.new(step)
    animation = Adw.TimedAnimation.new(widget, 0.0, 1.0, 140 if reduced else duration, target)
    animation.set_easing(Adw.Easing.EASE_OUT_CUBIC)
    animation.connect("done", lambda *_: (widget.set_opacity(1.0), widget.set_margin_top(base)))
    # Held on the widget so the animation lives as long as the card does.
    widget._pg_arrival = animation
    animation.play()


# ── state glyphs: one shape per state, so it reads without colour (v73 `.pgg`) ─

STATES = {
    "working": ("work", "Working"),
    "needs-you": ("needs", "Needs you"),
    "ready": ("review", "Ready to review"),
    "live": ("live", "Live"),
    "paused": ("paused", "Paused"),
    "done": ("live", "Done"),
    "draft": ("draft", "Not started yet"),
    "new": ("draft", "Not started yet"),
    # Shown, never stored: a goal of yours marked working while no agent here can pick it up.
    "waiting": ("draft", "Waiting for an agent"),
}


def state_kind(status: str) -> str:
    return STATES.get(status or "draft", ("draft", ""))[0]


def state_label(status: str) -> str:
    return STATES.get(status or "draft", ("draft", status.replace("-", " ").capitalize() if status else ""))[1]


class StateGlyph(Gtk.Widget):
    """A goal's state: an amber dot (needs you), a turning ring (working), a violet ring (ready),
    a glowing green dot (live), two bars (paused) or a dashed ring (not started)."""

    __gtype_name__ = "PlaygroundStateGlyph"

    def __init__(self, status: str = "draft", size: int = 18) -> None:
        super().__init__(accessible_role=Gtk.AccessibleRole.PRESENTATION, valign=Gtk.Align.CENTER,
                         halign=Gtk.Align.CENTER)
        self.size = size
        self.kind = "draft"
        self._angle = 0.0
        self._tick = 0
        self._began = 0
        self.add_css_class("pg-glyph")
        self.set_status(status)
        self.connect("map", lambda *_: self._sync_spin())
        self.connect("unmap", lambda *_: self._stop())

    def set_status(self, status: str) -> None:
        for kind in ("work", "needs", "review", "live", "paused", "draft"):
            self.remove_css_class(kind)
        self.kind = state_kind(status)
        self.add_css_class(self.kind)
        self._sync_spin()
        self.queue_draw()

    def _sync_spin(self) -> None:
        spin = self.kind == "work" and self.get_mapped() and not reduced_motion()
        if spin and not self._tick:
            self._began = 0
            self._tick = self.add_tick_callback(self._turn)
        elif not spin:
            self._stop()

    def _stop(self) -> None:
        if self._tick:
            self.remove_tick_callback(self._tick)
            self._tick = 0

    def _turn(self, _widget, clock) -> bool:
        now = clock.get_frame_time()
        if not self._began:
            self._began = now
        self._angle = ((now - self._began) / 900000.0) % 1.0 * 2 * math.pi
        self.queue_draw()
        return GLib.SOURCE_CONTINUE

    def do_measure(self, _orientation, _for_size):
        return self.size, self.size, -1, -1

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        w, h = self.get_width(), self.get_height()
        colour = self.get_color()
        cr = snapshot.append_cairo(Graphene.Rect().init(0, 0, w, h))
        cx, cy = w / 2, h / 2
        r, g, b, a = colour.red, colour.green, colour.blue, colour.alpha
        kind = self.kind
        if kind == "needs":
            cr.set_source_rgba(r, g, b, 0.18 * a)
            cr.arc(cx, cy, 7.5, 0, 2 * math.pi)
            cr.fill()
            cr.set_source_rgba(r, g, b, a)
            cr.arc(cx, cy, 4.5, 0, 2 * math.pi)
            cr.fill()
        elif kind == "live":
            for radius, alpha in ((8.5, 0.10), (7.5, 0.16)):
                cr.set_source_rgba(r, g, b, alpha * a)
                cr.arc(cx, cy, radius, 0, 2 * math.pi)
                cr.fill()
            cr.set_source_rgba(r, g, b, a)
            cr.arc(cx, cy, 4.5, 0, 2 * math.pi)
            cr.fill()
        elif kind == "work":
            cr.set_line_width(2)
            cr.set_source_rgba(r, g, b, a)
            start = self._angle
            cr.arc(cx, cy, 5, start, start + 1.5 * math.pi)
            cr.stroke()
        elif kind == "review":
            cr.set_source_rgba(r, g, b, 0.16 * a)
            cr.arc(cx, cy, 4, 0, 2 * math.pi)
            cr.fill()
            cr.set_line_width(2)
            cr.set_source_rgba(r, g, b, a)
            cr.arc(cx, cy, 4, 0, 2 * math.pi)
            cr.stroke()
        elif kind == "paused":
            cr.set_source_rgba(r, g, b, a)
            cr.rectangle(cx - 4.5, cy - 5, 3.2, 10)
            cr.rectangle(cx + 1.3, cy - 5, 3.2, 10)
            cr.fill()
        else:
            cr.set_line_width(1.5)
            cr.set_dash([2.2, 2.0])
            cr.set_source_rgba(r, g, b, a)
            cr.arc(cx, cy, 4.75, 0, 2 * math.pi)
            cr.stroke()


class Spinner(StateGlyph):
    """The small turning ring the step card and the pill use (v73 `.pgspin`)."""

    __gtype_name__ = "PlaygroundSpinner"

    def __init__(self, size: int = 14) -> None:
        super().__init__("working", size)
        self.add_css_class("pg-spin")


# ── marks: agents and projects ────────────────────────────────────────────────

AGENT_GLYPHS = {"claude": "sparkles", "codex": "code", "gemini": "sparkles", "ollama": "cpu", "local": "cpu",
                "ari": "zap", "opencode": "terminal", "goose": "terminal", "pi": "terminal", "team-key": "key-round"}
AGENT_HUES = {"claude": 42, "codex": 165, "gemini": 250, "ollama": 292, "local": 292, "team-key": 232,
              "opencode": 200, "goose": 120, "pi": 18}


def name_hue(name: str) -> int:
    return zlib.crc32((name or "?").lower().encode()) % 360


def _has_icon(name: str) -> bool:
    display = Gdk.Display.get_default()
    return display is not None and Gtk.IconTheme.get_for_display(display).has_icon(name)


class Mark(Gtk.Box):
    """A rounded tile in a hue, with a glyph or a letter (v73 `.pgam`, `.pgpm`).

    `icon_theme_name` uses an installed app icon instead (Ari's own mark) when the theme has it.
    """

    __gtype_name__ = "PlaygroundMark"

    def __init__(self, *, size: int = 22, hue: int = 260, glyph: str | None = None, letter: str = "",
                 icon_theme_name: str | None = None, texture: Gdk.Paintable | None = None,
                 accessible: str = "") -> None:
        super().__init__(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
                         accessible_role=Gtk.AccessibleRole.IMG if accessible else Gtk.AccessibleRole.PRESENTATION)
        self.set_size_request(size, size)
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self.add_css_class("pg-mark")
        if accessible:
            self.update_property([Gtk.AccessibleProperty.LABEL], [accessible])
        if texture is not None:
            picture = Gtk.Image.new_from_paintable(texture)
            picture.set_pixel_size(size)
            self.append(picture)
            self.add_css_class("picture")
            self.set_hexpand(False)
            return
        if icon_theme_name and _has_icon(icon_theme_name):
            image = Gtk.Image.new_from_icon_name(icon_theme_name)
            image.set_pixel_size(size)
            self.append(image)
            self.add_css_class("picture")
            return
        self.add_css_class(f"pg-hue-{_hue_bucket(hue)}")
        inner: Gtk.Widget
        if glyph:
            inner = icon(glyph, max(10, round(size * 0.6)))
        else:
            inner = Gtk.Label(label=(letter or "?")[:1].upper())
            inner.add_css_class("pg-mark-letter")
            attributes = Pango.AttrList()
            attributes.insert(Pango.attr_size_new_absolute(max(8, round(size * 0.48)) * Pango.SCALE))
            attributes.insert(Pango.attr_weight_new(Pango.Weight.BOLD))
            inner.set_attributes(attributes)
        inner.set_hexpand(True)
        inner.set_halign(Gtk.Align.CENTER)
        inner.set_valign(Gtk.Align.CENTER)
        self.append(inner)
        self.set_hexpand(False)


def _hue_bucket(hue: int) -> int:
    """Marks use twelve hues, one CSS class each, so their tiles come from the sheet, not from code."""
    return int(round((hue % 360) / 30.0)) % 12 * 30


def agent_mark(connection: dict | None, size: int = 22) -> Mark:
    connection = connection or {}
    provider = (connection.get("provider") or connection.get("kind") or "ari").lower()
    name = connection.get("label") or connection.get("name") or "Ari"
    if provider == "ari":
        mark = Mark(size=size, hue=300, glyph="zap", icon_theme_name="org.projectluma.Ari", accessible="")
        mark.add_css_class("pg-ari")
        return mark
    hue = AGENT_HUES.get(provider, name_hue(name))
    return Mark(size=size, hue=hue, glyph=AGENT_GLYPHS.get(provider, "sparkles"))


def project_mark(name: str, folder=None, size: int = 30) -> Mark:
    """A project's own icon if its folder has one, else its first letter on its hue."""
    texture = None
    if folder is not None:
        from pathlib import Path
        for candidate in (".playground/icon.svg", ".playground/icon.png", "src/mark.svg", "src/icon.svg",
                          "src/favicon.svg", "static/mark.svg", "static/icon.svg", "static/favicon.svg",
                          "public/icon.svg", "public/favicon.svg", "icon.svg"):
            path = Path(folder) / candidate
            if path.is_file():
                try:
                    texture = Gdk.Texture.new_from_filename(str(path))
                except GLib.Error:
                    texture = None
                if texture is not None:
                    break
    mark = Mark(size=size, hue=name_hue(name), letter=name, texture=texture)
    mark.add_css_class("pg-project-mark")
    return mark


# The open project's members' photos (`photo` in project.toml, checked by core.store), by name and by
# id, so every face drawn by name shows the photo without each caller having to look it up.
_FACES: dict[str, str] = {}
_TEXTURES: dict[tuple[str, int, int], Gdk.Texture | None] = {}


def set_faces(photos: dict[str, str]) -> None:
    """``{name or member id: photo path}`` for the project that is open now (replaces the last one's)."""
    _FACES.clear()
    for key, path in photos.items():
        if key and path:
            _FACES[str(key).strip().lower()] = str(path)


def face_photo(name: str) -> str | None:
    return _FACES.get(str(name or "").strip().lower())


def _photo_texture(path: str) -> Gdk.Texture | None:
    import os
    try:
        info = os.stat(path)
    except OSError:
        return None
    key = (path, int(info.st_mtime_ns), int(info.st_size))
    if key not in _TEXTURES:
        try:
            _TEXTURES[key] = Gdk.Texture.new_from_filename(path)
        except GLib.Error:
            _TEXTURES[key] = None
        if len(_TEXTURES) > 64:
            _TEXTURES.pop(next(iter(_TEXTURES)))
    return _TEXTURES[key]


def person_face(name: str, size: int = 24, photo: str | None = None) -> Gtk.Widget:
    """A person: their photo when the project has one for them, else their initials on their hue."""
    path = photo or face_photo(name)
    texture = _photo_texture(path) if path else None
    try:
        from luma_appkit import PersonAvatar
        face = PersonAvatar(name, size, picture=texture) if texture is not None else PersonAvatar(name, size)
    except Exception:  # noqa: BLE001
        face = Mark(size=size, hue=name_hue(name), letter=name, texture=texture)
        if texture is not None:
            face.add_css_class("pg-face-photo")
    if texture is not None:
        face.add_css_class("pg-has-photo")
        face.update_property([Gtk.AccessibleProperty.LABEL], [name])
    return face


class Presence(Gtk.Overlay):
    """A face with a presence dot (v73 `.pgpres`): green when here, grey when away."""

    def __init__(self, name: str, size: int = 24, *, here: bool = True, photo: str | None = None) -> None:
        super().__init__(valign=Gtk.Align.CENTER)
        self.set_child(person_face(name, size, photo))
        dot = Gtk.Box(halign=Gtk.Align.END, valign=Gtk.Align.END, can_target=False)
        dot.add_css_class("pg-presence")
        dot.add_css_class("here" if here else "away")
        self.add_overlay(dot)
        self.set_tooltip_text(f"{name} is here" if here else name)


# ── small parts ───────────────────────────────────────────────────────────────

class Meter(Gtk.Widget):
    """A thin meter (v73 `.pgmeter`, `.pgmini`): a fraction of a track, drawn from the sheet's colours."""

    __gtype_name__ = "PlaygroundMeter"

    def __init__(self, fraction: float = 0.0, *, width: int = -1, height: int = 6, extra: float = 0.0,
                 accessible: str = "") -> None:
        super().__init__(valign=Gtk.Align.CENTER, accessible_role=Gtk.AccessibleRole.METER)
        self.add_css_class("pg-meter")
        self._fraction = 0.0
        self._extra = extra
        self._height = height
        self._width = width
        self.set_size_request(width, height)
        self.set_fraction(fraction)
        if accessible:
            self.update_property([Gtk.AccessibleProperty.LABEL], [accessible])

    def set_fraction(self, fraction: float, extra: float | None = None) -> None:
        self._fraction = max(0.0, min(1.0, float(fraction or 0)))
        if extra is not None:
            self._extra = max(0.0, min(1.0 - self._fraction, float(extra)))
        self.update_property([Gtk.AccessibleProperty.VALUE_NOW, Gtk.AccessibleProperty.VALUE_MIN,
                              Gtk.AccessibleProperty.VALUE_MAX], [float(self._fraction * 100), 0.0, 100.0])
        self.queue_draw()

    def do_measure(self, orientation, _for_size):
        if orientation == Gtk.Orientation.VERTICAL:
            return self._height, self._height, -1, -1
        if self._width > 0:
            return self._width, self._width, -1, -1
        return 20, 60, -1, -1

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        w, h = self.get_width(), self.get_height()
        context = self.get_style_context()
        _ok, track = context.lookup_color("luma_control_fill")
        fill = self.get_color()
        radius = h / 2
        size = Graphene.Size().init(radius, radius)

        def rounded(x: float, width: float) -> Gsk.RoundedRect:
            rect = Gsk.RoundedRect()
            rect.init(Graphene.Rect().init(x, 0, width, h), size, size, size, size)
            return rect

        if _ok:
            snapshot.push_rounded_clip(rounded(0, w))
            snapshot.append_color(track, Graphene.Rect().init(0, 0, w, h))
            snapshot.pop()
        if self._fraction > 0:
            snapshot.push_rounded_clip(rounded(0, max(h, w * self._fraction)))
            snapshot.append_color(fill, Graphene.Rect().init(0, 0, w, h))
            snapshot.pop()
        if self._extra > 0:
            _found, accent = context.lookup_color("luma_accent")
            if _found:
                start = w * self._fraction
                snapshot.append_color(accent, Graphene.Rect().init(start, 0, w * self._extra, h))


def dot(kind: str = "good") -> Gtk.Box:
    """A 6 px status dot (v73 `.stat`, `.pgsd`)."""
    widget = Gtk.Box(valign=Gtk.Align.CENTER, can_target=False)
    widget.add_css_class("pg-dot")
    widget.add_css_class(kind)
    return widget


def pill(text: str, kind: str) -> Gtk.Box:
    """A goal's state as a pill (v73 `.pgpill`)."""
    widget = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
    widget.add_css_class("pg-pill")
    widget.add_css_class(kind)
    if kind == "work":
        widget.append(Spinner(10))
    widget.append(label(text))
    return widget


def tag(text: str, kind: str = "", glyph: str | None = None) -> Gtk.Box:
    """A small tag (v73 `.pgrecm`, `.pgtag`)."""
    widget = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
    widget.add_css_class("pg-tag")
    if kind:
        widget.add_css_class(kind)
    if glyph:
        widget.append(icon(glyph, 11))
    widget.append(label(text))
    return widget


def check_mark(status: str, size: int = 18) -> Gtk.Widget:
    """A check's state (v73 `.pgst`): a filled tick, a cross, an empty ring, or a turning ring."""
    if status in ("running", "run"):
        spinner = Spinner(size - 4)
        spinner.add_css_class("pg-st-run")
        return spinner
    widget = Gtk.Box(valign=Gtk.Align.START, halign=Gtk.Align.CENTER)
    widget.set_size_request(size, size)
    widget.add_css_class("pg-st")
    kind = {"pass": "pass", "fail": "fail"}.get(status, "wait")
    widget.add_css_class(kind)
    if kind != "wait":
        glyph = icon("check" if kind == "pass" else "x", 11)
        glyph.set_hexpand(True)
        widget.append(glyph)
    widget.set_hexpand(False)
    widget.update_property([Gtk.AccessibleProperty.LABEL],
                           [{"pass": "Passed", "fail": "Failed"}.get(kind, "Not checked yet")])
    widget.set_accessible_role(Gtk.AccessibleRole.IMG)
    return widget


class Sized(Gtk.Widget):
    """Its child at exactly this size, whatever the child would like (a picture's own size is not ours)."""

    __gtype_name__ = "PlaygroundSized"

    def __init__(self, child: Gtk.Widget, width: int, height: int) -> None:
        super().__init__(halign=Gtk.Align.CENTER, valign=Gtk.Align.START)
        self.child, self.width, self.height = child, width, height
        child.set_parent(self)

    def do_measure(self, orientation, _for_size):
        size = self.width if orientation == Gtk.Orientation.HORIZONTAL else self.height
        return size, size, -1, -1

    def do_size_allocate(self, width: int, height: int, _baseline: int) -> None:
        self.child.allocate(width, height, -1, None)

    def do_dispose(self) -> None:
        if self.child.get_parent() is self:
            self.child.unparent()


class Reflow(Gtk.Widget):
    """Two parts side by side when there's room, one above the other when there isn't
    (v73's proof card: the shot beside its checks, stacked under 460 px)."""

    __gtype_name__ = "PlaygroundReflow"

    def __init__(self, first: Gtk.Widget, second: Gtk.Widget, *, threshold: int = 460, spacing: int = 16) -> None:
        super().__init__()
        self.first, self.second = first, second
        self.threshold, self.spacing = threshold, spacing
        first.set_parent(self)
        second.set_parent(self)

    def _side(self, width: int) -> bool:
        return width < 0 or width >= self.threshold

    def do_get_request_mode(self):
        return Gtk.SizeRequestMode.HEIGHT_FOR_WIDTH

    def do_measure(self, orientation, for_size):
        f_min, f_nat, _, _ = self.first.measure(Gtk.Orientation.HORIZONTAL, -1)
        if orientation == Gtk.Orientation.HORIZONTAL:
            s_min, s_nat, _, _ = self.second.measure(Gtk.Orientation.HORIZONTAL, -1)
            return max(f_min, s_min), f_nat + self.spacing + s_nat, -1, -1
        if self._side(for_size):
            rest = -1 if for_size < 0 else max(1, for_size - f_nat - self.spacing)
            h1 = self.first.measure(Gtk.Orientation.VERTICAL, f_nat)
            h2 = self.second.measure(Gtk.Orientation.VERTICAL, rest)
            return max(h1[0], h2[0]), max(h1[1], h2[1]), -1, -1
        h1 = self.first.measure(Gtk.Orientation.VERTICAL, min(f_nat, for_size))
        h2 = self.second.measure(Gtk.Orientation.VERTICAL, for_size)
        return h1[0] + self.spacing + h2[0], h1[1] + self.spacing + h2[1], -1, -1

    def do_size_allocate(self, width: int, height: int, _baseline: int) -> None:
        _m, f_nat, _, _ = self.first.measure(Gtk.Orientation.HORIZONTAL, -1)
        if self._side(width):
            _m, h1, _, _ = self.first.measure(Gtk.Orientation.VERTICAL, f_nat)
            self.first.allocate(f_nat, min(height, h1), -1, None)
            rest = max(1, width - f_nat - self.spacing)
            self.second.allocate(rest, height, -1,
                                 Gsk.Transform().translate(Graphene.Point().init(f_nat + self.spacing, 0)))
            return
        f_w = min(width, f_nat)
        _m, h1, _, _ = self.first.measure(Gtk.Orientation.VERTICAL, f_w)
        self.first.allocate(f_w, h1, -1, Gsk.Transform().translate(Graphene.Point().init((width - f_w) / 2, 0)))
        self.second.allocate(width, max(1, height - h1 - self.spacing), -1,
                             Gsk.Transform().translate(Graphene.Point().init(0, h1 + self.spacing)))

    def do_dispose(self) -> None:
        for child in (self.first, self.second):
            if child.get_parent() is self:
                child.unparent()


# ── words about time ──────────────────────────────────────────────────────────

def parse_ts(value) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value))
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is not None:
        moment = moment.astimezone().replace(tzinfo=None)
    return moment


def day_words(moment: datetime, now: datetime | None = None) -> str:
    now = now or datetime.now()
    days = (now.date() - moment.date()).days
    if days == 0:
        return "Today"
    if days == 1:
        return "Yesterday"
    if 1 < days < 7:
        return moment.strftime("%A")
    if moment.year == now.year:
        return f"{moment.strftime('%b')} {moment.day}"
    return f"{moment.strftime('%b')} {moment.day}, {moment.year}"


def time_words(moment: datetime) -> str:
    hour = moment.hour % 12 or 12
    return f"{hour}:{moment.minute:02d} {'AM' if moment.hour < 12 else 'PM'}"


def when_words(moment: datetime | None, now: datetime | None = None) -> str:
    """'Today at 9:40 AM', 'Tuesday', 'Sep 30': how a person says when."""
    if moment is None:
        return ""
    now = now or datetime.now()
    day = day_words(moment, now)
    if day == "Today":
        return f"today at {time_words(moment)}"
    if day == "Yesterday":
        return "yesterday"
    return f"on {day}"


def duration_words(seconds) -> str:
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return ""
    if seconds < 10:
        return f"{seconds:.1f} s"
    if seconds < 60:
        return f"{round(seconds)} s"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"


def plural(count: int, one: str, many: str | None = None) -> str:
    return f"{count} {one if count == 1 else (many or one + 's')}"


def ago(moment: datetime | None) -> str:
    if moment is None:
        return ""
    delta = datetime.now() - moment
    if delta < timedelta(minutes=1):
        return "just now"
    if delta < timedelta(hours=1):
        return f"{int(delta.total_seconds() // 60)} min ago"
    return when_words(moment)
