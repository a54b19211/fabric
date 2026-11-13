"""ScratchPostgres against a real Docker daemon: a start that fails after the container exists is cleaned up by ID."""

import json
import subprocess

import pytest

from store.migrations.app_ledger import MigrationStateError
from store.migrations.scratch_db import LABEL, ScratchPostgres


def docker(*args):
    return subprocess.run(["docker", *args], capture_output=True, text=True)


def labelled(run_id):
    return docker("ps", "-aq", "--no-trunc", "--filter", f"label={LABEL}={run_id}").stdout.split()


@pytest.fixture
def occupied_port(tmp_path):
    """A host port that a container of this test holds (so the next container's bind fails inside Docker)."""
    holder = ScratchPostgres(manifest=str(tmp_path / "holder.jsonl"))
    url = holder.start()
    try:
        yield int(url.rsplit(":", 1)[1].split("/")[0])
    finally:
        holder.stop()


def test_a_port_taken_at_start_leaves_no_container_and_no_volume_and_the_manifest_says_what_happened(
        occupied_port, tmp_path, monkeypatch):
    victim = ScratchPostgres(manifest=str(tmp_path / "victim.jsonl"))
    monkeypatch.setattr(ScratchPostgres, "_free_port", staticmethod(lambda: occupied_port))
    with pytest.raises(MigrationStateError, match="docker start failed"):
        victim.start()
    assert victim.container_id is not None  # registered before it was started
    assert labelled(victim.run_id) == []  # removed by that ID
    events = [json.loads(line) for line in (tmp_path / "victim.jsonl").read_text().splitlines()]
    assert [e["event"] for e in events][:3] == ["run", "created", "volumes"] and events[-1]["event"] == "removed"
    assert events[-1]["containers_left"] == [] and events[-1]["volumes_left"] == []
    for volume in events[2]["volumes"]:
        assert docker("volume", "inspect", volume).returncode != 0  # the anonymous volume is gone too
