"""`scripts/bundle.py` lists, beside each archive's size, the bytes its files take once unpacked: what an installer tells a
person before the download starts."""

import importlib.util
import tarfile
from pathlib import Path

from tests.support.paths import REPO


def test_a_packed_program_reports_the_bytes_of_its_files_once_unpacked(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("bundle", REPO / "scripts" / "bundle.py")
    assert spec is not None and spec.loader is not None
    bundle = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bundle)
    root = tmp_path / "root"
    (root / "bin").mkdir(parents=True)
    (root / "bin/galaius").write_bytes(b"a" * 1000)
    (root / "lib.py").write_bytes(b"b" * 234)
    path, unpacked = bundle.Bundler.packed(root, tmp_path / "program.tar.gz")
    with tarfile.open(path) as archive:
        assert unpacked == 1234 == sum(member.size for member in archive if member.isreg())
