import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from reflex.types import strict_json
from tests.helpers import fixture_server, jev_response, chat_response


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = Path(__file__).resolve().parent.parent
        self.db = self.root / "workspace.sqlite3"
        self.empty_config = self.root / "empty-config.toml"
        self.empty_config.write_text("")

    def tearDown(self):
        self.temp.cleanup()

    def command(self, *args, config=None):
        return subprocess.run([sys.executable, "-m", "reflex", "--config",
                               str(config or self.empty_config), "--db", str(self.db), *args],
                              cwd=self.project, capture_output=True, text=True, timeout=20,
                              env={"PATH": "/usr/bin:/bin", "PYTHONIOENCODING": "utf-8"})

    def test_doctor_lists_missing_fields_without_network_or_database(self):
        result = self.command("doctor")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(strict_json(result.stdout)["ready"])
        self.assertFalse(self.db.exists())

    def test_run_without_keys_fails_before_creating_database(self):
        result = self.command("run", "Hello")
        self.assertEqual(result.returncode, 2)
        self.assertIn("not configured", result.stderr)
        self.assertFalse(self.db.exists())

    def test_init_and_schema_listing_work_without_keys(self):
        result = self.command("init")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.db.exists())
        result = self.command("tools")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(strict_json(result.stdout)), 11)

    def test_resume_and_export_across_real_cli_processes(self):
        replies = [("/jev", 200, jev_response("ask_clarification")),
                   ("/strong", 200, chat_response("ask_clarification", message="Which order?")),
                   ("/jev", 200, jev_response("get_order")),
                   ("/jev", 200, jev_response("refund_order")),
                   ("/jev", 200, jev_response("finish")),
                   ("/strong", 200, chat_response("finish", message="O-100 refund recorded locally"))]
        with fixture_server(replies) as (endpoint, requests):
            cfg = self.root / "config.toml"
            cfg.write_text('[http]\nmax_attempts=1\n' + '\n'.join(
                f'[{role}]\nendpoint="{endpoint}/{role}"\nmodel="{model}"\napi_key="test-secret-key"\n'
                for role, model in (("jev", "jev-1.13.0"), ("strong", "test-strong"))))
            result = self.command("init", config=cfg)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = self.command("run", "Refund one order", "--allow-refunds", config=cfg)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("waiting_for_user", result.stdout)
            session_id = re.search(r"session=([0-9a-f]{32})", result.stdout).group(1)
            result = self.command("resume", session_id, "Order O-100", "--order", "O-100", config=cfg)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("status=completed", result.stdout)
            exported = self.root / "trace.json"
            result = self.command("export", session_id, "--output", str(exported), config=cfg)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = strict_json(exported.read_text())
            self.assertEqual(data["state"]["status"], "completed")
            self.assertEqual(len(data["calls"]), 6)
            self.assertNotIn("test-secret-key", exported.read_text())
            result = self.command("export", session_id, "--output", str(exported), config=cfg)
            self.assertEqual(result.returncode, 2)

    def test_acceptance_file_validates_without_keys(self):
        result = self.command("eval", "--validate-only")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(strict_json(result.stdout)["valid_tasks"], 8)

    def test_split_and_audit_commands_without_credentials(self):
        source = str(self.project / "examples/suites/v03/bfcl/routing-base.jsonl")
        output = self.root / "split"
        result = self.command("experiment", "split", source, "--output", str(output))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(strict_json(result.stdout)["groups"], 271)
        result = self.command("experiment", "audit-split", str(output / "manifest.json"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(strict_json(result.stdout)["ready"])
        result = self.command("experiment", "run", "--suite", str(output / "dev.jsonl"),
                              "--split-manifest", str(output / "manifest.json"), "--partition", "test",
                              "--output", str(self.root / "bad-run"))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("not the declared split", result.stderr)
        self.assertFalse((self.root / "bad-run").exists())

    def test_risk_pair_cli_preflights_without_api_calls(self):
        result = self.command("experiment", "risk", "pair", "--plan", "examples/plans/risk-small.json",
            "--suite", "examples/splits/bfcl-routing/calibration.jsonl",
            "--split-manifest", "examples/splits/bfcl-routing/manifest.json")
        self.assertEqual(result.returncode, 3, result.stderr)
        report = strict_json(result.stdout)
        self.assertFalse(report["executed"])
        self.assertFalse(report["ready"])
        self.assertEqual(report["groups"], 55)
        self.assertEqual(report["zero_event_selected_groups_needed"]["unsafe"], 477)
        self.assertFalse(self.db.exists())
