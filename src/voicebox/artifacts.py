#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Shared helper for the ``record_dir`` artifact-path convention.

Both the pipecat child (``agent.py``, files it writes itself) and the browser
child (``browser_session.py``, files a page-scoped listener writes) report
artifacts under a caller-supplied ``record_dir`` by the same rule: absolute
path, included only if the file actually exists on disk.
"""

import os
from typing import Optional


def existing_artifact_path(record_dir: str, name: str) -> Optional[str]:
    """Absolute path to ``<record_dir>/<name>`` if that file exists, else ``None``."""
    path = os.path.abspath(os.path.join(record_dir, name))
    return path if os.path.exists(path) else None
