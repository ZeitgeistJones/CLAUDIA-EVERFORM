"""Render, quality-gate and atomically publish portrait versions.

Layout under public/everform/:
  manifest.json                         what the website reads (small, replaced atomically)
  {mode}/report.json                    every source image, its outcome and contribution
  {mode}/v/{version}/portrait.webp      display image
  {mode}/v/{version}/portrait.png       lossless download (kept for the current version only)
  {mode}/v/{version}/raw.webp           Stage A, the plain aligned mean
  {mode}/v/{version}/contact.webp       the aligned faces that formed it, by weight
  {mode}/v/{version}/version.json       exact source ids, weights and settings
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

import cv2
import numpy as np

from . import config
from .align import Template, normalize_to_eyes, warp_canvas
from .fuse import CachedFace, CanvasGeometry, StageA, combine, finish, image_weights, raw_view, refine
from .store import atomic_write_image, atomic_write_json, read_json

log = logging.getLogger("everform.publish")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass
class Render:
    portrait: np.ndarray        # uint8 BGR, Stage B finished
    raw: np.ndarray             # uint8 BGR, Stage A
    weights: dict[str, float]   # image id -> share of the portrait
    shares: dict[str, float]    # film -> share
    stage_a: StageA


def render(accs, template: Template, faces: Callable[[list[str]], Iterator[tuple[str, CachedFace]]],
           target: list[float], s=config.FUSION) -> Render | None:
    A = combine(accs, s.film_balance)
    if A is None:
        return None
    ledgers = {f: a.ledger for f, a in accs.items() if a.count > 0}
    masses = {f: a.mass for f, a in accs.items() if a.count > 0}
    weights = image_weights(ledgers, masses, s.film_balance)
    geo = CanvasGeometry.from_points(template.points, s.height, s.width)
    ids = sorted(weights)
    B = refine(lambda: faces(ids), weights, A, target, s, geo)
    portrait = finish(B.image, A, geo, s)
    raw = raw_view(A)
    # Let the fused geometry, not just the fused texture, follow her filmography.
    mean_shape = normalize_to_eyes(A.shape / max(sum(A.shares.values()), 1e-9), s) if A.shares else template.points
    if np.isfinite(mean_shape).all():
        portrait = warp_canvas(portrait, template.points, mean_shape, template)
        raw = warp_canvas(raw, template.points, mean_shape, template)
    return Render(portrait=portrait, raw=raw, weights=weights, shares=A.shares, stage_a=A)


def quality_gate(models, portrait: np.ndarray, reference: np.ndarray) -> tuple[bool, dict]:
    """A new version must still be one clear face that reads as Claudia."""
    from .analyze import detect_and_embed, face_mesh

    info: dict = {}
    if portrait is None or not np.isfinite(portrait).all():
        return False, {"reason": "render produced invalid pixels"}
    mean = float(portrait.mean())
    info["mean_luma"] = round(mean, 1)
    if not 12 <= mean <= 200:
        return False, {**info, "reason": "render is blank or blown out"}
    dets = detect_and_embed(models, portrait)
    info["faces"] = len(dets)
    if not dets:
        return False, {**info, "reason": "no face detected in the composite"}
    best = max(dets, key=lambda d: d.box[2] * d.box[3])
    ident = float(best.embedding @ reference)
    info["identity"] = round(ident, 3)
    if ident < config.THRESHOLDS.identity_accept:
        return False, {**info, "reason": f"composite no longer reads as Claudia (identity {ident:.2f})"}
    if face_mesh(models, portrait, best) is None:
        return False, {**info, "reason": "composite face has no coherent landmarks"}
    gray = cv2.cvtColor(portrait, cv2.COLOR_BGR2GRAY)
    x, y, w, h = [int(v) for v in best.box]
    roi = gray[max(0, y):y + h, max(0, x):x + w]
    sharp = float(cv2.Laplacian(roi, cv2.CV_32F).std()) if roi.size else 0.0
    info["sharpness"] = round(sharp, 2)
    if sharp < 2.0:
        return False, {**info, "reason": "composite face is too blurry"}
    return True, info


def contact_sheet(faces: Iterator[tuple[str, CachedFace]], order: list[str], cols: int = 12, tw: int = 128) -> np.ndarray:
    th = int(tw * 1.25)
    thumbs = {}
    for rid, f in faces:
        thumbs[rid] = cv2.resize(f.image, (tw, th), interpolation=cv2.INTER_AREA)
    ids = [r for r in order if r in thumbs]
    rows = max(1, (len(ids) + cols - 1) // cols)
    sheet = np.full((rows * th, cols * tw, 3), 14, np.uint8)
    for i, rid in enumerate(ids):
        r, c = divmod(i, cols)
        sheet[r * th:(r + 1) * th, c * tw:(c + 1) * tw] = thumbs[rid]
    return sheet


def version_hash(weights: dict[str, float], extra: dict) -> str:
    payload = json.dumps({"w": {k: round(v, 8) for k, v in sorted(weights.items())}, "x": extra}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def change_score(prev_path: Path, img: np.ndarray) -> float | None:
    """Mean absolute difference (0..255) against the previous version, centre-weighted."""
    if not prev_path.exists():
        return None
    prev = cv2.imread(str(prev_path))
    if prev is None or prev.shape != img.shape:
        return None
    d = np.abs(prev.astype(np.float32) - img.astype(np.float32)).mean(axis=2)
    H, W = d.shape
    ys, xs = np.mgrid[0:H, 0:W]
    wgt = np.exp(-(((xs - W / 2) / (W * 0.25)) ** 2 + ((ys - H * 0.42) / (H * 0.25)) ** 2))
    return round(float((d * wgt).sum() / wgt.sum()), 3)


def write_version(mode: str, vid: str, r: Render, sheet: np.ndarray, meta: dict, keep_png: bool = True) -> dict:
    """Write every file of a version into a temp dir, then move it into place in one rename."""
    base = config.PUBLIC_DIR / mode / "v"
    final = base / vid
    tmp = base / f".{vid}.tmp"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    atomic_write_image(tmp / "portrait.webp", r.portrait, ".webp", [cv2.IMWRITE_WEBP_QUALITY, 92])
    if keep_png:
        atomic_write_image(tmp / "portrait.png", r.portrait, ".png", [cv2.IMWRITE_PNG_COMPRESSION, 6])
    atomic_write_image(tmp / "raw.webp", r.raw, ".webp", [cv2.IMWRITE_WEBP_QUALITY, 90])
    atomic_write_image(tmp / "contact.webp", sheet, ".webp", [cv2.IMWRITE_WEBP_QUALITY, 80])
    atomic_write_json(tmp / "version.json", meta)
    if final.exists():
        shutil.rmtree(final)
    tmp.rename(final)
    rel = f"/everform/{mode}/v/{vid}"
    return {
        "image": f"{rel}/portrait.webp",
        "png": f"{rel}/portrait.png" if keep_png else None,
        "raw": f"{rel}/raw.webp",
        "contact": f"{rel}/contact.webp",
        "details": f"{rel}/version.json",
    }


def load_manifest() -> dict:
    m = read_json(config.PUBLIC_DIR / "manifest.json", default=None)
    if not isinstance(m, dict) or "modes" not in m:
        m = {"schema": 1, "modes": {}}
    return m


def save_manifest(m: dict) -> None:
    m["generatedAt"] = now_iso()
    atomic_write_json(config.PUBLIC_DIR / "manifest.json", m, indent=1)


def prune_pngs(mode: str, keep: set[str]) -> None:
    """Lossless PNGs are large; only the current version keeps one."""
    base = config.PUBLIC_DIR / mode / "v"
    if not base.exists():
        return
    for d in base.iterdir():
        if d.is_dir() and d.name not in keep and (d / "portrait.png").exists():
            (d / "portrait.png").unlink()


def remove_version_dirs(mode: str, ids: list[str]) -> None:
    base = config.PUBLIC_DIR / mode / "v"
    for vid in ids:
        p = base / vid
        if p.exists():
            shutil.rmtree(p)
