"""System appearance: presets, colour maths and the CSS generated from the saved settings.

Everything that reaches a template as CSS is built here from validated values only (hex colours checked by regex,
layout options checked against their choices), so the output is safe to embed. Nothing here touches secrets."""
import re
from types import SimpleNamespace

from django.db import DatabaseError

HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
COLOR_FIELDS = ("primary", "secondary", "accent", "background", "surface", "text", "border")

# Light-mode palettes. primary = buttons, links, active items; secondary = sidebar / dark chrome; accent = highlight marker.
PRESETS = {
    "graphite": {"label": "Graphite & Teal", "primary": "#0f766e", "secondary": "#14181d", "accent": "#8fd3cb",
                 "background": "#f4f5f6", "surface": "#ffffff", "text": "#1b2128", "border": "#dde1e6"},
    "midnight": {"label": "Midnight Blue", "primary": "#2c5f8a", "secondary": "#101a2b", "accent": "#9ab8d6",
                 "background": "#f3f5f8", "surface": "#ffffff", "text": "#17202c", "border": "#d9dee6"},
    "forest": {"label": "Forest", "primary": "#2f6b4f", "secondary": "#15201b", "accent": "#a5cdb8",
               "background": "#f3f5f3", "surface": "#ffffff", "text": "#1a221e", "border": "#dae0dc"},
    "plum": {"label": "Plum", "primary": "#6b4a7a", "secondary": "#1d1722", "accent": "#c9b3d4",
             "background": "#f6f4f7", "surface": "#ffffff", "text": "#211b26", "border": "#e0dbe4"},
    "bronze": {"label": "Stone & Bronze", "primary": "#8a5a2b", "secondary": "#1f1c19", "accent": "#d3b894",
               "background": "#f6f4f1", "surface": "#ffffff", "text": "#231f1b", "border": "#e2ddd6"},
}
DEFAULT_PRESET = "graphite"
DARK_NEUTRALS = {"background": "#12161b", "surface": "#1a2027", "text": "#e6e9ed", "border": "#2d3640"}
FONT_SCALE = {"small": ".85rem", "default": ".9rem", "large": ".98rem"}
FONT_FAMILY = {"inter": '"Inter", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif',
               "system": 'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif'}
SIDEBAR_WIDTH = {"expanded": "248px", "compact": "208px"}
DENSITY = {"comfortable": {"cell_y": ".65rem", "cell_x": "1rem", "card": "1rem", "content": "1.5rem"},
           "compact": {"cell_y": ".4rem", "cell_x": ".75rem", "card": ".75rem", "content": "1rem"}}

LAYOUT_DEFAULTS = {"sidebar_style": "dark", "sidebar_width": "expanded", "density": "comfortable", "font_scale": "default",
                   "font_family": "inter", "stat_columns": "4"}


# ------------------------------------------------------------------------------------------------ colour maths
def _rgb(color):
    return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))


def _hex(rgb):
    return "#%02x%02x%02x" % tuple(max(0, min(255, round(v))) for v in rgb)


def mix(a, b, amount):
    """`amount` of b mixed into a (0 = a, 1 = b)."""
    ra, rb = _rgb(a), _rgb(b)
    return _hex(tuple(x + (y - x) * amount for x, y in zip(ra, rb)))


def luminance(color):
    def channel(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(v) for v in _rgb(color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def clean_hex(value, fallback):
    return value.lower() if isinstance(value, str) and HEX.match(value) else fallback


# ------------------------------------------------------------------------------------------------- resolution
def resolve(values, dark=False):
    """Effective palette from submitted/saved values (blank or invalid colours fall back to the preset's)."""
    preset = PRESETS.get(values.get("theme_preset"), PRESETS[DEFAULT_PRESET])
    pal = {name: clean_hex(values.get(f"{name}_color"), preset[name]) for name in COLOR_FIELDS}
    if dark:  # custom neutrals belong to light mode; dark mode uses the matching dark palette
        pal.update(DARK_NEUTRALS)
    return pal


def derived(pal, dark=False):
    primary, surface, text, bg = pal["primary"], pal["surface"], pal["text"], pal["background"]
    return {
        "accent": primary, "accent-dark": mix(primary, "#000000", 0.18), "accent-soft": mix(primary, surface, 0.88 if not dark else 0.8),
        "highlight": pal["accent"], "ink": pal["secondary"], "ink-2": mix(pal["secondary"], "#ffffff", 0.07),
        "bg": bg, "card": surface, "text": text, "muted": mix(text, bg, 0.3), "border": pal["border"],
        "border-strong": mix(pal["border"], text, 0.22), "subtle": mix(surface, text, 0.04), "hover": mix(surface, primary, 0.07),
        "link": mix(primary, "#ffffff", 0.45) if dark else mix(primary, "#000000", 0.18),
    }


def _vars_block(pal, dark=False):
    d = derived(pal, dark)
    rgb = ", ".join(str(v) for v in _rgb(pal["primary"]))
    link_rgb = ", ".join(str(v) for v in _rgb(d["link"]))
    lines = [f"--gym-{k}: {v};" for k, v in d.items()]
    lines += [f"--bs-primary: {pal['primary']};", f"--bs-primary-rgb: {rgb};", f"--bs-link-color: {d['link']};", f"--bs-link-color-rgb: {link_rgb};",
              f"--bs-link-hover-color: {d['accent']};", f"--bs-body-color: {d['text']};", f"--bs-body-bg: {d['bg']};",
              f"--bs-border-color: {d['border']};", f"--bs-secondary-color: {d['muted']};", f"--bs-btn-focus-shadow-rgb: {rgb};"]
    return " ".join(lines)


def build_css(values, selector=":root", resolved_mode=None):
    """CSS for the given settings. `selector` is ':root' for the app and a container id for the live preview.
    `resolved_mode` forces light/dark (used by the preview); otherwise 'system' emits a prefers-color-scheme block."""
    mode = resolved_mode or values.get("mode") or "light"
    layout = {k: (values.get(k) if values.get(k) in allowed else LAYOUT_DEFAULTS[k]) for k, allowed in _ALLOWED.items()}
    dens = DENSITY[layout["density"]]
    light, dark = _vars_block(resolve(values)), _vars_block(resolve(values, dark=True), dark=True)
    common = (f"--sidebar-w: {SIDEBAR_WIDTH[layout['sidebar_width']]}; --bs-body-font-size: {FONT_SCALE[layout['font_scale']]}; "
              f"--bs-body-font-family: {FONT_FAMILY[layout['font_family']]}; --gym-cell-y: {dens['cell_y']}; --gym-cell-x: {dens['cell_x']}; "
              f"--gym-card-pad: {dens['card']}; --gym-content-pad: {dens['content']};")
    css = f"{selector} {{ {common} {dark if mode == 'dark' else light} }}"
    if mode == "system" and not resolved_mode:
        css += f"\n@media (prefers-color-scheme: dark) {{ {selector} {{ {dark} }} }}"
    return css


_ALLOWED = {
    "sidebar_style": ("dark", "light"), "sidebar_width": tuple(SIDEBAR_WIDTH), "density": tuple(DENSITY),
    "font_scale": tuple(FONT_SCALE), "font_family": tuple(FONT_FAMILY), "stat_columns": ("3", "4"),
}


# ---------------------------------------------------------------------------------------------------- loading
def values_of(obj):
    keys = ["theme_preset", "mode", "login_title", "login_message", *[f"{c}_color" for c in COLOR_FIELDS], *LAYOUT_DEFAULTS]
    return {k: getattr(obj, k, "") for k in keys}


def appearance():
    """The saved appearance row, or an unsaved default one. Never raises (e.g. before migrations have run)."""
    from .models import Appearance

    try:
        obj = Appearance.objects.filter(pk=1).first()
    except DatabaseError:
        obj = None
    return obj or Appearance(pk=1)


def template_context():
    obj = appearance()
    values = values_of(obj)
    mode = values["mode"] if values["mode"] in ("light", "dark", "system") else "light"
    layout = {k: (values[k] if values[k] in allowed else LAYOUT_DEFAULTS[k]) for k, allowed in _ALLOWED.items()}
    return SimpleNamespace(obj=obj, css=build_css(values), mode=mode, layout=layout)


def reset_theme_and_layout(obj):
    """Back to the default theme and layout. Branding (images, texts, name) is kept."""
    obj.theme_preset, obj.mode = DEFAULT_PRESET, "light"
    for c in COLOR_FIELDS:
        setattr(obj, f"{c}_color", "")
    for k, v in LAYOUT_DEFAULTS.items():
        setattr(obj, k, v)
    return obj
