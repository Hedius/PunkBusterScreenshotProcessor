"""
ESP overlay detection for PunkBuster screenshots.

AI generated.

Flags screenshots that most likely contain a cheat ESP overlay (player boxes,
corner brackets, skeletons, name/distance labels, health bars) so that admins
get a review queue instead of eyeballing every screenshot.

This module is deliberately standalone: nothing in the processing pipeline
imports it yet. Run it directly against the labelled sample sets:

    python esp_detection.py tune --ok samples_ok --esp samples_esp
    python esp_detection.py eval --ok samples_ok --esp samples_esp --out results
    python esp_detection.py scan screenshot.jpg

Design notes that matter when reading this file:

* PunkBuster downscales the whole screen to roughly 320x200, so ESP that is a
  couple of pixels wide simply does not survive the capture. Clients may return
  smaller captures still, so nothing here assumes a fixed size: `normalize_frame`
  keeps the frame's real resolution and `Geometry` scales every threshold to it.
* The two labelled sample sets are separable by RAW image dimensions alone
  (samples_ok is always 320x200, samples_esp is always 220 tall because it was
  cropped by the old static crop). `normalize_frame` trims that away - never
  extract features from a raw frame, and never feed dimensions in as a feature.
* Shape thresholds are not hand-picked. Several variants of each shape feature
  are emitted and a logistic regression fitted by `tune` decides what matters.
* Feed detectors the CROPPED game frame (`crop.crop` output). A raw PB PNG still
  carries the burned-in info text, which reads as overlay text.
* An all-black capture is reported as `capture_failed`, never scored: it is
  evidence of neither cheating nor innocence.
"""

import argparse
import csv
import hashlib
import json
import shutil
import sys
from importlib import resources
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

# Reference capture size. Frames are NOT resized to it - every pixel threshold
# is written against this size and scaled to whatever the real frame turns out
# to be (see Geometry).
REF_W, REF_H = 320, 200

MODEL_FILENAME = 'esp_model.json'


def _default_model_path():
    """
    Locate the fitted model as a package resource.

    importlib.resources keeps this working when image_processing is imported as
    an installed package rather than from the source tree; running this file
    directly as a script leaves __package__ empty, so fall back to __file__.
    """
    package = __package__ or (__spec__.parent if __spec__ else '')
    if package:
        try:
            return Path(str(resources.files(package).joinpath(MODEL_FILENAME)))
        except (ModuleNotFoundError, TypeError, NotADirectoryError):
            pass
    return Path(__file__).with_name(MODEL_FILENAME)


MODEL_PATH = _default_model_path()

# Saturated-overlay mask thresholds (HSV, OpenCV ranges: H 0-179, S/V 0-255).
SAT_MIN = 110
VAL_MIN = 130

# Palette histogram geometry.
HUE_BINS = 36
SAT_BINS = 8
SAT_HIST_MIN = 120
PALETTE_RARE_MASS = 0.01  # bins holding the rarest 1% of legit saturated pixels

# L2 penalty for the logistic baseline combiner (flat over 8-150 in a sweep).
L2_PENALTY = 60.0

# Gradient-boosted combiner defaults, chosen by the `compare` sweep: highest
# cross-validated recall subject to a 5% false-positive budget (95.0% recall at
# 4.99% FP). depth 5 was the only finalist that held its recall when re-scored on
# fresh fold shufflings, so the ranking is not a cross-validation lottery.
GBDT_DEFAULTS = {
    'trees': 400,
    'depth': 5,
    'lr': 0.05,
    'min_child': 20,
    'lam': 1.0,
    'bins': 64,
    'subsample': 0.8,
}

# A frame darker than this everywhere means PunkBuster captured nothing at all.
BLANK_MAX_VALUE = 24

IMAGE_SUFFIXES = ('.jpg', '.jpeg', '.png')

FEATURE_NAMES = (
    'sat_bright',       # saturated + bright pixels overall
    'pure_green',       # cheat health bars are near-pure green
    'green_run',        # ... and horizontally elongated
    'pure_red',
    'pure_cyan',
    'pure_magenta',
    'pure_yellow',
    'palette_outlier',  # saturated colour BF4's own HUD never uses
    'flat_color',       # overlays use flat colours, the HUD is anti-aliased
    'bar_loose',        # thin horizontal bars, permissive bounds
    'bar_tight',        # ... and strict bounds
    'hollow_box',       # box ESP
    'bracket',          # corner-bracket ESP
    'axis_line',        # long axis-aligned overlay lines
    'snapline',         # lines converging on the screen centre / bottom centre
    'thin_ratio',       # overlay made of thin strokes (skeletons, boxes)
    'text_row',         # small horizontal glyph runs in saturated colour
    'text_spread',      # ... scattered over several screen regions
    'sat_spread',       # saturated colour scattered over several regions
    'sat_blobs',        # number of distinct saturated components
    'menu_frame',       # scoreboard / menu capture (main false-positive source)
    'edge_density',     # scene complexity, used as context
    'tint_sat',         # night vision / thermal tint the whole frame
    'hue_focus',        # ... and collapse it onto one hue
    'stroke_frac',      # thin bright strokes of any colour (white skeleton ESP)
    'stroke_thin',      # ... how stroke-like they are
    'stroke_line',      # ... how many form straight segments
    'stroke_spread',    # ... scattered over several screen regions
    'center_frac',      # overlay mass at the screen centre (vehicle reticles)
    'edge_frac',        # overlay mass against the frame edge (BF4 chrome)
    'mirror_sym',       # overlay mirrors itself left-right (reticles do, ESP does not)
    'text_pitch',       # text rows at a regular vertical pitch (scoreboards)
    'text_align',       # ... sharing a left edge, i.e. columns
    'text_area',        # ... and how much of the frame they cover
)
PALETTE_INDEX = FEATURE_NAMES.index('palette_outlier')
N_FEATURES = len(FEATURE_NAMES)


@dataclass
class EspResult:
    """Outcome of scoring a single screenshot."""

    score: float                                    # 0-100, higher = more suspicious
    flagged: bool
    signals: dict = field(default_factory=dict)     # raw feature values by name
    reasons: list = field(default_factory=list)     # top contributing features
    capture_failed: bool = False                    # nothing rendered; score is meaningless
    model_version: str = None                       # content hash of the scoring model


# --------------------------------------------------------------------------- #
# Frame normalisation
# --------------------------------------------------------------------------- #

def _is_border(strip):
    """
    True for a letterbox bar or for the flat 1px frame line PunkBuster draws
    around the capture (the old crop kept that line, and a bright first row
    stops any naive dark-only scan dead).
    """
    if strip.size == 0:
        return False
    if strip.mean() < 8.0 and np.percentile(strip, 90) < 22.0:
        return True
    return bool(strip.std() < 3.0)


def _trim_black_borders(rgb):
    """
    Drop the letterbox/pillarbox bars left over by the old static crop.

    Scans inwards from each edge instead of testing every line independently: a
    high per-line percentile is defeated by JPEG ringing where bright overlay
    text sits next to the bar, which silently leaves an ~8% black band on every
    old-crop screenshot and turns "fraction of black pixels" into a label leak.
    """
    v = rgb.max(axis=2)
    top, bottom = 0, v.shape[0]
    while top < bottom and _is_border(v[top]):
        top += 1
    while bottom - 1 > top and _is_border(v[bottom - 1]):
        bottom -= 1
    left, right = 0, v.shape[1]
    while left < right and _is_border(v[top:bottom, left]):
        left += 1
    while right - 1 > left and _is_border(v[top:bottom, right - 1]):
        right -= 1
    if bottom - top < 16 or right - left < 16:
        return rgb  # essentially black frame - leave it alone
    return rgb[top:bottom, left:right]


def _inset(rgb, frac=0.015):
    """Shave a sliver off each edge so a leftover pixel of bar cannot survive."""
    dy = max(1, int(round(rgb.shape[0] * frac)))
    dx = max(1, int(round(rgb.shape[1] * frac)))
    if rgb.shape[0] - 2 * dy < 16 or rgb.shape[1] - 2 * dx < 16:
        return rgb
    return rgb[dy:-dy, dx:-dx]


def is_capture_failed(frame):
    """
    True when PunkBuster captured nothing: an essentially black frame.

    Such a frame is evidence of neither cheating nor innocence, so it is
    reported as a failed capture rather than scored - two of the labelled ESP
    samples are pure black and would otherwise sit in the false-negative pile
    forever, capping achievable recall for no reason.

    Keyed on brightness alone, deliberately. An earlier flatness test
    (``std < 2``) also swallowed thermal/infrared frames pointed at empty space:
    those are uniform mid-grey (mean ~60) but are perfectly real captures and
    must be scored. Genuine failures here run to mean <= 7 and max <= 23.
    """
    return bool(frame.max() < BLANK_MAX_VALUE)


def normalize_frame(image):
    """
    Strip the black bars and the PunkBuster frame line, and return the real
    game frame **at its own resolution**.

    Deliberately does not resize. Captures are not always REF_W x REF_H - a
    client can return a smaller screenshot - and rescaling a small capture up to
    a fixed size invents detail that was never transmitted. Scale invariance is
    handled instead by expressing every feature against the frame's own
    Geometry, which also keeps image dimensions out of the feature vector.
    """
    if isinstance(image, Image.Image):
        rgb = np.array(image.convert('RGB'))
    else:
        rgb = np.asarray(image)
        if rgb.ndim == 2:
            rgb = cv2.cvtColor(rgb, cv2.COLOR_GRAY2RGB)
    return np.ascontiguousarray(_inset(_trim_black_borders(rgb)))


# --------------------------------------------------------------------------- #
# Feature extraction
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Geometry:
    """
    The real size of the frame under test, and its scale against the reference.

    Captures are NOT always REF_W x REF_H - a client can hand PunkBuster a
    smaller screenshot. The cheat draws its overlay at screen resolution and
    PunkBuster downsamples the whole frame afterwards, so overlay features
    shrink in proportion to the capture width. Every pixel threshold below is
    therefore written for the reference capture and scaled to the real frame,
    and every area is a fraction of the real pixel count rather than of a
    constant. Nothing may resize the frame to a fixed size: upsampling a small
    capture invents detail that was never transmitted.
    """

    width: int
    height: int

    @property
    def pixels(self):
        return max(self.width * self.height, 1)

    @property
    def scale(self):
        return self.width / float(REF_W)

    def permille(self, count):
        """A pixel count as parts-per-thousand of this frame."""
        return float(count) / self.pixels * 1000.0

    def length(self, reference_px, minimum=1):
        """A length specified for the reference capture, in this frame's pixels."""
        return max(minimum, int(round(reference_px * self.scale)))

    def odd_length(self, reference_px, minimum=3):
        """Same, forced odd - OpenCV kernels demand it."""
        n = self.length(reference_px, minimum)
        return n if n % 2 else n + 1

    def area(self, reference_px, minimum=1):
        """An area specified for the reference capture, in this frame's pixels."""
        return max(minimum, int(round(reference_px * self.scale ** 2)))


def geometry_of(frame):
    return Geometry(int(frame.shape[1]), int(frame.shape[0]))


def _local_distinct(hsv, geo):
    """
    Pixels that stand out from their own surroundings.

    Absolute colour thresholds are useless on globally tinted frames: BF4's night
    vision and thermal optics paint the whole screen saturated green or orange,
    which lights up every "pure colour" mask (the top false positives) while an
    orange ESP skeleton on a thermal background lights up none of them (the worst
    false negative). Comparing each pixel against a median-blurred background
    estimate makes the masks care about local contrast instead of absolute hue.
    """
    ksize = geo.odd_length(15, minimum=3)
    hue = np.ascontiguousarray(hsv[:, :, 0])
    sat = np.ascontiguousarray(hsv[:, :, 1])
    val = np.ascontiguousarray(hsv[:, :, 2])
    hue_bg = cv2.medianBlur(hue, ksize).astype(np.int16)
    sat_bg = cv2.medianBlur(sat, ksize).astype(np.int16)
    val_bg = cv2.medianBlur(val, ksize).astype(np.int16)
    hue_delta = np.abs(hue.astype(np.int16) - hue_bg)
    hue_delta = np.minimum(hue_delta, 180 - hue_delta)
    val_delta = val.astype(np.int16) - val_bg
    distinct = ((val_delta > 35)
                | (sat.astype(np.int16) - sat_bg > 45)
                | (hue_delta > 12))
    return distinct, val_delta


def _stroke_mask(val_delta):
    """
    Thin bright strokes regardless of colour.

    Every saturation-gated mask is blind to white and grey overlays, which is
    exactly how white skeleton ESP slips through: S is near zero, so the pixels
    never enter sat_mask. This keys on local brightness contrast alone.
    """
    return val_delta > 45


def _color_masks(rgb, distinct):
    """
    Near-pure overlay colour masks, restricted to locally distinct pixels.

    Cheats draw flat primaries; BF4 anti-aliases and glows. ``distinct`` keeps a
    uniformly green night-vision frame from reading as one giant health bar.
    Thresholds here are intensities, not sizes, so they do not scale.
    """
    r = rgb[:, :, 0].astype(np.int16)
    g = rgb[:, :, 1].astype(np.int16)
    b = rgb[:, :, 2].astype(np.int16)
    gap = 60
    return {
        'pure_green': (g > 140) & (r < g - gap) & (b < g - gap) & distinct,
        'pure_red': (r > 140) & (g < r - gap) & (b < r - gap) & distinct,
        'pure_cyan': (g > 130) & (b > 130) & (r < np.minimum(g, b) - gap) & distinct,
        'pure_magenta': (r > 130) & (b > 130) & (g < np.minimum(r, b) - gap) & distinct,
        'pure_yellow': (r > 140) & (g > 140) & (b < np.minimum(r, g) - gap) & distinct,
    }


def _tint_features(hsv, geo):
    """
    How globally tinted the frame is, so the combiner can discount colour
    evidence on night-vision and thermal captures instead of being fooled by it.
    """
    sat = hsv[:, :, 1]
    hue = hsv[:, :, 0][sat >= 60]
    if hue.size == 0:
        return 0.0, 0.0
    counts = np.bincount(hue, minlength=180).astype(np.float64)
    dominant = int(np.argmax(np.convolve(counts, np.ones(11), mode='same')))
    delta = np.abs(hue.astype(np.int16) - dominant)
    delta = np.minimum(delta, 180 - delta)
    focus = float((delta <= 10).mean() * (hue.size / geo.pixels) * 100.0)
    return float(np.median(sat)), focus


def _components(mask, min_area=3):
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), 8)
    out = []
    for i in range(1, n):
        x, y, w, h, area = (int(v) for v in stats[i][:5])
        if area >= min_area:
            out.append((i, x, y, w, h, area, labels))
    return out


def _bar_boxes(mask, geo, max_h, min_w, min_aspect):
    """Thin horizontal bars, i.e. the health bars drawn above ESP boxes."""
    max_h = geo.length(max_h)
    min_w = geo.length(min_w, minimum=2)
    boxes = []
    for _, x, y, w, h, area, _ in _components(mask, min_area=geo.area(4)):
        if h <= max_h and w >= min_w and w / max(h, 1) >= min_aspect:
            if area / float(w * h) >= 0.5:
                boxes.append((x, y, w, h))
    return boxes


def _hollow_boxes(mask, geo):
    """Rectangles with an empty interior, i.e. classic 2D box ESP."""
    lo, hi = geo.length(7, minimum=4), geo.length(110)
    inset = geo.length(2)
    boxes = []
    for i, x, y, w, h, area, labels in _components(mask, min_area=geo.area(10)):
        if not (lo <= w <= hi and lo <= h <= hi):
            continue
        if area / float(w * h) >= 0.45:
            continue
        sub = labels[y:y + h, x:x + w] == i
        interior = sub[inset:-inset, inset:-inset] if min(w, h) > 2 * inset else sub[:0, :0]
        if interior.size and interior.mean() > 0.12:
            continue
        ring = float(sub.sum() - (interior.sum() if interior.size else 0))
        if ring / max(2.0 * (w + h), 1.0) >= 0.45:
            boxes.append((x, y, w, h))
    return boxes


def _brackets(mask, geo):
    """Corner-bracket ESP, where only the corners of the box are drawn."""
    lo, hi = geo.length(3, minimum=2), geo.length(22, minimum=6)
    inset = geo.length(2)
    boxes = []
    for i, x, y, w, h, area, labels in _components(mask, min_area=geo.area(5)):
        if not (lo <= w <= hi and lo <= h <= hi):
            continue
        if area / float(w * h) >= 0.6:
            continue
        sub = labels[y:y + h, x:x + w] == i
        border = sub.copy()
        if min(w, h) > 2 * inset:
            border[inset:-inset, inset:-inset] = False
        if sub.sum() and border.sum() / float(sub.sum()) >= 0.85:
            boxes.append((x, y, w, h))
    return boxes


def _lines(mask, geo):
    dil = cv2.dilate(mask.astype(np.uint8), np.ones((2, 2), np.uint8))
    found = cv2.HoughLinesP(dil * 255, 1, np.pi / 180,
                            threshold=geo.length(18, minimum=6),
                            minLineLength=geo.length(12, minimum=5),
                            maxLineGap=geo.length(3))
    if found is None:
        return np.zeros((0, 4), dtype=np.int32)
    return np.asarray(found).reshape(-1, 4)


def _snapline_count(lines, geo):
    """Lines whose extension passes near the screen centre or bottom centre."""
    if len(lines) == 0:
        return 0
    cx, cy = geo.width / 2.0, geo.height / 2.0
    origins = ((cx, cy), (cx, float(geo.height)))
    min_length = geo.length(20, minimum=8)
    tolerance = float(geo.length(6, minimum=2))
    count = 0
    for x1, y1, x2, y2 in lines:
        dx, dy = float(x2 - x1), float(y2 - y1)
        length = float(np.hypot(dx, dy))
        if length < min_length:
            continue
        for ox, oy in origins:
            if abs(dy * (ox - x1) - dx * (oy - y1)) / length < tolerance:
                count += 1
                break
    return count


def _text_rows(mask, geo):
    """Small horizontal glyph runs, i.e. ESP name and distance labels."""
    kernel = np.ones((1, geo.length(5, minimum=2)), np.uint8)
    closed = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_CLOSE, kernel)
    h_lo, h_hi = geo.length(2), geo.length(9, minimum=3)
    w_min = geo.length(10, minimum=4)
    boxes = []
    for _, x, y, w, h, area, _ in _components(closed.astype(bool), min_area=geo.area(8)):
        if (h_lo <= h <= h_hi and w >= w_min and w / max(h, 1) >= 2.5
                and area / float(w * h) < 0.85):
            boxes.append((x, y, w, h))
    return boxes


def _position_features(mask, geo):
    """
    Where the overlay sits, and whether it mirrors itself.

    Vehicle and aircraft reticles are centred and left-right symmetric about the
    screen axis; ESP is drawn wherever players happen to stand and is not. BF4
    also parks its own chrome (ammo, minimap, killfeed) against the frame edges.
    """
    total = float(mask.sum())
    if total == 0:
        return 0.0, 0.0, 0.0
    ys, xs = np.nonzero(mask)
    cx, cy = geo.width / 2.0, geo.height / 2.0
    radius = 0.18 * geo.height
    center = float((((xs - cx) ** 2 + (ys - cy) ** 2) <= radius ** 2).sum()) / total * 100.0
    mx, my = int(round(geo.width * 0.10)), int(round(geo.height * 0.15))
    on_edge = ((xs < mx) | (xs >= geo.width - mx) | (ys < my) | (ys >= geo.height - my))
    edge = float(on_edge.sum()) / total * 100.0
    pad = geo.length(2)
    tolerant = cv2.dilate(mask.astype(np.uint8),
                          np.ones((2 * pad + 1, 2 * pad + 1), np.uint8)).astype(bool)
    symmetry = float((mask & tolerant[:, ::-1]).sum()) / total * 100.0
    return center, edge, symmetry


def _text_layout_features(text_boxes, geo):
    """
    Scoreboards and squad lists stack many text rows at a regular pitch in
    aligned columns. ESP labels follow players around and do neither, so row
    regularity separates a menu capture from an overlay far better than the
    blackness test does - those tables sit on dark grey, not on black.
    """
    if len(text_boxes) < 4:
        return 0.0, 0.0, geo.permille(sum(b[2] * b[3] for b in text_boxes))
    boxes = sorted(text_boxes, key=lambda b: b[1])
    centers = np.array([b[1] + b[3] / 2.0 for b in boxes], dtype=float)
    gaps = np.diff(centers)
    gaps = gaps[gaps > 0]
    pitch = 0.0
    if len(gaps) >= 3:
        pitch = max(0.0, 1.0 - float(np.std(gaps)) / max(float(np.mean(gaps)), 1e-6)) * 100.0
    lefts = np.array([b[0] for b in boxes], dtype=float)
    tol = geo.length(6, minimum=2)
    align = max(int(np.sum(np.abs(lefts - v) <= tol)) for v in lefts) / len(lefts) * 100.0
    area = geo.permille(sum(b[2] * b[3] for b in text_boxes))
    return pitch, align, area


def _grid_spread(mask, geo, cells=4, min_px=3):
    """How many cells of a cells-by-cells grid contain overlay pixels."""
    threshold = geo.area(min_px)
    ys = np.linspace(0, geo.height, cells + 1).astype(int)
    xs = np.linspace(0, geo.width, cells + 1).astype(int)
    hit = 0
    for i in range(cells):
        for j in range(cells):
            if mask[ys[i]:ys[i + 1], xs[j]:xs[j + 1]].sum() >= threshold:
                hit += 1
    return hit


def _flat_color_score(rgb, sat_mask, geo):
    """Share of the frame taken by the few most common exact overlay colours."""
    if sat_mask.sum() == 0:
        return 0.0
    q = (rgb[sat_mask] >> 4).astype(np.int32)
    counts = np.bincount(q[:, 0] * 256 + q[:, 1] * 16 + q[:, 2])
    return geo.permille(np.sort(counts)[-3:].sum())


def _sat_histogram(hsv, sat_mask):
    """Hue/saturation histogram of the saturated pixels, for the palette model."""
    hist = np.zeros(HUE_BINS * SAT_BINS, dtype=np.int32)
    if sat_mask.sum() == 0:
        return hist
    h = hsv[:, :, 0][sat_mask].astype(np.int32)
    s = hsv[:, :, 1][sat_mask].astype(np.int32)
    hi = np.clip(h * HUE_BINS // 180, 0, HUE_BINS - 1)
    si = np.clip((s - SAT_HIST_MIN) * SAT_BINS // (256 - SAT_HIST_MIN), 0, SAT_BINS - 1)
    np.add.at(hist, hi * SAT_BINS + si, 1)
    return hist


def frame_features(frame, debug=False):
    """
    Extract the raw feature vector from a normalised frame of any size.

    Returns (features, sat_histogram, shapes, geometry). features[PALETTE_INDEX]
    is a placeholder that :func:`apply_palette` fills in once the palette model
    is known, which lets the whole corpus be decoded exactly once.
    """
    geo = geometry_of(frame)
    hsv = cv2.cvtColor(frame, cv2.COLOR_RGB2HSV)
    gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
    distinct, val_delta = _local_distinct(hsv, geo)
    sat_mask = (hsv[:, :, 1] >= SAT_MIN) & (hsv[:, :, 2] >= VAL_MIN) & distinct
    stroke = _stroke_mask(val_delta)
    stroke_px = int(stroke.sum())
    if stroke_px:
        stroke_dist = cv2.distanceTransform(stroke.astype(np.uint8), cv2.DIST_L2, 3)
        stroke_thin = float((stroke_dist[stroke] < 1.8).sum()) / stroke_px * 100.0
    else:
        stroke_thin = 0.0
    stroke_lines = len(_lines(stroke, geo))
    colors = _color_masks(frame, distinct)
    tint_sat, hue_focus = _tint_features(hsv, geo)

    green_run = cv2.erode(colors['pure_green'].astype(np.uint8),
                          np.ones((1, geo.length(5, minimum=2)), np.uint8))
    bars_loose = _bar_boxes(sat_mask, geo, max_h=4, min_w=4, min_aspect=2.5)
    bars_tight = _bar_boxes(sat_mask, geo, max_h=2, min_w=6, min_aspect=4.0)
    boxes = _hollow_boxes(sat_mask, geo)
    brackets = _brackets(sat_mask, geo)
    lines = _lines(sat_mask, geo)
    axis_lines = sum(1 for x1, y1, x2, y2 in lines
                     if abs(int(x1) - int(x2)) <= 1 or abs(int(y1) - int(y2)) <= 1)
    text = _text_rows(sat_mask, geo)

    sat_px = int(sat_mask.sum())
    if sat_px:
        dist = cv2.distanceTransform(sat_mask.astype(np.uint8), cv2.DIST_L2, 3)
        thin_ratio = float((dist[sat_mask] < 1.8).sum()) / sat_px * 100.0
    else:
        thin_ratio = 0.0

    center_frac, edge_frac, mirror_sym = _position_features(sat_mask, geo)
    text_pitch, text_align, text_area = _text_layout_features(text, geo)

    text_mask = np.zeros_like(sat_mask)
    for x, y, w, h in text:
        text_mask[y:y + h, x:x + w] = True

    values = {
        'sat_bright': geo.permille(sat_px),
        'pure_green': geo.permille(colors['pure_green'].sum()),
        'green_run': geo.permille(green_run.sum()),
        'pure_red': geo.permille(colors['pure_red'].sum()),
        'pure_cyan': geo.permille(colors['pure_cyan'].sum()),
        'pure_magenta': geo.permille(colors['pure_magenta'].sum()),
        'pure_yellow': geo.permille(colors['pure_yellow'].sum()),
        'palette_outlier': 0.0,
        'flat_color': _flat_color_score(frame, sat_mask, geo),
        'bar_loose': float(len(bars_loose)),
        'bar_tight': float(len(bars_tight)),
        'hollow_box': float(len(boxes)),
        'bracket': float(len(brackets)),
        'axis_line': float(axis_lines),
        'snapline': float(_snapline_count(lines, geo)),
        'thin_ratio': thin_ratio,
        'text_row': float(len(text)),
        'text_spread': float(_grid_spread(text_mask, geo, cells=4, min_px=1)),
        'sat_spread': float(_grid_spread(sat_mask, geo, cells=4, min_px=3)),
        'sat_blobs': float(len(_components(sat_mask, min_area=geo.area(3)))),
        'menu_frame': geo.permille((frame.max(axis=2) < 20).sum()),
        'edge_density': geo.permille((cv2.Canny(gray, 80, 200) > 0).sum()),
        'tint_sat': tint_sat,
        'hue_focus': hue_focus,
        'stroke_frac': geo.permille(stroke_px),
        'stroke_thin': stroke_thin,
        'stroke_line': float(stroke_lines),
        'stroke_spread': float(_grid_spread(stroke, geo, cells=4, min_px=3)),
        'center_frac': center_frac,
        'edge_frac': edge_frac,
        'mirror_sym': mirror_sym,
        'text_pitch': text_pitch,
        'text_align': text_align,
        'text_area': text_area,
    }
    feats = np.array([values[name] for name in FEATURE_NAMES], dtype=np.float64)

    shapes = None
    if debug:
        shapes = ([('bar', b) for b in bars_loose]
                  + [('box', b) for b in boxes]
                  + [('bracket', b) for b in brackets]
                  + [('text', b) for b in text])
    return feats, _sat_histogram(hsv, sat_mask), shapes, geo


def apply_palette(feats, hist, rare_mask, pixels):
    """Fill in the palette-outlier feature from a precomputed histogram."""
    feats = np.asarray(feats, dtype=np.float64).copy()
    rare_px = int((np.asarray(hist) * rare_mask).sum())
    feats[PALETTE_INDEX] = float(rare_px) / max(int(pixels), 1) * 1000.0
    return feats


# --------------------------------------------------------------------------- #
# Palette model and combiner
# --------------------------------------------------------------------------- #

def fit_palette(hists):
    """
    Learn which (hue, saturation) bins BF4's own HUD uses.

    Bins holding the rarest PALETTE_RARE_MASS of legit saturated pixels are
    marked rare; overlay colour landing there is evidence of a foreign overlay.
    """
    total = np.sum(np.asarray(hists, dtype=np.float64), axis=0)
    if total.sum() <= 0:
        return np.zeros(HUE_BINS * SAT_BINS, dtype=np.float64)
    prob = total / total.sum()
    order = np.argsort(prob)
    cumulative = np.cumsum(prob[order])
    rare = np.zeros_like(prob)
    rare[order[cumulative <= PALETTE_RARE_MASS]] = 1.0
    return rare


def _transform(x):
    """Heavy-tailed counts compress far better on a log scale."""
    return np.log1p(np.maximum(np.asarray(x, dtype=np.float64), 0.0))


def fit_logreg(x, y, l2=L2_PENALTY, iters=60):
    """
    Class-weighted L2 logistic regression via IRLS.

    Kept as the baseline combiner (``--combiner logistic``). It is beaten badly
    by the tree ensemble here because the evidence is conditional - colour only
    counts when the frame is not globally tinted, text only counts when it is
    not a regular table - and a linear model cannot express that. See
    :func:`fit_gbdt`.
    """
    n, d = x.shape
    xb = np.hstack([x, np.ones((n, 1))])
    n_pos = max(float(y.sum()), 1.0)
    n_neg = max(float((1 - y).sum()), 1.0)
    sw = np.where(y == 1, n / (2 * n_pos), n / (2 * n_neg))
    theta = np.zeros(d + 1)
    reg = np.eye(d + 1) * l2
    reg[d, d] = 0.0  # never regularise the intercept
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-np.clip(xb @ theta, -30, 30)))
        w = sw * np.clip(p * (1 - p), 1e-6, None)
        grad = xb.T @ (sw * (y - p)) - reg @ theta
        hess = xb.T @ (xb * w[:, None]) + reg
        try:
            step = np.linalg.solve(hess, grad)
        except np.linalg.LinAlgError:
            break
        theta += step
        if np.max(np.abs(step)) < 1e-8:
            break
    return theta[:d], float(theta[d])


# --------------------------------------------------------------------------- #
# Gradient-boosted trees - the combiner that is actually used
# --------------------------------------------------------------------------- #

def _quantile_bins(x, n_bins):
    return [np.unique(np.quantile(x[:, j], np.linspace(0, 1, n_bins + 1)[1:-1]))
            for j in range(x.shape[1])]


def _digitize(x, edges):
    out = np.zeros(x.shape, dtype=np.int16)
    for j in range(x.shape[1]):
        out[:, j] = np.searchsorted(edges[j], x[:, j], side='left')
    return out


def _build_tree(xb, grad, hess, rows, depth, min_child, lam, n_bins):
    """One regression tree on the gradient/hessian of the logistic loss."""
    g_sum, h_sum = grad[rows].sum(), hess[rows].sum()
    leaf = {'v': float(-g_sum / (h_sum + lam))}
    if depth == 0 or len(rows) < 2 * min_child:
        return leaf
    best = None
    for j in range(xb.shape[1]):
        col = xb[rows, j]
        gl = np.cumsum(np.bincount(col, weights=grad[rows], minlength=n_bins + 1))
        hl = np.cumsum(np.bincount(col, weights=hess[rows], minlength=n_bins + 1))
        cl = np.cumsum(np.bincount(col, minlength=n_bins + 1))
        gr, hr, cr = g_sum - gl, h_sum - hl, len(rows) - cl
        ok = (cl >= min_child) & (cr >= min_child)
        if not ok.any():
            continue
        gain = (gl ** 2 / (hl + lam) + gr ** 2 / (hr + lam)
                - g_sum ** 2 / (h_sum + lam)) / 2.0
        gain = np.where(ok, gain, -np.inf)
        b = int(np.argmax(gain))
        if best is None or gain[b] > best[0]:
            best = (float(gain[b]), j, b)
    if best is None or best[0] <= 0.0:
        return leaf
    _, j, b = best
    mask = xb[rows, j] <= b
    left, right = rows[mask], rows[~mask]
    if len(left) < min_child or len(right) < min_child:
        return leaf
    return {
        'f': int(j), 'b': int(b),
        'l': _build_tree(xb, grad, hess, left, depth - 1, min_child, lam, n_bins),
        'r': _build_tree(xb, grad, hess, right, depth - 1, min_child, lam, n_bins),
    }


def _predict_tree(xb, node):
    if 'v' in node:
        return np.full(xb.shape[0], node['v'])
    out = np.empty(xb.shape[0])
    mask = xb[:, node['f']] <= node['b']
    if mask.any():
        out[mask] = _predict_tree(xb[mask], node['l'])
    if (~mask).any():
        out[~mask] = _predict_tree(xb[~mask], node['r'])
    return out


def fit_gbdt(x, y, seed=0, params=None):
    """
    Class-weighted gradient boosting over the named CV features.

    Chosen over logistic regression on measured cross-validated results: at 90%
    recall it cut the false-positive rate from 11.8% to 2.6% on this corpus,
    because most of the remaining confusions are conditional rather than
    additive (a night-vision frame is uniformly green, a scoreboard is text at a
    regular pitch, a jet reticle is centred and mirror-symmetric). Every input
    is still a named, inspectable CV feature.
    """
    cfg = dict(GBDT_DEFAULTS, **(params or {}))
    rng = np.random.default_rng(seed)
    edges = _quantile_bins(x, cfg['bins'])
    xb = _digitize(x, edges)
    weight = np.where(y == 1, len(y) / (2 * max(float(y.sum()), 1.0)),
                      len(y) / (2 * max(float((1 - y).sum()), 1.0)))
    pred = np.zeros(len(y))
    trees = []
    n_sub = max(int(cfg['subsample'] * len(y)), 2 * cfg['min_child'])
    for _ in range(cfg['trees']):
        p = 1.0 / (1.0 + np.exp(-np.clip(pred, -30, 30)))
        grad = weight * (p - y)
        hess = weight * np.clip(p * (1 - p), 1e-6, None)
        rows = np.sort(rng.choice(len(y), n_sub, replace=False))
        tree = _build_tree(xb, grad, hess, rows, cfg['depth'], cfg['min_child'],
                           cfg['lam'], cfg['bins'])
        trees.append(tree)
        pred += cfg['lr'] * _predict_tree(xb, tree)
    return {
        'kind': 'gbdt',
        'edges': [e.tolist() for e in edges],
        'trees': trees,
        'lr': cfg['lr'],
        'params': cfg,
        'medians': np.median(x, axis=0).tolist(),
    }


def fit_combiner(x, y, kind='gbdt', seed=0, params=None):
    if kind == 'gbdt':
        return fit_gbdt(x, y, seed=seed, params=params)
    mu, sigma = _standardize(x)
    weights, bias = fit_logreg((x - mu) / sigma, y)
    return {'kind': 'logistic', 'weights': weights.tolist(), 'bias': bias,
            'mu': mu.tolist(), 'sigma': sigma.tolist(),
            'medians': np.median(x, axis=0).tolist()}


def score_with(combiner, x):
    """Score already-transformed feature rows, returning 0-100."""
    x = np.atleast_2d(np.asarray(x, dtype=np.float64))
    if combiner['kind'] == 'gbdt':
        edges = [np.asarray(e) for e in combiner['edges']]
        xb = _digitize(x, edges)
        raw = np.zeros(x.shape[0])
        for tree in combiner['trees']:
            raw += combiner['lr'] * _predict_tree(xb, tree)
    else:
        mu = np.asarray(combiner['mu'])
        sigma = np.asarray(combiner['sigma'])
        raw = ((x - mu) / sigma) @ np.asarray(combiner['weights']) + combiner['bias']
    return 100.0 / (1.0 + np.exp(-np.clip(raw, -30, 30)))


class EspModel:
    """Fitted palette + combiner, persisted next to this module as JSON."""

    def __init__(self, rare_mask, combiner, threshold, meta=None):
        self.rare_mask = np.asarray(rare_mask, dtype=np.float64)
        self.combiner = combiner
        self.threshold = float(threshold)
        self.meta = meta or {}
        self.version = None  # set by load() from the model file's content hash

    def score(self, feats):
        """Score raw (untransformed) feature rows."""
        return score_with(self.combiner, _transform(feats))

    def score_vector(self, feats):
        return float(self.score(np.asarray(feats)[None, :])[0])

    def explain(self, feats, top=4):
        """
        Which features pushed this score up.

        A tree ensemble has no per-feature weight, so this measures the drop in
        score when each feature alone is replaced by its training median.
        """
        feats = np.asarray(feats, dtype=np.float64)
        base = self.score_vector(feats)
        medians = np.asarray(self.combiner['medians'])
        probe = np.repeat(_transform(feats)[None, :], len(FEATURE_NAMES), axis=0)
        for j in range(len(FEATURE_NAMES)):
            probe[j, j] = medians[j]
        drops = base - score_with(self.combiner, probe)
        order = np.argsort(drops)[::-1][:top]
        return [FEATURE_NAMES[j] for j in order if drops[j] > 1.0]

    def save(self, path):
        payload = {
            'features': list(FEATURE_NAMES),
            'rare_mask': self.rare_mask.tolist(),
            'combiner': self.combiner,
            'threshold': self.threshold,
            'meta': self.meta,
        }
        Path(path).write_text(json.dumps(payload), encoding='utf-8')

    @classmethod
    def load(cls, path=MODEL_PATH):
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(
                f'No ESP model at {path}. Run: python {Path(__file__).name} tune '
                f'--ok samples_ok --esp samples_esp')
        raw = path.read_bytes()
        d = json.loads(raw.decode('utf-8'))
        if tuple(d.get('features', ())) != FEATURE_NAMES:
            raise ValueError(f'Model at {path} was fitted for a different feature set.')
        model = cls(d['rare_mask'], d['combiner'], d['threshold'], d.get('meta'))
        # Content hash of the model file. Stored alongside every score so that a
        # score from a retuned model is never silently compared against an older
        # one - `tune` overwrites this file in place, so the file path alone
        # identifies nothing.
        model.version = hashlib.sha256(raw).hexdigest()[:12]
        return model


# --------------------------------------------------------------------------- #
# Public API (not wired into the pipeline yet)
# --------------------------------------------------------------------------- #

def detect(image, model=None, threshold=None, explain=True):
    """
    Score a single screenshot for ESP overlays.

    ``image`` must be the **cropped game frame**, i.e. the output of
    ``crop.crop()``. Handing in a raw PunkBuster PNG pulls the burned-in info
    text into the frame and inflates the score badly (27.6 vs 9.4 on
    pb000001.png), because that text is exactly the bright flat glyph structure
    the text features look for.

    Pass the frame *before* JPEG compression - compression artifacts cost recall.

    ``explain`` builds the ``reasons`` list, which costs one ensemble evaluation
    per feature. In the collection pipeline the reasons are only ever printed for
    flagged screenshots, and the stream is >95% clean, so it pays to leave this
    off and explain afterwards (see :meth:`EspModel.explain`).
    """
    model = model or EspModel.load()
    frame = normalize_frame(image)
    if is_capture_failed(frame):
        return EspResult(score=0.0, flagged=False, signals={},
                         reasons=['capture_failed'], capture_failed=True,
                         model_version=model.version)
    feats, hist, _, geo = frame_features(frame)
    feats = apply_palette(feats, hist, model.rare_mask, geo.pixels)
    score = model.score_vector(feats)
    thr = model.threshold if threshold is None else float(threshold)
    flagged = score >= thr

    signals = {name: float(feats[i]) for i, name in enumerate(FEATURE_NAMES)}
    reasons = model.explain(feats) if (explain or flagged) else []
    return EspResult(score=score, flagged=flagged, signals=signals, reasons=reasons,
                     model_version=model.version)


# --------------------------------------------------------------------------- #
# Corpus handling
# --------------------------------------------------------------------------- #

def list_images(directory):
    return sorted(p for p in Path(directory).iterdir()
                  if p.suffix.lower() in IMAGE_SUFFIXES)


def _is_palette_split(path):
    """Stable 35% split of the clean set, reserved for fitting the palette."""
    digest = hashlib.md5(path.name.encode('utf-8')).hexdigest()
    return int(digest[:8], 16) % 100 < 35


def _load_one(path):
    with Image.open(path) as img:
        raw_size = img.size
        frame = normalize_frame(img)
    feats, hist, _, geo = frame_features(frame)
    return feats, hist, raw_size, is_capture_failed(frame), (geo.width, geo.height)


def extract_corpus(paths, jobs=8, label=''):
    """Decode and featurise every image once. OpenCV releases the GIL, so threads help."""
    feats = np.zeros((len(paths), N_FEATURES))
    hists = np.zeros((len(paths), HUE_BINS * SAT_BINS), dtype=np.int32)
    sizes = np.zeros((len(paths), 2), dtype=np.int32)
    blank = np.zeros(len(paths), dtype=bool)
    norm_sizes = np.zeros((len(paths), 2), dtype=np.int32)
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        for i, (f, h, s, is_blank, norm) in enumerate(pool.map(_load_one, paths)):
            feats[i], hists[i], sizes[i], blank[i], norm_sizes[i] = f, h, s, is_blank, norm
            done += 1
            if done % 500 == 0:
                print(f'  {label}{done}/{len(paths)}', flush=True)
    return feats, hists, sizes, blank, norm_sizes


def _cache_key(ok_paths, esp_paths):
    joined = '\n'.join(str(p) for p in list(ok_paths) + list(esp_paths))
    return hashlib.md5(joined.encode('utf-8')).hexdigest()


def build_corpus(ok_dir, esp_dir, jobs, cache=None, limit=0):
    ok_paths = list_images(ok_dir)
    esp_paths = list_images(esp_dir)
    if limit:
        ok_paths, esp_paths = ok_paths[:limit], esp_paths[:limit]
    key = _cache_key(ok_paths, esp_paths)

    if cache and Path(cache).exists():
        blob = np.load(cache, allow_pickle=False)
        if str(blob['key']) == key:
            print(f'Loaded cached features from {cache}')
            return (ok_paths, esp_paths, blob['feats'], blob['hists'], blob['sizes'],
                    blob['labels'], blob['blank'], blob['norm_sizes'])

    paths = ok_paths + esp_paths
    print(f'Extracting features from {len(paths)} images '
          f'({len(ok_paths)} clean, {len(esp_paths)} esp)...', flush=True)
    feats, hists, sizes, blank, norm_sizes = extract_corpus(paths, jobs=jobs)
    labels = np.concatenate([np.zeros(len(ok_paths)), np.ones(len(esp_paths))])
    if cache:
        Path(cache).parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(cache, key=key, feats=feats, hists=hists, sizes=sizes,
                            labels=labels, blank=blank, norm_sizes=norm_sizes)
    return ok_paths, esp_paths, feats, hists, sizes, labels, blank, norm_sizes


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #

def _rankdata(a):
    a = np.asarray(a, dtype=np.float64)
    order = np.argsort(a, kind='mergesort')
    ranks = np.empty(len(a), dtype=np.float64)
    sorted_a = a[order]
    i = 0
    while i < len(a):
        j = i
        while j + 1 < len(a) and sorted_a[j + 1] == sorted_a[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return ranks


def auc(pos, neg):
    """Area under the ROC curve, tie-aware."""
    pos, neg = np.asarray(pos, dtype=np.float64), np.asarray(neg, dtype=np.float64)
    if pos.size == 0 or neg.size == 0:
        return float('nan')
    ranks = _rankdata(np.concatenate([pos, neg]))
    return float((ranks[:pos.size].sum() - pos.size * (pos.size + 1) / 2.0)
                 / (pos.size * neg.size))


def _standardize(x):
    mu = x.mean(axis=0)
    sigma = x.std(axis=0)
    sigma[sigma < 1e-6] = 1.0
    return mu, sigma


def cross_val_scores(x, y, folds=5, seed=0, kind='gbdt', params=None):
    """Out-of-fold scores. With only ~180 positives, in-sample numbers are fiction."""
    rng = np.random.default_rng(seed)
    fold_of = np.zeros(len(y), dtype=int)
    for cls in (0, 1):
        idx = np.where(y == cls)[0]
        rng.shuffle(idx)
        fold_of[idx] = np.arange(len(idx)) % folds
    oof = np.zeros(len(y))
    xt = _transform(x)
    for k in range(folds):
        tr, te = fold_of != k, fold_of == k
        combiner = fit_combiner(xt[tr], y[tr], kind=kind, seed=seed, params=params)
        oof[te] = score_with(combiner, xt[te])
    return oof


def repeated_cross_val_scores(x, y, folds=5, repeats=3, kind='gbdt', params=None,
                              seed0=0):
    """
    Average out-of-fold scores over several fold shufflings.

    A single 5-fold split of ~180 positives puts the 90%-recall threshold on a
    steep part of the curve, where it swings by several points of false-positive
    rate between seeds. Averaging repeats makes the reported operating point
    something you can actually pick from.
    """
    return np.mean([cross_val_scores(x, y, folds=folds, seed=seed, kind=kind,
                                     params=params)
                    for seed in range(seed0, seed0 + repeats)], axis=0)


def threshold_for_recall(pos_scores, target):
    """Lowest score still catching ``target`` of the positives."""
    pos = np.sort(np.asarray(pos_scores))
    if pos.size == 0:
        return 100.0
    idx = int(np.floor((1.0 - target) * pos.size))
    return float(pos[min(idx, pos.size - 1)])


def threshold_for_fp(neg_scores, target):
    """
    Lowest threshold whose *actual* false-positive rate stays within ``target``.

    Not a quantile: features like green_run are zero for most clean frames, and
    a quantile lands exactly on that tie, so "flag everything" gets reported as
    if it hit the FP budget. Step past the tie instead.
    """
    neg = np.sort(np.asarray(neg_scores))[::-1]
    if neg.size == 0:
        return 0.0
    k = int(np.floor(target * neg.size))
    if k >= neg.size:
        return float(neg[-1])
    return float(np.nextafter(neg[k], np.inf))


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

def _report(x, y, oof, sizes, norm_sizes, ok_paths, target_recall, target_fp=None):
    pos, neg = oof[y == 1], oof[y == 0]
    print()
    print('=' * 78)
    print(f'Cross-validated results   clean={neg.size}  esp={pos.size}')
    print('=' * 78)
    print(f'ROC AUC (combined model): {auc(pos, neg):.4f}')

    print('\nPer-feature AUC (single feature, higher = more discriminative):')
    order = sorted(range(N_FEATURES),
                   key=lambda i: -abs(auc(x[y == 1][:, i], x[y == 0][:, i]) - 0.5))
    for i in order:
        print(f'  {FEATURE_NAMES[i]:<16s} {auc(x[y == 1][:, i], x[y == 0][:, i]):.4f}')

    print('\nOperating points (recall -> false-positive rate on clean):')
    for rec in (0.99, 0.95, 0.90, 0.85, 0.80, 0.70):
        thr = threshold_for_recall(pos, rec)
        print(f'  recall {rec:4.0%}  threshold {thr:6.2f}  ->  FP {(neg >= thr).mean():6.2%}'
              f'  ({int((neg >= thr).sum())} of {neg.size} clean images flagged)')

    print('\nOperating points (FP budget -> recall, actual FP shown):')
    for fp in (0.30, 0.20, 0.10, 0.05, 0.01):
        thr = threshold_for_fp(neg, fp)
        print(f'  FP budget {fp:4.0%}  threshold {thr:6.2f}  ->  recall {(pos >= thr).mean():6.2%}'
              f'  (actual FP {(neg >= thr).mean():5.2%})')

    print('\nLeak guards:')
    raw_dims = sizes[:, 0].astype(float) * 10000 + sizes[:, 1]
    norm_dims = norm_sizes[:, 0].astype(float) * 10000 + norm_sizes[:, 1]
    print(f'  AUC of RAW image dimensions     : {auc(raw_dims[y == 1], raw_dims[y == 0]):.4f}'
          '  <- the trap: the raw sets are separable by size alone')
    print(f'  AUC of TRIMMED frame dimensions : '
          f'{auc(norm_dims[y == 1], norm_dims[y == 0]):.4f}'
          '  <- after normalize_frame. Dimensions are never features, but'
          ' this should sit near 0.5')
    is_png = np.array([p.suffix.lower() == '.png' for p in ok_paths])
    if is_png.any() and (~is_png).any():
        png, jpg = oof[y == 0][is_png], oof[y == 0][~is_png]
        print(f'  clean PNG median score {np.median(png):6.2f} vs clean JPG '
              f'{np.median(jpg):6.2f}  <- format leak check, should be close')

    if target_fp is not None:
        thr = threshold_for_fp(neg, target_fp)
        goal = f'false positives at or below {target_fp:.1%}'
    else:
        thr = threshold_for_recall(pos, target_recall)
        goal = f'target recall {target_recall:.0%}'
    print(f'\nChosen operating point: {goal} -> threshold {thr:.2f}'
          f'  (recall {(pos >= thr).mean():.2%}, FP {(neg >= thr).mean():.2%})')
    return thr


def _draw_debug(frame, shapes, scale=4):
    colors = {'bar': (0, 255, 0), 'box': (255, 64, 64), 'bracket': (255, 200, 0),
              'text': (0, 200, 255)}
    big = cv2.resize(frame, (frame.shape[1] * scale, frame.shape[0] * scale),
                     interpolation=cv2.INTER_NEAREST)
    for kind, (x, y, w, h) in shapes or ():
        cv2.rectangle(big, (x * scale, y * scale), ((x + w) * scale, (y + h) * scale),
                      colors.get(kind, (255, 255, 255)), 1)
    return big


def scale_check(paths, labels, model, scales=(1.0, 0.75, 0.5), sample=250, seed=0):
    """
    Score the same screenshots shrunk to smaller capture sizes.

    A client can return a screenshot smaller than the reference capture, so the
    detector must not depend on a fixed size. This re-scores real frames at
    reduced scales: clean scores should stay put, and the recall column shows
    how much detail a smaller capture actually costs.
    """
    rng = np.random.default_rng(seed)
    idx = np.arange(len(paths))
    if len(idx) > sample:
        pos = idx[labels == 1]
        neg = rng.choice(idx[labels == 0], size=max(sample - len(pos), 1), replace=False)
        idx = np.concatenate([pos, neg])
    frames = []
    for i in idx:
        with Image.open(paths[i]) as img:
            frame = normalize_frame(img)
        if not is_capture_failed(frame):
            frames.append((frame, labels[i]))

    print('\nScale invariance (same frames, shrunk to smaller capture sizes):')
    print('  (model fitted on all rows, so the recall column is in-sample and'
          ' optimistic - only its degradation across scales is meaningful)')
    print('  scale   size      clean median   esp median   esp recall')
    for factor in scales:
        clean, esp = [], []
        for frame, label in frames:
            if factor != 1.0:
                width = max(32, int(round(frame.shape[1] * factor)))
                height = max(20, int(round(frame.shape[0] * factor)))
                scaled = cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)
            else:
                scaled = frame
            feats, hist, _, geo = frame_features(scaled)
            score = model.score_vector(apply_palette(feats, hist, model.rare_mask,
                                                     geo.pixels))
            (esp if label == 1 else clean).append(score)
        example = frames[0][0].shape
        size = f'{int(example[1] * factor)}x{int(example[0] * factor)}'
        recall = float(np.mean(np.asarray(esp) >= model.threshold)) if esp else float('nan')
        print(f'  {factor:4.2f}   {size:<9s} {np.median(clean):9.2f}    '
              f'{np.median(esp):9.2f}    {recall:9.1%}')


RESULT_BUCKETS = ('true_positives', 'false_positives', 'false_negatives',
                  'true_negatives', 'capture_failed')


def _write_scores_csv(out_dir, rows):
    """One row per image in both folders, so nothing is left unaccounted for."""
    path = Path(out_dir) / 'scores.csv'
    with path.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(['file', 'folder', 'label', 'score', 'scoring', 'bucket'])
        for image, label, score, scoring, bucket in rows:
            writer.writerow([image.name, image.parent.name,
                             'esp' if label == 1 else 'ok',
                             '' if scoring == 'not-scored' else f'{score:.2f}',
                             scoring, bucket])
    return path


def _folder_summary(rows, threshold):
    """Report each sample folder on its own terms, not just the fitted subset."""
    folders = {}
    for image, label, _, scoring, bucket in rows:
        folders.setdefault(image.parent.name, []).append((label, scoring, bucket))
    print(f'\nPer-folder results at threshold {threshold:.2f}:')
    for folder, items in sorted(folders.items()):
        total = len(items)
        failed = sum(1 for _, _, b in items if b == 'capture_failed')
        flagged = sum(1 for _, _, b in items if b in ('true_positives', 'false_positives'))
        scored = total - failed
        cv_rows = sum(1 for _, s, _ in items if s == 'cv')
        is_esp = items[0][0] == 1
        rate = flagged / scored if scored else 0.0
        print(f'  {folder:<12s} {total:5d} images | {scored} scored, '
              f'{failed} capture failed')
        if is_esp:
            print(f'  {"":<12s} {flagged:5d} flagged   (detected)        -> recall {rate:.1%}')
            print(f'  {"":<12s} {scored - flagged:5d} not flagged (missed)')
        else:
            print(f'  {"":<12s} {flagged:5d} flagged   (false positive) -> FP rate {rate:.1%}')
            print(f'  {"":<12s} {scored - flagged:5d} not flagged (correct)')
        if cv_rows < scored:
            print(f'  {"":<12s} ({cv_rows} scored cross-validated, {scored - cv_rows} on the '
                  'reserved palette split, which trained the palette but not the combiner)')


def _write_results(out_dir, entries, model, debug_overlays):
    """Copy scored images into true/false positive/negative folders for review."""
    for bucket in RESULT_BUCKETS:
        target = Path(out_dir) / bucket
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)
    counts = {}
    for path, score, bucket in entries:
        target = Path(out_dir) / bucket
        name = f'{score:05.1f}_{path.name}'
        shutil.copyfile(path, target / name)
        counts[bucket] = counts.get(bucket, 0) + 1
        if debug_overlays and bucket != 'capture_failed':
            with Image.open(path) as img:
                frame = normalize_frame(img)
            _, _, shapes, _ = frame_features(frame, debug=True)
            overlay = _draw_debug(frame, shapes)
            cv2.imwrite(str(target / f'{Path(name).stem}_debug.png'),
                        cv2.cvtColor(overlay, cv2.COLOR_RGB2BGR))
    print(f'\nResults written to {Path(out_dir).resolve()}')
    for bucket in RESULT_BUCKETS:
        print(f'  {bucket:<17s} {counts.get(bucket, 0)}')
    print(f'  (model threshold {model.threshold:.2f}; filenames are prefixed with the score)')


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

def _prepare(ok_paths, esp_paths, feats, hists, labels, blank, norm_sizes):
    """Fit the palette on a reserved clean split, then complete every feature vector."""
    reserved = np.array([_is_palette_split(p) for p in ok_paths]
                        + [False] * len(esp_paths))
    rare = fit_palette(hists[reserved & ~blank])
    pixels = norm_sizes[:, 0].astype(np.int64) * norm_sizes[:, 1].astype(np.int64)
    x = np.stack([apply_palette(feats[i], hists[i], rare, pixels[i])
                  for i in range(len(feats))])
    # Clean images used for the palette never train the combiner, and failed
    # captures never take part at all - they carry no evidence either way.
    fit_rows = ~reserved & ~blank
    if blank.any():
        print(f'Capture failed on {int(blank.sum())} images '
              f'({int((blank & (labels == 0)).sum())} clean, '
              f'{int((blank & (labels == 1)).sum())} esp) - excluded from fitting '
              'and from every metric below')
    print(f'Palette fitted on {int((reserved & ~blank).sum())} reserved clean images; '
          f'{int((labels[fit_rows] == 0).sum())} clean + {int((labels[fit_rows] == 1).sum())}'
          ' esp images used for the combiner')
    return rare, x, fit_rows


def model_importance(model, x, y, top=12):
    """
    Global feature importance for a tree ensemble.

    Mean drop in score across the ESP rows when one feature is held at its
    training median - the same measure :meth:`EspModel.explain` uses per image,
    so the global ranking and the per-image reasons stay consistent.
    """
    rows = x[y == 1]
    if len(rows) == 0:
        return []
    xt = _transform(rows)
    base = score_with(model.combiner, xt)
    medians = np.asarray(model.combiner['medians'])
    drops = []
    for j, name in enumerate(FEATURE_NAMES):
        probe = xt.copy()
        probe[:, j] = medians[j]
        drops.append((name, float(np.mean(base - score_with(model.combiner, probe)))))
    return sorted(drops, key=lambda kv: -kv[1])[:top]


def cmd_tune(args):
    ok_paths, esp_paths, feats, hists, sizes, labels, blank, norm_sizes = build_corpus(
        args.ok, args.esp, args.jobs, args.cache, args.limit)
    rare, x, fit_rows = _prepare(ok_paths, esp_paths, feats, hists, labels, blank, norm_sizes)

    xf, yf = x[fit_rows], labels[fit_rows]
    oof = repeated_cross_val_scores(xf, yf, folds=args.folds, repeats=args.repeats,
                                    kind=args.combiner)
    thr = _report(xf, yf, oof, sizes[fit_rows], norm_sizes[fit_rows],
                  [p for p, keep in zip(ok_paths, fit_rows[:len(ok_paths)]) if keep],
                  args.target_recall, args.target_fp)

    model = EspModel(rare, fit_combiner(_transform(xf), yf, kind=args.combiner), thr, meta={
        'clean_images': int((yf == 0).sum()),
        'esp_images': int((yf == 1).sum()),
        'target_recall': args.target_recall,
        'target_fp': args.target_fp,
        'cv_auc': round(auc(oof[yf == 1], oof[yf == 0]), 4),
        'capture_failed_excluded': int(blank.sum()),
        'combiner': args.combiner,
    })
    model.save(args.model)
    size_kb = Path(args.model).stat().st_size / 1024
    print(f'\nModel saved to {Path(args.model).resolve()} '
          f'({size_kb:.0f} KB, combiner={args.combiner})')
    print('\nFeature importance (mean score drop when held at its median):')
    for name, drop in model_importance(model, xf, yf):
        print(f'  {name:<16s} {drop:6.2f}')
    return 0


def cmd_eval(args):
    ok_paths, esp_paths, feats, hists, sizes, labels, blank, norm_sizes = build_corpus(
        args.ok, args.esp, args.jobs, args.cache, args.limit)
    rare, x, fit_rows = _prepare(ok_paths, esp_paths, feats, hists, labels, blank, norm_sizes)

    xf, yf = x[fit_rows], labels[fit_rows]
    all_paths = ok_paths + esp_paths
    paths = [p for p, keep in zip(all_paths, fit_rows) if keep]
    clean_paths = [p for p, lab in zip(paths, yf) if lab == 0]
    oof = repeated_cross_val_scores(xf, yf, folds=args.folds, repeats=args.repeats,
                                    kind=args.combiner)
    thr = _report(xf, yf, oof, sizes[fit_rows], norm_sizes[fit_rows], clean_paths,
                  args.target_recall, args.target_fp)

    model = EspModel(rare, fit_combiner(_transform(xf), yf, kind=args.combiner), thr)

    if args.out:
        # Score every image in both folders. Rows that took part in the fit get
        # their honest cross-validated score. The reserved palette split has no
        # out-of-fold score, so it falls back to the final model - those rows
        # never trained the combiner (only the palette histogram), so their
        # scores are near-honest, but they are labelled separately rather than
        # quietly mixed in with the cross-validated ones.
        scores = np.zeros(len(all_paths))
        scoring = np.array(['not-scored'] * len(all_paths), dtype=object)
        scores[fit_rows] = oof
        scoring[fit_rows] = 'cv'
        rest = ~fit_rows & ~blank
        if rest.any():
            scores[rest] = model.score(x[rest])
            scoring[rest] = 'palette-split'

        entries, rows = [], []
        for i, path in enumerate(all_paths):
            label = int(labels[i])
            if blank[i]:
                bucket = 'capture_failed'
            elif label == 1:
                bucket = 'true_positives' if scores[i] >= thr else 'false_negatives'
            else:
                bucket = 'false_positives' if scores[i] >= thr else 'true_negatives'
            rows.append((path, label, float(scores[i]), scoring[i], bucket))
            if bucket != 'true_negatives' or args.all_buckets:
                entries.append((path, 0.0 if blank[i] else float(scores[i]), bucket))

        if args.scale_check:
            scale_check(paths, yf, model, sample=args.scale_check)
        _write_results(args.out, entries, model, args.debug_overlays)
        csv_path = _write_scores_csv(args.out, rows)
        _folder_summary(rows, thr)
        print(f'\nEvery image is listed in {csv_path}')
        if not args.all_buckets:
            print('  (true_negatives were not copied - pass --all-buckets to write them too)')
    return 0


COMPARE_GRID = (
    ('logistic', {}),
    ('gbdt', {'depth': 2, 'trees': 400, 'lr': 0.05}),
    ('gbdt', {'depth': 3, 'trees': 300, 'lr': 0.05}),
    ('gbdt', {'depth': 3, 'trees': 600, 'lr': 0.05}),
    ('gbdt', {'depth': 4, 'trees': 400, 'lr': 0.05}),
    ('gbdt', {'depth': 5, 'trees': 400, 'lr': 0.05}),
    ('gbdt', {'depth': 3, 'trees': 200, 'lr': 0.10}),
    ('gbdt', {'depth': 3, 'trees': 800, 'lr': 0.03}),
    ('gbdt', {'depth': 3, 'trees': 400, 'lr': 0.05, 'min_child': 10}),
    ('gbdt', {'depth': 3, 'trees': 400, 'lr': 0.05, 'min_child': 40}),
    ('gbdt', {'depth': 3, 'trees': 400, 'lr': 0.05, 'subsample': 1.0}),
    ('gbdt', {'depth': 4, 'trees': 600, 'lr': 0.04, 'min_child': 10}),
    ('gbdt', {'depth': 2, 'trees': 800, 'lr': 0.05}),
    ('gbdt', {'depth': 3, 'trees': 400, 'lr': 0.05, 'lam': 5.0}),
)


def _config_label(kind, params):
    if kind == 'logistic':
        return 'logistic (baseline)'
    bits = [f'd{params.get("depth", GBDT_DEFAULTS["depth"])}',
            f'n{params.get("trees", GBDT_DEFAULTS["trees"])}',
            f'lr{params.get("lr", GBDT_DEFAULTS["lr"])}']
    for key, short in (('min_child', 'mc'), ('subsample', 'sub'), ('lam', 'lam')):
        if key in params:
            bits.append(f'{short}{params[key]}')
    return 'gbdt ' + ' '.join(bits)


def _operating_point(scores, y, fp_budget):
    """Recall achievable while the actual false-positive rate stays within budget."""
    pos, neg = scores[y == 1], scores[y == 0]
    thr = threshold_for_fp(neg, fp_budget)
    return {
        'threshold': thr,
        'recall': float((pos >= thr).mean()),
        'fp': float((neg >= thr).mean()),
        'missed': int((pos < thr).sum()),
    }


def cmd_compare(args):
    """
    Sweep combiner configurations and pick the best under a false-positive budget.

    Selection happens on one set of fold shufflings and the finalists are then
    re-scored on completely fresh ones. Picking the winner of a sweep on the same
    splits used to rank it is how a cross-validation lottery gets mistaken for an
    improvement, and with 180 positives a point of recall is well inside the noise.
    """
    ok_paths, esp_paths, feats, hists, sizes, labels, blank, norm_sizes = build_corpus(
        args.ok, args.esp, args.jobs, args.cache, args.limit)
    rare, x, fit_rows = _prepare(ok_paths, esp_paths, feats, hists, labels, blank,
                                 norm_sizes)
    xf, yf = x[fit_rows], labels[fit_rows]
    budget = args.fp_budget

    print(f'\nSweeping {len(COMPARE_GRID)} configurations on {int((yf == 0).sum())} clean '
          f'+ {int((yf == 1).sum())} esp images')
    print(f'Objective: highest recall with the false-positive rate at or below '
          f'{budget:.0%}\n')
    header = (f'{"configuration":<34s} {"AUC":>6s} | {"recall":>7s} {"FP":>6s} '
              f'{"missed":>7s} | {"recall":>7s} {"recall":>7s}')
    print(header)
    print(f'{"":<34s} {"":>6s} | {"@" + f"{budget:.0%}":>7s} {"actual":>6s} {"of esp":>7s} '
          f'| {"@2%":>7s} {"@1%":>7s}')
    print('-' * len(header))

    results = []
    for kind, params in COMPARE_GRID:
        oof = repeated_cross_val_scores(xf, yf, folds=args.folds, repeats=args.repeats,
                                        kind=kind, params=params or None)
        main = _operating_point(oof, yf, budget)
        tight = _operating_point(oof, yf, 0.02)
        tighter = _operating_point(oof, yf, 0.01)
        label = _config_label(kind, params)
        results.append((main['recall'], label, kind, params, main))
        print(f'{label:<34s} {auc(oof[yf == 1], oof[yf == 0]):6.4f} | '
              f'{main["recall"]:7.1%} {main["fp"]:6.2%} {main["missed"]:7d} | '
              f'{tight["recall"]:7.1%} {tighter["recall"]:7.1%}', flush=True)

    results.sort(key=lambda r: -r[0])
    finalists = results[:args.finalists]
    print(f'\nRe-scoring the top {len(finalists)} on fresh fold shufflings '
          f'(seeds {args.confirm_seed}+), to check the ranking is not a CV lottery:')
    print(f'{"configuration":<34s} {"selection":>10s} {"confirm":>10s} {"FP":>7s}')
    print('-' * 64)
    confirmed = []
    for recall, label, kind, params, _ in finalists:
        oof = repeated_cross_val_scores(xf, yf, folds=args.folds,
                                        repeats=args.confirm_repeats, kind=kind,
                                        params=params or None, seed0=args.confirm_seed)
        point = _operating_point(oof, yf, budget)
        confirmed.append((point['recall'], label, kind, params, point))
        print(f'{label:<34s} {recall:10.1%} {point["recall"]:10.1%} {point["fp"]:7.2%}')

    confirmed.sort(key=lambda r: -r[0])
    best_recall, best_label, best_kind, best_params, best_point = confirmed[0]
    print(f'\nWinner under a {budget:.0%} false-positive budget: {best_label}')
    print(f'  recall {best_recall:.1%}  ({best_point["missed"]} of '
          f'{int(yf.sum())} esp images missed)')
    print(f'  false positives {best_point["fp"]:.2%} '
          f'({int(best_point["fp"] * (yf == 0).sum())} of {int((yf == 0).sum())} clean)')
    print(f'  threshold {best_point["threshold"]:.2f}')
    if best_kind == 'gbdt' and best_params:
        merged = dict(GBDT_DEFAULTS, **best_params)
        print(f'\n  To adopt it, set GBDT_DEFAULTS to: {merged}')
        print(f'  then: python {Path(__file__).name} tune --ok {args.ok} --esp {args.esp}'
              f' --target-recall {best_recall:.2f}')
    return 0


def cmd_scan(args):
    model = EspModel.load(args.model)
    targets = []
    for raw in args.paths:
        path = Path(raw)
        targets.extend(list_images(path) if path.is_dir() else [path])
    flagged = failed = 0
    for path in targets:
        with Image.open(path) as img:
            result = detect(img, model=model, threshold=args.threshold)
        if result.capture_failed:
            failed += 1
            print(f'FAIL      --  {path.name}  capture failed (nothing rendered)')
            continue
        flagged += result.flagged
        mark = 'ESP ' if result.flagged else '    '
        print(f'{mark}{result.score:6.2f}  {path.name}  {", ".join(result.reasons)}')
    scored = len(targets) - failed
    print(f'\n{flagged} of {scored} scored images flagged '
          f'(threshold {args.threshold if args.threshold else model.threshold:.2f})'
          + (f'; {failed} capture failed' if failed else ''))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    subs = parser.add_subparsers(dest='command', required=True)

    def add_corpus_args(sub):
        sub.add_argument('--ok', default='samples_ok', help='directory of clean screenshots')
        sub.add_argument('--esp', default='samples_esp', help='directory of ESP screenshots')
        sub.add_argument('--model', default=str(MODEL_PATH), help='model JSON path')
        sub.add_argument('--jobs', type=int, default=8, help='decoding threads')
        sub.add_argument('--folds', type=int, default=5, help='cross-validation folds')
        sub.add_argument('--combiner', choices=('gbdt', 'logistic'), default='gbdt',
                         help='gbdt (default) or the linear baseline')
        sub.add_argument('--repeats', type=int, default=3,
                         help='cross-validation repeats averaged for a stable threshold')
        sub.add_argument('--target-recall', type=float, default=0.90,
                         dest='target_recall', help='recall the threshold is picked for')
        sub.add_argument('--target-fp', type=float, default=None, dest='target_fp',
                         help='pick the threshold by false-positive budget instead, '
                              'e.g. 0.05 for the highest recall with FP at or below 5%%')
        sub.add_argument('--cache', default='results/features.npz',
                         help='feature cache (delete it after changing features)')
        sub.add_argument('--limit', type=int, default=0, help='cap images per set (debugging)')

    tune = subs.add_parser('tune', help='fit the palette model and combiner')
    add_corpus_args(tune)
    tune.set_defaults(func=cmd_tune)

    ev = subs.add_parser('eval', help='cross-validated evaluation, copies results out')
    add_corpus_args(ev)
    ev.add_argument('--out', default='results', help='where to copy scored images')
    ev.add_argument('--debug-overlays', action='store_true', dest='debug_overlays',
                    help='also write annotated frames showing what fired')
    ev.add_argument('--all-buckets', action='store_true', dest='all_buckets',
                    help='also copy true_negatives, so every picture lands in a folder')
    ev.add_argument('--scale-check', type=int, default=250, dest='scale_check',
                    help='re-score this many frames at smaller capture sizes (0 = off)')
    ev.set_defaults(func=cmd_eval)

    cmp_ = subs.add_parser('compare', help='sweep combiner configurations')
    add_corpus_args(cmp_)
    cmp_.add_argument('--fp-budget', type=float, default=0.05, dest='fp_budget',
                      help='maximum acceptable false-positive rate')
    cmp_.add_argument('--finalists', type=int, default=4,
                      help='how many top configs to re-score on fresh splits')
    cmp_.add_argument('--confirm-repeats', type=int, default=3, dest='confirm_repeats')
    cmp_.add_argument('--confirm-seed', type=int, default=100, dest='confirm_seed')
    cmp_.set_defaults(func=cmd_compare)

    scan = subs.add_parser('scan', help='score arbitrary screenshots')
    scan.add_argument('paths', nargs='+', help='image files or directories')
    scan.add_argument('--model', default=str(MODEL_PATH))
    scan.add_argument('--threshold', type=float, default=None)
    scan.set_defaults(func=cmd_scan)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
