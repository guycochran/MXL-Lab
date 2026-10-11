# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Rankin
"""Safe operations on MXL flow directories owned by this lab."""

import os
import shutil
import uuid


def flow_path(domain, flow_id):
    """Return the exact directory for one UUID flow, rejecting path-like IDs."""
    try:
        canonical = str(uuid.UUID(flow_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"invalid MXL flow UUID: {flow_id!r}") from exc
    return os.path.join(domain, f"{canonical}.mxl-flow")


def clear_owned_flows(domain, flow_ids):
    """Remove only the named flow directories and return the removed paths."""
    removed = []
    for flow_id in dict.fromkeys(flow_ids):
        path = flow_path(domain, flow_id)
        if not os.path.lexists(path):
            continue
        if os.path.islink(path) or not os.path.isdir(path):
            os.unlink(path)
        else:
            shutil.rmtree(path)
        removed.append(path)
    return removed
