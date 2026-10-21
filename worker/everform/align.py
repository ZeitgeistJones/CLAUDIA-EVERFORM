"""Canonical face template and piecewise-affine alignment.

Each source face is mapped onto one shared canvas so her eyes, nose, mouth and jaw
land on the same pixels. Inside the face the 478 landmarks drive a piecewise-affine
warp; outside it (hair, neck, shoulders) the map relaxes to a plain similarity
transform through rings of control points, so hair and shoulders are moved
rigidly instead of being stretched.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np
from scipy.spatial import Delaunay

from . import landmarks as L
from .config import FUSION, FusionSettings

RING_SCALES = (1.3, 1.75, 2.4)


def umeyama(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """Least-squares similarity transform (rotation, uniform scale, translation), no reflection."""
    src = np.asarray(src, np.float64)
    dst = np.asarray(dst, np.float64)
    ms, md = src.mean(0), dst.mean(0)
    sc, dc = src - ms, dst - md
    cov = dc.T @ sc / len(src)
    U, S, Vt = np.linalg.svd(cov)
    d = np.sign(np.linalg.det(U @ Vt))
    D = np.diag([1.0, d])
    R = U @ D @ Vt
    var = (sc ** 2).sum() / len(src)
    s = (S * np.diag(D)).sum() / var if var > 0 else 1.0
    t = md - s * R @ ms
    return np.hstack([s * R, t[:, None]])


def apply_affine(M: np.ndarray, pts: np.ndarray) -> np.ndarray:
    pts = np.asarray(pts, np.float64)
    return pts @ M[:, :2].T + M[:, 2]


def invert_affine(M: np.ndarray) -> np.ndarray:
    return cv2.invertAffineTransform(M.astype(np.float64))


def affine_scale(M: np.ndarray) -> float:
    return float(np.sqrt(abs(np.linalg.det(M[:, :2]))))


def eye_centers(pts: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(her right eye, her left eye) centres; her right eye is on the viewer's left."""
    return pts[L.RIGHT_EYE].mean(0), pts[L.LEFT_EYE].mean(0)


def _eye_targets(s: FusionSettings) -> tuple[np.ndarray, np.ndarray]:
    iod = s.iod * s.width
    y = s.eye_y * s.height
    return np.array([s.width / 2 - iod / 2, y]), np.array([s.width / 2 + iod / 2, y])


def normalize_to_eyes(pts: np.ndarray, s: FusionSettings = FUSION) -> np.ndarray:
    r, l = eye_centers(pts)
    tr, tl = _eye_targets(s)
    M = umeyama(np.stack([r, l]), np.stack([tr, tl]))
    return apply_affine(M, pts)


def build_template(shapes: list[np.ndarray], s: FusionSettings = FUSION, iterations: int = 4) -> np.ndarray:
    """Generalised Procrustes mean of her real landmark shapes, pinned to the eye line."""
    if not shapes:
        raise ValueError("cannot build a template from zero shapes")
    aligned = [normalize_to_eyes(p, s) for p in shapes]
    mean = np.mean(aligned, axis=0)
    for _ in range(iterations):
        aligned = [apply_affine(umeyama(p[L.STABLE], mean[L.STABLE]), p) for p in aligned]
        mean = normalize_to_eyes(np.median(aligned, axis=0), s)
    return mean


@dataclass
class Template:
    points: np.ndarray            # (478, 2) canonical landmark positions on the canvas
    width: int
    height: int

    def __post_init__(self) -> None:
        self.points = np.asarray(self.points, np.float64)
        self.ring = self._ring_points()
        self.control = np.vstack([self.points, self.ring])
        self.triangles = Delaunay(self.control).simplices.astype(np.int32)
        self._grid: WarpGrid | None = None

    def _ring_points(self) -> np.ndarray:
        oval = self.points[L.FACE_OVAL]
        c = oval.mean(0)
        rings = [c + k * (oval[::2] - c) for k in RING_SCALES]
        W, H = self.width, self.height
        m = 0.35 * max(W, H)
        xs = np.linspace(-m, W + m, 7)
        ys = np.linspace(-m, H + m, 8)
        border = [(x, -m) for x in xs] + [(x, H + m) for x in xs] + [(-m, y) for y in ys[1:-1]] + [(W + m, y) for y in ys[1:-1]]
        return np.vstack(rings + [np.array(border)])

    @property
    def grid(self) -> "WarpGrid":
        if self._grid is None:
            self._grid = WarpGrid(self.control, self.triangles, self.width, self.height)
        return self._grid

    def to_dict(self) -> dict:
        return {"width": self.width, "height": self.height, "points": np.round(self.points, 3).tolist()}

    @classmethod
    def from_dict(cls, d: dict) -> "Template":
        return cls(points=np.array(d["points"], np.float64), width=int(d["width"]), height=int(d["height"]))


class WarpGrid:
    """Rasterised triangle membership + barycentric coordinates on the destination canvas.

    Computed once per destination shape; mapping any source mesh onto it is then a
    single vectorised interpolation followed by one cv2.remap.
    """

    def __init__(self, dst_pts: np.ndarray, triangles: np.ndarray, width: int, height: int):
        self.width, self.height = width, height
        self.dst = np.asarray(dst_pts, np.float64)
        self.triangles = triangles
        tri_idx = np.full((height, width), -1, np.int32)
        shift = 4
        for t, tri in enumerate(triangles):
            poly = np.round(self.dst[tri] * (1 << shift)).astype(np.int32)
            cv2.fillConvexPoly(tri_idx, poly, int(t), lineType=cv2.LINE_8, shift=shift)
        if (tri_idx < 0).any():
            # Pixels on rounding seams: borrow the nearest covered pixel's triangle.
            miss = (tri_idx < 0).astype(np.uint8)
            _, labels = cv2.distanceTransformWithLabels(miss, cv2.DIST_L2, 3, labelType=cv2.DIST_LABEL_PIXEL)
            covered = np.argwhere(miss == 0)
            lut = np.zeros(labels.max() + 1, np.int32)
            lab_cov = labels[miss == 0]
            lut[lab_cov] = tri_idx[covered[:, 0], covered[:, 1]]
            tri_idx = np.where(miss == 1, lut[labels], tri_idx)
        self.tri_idx = tri_idx
        ys, xs = np.mgrid[0:height, 0:width].astype(np.float64)
        A = self.dst[triangles[:, 0]]
        B = self.dst[triangles[:, 1]]
        C = self.dst[triangles[:, 2]]
        T = np.stack([B - A, C - A], axis=2)  # (T,2,2) columns are edge vectors
        det = T[:, 0, 0] * T[:, 1, 1] - T[:, 0, 1] * T[:, 1, 0]
        det = np.where(np.abs(det) < 1e-9, 1e-9, det)
        inv = np.empty_like(T)
        inv[:, 0, 0] = T[:, 1, 1] / det
        inv[:, 0, 1] = -T[:, 0, 1] / det
        inv[:, 1, 0] = -T[:, 1, 0] / det
        inv[:, 1, 1] = T[:, 0, 0] / det
        ti = tri_idx.ravel()
        px = xs.ravel() - A[ti, 0]
        py = ys.ravel() - A[ti, 1]
        u = inv[ti, 0, 0] * px + inv[ti, 0, 1] * py
        v = inv[ti, 1, 0] * px + inv[ti, 1, 1] * py
        self.bary = np.stack([1 - u - v, u, v], axis=1).astype(np.float32)
        self.dst_area = 0.5 * det

    def map_from(self, src_pts: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Dense (map_x, map_y): for each destination pixel, where to sample the source."""
        S = np.asarray(src_pts, np.float32)[self.triangles]  # (T,3,2)
        ti = self.tri_idx.ravel()
        b = self.bary
        sx = b[:, 0] * S[ti, 0, 0] + b[:, 1] * S[ti, 1, 0] + b[:, 2] * S[ti, 2, 0]
        sy = b[:, 0] * S[ti, 0, 1] + b[:, 1] * S[ti, 1, 1] + b[:, 2] * S[ti, 2, 1]
        return sx.reshape(self.height, self.width), sy.reshape(self.height, self.width)

    def triangle_area_ratio(self, src_pts: np.ndarray) -> np.ndarray:
        """Signed source/destination area per triangle. <1 means foreshortened, <0 folded."""
        S = np.asarray(src_pts, np.float64)[self.triangles]
        e1 = S[:, 1] - S[:, 0]
        e2 = S[:, 2] - S[:, 0]
        area = 0.5 * (e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0])
        return area / np.where(np.abs(self.dst_area) < 1e-9, 1e-9, self.dst_area)


@dataclass
class AlignedFace:
    image: np.ndarray           # uint8 BGR on the template canvas
    valid: np.ndarray           # float32 0..1, pixel came from inside the source frame
    stretch: np.ndarray         # float32 0..1, down-weights foreshortened / folded regions
    shape: np.ndarray           # (478,2) her landmarks after the similarity fit (true geometry)
    similarity: np.ndarray      # 2x3 source -> canvas similarity
    source_scale: float         # canvas px per source px before any pre-resize


def align_face(img: np.ndarray, lm: np.ndarray, template: Template) -> AlignedFace:
    lm = np.asarray(lm, np.float64)
    S = umeyama(lm[L.STABLE], template.points[L.STABLE])
    scale = affine_scale(S)
    src = img
    f = 1.0
    if scale < 0.8:
        # Downscale large faces first so the remap does not alias.
        f = scale / 0.8
        src = cv2.resize(img, (max(1, round(img.shape[1] * f)), max(1, round(img.shape[0] * f))), interpolation=cv2.INTER_AREA)
    lm_s = lm * f
    S_s = umeyama(lm_s[L.STABLE], template.points[L.STABLE])
    ring_src = apply_affine(invert_affine(S_s), template.ring)
    control_src = np.vstack([lm_s, ring_src])
    grid = template.grid
    mx, my = grid.map_from(control_src)
    aligned = cv2.remap(src, mx, my, interpolation=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
    h, w = src.shape[:2]
    inside = ((mx >= 0) & (mx <= w - 1) & (my >= 0) & (my <= h - 1)).astype(np.float32)
    valid = cv2.GaussianBlur(inside, (0, 0), 3)

    sim_src = np.vstack([apply_affine(S_s, lm_s), template.ring])
    ratio = grid.triangle_area_ratio(sim_src)
    tri_w = np.where(ratio <= 0, 0.0, np.clip(ratio, 0, 1) ** 1.5).astype(np.float32)
    stretch = cv2.GaussianBlur(tri_w[grid.tri_idx], (0, 0), 5)

    return AlignedFace(
        image=aligned,
        valid=valid * (inside > 0),
        stretch=stretch,
        shape=apply_affine(S, lm),
        similarity=S,
        source_scale=scale,
    )


def warp_canvas(img: np.ndarray, from_pts: np.ndarray, to_pts: np.ndarray, template: Template) -> np.ndarray:
    """Warp a canvas image whose face sits at `from_pts` so it sits at `to_pts`."""
    ring = template.ring
    dst = np.vstack([to_pts, ring])
    src = np.vstack([from_pts, ring])
    grid = WarpGrid(dst, template.triangles, template.width, template.height)
    mx, my = grid.map_from(src)
    return cv2.remap(img, mx, my, interpolation=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT)
