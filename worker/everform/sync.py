"""Top-level operations: sync, rebuild, history, report."""

from __future__ import annotations

import hashlib
import json
import logging
import time

from . import config, gallery
from .fuse import FilmAccumulator, image_weights
from .pipeline import Engine, RunReport, counts_for, iter_faces, run_lock
from .publish import (change_score, contact_sheet, load_manifest, now_iso, prune_pngs, quality_gate,
                      remove_version_dirs, render, save_manifest, version_hash, write_version)
from .store import atomic_write_json

log = logging.getLogger("everform.sync")


def _settings_key(eng: Engine) -> dict:
    return {"fusion": config.FUSION.to_dict(), "template": eng.identity_hash, "pipeline": config.PIPELINE_VERSION,
            "render": config.RENDER_VERSION}


def _film_titles(eng: Engine) -> dict[str, dict]:
    return {f["id"]: f for f in eng.index.meta.get("films", [])}


def _mode_entry(manifest: dict, mode: str) -> dict:
    return manifest["modes"].setdefault(mode, {"label": config.MODE_LABELS[mode], "current": None, "versions": []})


def publish_version(eng: Engine, mode: str, accs: dict[str, FilmAccumulator], *, kind: str, label: str,
                    manifest: dict, force: bool = False) -> dict:
    """Render, gate and publish one portrait state. Returns an outcome record."""
    live = {f: a for f, a in accs.items() if a.count > 0}
    if not live:
        return {"outcome": "empty"}
    weights = image_weights({f: a.ledger for f, a in live.items()}, {f: a.mass for f, a in live.items()}, config.FUSION.film_balance)
    vhash = version_hash(weights, _settings_key(eng))
    me = _mode_entry(manifest, mode)
    if kind == "live" and not force:
        cur = next((v for v in me["versions"] if v["id"] == me.get("current")), None)
        if cur and cur.get("hash") == vhash:
            return {"outcome": "unchanged", "version": cur["id"]}

    t0 = time.time()
    r = render(live, eng.identity.template, lambda ids: iter_faces(ids), eng.identity.reference.skin_target)
    ok, gate = quality_gate(eng.models, r.portrait, eng.identity.reference.embedding)
    if not ok:
        log.error("%s %s render rejected: %s", mode, kind, gate.get("reason"))
        if kind == "live":
            me["lastRejected"] = {"at": now_iso(), "hash": vhash, "gate": gate}
        return {"outcome": "rejected", "gate": gate}

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    vid = f"{stamp}-{vhash}"
    order = sorted(weights, key=lambda k: -weights[k])
    sheet = contact_sheet(iter_faces(order), order)
    titles = _film_titles(eng)
    films = [f["id"] for f in eng.index.meta.get("films", []) if f["id"] in live]
    srcs = eng.index.records
    meta = {
        "id": vid, "mode": mode, "kind": kind, "label": label, "createdAt": now_iso(), "hash": vhash,
        "fused": len(weights), "films": films, "filmShares": {f: round(s, 5) for f, s in r.shares.items()},
        "sources": [{"id": k, "film": srcs[k]["source"]["film"], "weight": round(weights[k], 6), "page": srcs[k]["source"]["page"]} for k in order],
        "settings": _settings_key(eng), "gate": gate,
    }
    prev = next((v for v in me["versions"] if v["id"] == me.get("current")), None)
    prev_img = config.PUBLIC_DIR / mode / "v" / prev["id"] / "portrait.webp" if prev else None
    files = write_version(mode, vid, r, sheet, meta, keep_png=(kind == "live"))
    change = change_score(prev_img, r.portrait) if (prev_img and kind == "live") else None
    entry = {
        "id": vid, "kind": kind, "label": label, "createdAt": meta["createdAt"], "hash": vhash,
        "fused": len(weights), "films": films, "filmTitles": [titles.get(f, {}).get("title", f) for f in films],
        "change": change, "identity": gate.get("identity"), **files,
    }
    me["versions"].append(entry)
    if kind == "live":
        me["current"] = vid
        me.pop("lastRejected", None)
        prune_pngs(mode, {vid})
        # Keep one live portrait plus the reconstructed timeline — older lives only clutter git.
        stale = [v["id"] for v in me["versions"] if v["kind"] == "live" and v["id"] != vid]
        if stale:
            me["versions"] = [v for v in me["versions"] if v["id"] not in stale]
            remove_version_dirs(mode, stale)
    log.info("%s: published %s (%s, %d images, %.0fs)", mode, vid, label, len(weights), time.time() - t0)
    return {"outcome": "published", "version": vid, "change": change}


def build_history(eng: Engine, mode: str, accs: dict[str, FilmAccumulator], manifest: dict) -> list[str]:
    """Reconstruct the portrait as it stood after each earlier film, in the gallery's film order.

    The archive gives films' release months, not image dates, so states are keyed to films.
    """
    me = _mode_entry(manifest, mode)
    old = [v["id"] for v in me["versions"] if v["kind"] == "reconstructed"]
    me["versions"] = [v for v in me["versions"] if v["kind"] != "reconstructed"]
    remove_version_dirs(mode, old)
    order = [f for f in eng.index.meta.get("films", []) if f["id"] in accs and accs[f["id"]].count > 0]
    made = []
    for k in range(1, len(order)):
        subset = {f["id"]: accs[f["id"]] for f in order[:k]}
        f = order[k - 1]
        label = f"After {f['title']}" + (f" ({f['released']})" if f.get("released") else "")
        res = publish_version(eng, mode, subset, kind="reconstructed", label=label, manifest=manifest, force=True)
        if res.get("outcome") == "published":
            made.append(res["version"])
    recon = [v for v in me["versions"] if v["kind"] == "reconstructed"]
    rest = [v for v in me["versions"] if v["kind"] != "reconstructed"]
    me["versions"] = recon + rest
    me["historyKey"] = _history_key(eng, accs)
    return made


def _history_key(eng: Engine, accs: dict[str, FilmAccumulator]) -> str:
    """History is re-rendered when the film list or the render settings change, not per new image."""
    films = [f["id"] for f in eng.index.meta.get("films", []) if f["id"] in accs and accs[f["id"]].count > 0]
    raw = json.dumps({"films": films, "settings": _settings_key(eng)}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def write_report(eng: Engine, mode: str, desired: dict, groups: dict, weights: dict) -> dict:
    rows = []
    titles = _film_titles(eng)
    for rid, r in sorted(eng.index.live().items()):
        src = r["source"]
        if not gallery.in_mode(gallery.SourceRecord(**src), mode):
            continue
        a = r.get("analysis") or {}
        status = "fused" if rid in weights else (a.get("status") or "pending")
        if status == "included":
            status = "excluded"  # eligible but not fused (e.g. face cache missing): never claim it contributed
        ov = eng.overrides.get(rid)
        rows.append({
            "id": rid, "film": src["film"], "filmTitle": titles.get(src["film"], {}).get("title", src["film_title"]),
            "page": src["page"], "thumb": src["url_s"], "framing": src.get("framing"), "look": src.get("look"),
            "status": status, "reasons": a.get("reasons", []) if status != "fused" else [],
            "quality": a.get("quality"), "identity": a.get("identity"), "pose": a.get("pose"),
            "weight": round(weights.get(rid, 0.0), 6),
            "duplicates": [g for g in groups.get(rid, []) if g != rid],
            "override": ov,
        })
    rep = {"mode": mode, "label": config.MODE_LABELS[mode], "generatedFrom": eng.index.meta.get("gallery_total"), "images": rows}
    path = config.PUBLIC_DIR / mode / "report.json"
    old = None
    if path.exists():
        try:
            old = json.loads(path.read_text())
        except ValueError:
            old = None
    if old != rep:
        atomic_write_json(path, rep, indent=None)
    for rid in eng.index.live():
        eng.index.records[rid].setdefault("contribution", {})[mode] = round(weights.get(rid, 0.0), 6)
    return rep


def write_known(eng: Engine) -> None:
    """IDs the worker has already taken in, per mode, so the website can say what is still waiting."""
    known = {m: sorted(rid for rid, r in eng.index.live().items()
                       if gallery.in_mode(gallery.SourceRecord(**r["source"]), m)) for m in config.MODES}
    path = config.PUBLIC_DIR / "known.json"
    try:
        old = json.loads(path.read_text())
    except (OSError, ValueError):
        old = None
    if old != known:
        atomic_write_json(path, known, indent=None)


def _strip(m: dict) -> str:
    return json.dumps({k: v for k, v in m.items() if k != "generatedAt"}, sort_keys=True)


def run_sync(modes: list[str] | None = None, *, force: bool = False, limit: int | None = None,
             history: str = "auto", session=None) -> dict:
    modes = modes or list(config.MODES)
    with run_lock():
        eng = Engine(session=session)
        report = RunReport(started=now_iso())
        films, canon = eng.refresh_sources(report)
        if eng.identity is None:
            eng.identity = eng.bootstrap_identity(canon, limit=limit)
        eng.analyze_pending(canon, modes, report, limit=limit)
        eng.apply_prompt_gates()

        manifest = load_manifest()
        before = _strip(manifest)
        manifest["about"] = {
            "title": "CLAUDIA EVERFORM",
            "credit": "Claudia by anabology (open character). Source images: claudia.gallery.",
            "disclaimer": "Unofficial fan artwork. Not affiliated with Anthropic or claudia.gallery.",
            "gallery": config.GALLERY_URL,
        }
        for mode in modes:
            desired, groups = eng.desired_weights(mode)
            accs, delta = eng.reconcile(mode, desired)
            res = publish_version(eng, mode, accs, kind="live", label="Now", manifest=manifest, force=force)
            me = _mode_entry(manifest, mode)
            if res.get("outcome") == "published" and (history == "always" or (
                    history == "auto" and me.get("historyKey") != _history_key(eng, accs))):
                build_history(eng, mode, accs, manifest)
            live = {f: a for f, a in accs.items() if a.count > 0}
            weights = image_weights({f: a.ledger for f, a in live.items()}, {f: a.mass for f, a in live.items()},
                                    config.FUSION.film_balance) if live else {}
            write_report(eng, mode, desired, groups, weights)
            counts = counts_for(eng.index, mode, set(weights), eng.overrides)
            per_film = []
            shares = {}
            if live:
                from .fuse import film_shares
                shares = film_shares({f: a.mass for f, a in live.items()}, config.FUSION.film_balance)
            for f in eng.index.meta.get("films", []):
                fid = f["id"]
                matching = sum(1 for r in eng.index.live().values()
                               if r["source"]["film"] == fid and gallery.in_mode(gallery.SourceRecord(**r["source"]), mode))
                if not matching:
                    continue
                per_film.append({"id": fid, "title": f["title"], "released": f.get("released"), "matching": matching,
                                 "fused": sum(1 for k in weights if eng.index.records[k]["source"]["film"] == fid),
                                 "share": round(shares.get(fid, 0.0), 4)})
            me["counts"] = counts
            me["films"] = per_film
            me["filmBalance"] = config.FUSION.film_balance
            cur = next((v for v in me["versions"] if v["id"] == me.get("current")), None)
            me["updatedAt"] = cur["createdAt"] if cur else None
            report.modes[mode] = {"delta": delta, **res, "counts": counts}
            log.info("%s: %s %s", mode, res.get("outcome"), counts)
        if _strip(manifest) != before:
            save_manifest(manifest)
        write_known(eng)
        eng.index.save()
        out = report.to_dict()
        out["finished"] = now_iso()
        log_path = config.CACHE_DIR / "runs.jsonl"
        with open(log_path, "a") as fh:
            fh.write(json.dumps(out) + "\n")
        return out


def rebuild(modes: list[str] | None = None) -> dict:
    """Discard accumulators and re-derive them from cached faces, then publish if anything differs."""
    modes = modes or list(config.MODES)
    for m in modes:
        d = config.CACHE_DIR / "acc" / m
        for p in d.glob("*.npz"):
            p.unlink()
    return run_sync(modes)


def regenerate_history(modes: list[str] | None = None) -> None:
    modes = modes or list(config.MODES)
    with run_lock():
        eng = Engine()
        if eng.identity is None:
            raise RuntimeError("run a sync first")
        manifest = load_manifest()
        for mode in modes:
            accs = {f: a for f, a in eng.load_accs(mode).items() if a.count > 0}
            build_history(eng, mode, accs, manifest)
        save_manifest(manifest)
