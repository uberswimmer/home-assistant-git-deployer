#!/usr/bin/env python3
from __future__ import annotations

# =============================================================================
# HOME AUTOMATION GIT DEPLOYER 1.2.1
# =============================================================================
# Version history:
# 1.2.1 - 2026-08-23 - Added a metadata-only repository-managed update test release with no deployment-logic changes.
# 1.2.0 - 2026-08-23 - Prepared the deployer for repository-managed Home
# Assistant distribution and explicitly ignored GitHub workflow metadata.
# =============================================================================

import sys

import deployer_v110 as previous

VERSION = "1.2.1"
PREVIOUS_CLASSIFY_PATH = previous.classify_path


def classify_path(path: str) -> str:
    """Ignore GitHub repository metadata while preserving all 1.1 rules."""
    if path.startswith(".github/"):
        return "ignored"
    return PREVIOUS_CLASSIFY_PATH(path)


# Reuse the proven 1.1 implementation while changing only the version and the
# repository-path classification needed for GitHub Actions publishing metadata.
previous.VERSION = VERSION
previous.classify_path = classify_path


if __name__ == "__main__":
    sys.exit(previous.main())
