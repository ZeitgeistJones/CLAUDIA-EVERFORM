"""Atomic file helpers and the persistent source index."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from . import config


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)  # mkstemp creates 0600; published files must be readable by the web server
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def atomic_write_json(path: Path, obj: Any, indent: int | None = 1) -> None:
    atomic_write_bytes(path, json.dumps(obj, indent=indent, ensure_ascii=False, sort_keys=False).encode("utf-8"))


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with open(path, "rb") as f:
            return json.loads(f.read())
    except FileNotFoundError:
        return default


def atomic_write_image(path: Path, img: np.ndarray, ext: str | None = None, params: list[int] | None = None) -> None:
    ext = ext or path.suffix
    ok, buf = cv2.imencode(ext, img, params or [])
    if not ok:
        raise RuntimeError(f"could not encode {path}")
    atomic_write_bytes(path, buf.tobytes())


def atomic_save_npz(path: Path, **arrays: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".npz")
    os.close(fd)
    try:
        np.savez(tmp, **arrays)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


class SourceIndex:
    """Every Claudia record the worker has seen, with its processing outcome.

    Stored as data/index.json. Records are never dropped silently: images that
    disappear from the gallery are marked deleted so their contribution is removed.
    """

    def __init__(self, path: Path | None = None):
        self.path = path or (config.DATA_DIR / "index.json")
        raw = read_json(self.path, default=None) or {}
        self.records: dict[str, dict] = raw.get("records", {})
        self.meta: dict = raw.get("meta", {})

    def save(self) -> None:
        ordered = dict(sorted(self.records.items()))
        atomic_write_json(self.path, {"meta": self.meta, "records": ordered}, indent=None)

    def get(self, rid: str) -> dict | None:
        return self.records.get(rid)

    def upsert_source(self, rec: dict, fingerprint: str) -> str:
        """Returns 'new', 'modified', 'restored' or 'unchanged'."""
        cur = self.records.get(rec["id"])
        if cur is None:
            self.records[rec["id"]] = {"source": rec, "fingerprint": fingerprint, "deleted": False}
            return "new"
        state = "unchanged"
        if cur.get("deleted"):
            state = "restored"
        if cur.get("fingerprint") != fingerprint:
            state = "modified"
            cur.pop("analysis", None)
        cur["source"] = rec
        cur["fingerprint"] = fingerprint
        cur["deleted"] = False
        return state

    def mark_deleted(self, rid: str) -> None:
        if rid in self.records:
            self.records[rid]["deleted"] = True

    def live(self) -> dict[str, dict]:
        return {k: v for k, v in self.records.items() if not v.get("deleted")}
