"""The fusion engine.

Stage A, authentic fusion
    A film-balanced, segmentation-weighted mean of landmark-aligned faces. It is
    kept as per-film sufficient statistics (weighted sums), so adding or removing
    one image touches one film's sums and never reprocesses the others.

Stage B, refined fusion (deterministic, no generative model)
    Re-reads the cached aligned faces and
      1. replaces the plain mean with a robust (Welsch / IRLS) mean, so that a
         raised hand, a boom mic or a stray lock of hair in a few frames is voted
         out instead of ghosting into everyone;
      2. rebuilds fine detail from the band-pass content of the sources that agree
         with the consensus at each pixel (eyelids, lashes, lip line, hair strands),
         which an average otherwise washes out;
      3. composites the head onto a neutral cinematic backdrop and adds grain.
    Every pixel still comes from her aligned source frames; nothing is invented.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

import cv2
import numpy as np

from .analyze import SEG_CLASSES
from .config import FUSION, FusionSettings
from .store import atomic_save_npz

log = logging.getLogger("everform.fuse")


# ---------------------------------------------------------------- cached faces

@dataclass
class CachedFace:
    image: np.ndarray       # uint8 BGR canvas
    masks: np.ndarray       # uint8 (H,W,6)
    valid: np.ndarray       # uint8
    stretch: np.ndarray     # uint8
    shape: np.ndarray       # (478,2)
    skin_lab: np.ndarray    # (6,)


def face_signature(image: np.ndarray, pts: np.ndarray) -> np.ndarray:
    """Tiny normalised grey patch of the inner face, for near-duplicate detection."""
    x0, y0 = pts.min(0)
    x1, y1 = pts.max(0)
    g = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)[int(y0):int(y1), int(x0):int(x1)]
    g = cv2.resize(g, (32, 40), interpolation=cv2.INTER_AREA).astype(np.float32)
    g = (g - g.mean()) / (g.std() + 1e-6)
    return g.astype(np.float16)


def save_face(path: Path, image, masks, valid, stretch, shape, skin_lab, embedding=None, signature=None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp.npz")
    np.savez_compressed(
        tmp,
        image=image,
        masks=np.clip(masks * 255 + 0.5, 0, 255).astype(np.uint8),
        valid=np.clip(valid * 255 + 0.5, 0, 255).astype(np.uint8),
        stretch=np.clip(stretch * 255 + 0.5, 0, 255).astype(np.uint8),
        shape=np.asarray(shape, np.float32),
        skin_lab=np.asarray(skin_lab if skin_lab is not None else [np.nan] * 6, np.float32),
        embedding=np.asarray(embedding if embedding is not None else np.zeros(128), np.float32),
        signature=np.asarray(signature if signature is not None else np.zeros((40, 32)), np.float16),
    )
    tmp.replace(path)


def load_face(path: Path) -> CachedFace:
    with np.load(path) as z:
        return CachedFace(z["image"], z["masks"], z["valid"], z["stretch"], z["shape"], z["skin_lab"])


def load_face_keys(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """(embedding, signature) without decompressing the image."""
    with np.load(path) as z:
        return z["embedding"].astype(np.float32), z["signature"].astype(np.float32)


# ---------------------------------------------------------------- per-image contribution

def pixel_weight(face: CachedFace, s: FusionSettings = FUSION) -> np.ndarray:
    cw = np.array([s.class_weights[k] for k in SEG_CLASSES], np.float32) / 255.0
    w = face.masks.astype(np.float32) @ cw
    return w * (face.valid.astype(np.float32) / 255.0) * (face.stretch.astype(np.float32) / 255.0)


PRESENCE = np.array([0.0, 1.0, 1.0, 1.0, 0.6, 0.7], np.float32) / 255.0


def person_alpha(face: CachedFace) -> np.ndarray:
    """How much of her (not her costume) is at each pixel. Garments are mostly transparent
    so the shoulders sink into the backdrop instead of averaging into fog."""
    return face.masks.astype(np.float32) @ PRESENCE


@dataclass
class CanvasGeometry:
    """Template-derived regions used when accumulating and finishing."""
    accent_exclude: np.ndarray  # bool: her face below the brows (skin and lips are not accents)
    chin_y: float
    face_h: float
    brow_y: float

    @classmethod
    def from_points(cls, pts: np.ndarray, H: int, W: int) -> "CanvasGeometry":
        from . import landmarks as L
        brow_y = float(pts[L.RIGHT_BROW + L.LEFT_BROW][:, 1].max())
        face = np.zeros((H, W), np.uint8)
        cv2.fillPoly(face, [np.round(pts[L.FACE_OVAL]).astype(np.int32)], 1)
        face = cv2.dilate(face, np.ones((9, 9), np.uint8))
        yy = np.arange(H)[:, None]
        excl = (face > 0) & (yy > brow_y - 30)
        chin = float(pts[L.CHIN][1])
        return cls(accent_exclude=excl, chin_y=chin, face_h=chin - float(pts[L.FOREHEAD][1]), brow_y=brow_y)


def clay_pixels(face: CachedFace, geo: CanvasGeometry) -> np.ndarray:
    """Pixels in her hair that are the clay accent colour (#D97757): the streak and the star clip."""
    hsv = cv2.cvtColor(face.image, cv2.COLOR_BGR2HSV)
    h, sat, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    clay = (h >= 2) & (h <= 14) & (sat >= 140) & (v >= 70) & ~geo.accent_exclude
    return clay.astype(np.float32) * (face.masks[..., 1].astype(np.float32) / 255.0)


def colour_normalize(face: CachedFace, target: list[float], strength: float) -> np.ndarray:
    """Pull her skin's exposure and colour temperature toward the reference, partially."""
    img = face.image.astype(np.float32) / 255.0
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)  # L 0..100, a,b about -127..127
    sk = face.skin_lab
    if target and sk is not None and np.isfinite(sk).all():
        m = np.array([sk[0] * 100 / 255, sk[1] - 128, sk[2] - 128], np.float32)
        sd = np.array([sk[3] * 100 / 255], np.float32)
        t = np.array([target[0] * 100 / 255, target[1] - 128, target[2] - 128], np.float32)
        tsd = target[3] * 100 / 255
        g = float(np.clip((tsd / max(sd[0], 1e-3)) ** (0.5 * strength), 0.75, 1.35))
        lab[..., 0] = (lab[..., 0] - m[0]) * g + m[0] + strength * (t[0] - m[0])
        lab[..., 1] += strength * (t[1] - m[1])
        lab[..., 2] += strength * (t[2] - m[2])
        lab[..., 0] = np.clip(lab[..., 0], 0, 100)
    out = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
    return np.clip(out, 0, 1) * 255.0


# ---------------------------------------------------------------- Stage A sufficient statistics

@dataclass
class FilmAccumulator:
    """Weighted sums for one film in one mode. Adding is +, removing is -."""
    H: int
    W: int
    num: np.ndarray = None
    den: np.ndarray = None
    a_num: np.ndarray = None
    a_den: np.ndarray = None
    hair: np.ndarray = None
    clay: np.ndarray = None
    shape: np.ndarray = None
    mass: float = 0.0
    count: int = 0
    ledger: dict = field(default_factory=dict)  # image id -> weight currently applied

    ARRAYS = ("num", "den", "a_num", "a_den", "hair", "clay", "shape")

    def __post_init__(self):
        H, W = self.H, self.W
        shapes = {"num": (H, W, 3), "shape": (478, 2)}
        for name in self.ARRAYS:
            if getattr(self, name) is None:
                dtype = np.float64 if name == "shape" else np.float32
                setattr(self, name, np.zeros(shapes.get(name, (H, W)), dtype))

    def apply(self, rid: str, face: CachedFace, weight: float, sign: int, target: list[float],
              s: FusionSettings, geo: CanvasGeometry) -> None:
        img = colour_normalize(face, target, s.color_strength)
        m = pixel_weight(face, s) * weight
        v = (face.valid.astype(np.float32) / 255.0) * weight
        k = float(sign)
        self.num += k * img * m[..., None]
        self.den += k * m
        self.a_num += k * v * person_alpha(face)
        self.a_den += k * v
        self.hair += k * v * (face.masks[..., 1].astype(np.float32) / 255.0)
        self.clay += k * v * clay_pixels(face, geo)
        self.shape += k * weight * face.shape.astype(np.float64)
        self.mass += k * weight
        self.count += sign
        if sign > 0:
            self.ledger[rid] = weight
        else:
            self.ledger.pop(rid, None)
        if self.count == 0:
            # Exact reset so float residue never lingers in an emptied film.
            for name in self.ARRAYS:
                getattr(self, name)[:] = 0
            self.mass = 0.0

    def save(self, path: Path) -> None:
        import json
        meta = json.dumps({"mass": self.mass, "count": self.count, "ledger": self.ledger}).encode()
        atomic_save_npz(path, meta=np.frombuffer(meta, np.uint8), **{n: getattr(self, n) for n in self.ARRAYS})

    @classmethod
    def load(cls, path: Path, H: int, W: int) -> "FilmAccumulator":
        import json
        if not path.exists():
            return cls(H, W)
        try:
            with np.load(path) as z:
                meta = json.loads(bytes(z["meta"]).decode())
                arrays = {n: z[n].copy() for n in cls.ARRAYS}
        except (OSError, KeyError, ValueError) as e:
            log.warning("accumulator %s unreadable (%s), discarding", path, e)
            return cls(H, W)
        acc = cls(H, W, mass=float(meta["mass"]), count=int(meta["count"]), ledger=dict(meta["ledger"]), **arrays)
        if acc.num.shape != (H, W, 3):
            log.warning("accumulator %s has the wrong canvas size, discarding", path)
            return cls(H, W)
        return acc


def film_shares(masses: dict[str, float], mode: str) -> dict[str, float]:
    """Share of the portrait each film controls.

    image : proportional to its total image weight (big films dominate)
    film  : equal per film (a film with two usable frames weighs as much as one with forty)
    sqrt  : proportional to the square root of its image weight (default)
    """
    live = {f: m for f, m in masses.items() if m > 1e-9}
    if not live:
        return {}
    if mode == "image":
        raw = live
    elif mode == "film":
        raw = {f: 1.0 for f in live}
    else:
        raw = {f: float(np.sqrt(m)) for f, m in live.items()}
    tot = sum(raw.values())
    return {f: v / tot for f, v in raw.items()}


def image_weights(ledgers: dict[str, dict[str, float]], masses: dict[str, float], mode: str) -> dict[str, float]:
    """Global per-image weight after film balancing (sums to 1)."""
    shares = film_shares(masses, mode)
    out = {}
    for film, led in ledgers.items():
        if film not in shares:
            continue
        for rid, w in led.items():
            out[rid] = shares[film] * w / masses[film]
    return out


@dataclass
class StageA:
    mean: np.ndarray     # float32 BGR 0..255
    alpha: np.ndarray    # float32 0..1
    coverage: np.ndarray  # float32 total weight per pixel (for diagnostics)
    shape: np.ndarray    # (478,2) film-balanced mean landmark geometry
    hair: np.ndarray     # 0..1 share of frames with hair at this pixel
    clay: np.ndarray     # 0..1 share of frames with the clay accent at this pixel
    shares: dict = field(default_factory=dict)


def combine(accs: dict[str, FilmAccumulator], balance: str) -> StageA | None:
    masses = {f: a.mass for f, a in accs.items() if a.count > 0}
    shares = film_shares(masses, balance)
    if not shares:
        return None
    any_acc = next(iter(accs.values()))
    tot = {n: np.zeros_like(getattr(any_acc, n)) for n in FilmAccumulator.ARRAYS}
    for f, sh in shares.items():
        a = accs[f]
        k = sh / a.mass
        for n in FilmAccumulator.ARRAYS:
            tot[n] += k * getattr(a, n)
    mean = tot["num"] / np.maximum(tot["den"], 1e-6)[..., None]
    ad = np.maximum(tot["a_den"], 1e-6)
    return StageA(
        mean=mean.astype(np.float32),
        alpha=np.clip(tot["a_num"] / ad, 0, 1).astype(np.float32),
        coverage=tot["den"],
        shape=tot["shape"],
        hair=np.clip(tot["hair"] / ad, 0, 1).astype(np.float32),
        # Share of frames *with hair at this pixel* that show clay there.
        clay=np.clip(_blur(tot["clay"], 2.5) / np.maximum(_blur(tot["hair"], 2.5), 0.15 * ad), 0, 1).astype(np.float32),
        shares=shares,
    )


# ---------------------------------------------------------------- Stage B refinement

BANDS = ((0.0, 1.0), (1.0, 2.2), (2.2, 4.5))


def _blur(x: np.ndarray, sigma: float) -> np.ndarray:
    return x if sigma <= 0 else cv2.GaussianBlur(x, (0, 0), sigma)


def _bands(img: np.ndarray) -> list[np.ndarray]:
    out = []
    for lo, hi in BANDS:
        out.append(_blur(img, lo) - _blur(img, hi))
    return out


@dataclass
class StageB:
    image: np.ndarray        # float32 BGR 0..255, head on canvas (no background)
    alpha: np.ndarray
    agreement: np.ndarray    # 0..1, how strongly the sources agree per pixel


def refine(
    faces: Callable[[], Iterator[tuple[str, CachedFace]]],
    weights: dict[str, float],
    stage_a: StageA,
    target: list[float],
    s: FusionSettings = FUSION,
    geo: CanvasGeometry | None = None,
) -> StageB:
    """Robust consensus + detail reconstruction, streamed from the face cache."""
    mu = stage_a.mean.copy()
    H, W = mu.shape[:2]

    def residual(img: np.ndarray, ref: np.ndarray) -> np.ndarray:
        d = _blur(img, 1.5) - _blur(ref, 1.5)
        return np.sqrt((d * d).sum(axis=2))

    # Pass 0: spread of the sources around the plain mean.
    r2 = np.zeros((H, W), np.float32)
    dn = np.zeros((H, W), np.float32)
    for rid, f in faces():
        g = weights.get(rid, 0.0)
        if g <= 0:
            continue
        m = pixel_weight(f, s) * g
        r = residual(colour_normalize(f, target, s.color_strength), mu)
        r2 += m * r * r
        dn += m
    scale = np.maximum(_blur(np.sqrt(r2 / np.maximum(dn, 1e-6)), 4), 10.0)

    iters = max(1, s.robust_iterations)
    for it in range(iters):
        last = it == iters - 1
        num = np.zeros((H, W, 3), np.float32)
        den = np.zeros((H, W), np.float32)
        r2[:] = 0
        dn[:] = 0
        if last:
            b_num = [np.zeros((H, W, 3), np.float32) for _ in BANDS]
            b_den = np.zeros((H, W), np.float32)
            b_en = [np.zeros((H, W), np.float32) for _ in BANDS]
        for rid, f in faces():
            g = weights.get(rid, 0.0)
            if g <= 0:
                continue
            img = colour_normalize(f, target, s.color_strength)
            base = pixel_weight(f, s) * g
            r = residual(img, mu)
            rho = np.exp(-(r / (1.25 * scale)) ** 2)
            m = base * rho
            num += img * m[..., None]
            den += m
            r2 += m * r * r
            dn += m
            if last:
                # Detail comes preferentially from the frames that agree with the consensus here.
                hw = base * rho ** 6
                for k, band in enumerate(_bands(img)):
                    b_num[k] += band * hw[..., None]
                    b_en[k] += hw * (band * band).sum(axis=2)
                b_den += hw
        mu_new = num / np.maximum(den, 1e-6)[..., None]
        thin = den < 1e-5
        mu_new[thin] = stage_a.mean[thin]
        mu = mu_new
        scale = np.maximum(_blur(np.sqrt(r2 / np.maximum(dn, 1e-6)), 4), 8.0)

    agreement = np.clip(den / np.maximum(stage_a.coverage, 1e-6), 0, 1)

    base_img = mu.copy()
    detail = np.zeros_like(mu)
    smooth_mu = _blur(mu, BANDS[-1][1])
    gains = (s.detail_gain_max, min(1.6, s.detail_gain_max), 1.25)
    for k, (lo, hi) in enumerate(BANDS):
        band = b_num[k] / np.maximum(b_den, 1e-6)[..., None]
        en = _blur(b_en[k] / np.maximum(b_den, 1e-6), 3)
        cur = _blur((band * band).sum(axis=2), 3)
        gain = np.clip(np.sqrt(en / np.maximum(cur, 1e-3)), 1.0, gains[k])
        detail += band * gain[..., None]
    if geo is not None:
        # Costume texture below the jaw is a blend of unrelated garments; keep it soft.
        ys = np.arange(H, dtype=np.float32)[:, None]
        keep = (1 - 0.8 * _smoothstep(ys, geo.chin_y, geo.chin_y + 0.5 * geo.face_h))[..., None]
        detail = detail * keep + (mu - smooth_mu) * (1 - keep)
    out = smooth_mu + detail
    # Where the detail-weighted support is negligible, fall back to the robust mean.
    support = np.clip(b_den / np.maximum(b_den.max() * 0.02, 1e-9), 0, 1)
    out = support[..., None] * out + (1 - support[..., None]) * base_img
    return StageB(image=np.clip(out, 0, 255).astype(np.float32), alpha=stage_a.alpha, agreement=agreement)


# ---------------------------------------------------------------- finishing

def backdrop(H: int, W: int, tint: np.ndarray | None = None) -> np.ndarray:
    """Neutral, dark studio backdrop: a soft pool of light behind the head."""
    ys, xs = np.mgrid[0:H, 0:W].astype(np.float32)
    cx, cy = W * 0.5, H * 0.34
    d = np.sqrt(((xs - cx) / (W * 0.7)) ** 2 + ((ys - cy) / (H * 0.6)) ** 2)
    glow = np.clip(1 - d, 0, 1) ** 1.4
    base = np.array([17, 16, 17], np.float32) if tint is None else tint
    hi = base + np.array([58, 58, 64], np.float32)
    return base[None, None] + (hi - base)[None, None] * glow[..., None]


CLAY_BGR = np.array([87, 119, 217], np.float32)  # #D97757


def _smoothstep(x: np.ndarray, lo: float, hi: float) -> np.ndarray:
    t = np.clip((x - lo) / (hi - lo), 0, 1)
    return t * t * (3 - 2 * t)


def composite(head: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    """Place the fused head on the backdrop using the fused silhouette."""
    H, W = alpha.shape
    a = _blur(_smoothstep(alpha, 0.2, 0.8), 2.0)
    return head * a[..., None] + backdrop(H, W) * (1 - a[..., None])


def finish(head: np.ndarray, A: StageA, geo: CanvasGeometry, s: FusionSettings = FUSION) -> np.ndarray:
    """Stage B finishing: hair and accent colour, shoulder shadow, grade, grain."""
    H, W = A.alpha.shape
    img = np.clip(head, 0, 255).astype(np.float32) / 255.0
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)

    # Black hair carries the colour of each set's lighting; averaged, that reads as a cast.
    hair = _blur(_smoothstep(A.hair, 0.25, 0.7), 3)
    lab[..., 1] *= 1 - 0.8 * hair
    lab[..., 2] *= 1 - 0.8 * hair
    lab[..., 0] *= 1 - 0.1 * hair

    # The streak and star clip: drawn only where enough of her frames have clay at that pixel.
    in_hair = A.hair > 0.5
    peak = float(A.clay[in_hair].max()) if in_hair.any() else 0.0
    k = _smoothstep(A.clay, max(0.06, 0.45 * peak), max(0.12, 0.95 * peak))
    k = cv2.GaussianBlur(k, (0, 0), sigmaX=3, sigmaY=9) * hair  # hair falls vertically
    if k.max() > 0:
        clay_lab = cv2.cvtColor(CLAY_BGR[None, None] / 255.0, cv2.COLOR_BGR2LAB)[0, 0]
        L = lab[..., 0]
        strands = cv2.GaussianBlur(L, (0, 0), sigmaX=0.8, sigmaY=4) - cv2.GaussianBlur(L, (0, 0), sigmaX=4, sigmaY=4)
        target_L = np.clip(0.42 * clay_lab[0] + 0.58 * L + 2.2 * strands, 0, 100)
        lab[..., 0] = L * (1 - k) + target_L * k
        lab[..., 1] = lab[..., 1] * (1 - k) + clay_lab[1] * 0.7 * k
        lab[..., 2] = lab[..., 2] * (1 - k) + clay_lab[2] * 0.7 * k
    img = np.clip(cv2.cvtColor(lab, cv2.COLOR_LAB2BGR), 0, 1) * 255.0

    out = composite(img, A.alpha) / 255.0
    ys, xs = np.mgrid[0:H, 0:W].astype(np.float32)
    # Costumes change every film, so their averaged fine detail is only crossing
    # seams and folds. Below the neck keep the soft silhouette, not the lines.
    y0 = geo.chin_y + 0.1 * geo.face_h
    soften = _smoothstep(ys, y0 - 0.08 * geo.face_h, y0 + 0.3 * geo.face_h) * (1 - hair)
    soft = cv2.GaussianBlur(out, (0, 0), sigmaX=7)
    out = out * (1 - soften[..., None]) + soft * soften[..., None]
    # Let the shoulders fall into shadow so the face leads.
    shade = 1 - 0.68 * _smoothstep(ys, y0, y0 + 1.2 * geo.face_h)
    sat = 1 - 0.7 * _smoothstep(ys, y0 - 0.1 * geo.face_h, y0 + 0.5 * geo.face_h)
    grey = out.mean(axis=2, keepdims=True)
    out = grey + (out - grey) * sat[..., None]
    out = out * shade[..., None]
    out = out + 0.12 * (out - 0.5) * 4 * out * (1 - out)  # gentle S-curve
    v = 1 - 0.22 * (((xs - W / 2) / (W * 0.72)) ** 2 + ((ys - H * 0.42) / (H * 0.8)) ** 2)
    out = out * np.clip(v, 0.5, 1)[..., None] * 255.0
    if s.grain > 0:
        rng = np.random.default_rng(s.seed)
        n = _blur(rng.normal(0, s.grain, (H, W)).astype(np.float32), 0.6) * 1.6
        out = out + n[..., None]
    return np.clip(out + 0.5, 0, 255).astype(np.uint8)


def raw_view(A: StageA) -> np.ndarray:
    """Stage A as-is: the plain weighted mean on the backdrop, no refinement or grading."""
    return np.clip(composite(A.mean, A.alpha) + 0.5, 0, 255).astype(np.uint8)
