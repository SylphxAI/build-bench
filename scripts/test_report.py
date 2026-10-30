"""Selection regressions using the archived Actions job schema; no builds or API calls."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import report


class GitHubReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "github").mkdir()
        self.root = root
        self.patch = patch.object(report, "ROOT", str(root))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        archive = Path(__file__).resolve().parents[1] / "results/github/37582027940.jobs.json"
        self.job = next(j for j in json.loads(archive.read_text())["jobs"] if j["name"] == "run (ripgrep, 1)")

    def save(self, run_id, *, target="ripgrep", ns=range(1, 6), conclusion="success", build=True, prime=False):
        jobs = []
        if prime:
            jobs.append({"name": f"prime ({target})", "conclusion": "failure"})
        for n in ns:
            job = copy.deepcopy(self.job)
            job.update(name=f"run ({target}, {n})", conclusion=conclusion)
            step = next(s for s in job["steps"] if s["name"] == "build ripgrep")
            step["name"] = f"build {target}"
            job["steps"] = [step] if build else []
            jobs.append(job)
        (self.root / f"github/{run_id}.jobs.json").write_text(json.dumps({"run_id": run_id, "cache": "warm", "jobs": jobs}))

    def rows(self):
        return report.latest(report.github())

    def test_newer_failed_cell_replaces_old_successes(self):
        self.save(1)
        self.save(2, conclusion="failure", build=False)
        rows = self.rows()
        self.assertEqual(len(rows), 5)
        self.assertEqual({r["run_id"] for r in rows}, {2})
        self.assertTrue(all(not r["ok"] and r["command"] is None for r in rows))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            report.main()
        self.assertIn("| 0/5 | - | - | - | - | - | - |", output.getvalue())

    def test_missing_samples_are_visible_without_old_sample_backfill(self):
        self.save(1)
        self.save(2, ns=[1])
        rows = self.rows()
        self.assertEqual({r["run_id"] for r in rows}, {2})
        self.assertEqual(sum(r["ok"] for r in rows), 1)
        self.assertEqual([r["n"] for r in rows if r["result"] == "missing"], [2, 3, 4, 5])
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(report.sys, "argv", ["report.py", "--runs"]):
            report.main()
        self.assertIn("| 1/5 |", output.getvalue())
        self.assertIn("| 2 | - | missing | - | - | - |", output.getvalue())

    def test_failed_prime_with_no_matrix_samples_replaces_old_cell(self):
        self.save(1)
        self.save(2, ns=[], prime=True)
        rows = self.rows()
        self.assertEqual(len(rows), 5)
        self.assertEqual({r["run_id"] for r in rows}, {2})
        self.assertTrue(all(r["result"] == "missing" for r in rows))

    def test_targeted_rerun_preserves_other_targets(self):
        self.save(1, target="caddy")
        self.save(2)
        self.assertEqual({(r["target"], r["run_id"]) for r in self.rows()}, {("caddy", 1), ("ripgrep", 2)})

    def test_cancelled_and_skipped_jobs_remain_in_denominator(self):
        for conclusion in ["cancelled", "skipped"]:
            with self.subTest(conclusion=conclusion):
                self.save(1, conclusion=conclusion, build=False)
                rows = self.rows()
                self.assertEqual(len(rows), 5)
                self.assertTrue(all(not r["ok"] and r["result"] == conclusion for r in rows))

    def test_unexpanded_workflow_cannot_silently_reuse_older_cells(self):
        self.save(1)
        (self.root / "github/2.jobs.json").write_text(json.dumps({"run_id": 2, "cache": "warm", "jobs": [{"name": "run", "conclusion": "skipped"}]}))
        with self.assertRaisesRegex(ValueError, "2.*target"):
            self.rows()

    def test_in_progress_job_with_no_end_time_is_visible(self):
        self.save(1, ns=[1], build=False)
        path = self.root / "github/1.jobs.json"
        run = json.loads(path.read_text())
        run["jobs"][0].update(conclusion=None, status="queued", started_at=None, completed_at=None)
        path.write_text(json.dumps(run))
        row = next(r for r in self.rows() if r["n"] == 1)
        self.assertEqual(row["result"], "queued")
        self.assertFalse(row["ok"])
        self.assertIsNone(row["wall"])
        self.assertIsNone(row["queue"])


if __name__ == "__main__":
    unittest.main()
