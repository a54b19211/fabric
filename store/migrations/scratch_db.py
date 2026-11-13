"""A throwaway PostgreSQL container for fingerprinting a role's migration files, torn down by what was recorded.

Starting writes one JSON line per fact to a manifest file: the run id, the container ID `docker run` returned, its
image, the label this run put on it, and the volume names `docker inspect` showed. Teardown acts on those records only:
it checks the recorded ID still carries this run's label, stops it, removes it with its anonymous volumes (`docker rm
-v`), removes the recorded volume names, and confirms by label that nothing of this run is left. A container is never
found, stopped or removed by its name.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from store.migrations.app_ledger import MigrationStateError

LABEL = "r2-runner-run"
DEFAULT_IMAGE = "paradedb/paradedb:v0.25.11-pg17"
PORTS = range(19800, 19990)


def _run(args, **kwargs):
    return subprocess.run(args, capture_output=True, text=True, **kwargs)


class ScratchPostgres:
    def __init__(self, *, image: str | None = None, manifest: str | None = None, run=_run, sleep=time.sleep):
        self.image = image or os.environ.get("FABRIC_SCRATCH_PG_IMAGE", DEFAULT_IMAGE)
        self.run_id = uuid.uuid4().hex[:12]
        self.manifest = Path(manifest or Path(tempfile.gettempdir()) / f"r2-runner-{self.run_id}.containers.jsonl")
        self._run, self._sleep = run, sleep
        self.container_id: str | None = None
        self.volumes: list[str] = []

    # ---- records ----
    def _append(self, line: dict) -> None:
        with self.manifest.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")

    def _prepare_manifest(self) -> None:
        """Before anything is created: the manifest's directory exists and can be written. A library that cannot be
        recorded is not started."""
        try:
            self.manifest.parent.mkdir(parents=True, exist_ok=True)
            self._append({"at": datetime.now(timezone.utc).isoformat(), "run": self.run_id, "event": "run",
                          "label": f"{LABEL}={self.run_id}", "image": self.image})
        except OSError as exc:
            raise MigrationStateError(
                f"cannot write the container manifest {self.manifest}: {exc}; nothing was started") from None

    def _record(self, **fact) -> None:
        """Registration never gets in the way of cleanup: if the manifest cannot be written now, the line goes to stderr
        (the diagnostic that survives) and the run carries on - the ID is also in memory, and teardown acts on it."""
        line = {"at": datetime.now(timezone.utc).isoformat(), "run": self.run_id, **fact}
        try:
            self._append(line)
        except OSError as exc:
            print(f"[scratch_db] manifest {self.manifest} not writable ({exc}); record: "
                  + json.dumps(line, ensure_ascii=False), file=sys.stderr, flush=True)

    def _docker(self, *args) -> str:
        result = self._run(["docker", *args])
        if result.returncode != 0:
            raise MigrationStateError(f"docker {args[0]} failed: {result.stderr.strip()[:300]}")
        return result.stdout.strip()

    @staticmethod
    def _free_port() -> int:
        for port in PORTS:
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", port)) != 0:
                    return port
        raise MigrationStateError("no free port in 19800-19989 for the throwaway library")

    # ---- lifecycle ----
    def start(self) -> str:
        """Start the container; returns the SQLAlchemy URL of its empty database."""
        self._prepare_manifest()
        port = self._free_port()
        try:
            self.container_id = self._docker(
                "create", "--label", f"{LABEL}={self.run_id}", "-e", "POSTGRES_HOST_AUTH_METHOD=trust",
                "-e", "POSTGRES_DB=scratch", "-p", f"127.0.0.1:{port}:5432", self.image)
            self._record(event="created", id=self.container_id, image=self.image, label=f"{LABEL}={self.run_id}",
                         port=port)
            volumes = self._docker("inspect", "-f",
                                   '{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}} {{end}}{{end}}',
                                   self.container_id)
            self.volumes = volumes.split()
            self._record(event="volumes", id=self.container_id, volumes=self.volumes)
            self._docker("start", self.container_id)  # a port taken meanwhile fails here, after the ID is registered
            self._wait_ready()
        except BaseException:
            # whatever failed - the create, its registration, the start - a container this run made is torn down by the
            # ID known here (or found by the run's unique label when create itself failed) before the error leaves
            if self.container_id is None:
                self._adopt_leftover()
            else:
                self.stop()
            raise
        return f"postgresql://postgres@127.0.0.1:{port}/scratch"

    def _adopt_leftover(self) -> None:
        """After a failed create: containers carrying this run's (unique) label are this run's; register and remove them."""
        found = self._run(["docker", "ps", "-aq", "--no-trunc", "--filter", f"label={LABEL}={self.run_id}"]).stdout.split()
        if found:
            self.container_id = found[0]
            self._record(event="created", id=self.container_id, image=self.image, label=f"{LABEL}={self.run_id}",
                         note="found after a failed create")
            volumes = self._run(["docker", "inspect", "-f", '{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}} {{end}}{{end}}',
                                 self.container_id]).stdout.split()
            self.volumes = volumes
            self.stop()

    def _wait_ready(self) -> None:
        for _ in range(90):
            ready = self._run(["docker", "exec", self.container_id, "pg_isready", "-U", "postgres", "-h", "127.0.0.1"])
            if ready.returncode == 0:
                self._sleep(3)  # the image restarts PostgreSQL once after its init scripts
                self._run(["docker", "exec", self.container_id, "psql", "-U", "postgres", "-d", "scratch", "-c",
                           "CREATE EXTENSION IF NOT EXISTS vector"])
                return
            self._sleep(1)
        raise MigrationStateError("the throwaway library did not become ready")

    def stop(self) -> dict:
        """Tear down what this run recorded; raises if anything of it is left."""
        if self.container_id is None:
            return {"container_left": False, "volumes_left": []}
        label = self._run(["docker", "inspect", "-f", f'{{{{index .Config.Labels "{LABEL}"}}}}', self.container_id])
        if label.returncode == 0 and label.stdout.strip() != self.run_id:
            self._record(event="not_touched", id=self.container_id, reason="label is not this run's")
            raise MigrationStateError(f"container {self.container_id} does not carry this run's label; not touched")
        if label.returncode == 0:
            self._run(["docker", "stop", "-t", "20", self.container_id])
            self._run(["docker", "rm", "-v", self.container_id])
        for volume in self.volumes:
            if self._run(["docker", "volume", "inspect", volume]).returncode == 0:
                self._run(["docker", "volume", "rm", volume])
        left = self._run(["docker", "ps", "-aq", "--no-trunc", "--filter", f"label={LABEL}={self.run_id}"]).stdout.split()
        volumes_left = [v for v in self.volumes if self._run(["docker", "volume", "inspect", v]).returncode == 0]
        self._record(event="removed", id=self.container_id, containers_left=left, volumes_left=volumes_left)
        if left or volumes_left:
            raise MigrationStateError(f"left behind after teardown: containers {left} volumes {volumes_left}")
        return {"container_left": False, "volumes_left": []}

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False
