"""The throwaway-library container is started and torn down by the IDs and volumes it recorded (a fake docker)."""

import json
from types import SimpleNamespace

import pytest

from migration.app_ledger import MigrationStateError
from migration.scratch_db import LABEL, ScratchPostgres


class FakeDocker:
    def __init__(self, *, ready=True, label=None, leave_container=False):
        self.calls, self.ready, self.label, self.leave = [], ready, label, leave_container
        self.volumes_present = {"anon-vol-1", "anon-vol-2"}
        self.container_present = False

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        out, code = "", 0
        verb = args[1]
        if verb in ("run", "create"):
            self.container_present, out = True, "c0ffee" * 10
        elif verb == "inspect" and args[2] == "-f" and "Mounts" in args[3]:
            out = "anon-vol-1 anon-vol-2 "
        elif verb == "inspect" and args[2] == "-f":
            out, code = (self.label or self._run_id) + "\n", 0 if self.container_present else 1
        elif verb == "exec" and "pg_isready" in args:
            code = 0 if self.ready else 1
        elif verb == "rm":
            self.container_present = self.leave
            self.volumes_present -= {"anon-vol-1"}  # `rm -v` takes the anonymous volume
        elif verb == "volume" and args[2] == "inspect":
            code = 0 if args[3] in self.volumes_present else 1
        elif verb == "volume" and args[2] == "rm":
            self.volumes_present.discard(args[3])
        elif verb == "ps":
            out = "c0ffee" * 10 if self.container_present else ""
        return SimpleNamespace(returncode=code, stdout=out, stderr="")

    _run_id = ""


def scratch(tmp_path, docker):
    sp = ScratchPostgres(image="img:1", manifest=str(tmp_path / "m.jsonl"), run=docker, sleep=lambda s: None)
    docker._run_id = sp.run_id
    return sp


def lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_the_container_id_label_and_volumes_are_recorded_before_anything_is_removed(tmp_path):
    docker = FakeDocker()
    sp = scratch(tmp_path, docker)
    url = sp.start()
    assert url.startswith("postgresql://postgres@127.0.0.1:") and url.endswith("/scratch")
    created = lines(tmp_path / "m.jsonl")
    assert created[0]["event"] == "created" and created[0]["id"] == "c0ffee" * 10
    assert created[0]["label"] == f"{LABEL}={sp.run_id}"
    assert created[1]["event"] == "volumes" and created[1]["volumes"] == ["anon-vol-1", "anon-vol-2"]
    run_call = next(c for c in docker.calls if c[1] == "create")
    assert "--label" in run_call and f"{LABEL}={sp.run_id}" in run_call and "--name" not in run_call
    assert any(arg.startswith("127.0.0.1:") for arg in run_call)  # bound to loopback only


def test_teardown_acts_on_the_recorded_id_and_volume_names_and_checks_by_label(tmp_path):
    docker = FakeDocker()
    sp = scratch(tmp_path, docker)
    sp.start()
    assert sp.stop() == {"container_left": False, "volumes_left": []}
    removal = [c for c in docker.calls if c[1] in ("stop", "rm")]
    assert removal == [["docker", "stop", "-t", "20", "c0ffee" * 10], ["docker", "rm", "-v", "c0ffee" * 10]]
    assert ["docker", "volume", "rm", "anon-vol-2"] in docker.calls  # the recorded name; vol-1 went with rm -v
    assert any(c[1] == "ps" and f"label={LABEL}={sp.run_id}" in c for c in docker.calls)
    assert lines(tmp_path / "m.jsonl")[-1]["event"] == "removed"
    assert not any("--filter" in c and any(a.startswith("name=") for a in c) for c in docker.calls)  # never by name


def test_a_container_that_does_not_carry_this_runs_label_is_not_touched(tmp_path):
    docker = FakeDocker(label="somebody-elses-run")
    sp = scratch(tmp_path, docker)
    sp.start()
    with pytest.raises(MigrationStateError, match="does not carry this run's label"):
        sp.stop()
    assert not [c for c in docker.calls if c[1] in ("stop", "rm")]
    assert lines(tmp_path / "m.jsonl")[-1]["event"] == "not_touched"


def test_something_left_after_teardown_is_an_error_not_a_quiet_success(tmp_path):
    docker = FakeDocker(leave_container=True)
    sp = scratch(tmp_path, docker)
    sp.start()
    with pytest.raises(MigrationStateError, match="left behind"):
        sp.stop()


def test_a_library_that_never_becomes_ready_is_torn_down_before_the_error_leaves(tmp_path):
    docker = FakeDocker(ready=False)
    sp = scratch(tmp_path, docker)
    with pytest.raises(MigrationStateError, match="did not become ready"):
        sp.start()
    assert ["docker", "rm", "-v", "c0ffee" * 10] in docker.calls


def test_stopping_before_starting_does_nothing(tmp_path):
    docker = FakeDocker()
    assert scratch(tmp_path, docker).stop() == {"container_left": False, "volumes_left": []}
    assert docker.calls == []


def test_a_start_that_fails_after_the_container_exists_is_torn_down_by_its_id(tmp_path):
    docker = FakeDocker()
    original = docker.__call__

    def failing_start(args, **kwargs):
        if args[1] == "start":
            docker.calls.append(args)
            return SimpleNamespace(returncode=1, stdout="", stderr="port is already allocated")
        return original(args, **kwargs)

    sp = ScratchPostgres(image="img:1", manifest=str(tmp_path / "m.jsonl"), run=failing_start, sleep=lambda s: None)
    docker._run_id = sp.run_id
    with pytest.raises(MigrationStateError, match="docker start failed"):
        sp.start()
    assert ["docker", "rm", "-v", "c0ffee" * 10] in docker.calls
    assert lines(tmp_path / "m.jsonl")[0]["event"] == "created"


def test_a_failed_create_that_left_a_labelled_container_is_still_removed(tmp_path):
    docker = FakeDocker()
    original = docker.__call__

    def failing_create(args, **kwargs):
        if args[1] == "create":
            docker.calls.append(args)
            docker.container_present = True  # created, then failed
            return SimpleNamespace(returncode=1, stdout="", stderr="boom")
        return original(args, **kwargs)

    sp = ScratchPostgres(image="img:1", manifest=str(tmp_path / "m.jsonl"), run=failing_create, sleep=lambda s: None)
    docker._run_id = sp.run_id
    with pytest.raises(MigrationStateError, match="docker create failed"):
        sp.start()
    assert ["docker", "rm", "-v", "c0ffee" * 10] in docker.calls
