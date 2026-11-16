"""The processors of the processing layer and the sources each one applies to, declared in one place.

A processor's applicability is a predicate over the source (not a list kept beside it): the sources with attachments are
the collection catalog's, the registry-difference sources are the ones named here, the sources that are never cleaned
are the graph sources. The capabilities Fabric publishes per source (store.source_listing) and the
processing stages (clean.clean_stage, clean.registry_changes) read these names, so there is one list to change.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from clean import attachment_parse, clean_text, profile, registry_diff

# old graph packages (retired, moved to Atlas) are not cleaned
UNCLEANED_SOURCES = ("reviewed_graph", "primekg_graph")
# registry records whose versions are compared field by field (registry_diff)
REGISTRY_DIFF_SOURCES = frozenset({"clinicaltrials"})


def _attachment_sources() -> frozenset[str]:
    from fetch.catalog import ATTACHMENT_SOURCES

    return ATTACHMENT_SOURCES


@dataclass(frozen=True)
class Processor:
    name: str
    version: str
    artifact: str
    applies: Callable[[str], bool]


PROCESSORS = (
    Processor("clean_text", clean_text.PROCESSOR_VERSION, "derived_clean_text", lambda s: s not in UNCLEANED_SOURCES),
    Processor("attachment_parse", attachment_parse.PROCESSOR_VERSION, "derived_clean_text",
              lambda s: s in _attachment_sources()),
    Processor("registry_diff", registry_diff.PROCESSOR_VERSION, "derived_registry_changes",
              lambda s: s in REGISTRY_DIFF_SOURCES),
    Processor("profile", profile.PROCESSOR_VERSION, "derived_profiles", lambda s: s not in UNCLEANED_SOURCES),
)


def processor(name: str) -> Processor:
    return next(item for item in PROCESSORS if item.name == name)


def applicable(source_id: str) -> dict[str, str]:
    """processor name -> version, for the processors that apply to this source."""
    return {item.name: item.version for item in PROCESSORS if item.applies(source_id)}
