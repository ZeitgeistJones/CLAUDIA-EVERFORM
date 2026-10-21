"""The sync loop: gallery -> index -> analysis -> ledgers -> render -> publish.

Every step is idempotent. Running it twice with no gallery changes does no
analysis, applies no accumulator deltas and publishes nothing.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

import numpy as np

from . import config, gallery
from .align import Template
from .download import _safe_name, fetch_image
from .fuse import CachedFace, CanvasGeometry, FilmAccumulator, face_signature, load_face, load_face_keys, save_face
from .store import SourceIndex, atomic_write_json, read_json

log = logging.getLogger("everform.pipeline")

IDENTITY_PATH = lambda: config.DATA_DIR / "identity.json"  # noqa: E731
OVERRIDES_PATH = lambda: config.DATA_DIR / "overrides.json"  # noqa: E731


def face_path(rid: str) -> Path:
    return config.CACHE_DIR / "faces" / f"{_safe_name(rid)}.npz"


def iter_faces(ids: Iterable[str]) -> Iterator[tuple[str, CachedFace]]:
    for rid in ids:
        p = face_path(rid)
        if p.exists():
            yield rid, load_face(p)


@contextmanager
def run_lock():
    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fh = open(config.CACHE_DIR / ".lock", "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RuntimeError("another Everform run is in progress")
    try:
        yield
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


# ---------------------------------------------------------------- identity lock

@dataclass
class Identity:
    reference: "object"   # identity.Reference
    template: Template

    def template_hash(self) -> str:
        return hashlib.sha256(json.dumps(self.template.to_dict()).encode()).hexdigest()[:12]


def load_identity() -> Identity | None:
    from .identity import Reference
    d = read_json(IDENTITY_PATH(), default=None)
    if not d:
        return None
    return Identity(reference=Reference.from_dict(d["reference"]), template=Template.from_dict(d["template"]))


def save_identity(idn: Identity, template_from: list[str]) -> Identity:
    """Persist the lock and return it exactly as stored, so later runs hash the same values."""
    atomic_write_json(IDENTITY_PATH(), {
        "about": "Claudia's identity lock: SFace reference embedding and the canonical landmark template. "
                 "Frozen after bootstrap; delete this file to re-derive it.",
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "pipeline": config.PIPELINE_VERSION,
        "reference": idn.reference.to_dict(),
        "template_from": template_from,
        "template": idn.template.to_dict(),
    }, indent=None)
    stored = load_identity()
    if stored is None:
        raise RuntimeError(f"identity lock could not be read back from {IDENTITY_PATH()}")
    return stored


# ---------------------------------------------------------------- dedup

def duplicate_groups(ids: list[str], keys: dict[str, tuple[np.ndarray, np.ndarray]]) -> dict[str, list[str]]:
    """Union-find over near-identical frames (same face embedding AND same pixels)."""
    th = config.THRESHOLDS
    parent = {r: r for r in ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    have = [r for r in ids if r in keys]
    if len(have) > 1:
        E = np.array([keys[r][0] for r in have])
        S = np.array([keys[r][1].ravel() for r in have])
        S = S / (np.linalg.norm(S, axis=1, keepdims=True) + 1e-6)
        esim = E @ E.T
        psim = S @ S.T
        for i in range(len(have)):
            for j in range(i + 1, len(have)):
                if esim[i, j] >= th.duplicate_embedding and psim[i, j] >= th.duplicate_pixel:
                    parent[find(have[i])] = find(have[j])
    groups: dict[str, list[str]] = {}
    for r in ids:
        groups.setdefault(find(r), []).append(r)
    return {r: sorted(groups[find(r)]) for r in ids}


# ---------------------------------------------------------------- the engine

@dataclass
class RunReport:
    started: str
    fetched: dict = field(default_factory=dict)
    changes: Counter = field(default_factory=Counter)
    analyzed: int = 0
    errors: list = field(default_factory=list)
    modes: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"started": self.started, "fetched": self.fetched, "changes": dict(self.changes),
                "analyzed": self.analyzed, "errors": self.errors[:50], "modes": self.modes}


class Engine:
    def __init__(self, session=None):
        config.ensure_dirs()
        self.session = session or gallery.make_session()
        self.index = SourceIndex()
        self.identity = load_identity()
        self._models = None
        self.overrides: dict[str, str] = read_json(OVERRIDES_PATH(), default={}) or {}

    @property
    def models(self):
        if self._models is None:
            from .analyze import Models
            self._models = Models()
        return self._models

    # -------------------------------------------------------- gallery

    def refresh_sources(self, report: RunReport) -> tuple[list[dict], gallery.CanonInfo]:
        records, meta = gallery.fetch_records(self.session)
        films = gallery.fetch_films(self.session, records)
        canon = gallery.fetch_canon(self.session)
        report.fetched = {**meta, "films": len(films)}
        tracked = [r for r in records if gallery.is_full_archive(r)]
        prev_total = int(self.index.meta.get("gallery_total") or 0)
        seen = set()
        for r in tracked:
            seen.add(r.id)
            report.changes[self.index.upsert_source(r.to_dict(), r.fingerprint())] += 1
        gone = [rid for rid in self.index.live() if rid not in seen]
        if gone and prev_total and len(records) < 0.5 * prev_total:
            # A half-empty response is an API problem, not a mass deletion.
            log.error("gallery returned %d records (was %d); not deleting %d images", len(records), prev_total, len(gone))
            report.errors.append(f"suspiciously small gallery response ({len(records)} records); deletions skipped")
        else:
            for rid in gone:
                self.index.mark_deleted(rid)
            report.changes["deleted"] += len(gone)
        self.index.meta.update({
            "gallery_total": len(records),
            "films": films,
            "stylized_looks": sorted(canon.stylized_looks),
            "canon_faces": canon.reference_ids,
        })
        return films, canon

    # -------------------------------------------------------- analysis

    def _stylized(self, src: dict, canon: gallery.CanonInfo) -> tuple[bool, str]:
        if src.get("look") in canon.stylized_looks:
            return True, f"stylized depiction ({src.get('look')} look), not her photographic self"
        if src.get("framing") == "sheet" or src.get("kind") == "sheet":
            return True, "reference sheet, not a film frame"
        return False, ""

    def _analyze(self, rid: str, canon: gallery.CanonInfo, *, identity: Identity | None):
        from .analyze import Context, analyze_image
        src = self.index.records[rid]["source"]
        img, sha = fetch_image(self.session, rid, src["url_m"])
        sty, why = self._stylized(src, canon)
        eye = gallery.worn_eyewear_reason(src.get("prompt"))
        ref = identity.reference if identity else None
        ctx = Context(reference=ref.embedding if ref else None, template=identity.template if identity else None,
                      stylized=sty, stylized_reason=why, pitch_offset=ref.pitch_offset if ref else 0.0,
                      eyewear_reason=eye or "")
        return analyze_image(self.models, img, ctx), sha

    def bootstrap_identity(self, canon: gallery.CanonInfo, limit: int | None = None) -> Identity:
        from .identity import build_canonical_template, build_reference, skin_target
        cands = [rid for rid, r in self.index.live().items() if gallery.is_film_canon(gallery.SourceRecord(**r["source"]))]
        cands.sort()
        if limit:
            seeds = [c for c in canon.reference_ids if c in cands]
            cands = seeds + [c for c in cands if c not in seeds][: max(0, limit - len(seeds))]
        log.info("bootstrapping identity from %d Film Canon images", len(cands))
        boot = {}
        for rid in cands:
            try:
                boot[rid], _ = self._analyze(rid, canon, identity=None)
            except Exception as e:  # noqa: BLE001
                log.warning("bootstrap skip %s: %s", rid, e)
        ref = build_reference(boot, canon.reference_ids)
        tpl, used = build_canonical_template(boot, ref)
        idn = Identity(reference=ref, template=tpl)
        full = {}
        for rid in ref.members:
            try:
                full[rid], _ = self._analyze(rid, canon, identity=idn)
            except Exception as e:  # noqa: BLE001
                log.warning("skin sample skip %s: %s", rid, e)
        ref.skin_target = skin_target(full, [r for r, a in full.items() if a.status == "included"])
        return save_identity(idn, used)

    def needs_analysis(self, rid: str) -> bool:
        r = self.index.records[rid]
        a = r.get("analysis")
        if not a or a.get("pipeline") != config.PIPELINE_VERSION or a.get("status") == "error":
            return True
        if a.get("identity_hash") != self.identity_hash:
            return True
        return bool(a.get("cached")) and not face_path(rid).exists()

    @property
    def identity_hash(self) -> str:
        return self.identity.template_hash() if self.identity else ""

    def analyze_pending(self, canon: gallery.CanonInfo, modes: list[str], report: RunReport, limit: int | None = None) -> None:
        todo = []
        for rid, r in self.index.live().items():
            rec = gallery.SourceRecord(**r["source"])
            if any(gallery.in_mode(rec, m) for m in modes) and self.needs_analysis(rid):
                todo.append(rid)
        # Canon first so the default portrait is ready soonest.
        todo.sort(key=lambda rid: (not self.index.records[rid]["source"]["plate"], rid))
        if limit is not None:
            todo = todo[:limit]
        log.info("%d images need analysis", len(todo))
        t0 = time.time()
        for i, rid in enumerate(todo, 1):
            try:
                a, sha = self._analyze(rid, canon, identity=self.identity)
                summ = a.summary()
                summ["cached"] = False
                if a.aligned is not None and a.masks is not None:
                    det = a.detections[a.chosen]
                    save_face(face_path(rid), a.aligned.image, a.masks, a.aligned.valid, a.aligned.stretch,
                              a.aligned.shape, a.skin_lab, det.embedding,
                              face_signature(a.aligned.image, self.identity.template.points))
                    summ["cached"] = True
            except Exception as e:  # noqa: BLE001
                log.warning("analysis failed for %s: %s", rid, e)
                summ, sha = {"status": "error", "reasons": [f"processing error: {e}"]}, None
                report.errors.append(f"{rid}: {e}")
            summ.update({"pipeline": config.PIPELINE_VERSION, "identity_hash": self.identity_hash,
                         "sha256": sha, "analyzed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
            self.index.records[rid]["analysis"] = summ
            report.analyzed += 1
            if i % 25 == 0:
                self.index.save()  # progress survives interruption
                log.info("analyzed %d/%d (%.1fs/image)", i, len(todo), (time.time() - t0) / i)
        self.index.save()

    def apply_prompt_gates(self) -> int:
        """Re-apply prompt-only exclusions (eyewear) to already-analysed images.

        Transparent tinted glasses often fail the pixel sunglass score. The gallery
        prompt is enough to catch them without re-running the vision models.
        """
        n = 0
        for rid, r in self.index.live().items():
            a = r.get("analysis")
            if not a or a.get("status") != "included":
                continue
            reason = gallery.worn_eyewear_reason(r["source"].get("prompt"))
            if not reason:
                continue
            reasons = list(a.get("reasons") or [])
            if reason not in reasons:
                reasons.append(reason)
            a["status"] = "excluded"
            a["reasons"] = reasons
            a["prompt_gate"] = "eyewear"
            n += 1
        if n:
            log.info("prompt gate excluded %d previously-included images (worn eyewear)", n)
            self.index.save()
        return n

    # -------------------------------------------------------- selection

    def eligible(self, mode: str) -> dict[str, dict]:
        """Records in this mode whose face may enter the fusion (before dedup)."""
        out = {}
        for rid, r in self.index.live().items():
            if not gallery.in_mode(gallery.SourceRecord(**r["source"]), mode):
                continue
            a = r.get("analysis") or {}
            ov = self.overrides.get(rid)
            ok = a.get("status") == "included"
            if ov == "include" and a.get("cached"):
                ok = True
            elif ov == "exclude":
                ok = False
            if ok and face_path(rid).exists():
                out[rid] = r
        return out

    def desired_weights(self, mode: str) -> tuple[dict[str, tuple[str, float]], dict[str, list[str]]]:
        el = self.eligible(mode)
        keys = {}
        for rid in el:
            try:
                keys[rid] = load_face_keys(face_path(rid))
            except (OSError, KeyError, ValueError) as e:
                log.warning("unreadable face cache for %s: %s", rid, e)
        groups = duplicate_groups(sorted(el), keys)
        desired = {}
        for rid, r in el.items():
            q = float(r["analysis"].get("quality") or 0.0)
            if self.overrides.get(rid) == "include":
                q = max(q, 0.2)
            desired[rid] = (r["source"]["film"], q / len(groups[rid]))
        return desired, groups

    # -------------------------------------------------------- accumulators

    def acc_dir(self, mode: str) -> Path:
        return config.CACHE_DIR / "acc" / mode

    def acc_signature(self) -> dict:
        s = config.FUSION
        return {"template": self.identity_hash, "w": s.width, "h": s.height, "classes": s.class_weights,
                "colour": s.color_strength, "target": self.identity.reference.skin_target,
                "pipeline": config.PIPELINE_VERSION}

    def load_accs(self, mode: str) -> dict[str, FilmAccumulator]:
        d = self.acc_dir(mode)
        d.mkdir(parents=True, exist_ok=True)
        sig = read_json(d / "meta.json", default=None)
        if sig != self.acc_signature():
            if sig is not None:
                log.info("accumulator settings changed for %s; rebuilding from cached faces", mode)
            for p in d.glob("*.npz"):
                p.unlink()
            atomic_write_json(d / "meta.json", self.acc_signature())
        s = config.FUSION
        return {p.stem: FilmAccumulator.load(p, s.height, s.width) for p in d.glob("*.npz")}

    def reconcile(self, mode: str, desired: dict[str, tuple[str, float]]) -> tuple[dict[str, FilmAccumulator], dict]:
        """Bring each film's sums in line with `desired` by applying only the differences."""
        s = config.FUSION
        accs = self.load_accs(mode)
        geo = CanvasGeometry.from_points(self.identity.template.points, s.height, s.width)
        target = self.identity.reference.skin_target
        films = {f for f, _ in desired.values()} | set(accs)
        delta = {"added": 0, "removed": 0, "reweighted": 0, "rebuilt_films": []}
        for film in sorted(films):
            acc = accs.get(film) or FilmAccumulator(s.height, s.width)
            want = {rid: w for rid, (f, w) in desired.items() if f == film}
            have = dict(acc.ledger)
            remove = [r for r in have if r not in want or abs(have[r] - want[r]) > 1e-12]
            add = [r for r in want if r not in have or r in remove]
            if not remove and not add:
                accs[film] = acc
                continue
            try:
                for rid in remove:
                    acc.apply(rid, load_face(face_path(rid)), have[rid], -1, target, s, geo)
            except (FileNotFoundError, OSError, KeyError, ValueError):
                # A removed image's cached face is gone: rebuild this film from what remains.
                log.info("rebuilding film %s for %s from cached faces", film, mode)
                acc = FilmAccumulator(s.height, s.width)
                add = list(want)
                delta["rebuilt_films"].append(film)
            for rid in add:
                acc.apply(rid, load_face(face_path(rid)), want[rid], +1, target, s, geo)
            delta["reweighted"] += len(set(remove) & set(add))
            delta["removed"] += len(set(remove) - set(add))
            delta["added"] += len(set(add) - set(remove))
            path = self.acc_dir(mode) / f"{film}.npz"
            if acc.count == 0:
                if path.exists():
                    path.unlink()
            else:
                acc.save(path)
            accs[film] = acc
        return {f: a for f, a in accs.items() if a.count > 0}, delta


def counts_for(index: SourceIndex, mode: str, contributors: set[str], overrides: dict) -> dict:
    c = Counter()
    for rid, r in index.live().items():
        if not gallery.in_mode(gallery.SourceRecord(**r["source"]), mode):
            continue
        c["matching"] += 1
        a = r.get("analysis") or {}
        if rid in contributors:
            c["fused"] += 1
        elif not a:
            c["pending"] += 1
        elif a.get("status") == "error":
            c["errors"] += 1
        elif a.get("status") == "review" and overrides.get(rid) is None:
            c["review"] += 1
        else:
            c["excluded"] += 1
    return {k: c.get(k, 0) for k in ("matching", "fused", "excluded", "review", "pending", "errors")}
