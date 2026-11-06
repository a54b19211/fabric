"""The one lease mechanism behind Fabric's PostgreSQL work queues.

A job row carries ``lease_token`` and ``lease_until``. A worker owns the job while its token is in the row (and,
where the queue asks for it, the lease has not run out); it renews while it works and clears or expires the
lease when it is done. Claims lock the candidate row with ``FOR UPDATE ... SKIP LOCKED``, so two workers never
wait on each other or take the same job; a row whose lease ran out is claimable again.

The queues differ in what makes a job due, how jobs are ordered and what else a claim or a release writes; that
stays with each queue. What is the same everywhere lives here: the vacancy test, taking the lease with a fresh
token, checking and renewing ownership, and the guarded update only the current owner can make.
Everything runs inside the caller's transaction (``conn``).
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import text

NOW = "clock_timestamp()"
_UNTIL = NOW + "+make_interval(secs=>:_lease_seconds)"


def vacant(alias: str = "") -> str:
    """SQL: the row has no live lease (never leased, released, or run out)."""
    p = alias + "." if alias else ""
    return f"({p}lease_until IS NULL OR {p}lease_until<{NOW})"


def backoff(attempts: int, *, base: float, cap: float, max_exponent: int = 8) -> float:
    """Exponential retry delay: base * 2**attempts, the exponent and the result both capped."""
    return min(cap, base * 2 ** min(attempts, max_exponent))


@dataclass(frozen=True)
class LeaseQueue:
    """A table of leased jobs; ``key`` names the columns that identify one job."""

    table: str
    key: tuple[str, ...]

    def _match(self, job) -> tuple[str, dict]:
        where = " AND ".join(f"{column}=:_key_{column}" for column in self.key)
        return where, {f"_key_{column}": job[column] for column in self.key}

    @staticmethod
    def _owned(unexpired: bool) -> str:
        return "lease_token=:_lease_token" + (f" AND lease_until>{NOW}" if unexpired else "")

    def claim(self, conn, *, lease_seconds: float, where: str, order_by: str, params: dict | None = None,
              alias: str = "q", columns: str | None = None, joins: str = "", free: str | None = None,
              also_set: str = "") -> dict | None:
        """Lock the first due job that is free and take its lease; None when there is none.

        ``where``: what makes a job due (without the lease test). ``free``: replaces the default vacancy test
        (``vacant(alias)``) for queues whose state column also says whether a job is held. ``also_set``: more
        assignments written with the lease (``:_lease_seconds`` is bound). Returns the selected row plus its new
        ``lease_token``."""
        sql = (f"SELECT {columns or alias + '.*'} FROM {self.table} {alias} {joins} "
               f"WHERE ({where}) AND {free or vacant(alias)} "
               f"ORDER BY {order_by} FOR UPDATE OF {alias} SKIP LOCKED LIMIT 1")
        row = conn.execute(text(sql), params or {}).mappings().first()
        if row is None:
            return None
        token = uuid4().hex
        match, keys = self._match(row)
        conn.execute(text(
            f"UPDATE {self.table} SET lease_token=:_lease_token,lease_until={_UNTIL}"
            + (f",{also_set}" if also_set else "") + f" WHERE {match}"),
            {**(params or {}), **keys, "_lease_token": token, "_lease_seconds": lease_seconds})
        return dict(row, lease_token=token)

    def holds(self, conn, job, *, unexpired: bool = True, lock: bool = False) -> bool:
        """Whether ``job["lease_token"]`` still owns the job; ``lock`` keeps the row locked for the transaction."""
        match, keys = self._match(job)
        return conn.execute(text(
            f"SELECT 1 FROM {self.table} WHERE {match} AND {self._owned(unexpired)}" + (" FOR UPDATE" if lock else "")
        ), {**keys, "_lease_token": job["lease_token"]}).first() is not None

    def renew(self, conn, job, lease_seconds: float) -> bool:
        """Extend a live lease; False when the lease is no longer this worker's."""
        return self.update(conn, job, f"lease_until={_UNTIL}", {"_lease_seconds": lease_seconds}) == 1

    def update(self, conn, job, assignments: str, params: dict | None = None, *, unexpired: bool = True,
               where_also: str = "") -> int:
        """UPDATE only the current owner can make; returns the row count (0: the lease was lost)."""
        match, keys = self._match(job)
        return conn.execute(text(
            f"UPDATE {self.table} SET {assignments} WHERE {match} AND {self._owned(unexpired)}"
            + (f" AND {where_also}" if where_also else "")
        ), {**(params or {}), **keys, "_lease_token": job["lease_token"]}).rowcount

    def release(self, conn, job, assignments: str = "", params: dict | None = None, *, unexpired: bool = True,
                where_also: str = "") -> int:
        """Clear the lease (token and time) together with ``assignments``; owner only, returns the row count."""
        return self.update(conn, job, "lease_token=NULL,lease_until=NULL" + (f",{assignments}" if assignments else ""),
                           params, unexpired=unexpired, where_also=where_also)

    def claim_key(self, conn, values: dict, lease_seconds: float) -> str | None:
        """Claims-table form: take the lease on the row named by ``values`` (inserting it when absent) unless a
        live lease holds it. Returns the new token, or None when someone else holds the job."""
        token = uuid4().hex
        columns = ",".join(self.key)
        got = conn.execute(text(
            f"INSERT INTO {self.table}({columns},lease_token,lease_until) "
            f"VALUES ({','.join(':_key_' + c for c in self.key)},:_lease_token,{_UNTIL}) "
            f"ON CONFLICT ({columns}) DO UPDATE SET lease_token=EXCLUDED.lease_token,lease_until=EXCLUDED.lease_until "
            f"WHERE {self.table}.lease_until<={NOW} RETURNING lease_token"
        ), {**{f"_key_{c}": values[c] for c in self.key}, "_lease_token": token,
            "_lease_seconds": lease_seconds}).scalar()
        return token if got == token else None
