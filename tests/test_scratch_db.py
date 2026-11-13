"""The throwaway-library container is started and torn down by the IDs and volumes it recorded (a fake docker)."""

import json
from types import SimpleNamespace

import pytest

from store.migrations.app_ledger import MigrationStateError
from store.migrations.scratch_db import LABEL, ScratchPostgres


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
    assert created[0]["event"] == "run" and created[0]["label"] == f"{LABEL}={sp.run_id}"  # before the create
    assert created[1]["event"] == "created" and created[1]["id"] == "c0ffee" * 10
    assert created[1]["label"] == f"{LABEL}={sp.run_id}"
    assert created[2]["event"] == "volumes" and created[2]["volumes"] == ["anon-vol-1", "anon-vol-2"]
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
    assert [e["event"] for e in lines(tmp_path / "m.jsonl")][:2] == ["run", "created"]


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


def failing_start(docker):
    original = docker.__call__

    def run(args, **kwargs):
        if args[1] == "start":
            docker.calls.append(args)
            return SimpleNamespace(returncode=1, stdout="", stderr="port is already allocated")
        return original(args, **kwargs)

    return run


def test_a_manifest_that_cannot_be_written_stops_the_run_before_anything_is_created(tmp_path):
    (tmp_path / "file").write_text("not a directory")
    docker = FakeDocker()
    sp = ScratchPostgres(image="img:1", manifest=str(tmp_path / "file" / "m.jsonl"), run=docker, sleep=lambda s: None)
    docker._run_id = sp.run_id
    with pytest.raises(MigrationStateError, match="cannot write the container manifest.*nothing was started"):
        sp.start()
    assert docker.calls == [] and sp.container_id is None  # not even a create


def test_a_missing_manifest_directory_is_prepared_before_the_create(tmp_path):
    docker = FakeDocker()
    sp = ScratchPostgres(image="img:1", manifest=str(tmp_path / "new" / "deeper" / "m.jsonl"), run=docker,
                         sleep=lambda s: None)
    docker._run_id = sp.run_id
    sp.start()
    first = lines(tmp_path / "new" / "deeper" / "m.jsonl")[0]
    assert first["event"] == "run" and first["label"] == f"{LABEL}={sp.run_id}"  # written before the create
    assert docker.calls[0][1] == "create"
    sp.stop()


def test_a_manifest_that_fails_to_write_after_the_create_never_prevents_the_cleanup(tmp_path, monkeypatch, capsys):
    docker = FakeDocker()
    sp = ScratchPostgres(image="img:1", manifest=str(tmp_path / "m.jsonl"), run=failing_start(docker),
                         sleep=lambda s: None)
    docker._run_id = sp.run_id
    appended = []
    real = ScratchPostgres._append

    def disk_full_after_the_first_line(self, line):
        if appended:
            raise OSError(28, "No space left on device")
        appended.append(line)
        real(self, line)

    monkeypatch.setattr(ScratchPostgres, "_append", disk_full_after_the_first_line)
    with pytest.raises(MigrationStateError, match="docker start failed"):  # the real error, not the I/O one
        sp.start()
    assert ["docker", "rm", "-v", "c0ffee" * 10] in docker.calls  # removed by the ID known in memory
    assert not docker.container_present
    err = capsys.readouterr().err
    assert "not writable" in err and "c0ffee" * 10 in err  # the registration and the result reach stderr
    assert '"event": "removed"' in err and '"containers_left": []' in err


def test_the_registration_itself_failing_still_tears_the_container_down(tmp_path, monkeypatch, capsys):
    docker = FakeDocker()
    sp = ScratchPostgres(image="img:1", manifest=str(tmp_path / "m.jsonl"), run=docker, sleep=lambda s: None)
    docker._run_id = sp.run_id

    def boom(**fact):
        raise RuntimeError("registration bug")

    monkeypatch.setattr(sp, "_record", boom)
    with pytest.raises(RuntimeError, match="registration bug"):
        sp.start()
    assert ["docker", "rm", "-v", "c0ffee" * 10] in docker.calls
