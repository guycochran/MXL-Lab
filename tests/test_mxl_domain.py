# SPDX-License-Identifier: Apache-2.0

import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / "gui"))

from lab.mxl_domain import clear_owned_flows, flow_path  # noqa: E402


FLOW_A = "4d584c10-2001-4c10-8000-000000000001"
FLOW_B = "4d584c10-2002-4c10-8000-000000000002"
UNRELATED = "4d584c10-9999-4c10-8000-000000000999"


class MxlDomainTests(unittest.TestCase):
    def test_flow_path_rejects_path_traversal(self):
        with self.assertRaises(ValueError):
            flow_path("/dev/shm/mxl/domain_ndi_lab", "../domain_1")

    def test_cleanup_removes_only_owned_flows(self):
        with tempfile.TemporaryDirectory() as domain:
            owned_a = pathlib.Path(flow_path(domain, FLOW_A))
            owned_b = pathlib.Path(flow_path(domain, FLOW_B))
            unrelated = pathlib.Path(flow_path(domain, UNRELATED))
            owned_a.mkdir()
            owned_b.mkdir()
            unrelated.mkdir()
            (unrelated / "grain").write_text("keep")

            removed = clear_owned_flows(domain, (FLOW_A, FLOW_B))

            self.assertEqual(set(removed), {os.fspath(owned_a), os.fspath(owned_b)})
            self.assertFalse(owned_a.exists())
            self.assertFalse(owned_b.exists())
            self.assertEqual((unrelated / "grain").read_text(), "keep")

    def test_cleanup_unlinks_owned_symlink_without_following_it(self):
        with tempfile.TemporaryDirectory() as domain, tempfile.TemporaryDirectory() as outside:
            target = pathlib.Path(outside) / "keep"
            target.mkdir()
            link = pathlib.Path(flow_path(domain, FLOW_A))
            link.symlink_to(target, target_is_directory=True)

            clear_owned_flows(domain, (FLOW_A,))

            self.assertFalse(link.exists())
            self.assertTrue(target.exists())


if __name__ == "__main__":
    unittest.main()
