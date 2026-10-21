"""Claudia's identity lock: a reference embedding and a canonical face template.

Both are derived once from official material and then frozen in data/ so every
later image is judged against the same Claudia:

  * Seeds: the six images canon.json lists under "faces" (one per film).
  * Reference: seeds plus single-face Film Canon close-ups that agree with them,
    averaged with outlier trimming.
  * Template: Procrustes mean of her near-frontal, neutral reference landmarks,
    so the canvas keeps her own adult proportions rather than a generic face.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

from .align import Template, build_template
from .analyze import Analysis
from .config import FUSION

log = logging.getLogger("everform.identity")


@dataclass
class Reference:
    embedding: np.ndarray
    members: list[str]
    seeds: list[str]
    pitch_offset: float
    skin_target: list[float]

    def to_dict(self) -> dict:
        return {
            "embedding": [round(float(v), 6) for v in self.embedding],
            "members": self.members,
            "seeds": self.seeds,
            "pitch_offset": round(self.pitch_offset, 2),
            "skin_target": [round(v, 2) for v in self.skin_target],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Reference":
        e = np.array(d["embedding"], np.float32)
        return cls(embedding=e / (np.linalg.norm(e) + 1e-9), members=list(d["members"]), seeds=list(d.get("seeds", [])),
                   pitch_offset=float(d.get("pitch_offset", 0.0)), skin_target=list(d.get("skin_target", [])))


def _unit(v: np.ndarray) -> np.ndarray:
    return v / (np.linalg.norm(v) + 1e-9)


def build_reference(analyses: dict[str, Analysis], seed_ids: list[str]) -> Reference:
    """`analyses` are bootstrap analyses (no identity filter) of Film Canon images."""
    seeds = []
    for sid in seed_ids:
        a = analyses.get(sid)
        if a and a.detections:
            seeds.append((sid, max(a.detections, key=lambda d: d.box[2] * d.box[3]).embedding))
    if len(seeds) < 2:
        raise RuntimeError(f"only {len(seeds)} canonical face seeds were detected; cannot lock identity")
    c = _unit(np.mean([e for _, e in seeds], axis=0))

    cands = []
    for rid, a in analyses.items():
        if a.n_faces == 1 and a.detections[0].box[2] >= 150 and a.detections[0].score >= 0.8:
            cands.append((rid, a.detections[0].embedding))
    members = dict(seeds)
    for _ in range(3):
        sims = {rid: float(e @ c) for rid, e in cands}
        members = dict(seeds)
        members.update({rid: e for rid, e in cands if sims[rid] >= 0.5})
        E = np.array(list(members.values()))
        s = E @ c
        keep = s >= np.quantile(s, 0.15)
        c = _unit(E[keep].mean(0))
    log.info("identity reference: %d seeds, %d members", len(seeds), len(members))

    pitches = [analyses[r].mesh.pitch for r in members if r in analyses and analyses[r].mesh is not None]
    pitch_offset = float(np.median(pitches)) if pitches else 0.0
    return Reference(embedding=c.astype(np.float32), members=sorted(members), seeds=[s for s, _ in seeds],
                     pitch_offset=pitch_offset, skin_target=[])


def build_canonical_template(analyses: dict[str, Analysis], ref: Reference) -> tuple[Template, list[str]]:
    shapes, used = [], []
    for rid in ref.members:
        a = analyses.get(rid)
        if a is None or a.mesh is None:
            continue
        m = a.mesh
        if abs(m.yaw) <= 14 and abs(m.pitch - ref.pitch_offset) <= 12 and abs(m.roll) <= 30 \
                and m.blend.get("jawOpen", 0) < 0.15 and a.measures.get("iod_px", 0) >= 70:
            shapes.append(m.points)
            used.append(rid)
    if len(shapes) < 5:
        # Fall back to every reference member with a mesh rather than fail.
        used = [r for r in ref.members if analyses.get(r) is not None and analyses[r].mesh is not None]
        shapes = [analyses[r].mesh.points for r in used]
    pts = build_template(shapes, FUSION)
    log.info("canonical template from %d near-frontal faces", len(shapes))
    return Template(points=pts, width=FUSION.width, height=FUSION.height), used


def skin_target(analyses: dict[str, Analysis], ids: list[str]) -> list[float]:
    vals = [analyses[r].skin_lab for r in ids if r in analyses and analyses[r].skin_lab]
    if not vals:
        return [175.0, 141.0, 144.0, 22.0, 4.0, 6.0]
    return [float(v) for v in np.median(np.array(vals), axis=0)]
