"""`scripts/bundle.py` lists, beside each archive's size, the disk its files take once unpacked, as `du` counts it (whole
4 KiB blocks, one per folder): what an installer tells a person before the download starts."""

import importlib.util
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

from tests.support.paths import REPO


def packed(tmp_path: Path) -> tuple[Path, int]:
    spec = importlib.util.spec_from_file_location("bundle", REPO / "scripts" / "bundle.py")
    assert spec is not None and spec.loader is not None
    bundle = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bundle)
    root = tmp_path / "root"
    (root / "bin").mkdir(parents=True)
    (root / "bin/galaius").write_bytes(b"a" * 1000)
    (root / "lib.py").write_bytes(b"b" * 4097)
    (root / "empty").write_bytes(b"")
    (root / "link").symlink_to("lib.py")
    return bundle.Bundler.packed(root, tmp_path / "program.tar.gz")


@pytest.mark.skipif(os.name == "nt", reason="symlinks")
def test_a_packed_program_reports_whole_blocks_per_file_and_folder(tmp_path: Path) -> None:
    path, unpacked = packed(tmp_path)
    with tarfile.open(path):
        pass
    # the folder, bin/, 1000 B -> 1 block, 4097 B -> 2 blocks, an empty file and a symlink -> none
    assert unpacked == 4096 * (1 + 1 + 1 + 2)


@pytest.mark.skipif(os.name == "nt" or not Path("/usr/bin/du").exists(), reason="du")
def test_the_figure_is_within_a_few_percent_of_du_on_this_file_system(tmp_path: Path) -> None:
    _, unpacked = packed(tmp_path)
    measured = int(subprocess.run(["du", "-sk", str(tmp_path / "root")], capture_output=True, text=True, check=True).stdout.split()[0]) * 1024
    if os.statvfs(tmp_path).f_bsize != 4096:
        pytest.skip("not a 4 KiB block file system")
    assert abs(unpacked - measured) <= 0.05 * measured
