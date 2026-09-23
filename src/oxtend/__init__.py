"""oxtend — the Orion Xtend bundle CLI (SPEC-67, ORION-688).

Turns an extension or client source directory into the signed, self-contained artifact
the extension kernel installs:

    validate → lock → build → contract-test → sign → package → push

The one design rule worth stating up front: **oxtend never re-implements a kernel
rule.** The manifest model and the scoped-migration gate are imported from the pinned
`orion-backend` wheel (`oxtend.manifest_vendored`). The reference CLI duplicated both,
which means a build tool and a runtime that can disagree about what is valid — CI
passes, install fails, and the error surfaces on a customer's cluster.
"""

from __future__ import annotations

__version__ = "0.1.1"

__all__ = ["__version__"]
