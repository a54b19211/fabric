"""Named compatibility mappings: reviewed knowledge that a database structure which matches no clean prefix of a
role's files is nevertheless understood.

There is no generic "accept this difference" switch. An unknown structure produces a read-only difference report and
stops. To get past it, either a person who has looked at that exact structure adds an entry here (it is a code
change, reviewed like one), or a new corrective migration (a new V file, never an edit of an old one) brings the
structure to a known prefix. An entry matches only the exact observed fingerprint it names.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class CompatMapping:
    name: str
    db_role: str
    observed_fingerprint_sha256: str  # migration.fingerprint.digest of what the database showed
    meaning: str  # what this structure is and why it is understood
    treatment: Literal["equivalent_to_prefix", "requires_fix_migration"]
    target: int | str  # prefix length (equivalent_to_prefix) or the corrective migration's version (V0xx)
    reviewed_by: str


COMPAT_MAPPINGS: tuple[CompatMapping, ...] = ()


def find(role: str, fingerprint_sha256: str) -> CompatMapping | None:
    for mapping in COMPAT_MAPPINGS:
        if mapping.db_role == role and mapping.observed_fingerprint_sha256 == fingerprint_sha256:
            return mapping
    return None
