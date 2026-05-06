from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import backend


class BackendRequestTests(unittest.TestCase):
    def test_main_accepts_json_backend_request(self) -> None:
        request = {
            "schema_version": 1,
            "command": "status",
            "options": {
                "service_node": True,
                "dashboard": True,
                "dashboard_host": "0.0.0.0",
                "dashboard_port": 18080,
            },
        }
        with tempfile.NamedTemporaryFile("w", encoding="utf-8") as handle:
            json.dump(request, handle)
            handle.flush()

            with patch.object(
                backend,
                "backend_status",
                return_value={"ok": True},
            ) as status:
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    exit_code = backend.main(["--request-json", handle.name])

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), {"ok": True})
        status.assert_called_once()
        self.assertTrue(status.call_args.kwargs["service_node"])
        self.assertTrue(status.call_args.kwargs["dashboard_enabled"])
        self.assertEqual(status.call_args.kwargs["dashboard_host"], "0.0.0.0")
        self.assertEqual(status.call_args.kwargs["dashboard_port"], 18080)


if __name__ == "__main__":
    unittest.main()
