"""Adversarial proof of safeguard #1 (per-run isolation): a workspace A run cannot leave anything
a workspace B run, scheduled after it on the same machine, can read — disk, env, or /tmp. Real
gVisor containers; skipped where `runsc` is not registered as a Docker runtime."""

from uuid import uuid4

import pytest

from interact.sandbox import gvisor_available, run_pooled

pytestmark = pytest.mark.skipif(not gvisor_available(), reason="gVisor (runsc) is not installed/registered as a Docker runtime here")


def test_workspace_b_cannot_read_a_file_workspace_a_wrote() -> None:
    tenant_a_secret = f"secret-{uuid4().hex}"
    result_a = run_pooled(run_id=uuid4(), command=["sh", "-c", f'echo "{tenant_a_secret}" > /work/leftover.txt; echo wrote'])
    assert result_a.exit_code == 0 and "wrote" in result_a.stdout

    result_b = run_pooled(run_id=uuid4(), command=["sh", "-c", "cat /work/leftover.txt 2>&1 || echo NOTHING_THERE"])

    assert result_b.exit_code == 0
    assert tenant_a_secret not in result_b.stdout
    assert "NOTHING_THERE" in result_b.stdout


def test_workspace_b_cannot_read_workspace_as_env_var() -> None:
    tenant_a_secret = f"env-secret-{uuid4().hex}"
    result_a = run_pooled(run_id=uuid4(), command=["sh", "-c", "echo set"], env={"TENANT_A_TOKEN": tenant_a_secret})
    assert result_a.exit_code == 0

    result_b = run_pooled(run_id=uuid4(), command=["sh", "-c", 'echo "TENANT_A_TOKEN=${TENANT_A_TOKEN:-UNSET}"'])

    assert "UNSET" in result_b.stdout
    assert tenant_a_secret not in result_b.stdout


def test_workspace_b_cannot_read_workspace_as_tmp_scratch_file() -> None:
    tenant_a_secret = f"tmp-secret-{uuid4().hex}"
    result_a = run_pooled(run_id=uuid4(), command=["sh", "-c", f'echo "{tenant_a_secret}" > /tmp/scratch.txt; echo wrote'])
    assert result_a.exit_code == 0 and "wrote" in result_a.stdout

    result_b = run_pooled(run_id=uuid4(), command=["sh", "-c", "cat /tmp/scratch.txt 2>&1 || echo NOTHING_THERE"])

    assert tenant_a_secret not in result_b.stdout
    assert "NOTHING_THERE" in result_b.stdout


def test_an_input_file_path_cannot_escape_the_run_staging_directory() -> None:
    with pytest.raises(ValueError, match="escapes"):
        run_pooled(run_id=uuid4(), command=["true"], input_files={"../escape.txt": b"x"})
