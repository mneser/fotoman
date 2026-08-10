"""Basic normalize / inspect / dedup tests (no Google network)."""

from __future__ import annotations

import zipfile
from pathlib import Path

from reclaim.normalize import inspect_input, normalize
from reclaim.place import DedupMode, Layout
from reclaim.util import ExitCode, file_sha1


def _make_zip(path: Path, files: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)


def test_inspect_and_normalize_facebook_like(tmp_path: Path) -> None:
    zpath = tmp_path / "facebook-posts.zip"
    payload = b"fake-jpeg-bytes-aaaa"
    _make_zip(
        zpath,
        {
            "posts/media/album1/photo1.jpg": payload,
            "posts/media/album1/photo2.png": b"other-bytes",
        },
    )
    report = inspect_input(zpath)
    assert report.kind == "zip"
    assert report.media_count == 2
    assert report.source_guess == "facebook"

    out = tmp_path / "out"
    stats, code = normalize(
        zpath,
        out,
        source="facebook",
        layout=Layout.ALBUMS,
        dedup=DedupMode.SKIP,
    )
    assert code == ExitCode.OK
    assert stats.extracted == 2
    assert (out / "facebook").exists()
    assert (out / ".reclaim" / "manifest.jsonl").exists()


def test_dedup_skip_on_renormalize(tmp_path: Path) -> None:
    zpath = tmp_path / "ig.zip"
    data = b"unique-content-xyz"
    _make_zip(zpath, {"media/posts/a.jpg": data})
    out = tmp_path / "out"
    stats1, code1 = normalize(zpath, out, source="instagram", dedup=DedupMode.SKIP)
    assert code1 == ExitCode.OK
    assert stats1.extracted == 1

    stats2, code2 = normalize(zpath, out, source="instagram", dedup=DedupMode.SKIP)
    assert code2 == ExitCode.OK
    assert stats2.skipped_existing >= 1
    assert stats2.extracted == 0


def test_multipart_detection(tmp_path: Path) -> None:
    from reclaim.normalize import find_multipart_zips

    p1 = tmp_path / "takeout-20240101T000000Z-001.zip"
    p2 = tmp_path / "takeout-20240101T000000Z-002.zip"
    for p in (p1, p2):
        _make_zip(p, {"Takeout/Google Photos/Album/x.jpg": b"x"})
    parts = find_multipart_zips(p1)
    assert len(parts) == 2


def test_sha1_stable(tmp_path: Path) -> None:
    f = tmp_path / "a.bin"
    f.write_bytes(b"hello")
    assert file_sha1(f) == file_sha1(f)
