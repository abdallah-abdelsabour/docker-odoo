"""Tests for odoo_rpc.py against a fake Odoo server (no real instance needed).

Run: python3 -m unittest discover -s tools/tests
"""
import io
import json
import os
import sys
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import odoo_rpc  # noqa: E402

PARAMS = [{"id": 1, "key": "web.base.url", "value": "https://x.odoo.com", "write_date": "2026-01-01"},
          {"id": 2, "key": "mail.catchall.alias", "value": "catchall", "write_date": "2026-01-01"}]


def fake_model_call(model, method, ids, kw):
    """Tiny in-memory stand-in for the ORM, shared by both protocols."""
    if method == "context_get":
        return {"lang": "en_US", "tz": "Asia/Riyadh"}
    if model == "ir.config_parameter" and method == "search_read":
        needle = next((d[2] for d in kw.get("domain", []) if d[0] == "key"), "")
        return [p for p in PARAMS if needle.strip("%") in p["key"]]
    if model == "ir.config_parameter" and method == "get_param":
        return next((p["value"] for p in PARAMS if p["key"] == kw["key"]), False)
    if model == "ir.config_parameter" and method == "set_param":
        PARAMS.append({"id": 3, "key": kw["key"], "value": kw["value"], "write_date": "now"})
        return True
    if model == "res.partner" and method == "write":
        return {"ids": ids, "vals": kw["vals"]}
    if method == "search_count":
        return 42
    if method == "search_read":
        row = {"id": 2, "name": "Admin", "login": "admin", "company_id": [1, "Co"],
               "currency_id": [1, "SAR"], "country_id": False}
        return [{"id": row["id"], **{f: row.get(f, False) for f in kw.get("fields") or row}}]
    raise KeyError(f"{model}.{method}")


class FakeOdoo(BaseHTTPRequestHandler):
    json2 = True
    requests = []

    def log_message(self, *a):
        pass

    def _send(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
        FakeOdoo.requests.append((self.path, dict(self.headers), json.loads(json.dumps(payload))))
        if self.path.startswith("/json/2/"):
            if not FakeOdoo.json2:
                body = b"<html>404</html>"
                self.send_response(404)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.headers.get("Authorization") != "bearer KEY":
                return self._send(401, {"name": "werkzeug.exceptions.Unauthorized", "message": "Invalid apikey"})
            _, _, _, model, method = self.path.split("/")
            ids = payload.pop("ids", None)
            payload.pop("context", None)
            try:
                return self._send(200, fake_model_call(model, method, ids, payload))
            except KeyError as exc:
                return self._send(422, {"name": "builtins.AttributeError", "message": str(exc)})
        if self.path == "/jsonrpc":
            params = payload["params"]
            if params["service"] == "common" and params["method"] == "version":
                return self._send(200, {"result": {"server_version": "19.0+e"}})
            if params["service"] == "common" and params["method"] == "authenticate":
                return self._send(200, {"result": 2 if params["args"][2] == "KEY" else False})
            db, uid, pwd, model, method, args, kw = params["args"]
            ids = args.pop(0) if method in ("write", "read") else None
            names = odoo_rpc._POSITIONAL_NAMES.get(method, [])
            kw.update(zip(names, args))
            kw.pop("context", None)
            try:
                return self._send(200, {"result": fake_model_call(model, method, ids, kw)})
            except KeyError as exc:
                return self._send(200, {"error": {"message": "Odoo Server Error",
                                                  "data": {"name": "AttributeError", "message": str(exc)}}})
        self._send(404, {})


class OdooRPCTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeOdoo)
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        FakeOdoo.json2 = True
        FakeOdoo.requests.clear()
        for key in ("ODOO_URL", "ODOO_DB", "ODOO_API_KEY", "ODOO_LOGIN", "ODOO_PASSWORD", "ODOO_PROTOCOL"):
            os.environ.pop(key, None)

    def run_cli(self, *argv, login=None):
        base = ["--url", self.url, "--db", "testdb", "--api-key", "KEY", "--env-file", os.devnull]
        if login:
            base += ["--login", login]
        out = io.StringIO()
        with redirect_stdout(out):
            code = odoo_rpc.main(base + list(argv))
        return code, out.getvalue()

    def test_json2_detected_and_headers(self):
        client = odoo_rpc.OdooClient(self.url, "testdb", api_key="KEY")
        self.assertEqual(client.protocol, "json2")
        client.call("res.partner", "write", ids=[5], vals={"name": "X"})
        path, headers, payload = FakeOdoo.requests[-1]
        self.assertEqual(path, "/json/2/res.partner/write")
        self.assertEqual(headers["X-Odoo-Database"], "testdb")
        self.assertEqual(payload, {"ids": [5], "vals": {"name": "X"}})

    def test_fallback_to_jsonrpc_on_old_servers(self):
        FakeOdoo.json2 = False
        client = odoo_rpc.OdooClient(self.url, "testdb", api_key="KEY", login="admin")
        self.assertEqual(client.protocol, "jsonrpc")
        self.assertEqual(client.call("res.partner", "write", ids=[5], vals={"name": "X"}),
                         {"ids": [5], "vals": {"name": "X"}})

    def test_positional_args_mapped_for_json2(self):
        client = odoo_rpc.OdooClient(self.url, "testdb", api_key="KEY", protocol="json2")
        self.assertEqual(client.call("ir.config_parameter", "get_param", "web.base.url"), "https://x.odoo.com")

    def test_errors_are_reported(self):
        client = odoo_rpc.OdooClient(self.url, "testdb", api_key="KEY", protocol="json2")
        with self.assertRaises(odoo_rpc.OdooRPCError) as ctx:
            client.call("res.partner", "nope")
        self.assertEqual(ctx.exception.status, 422)
        bad = odoo_rpc.OdooClient(self.url, "testdb", api_key="WRONG", protocol="json2")
        with self.assertRaisesRegex(odoo_rpc.OdooRPCError, "Invalid apikey"):
            bad.call("res.users", "context_get")

    def test_params_command(self):
        code, out = self.run_cli("params", "mail")
        self.assertEqual(code, 0)
        self.assertIn("mail.catchall.alias", out)
        self.assertNotIn("web.base.url", out)

    def test_write_requires_confirmation(self):
        code, out = self.run_cli("call", "res.partner", "write", "--ids", "7", "--kwargs", '{"vals": {"name": "Y"}}')
        self.assertEqual(code, 0)
        self.assertFalse(any(p.endswith("/write") for p, _, _ in FakeOdoo.requests))
        code, out = self.run_cli("--yes", "--json", "call", "res.partner", "write", "--ids", "7",
                                 "--kwargs", '{"vals": {"name": "Y"}}')
        self.assertEqual(json.loads(out), {"ids": [7], "vals": {"name": "Y"}})

    def test_info_command(self):
        code, out = self.run_cli("info", login="admin")
        self.assertEqual(code, 0)
        self.assertIn("19.0+e", out)
        self.assertIn("Admin <admin>", out)

    def test_python_style_domain(self):
        self.assertEqual(odoo_rpc.parse_literal("[('a','=',True)]", "domain"), [("a", "=", True)])


if __name__ == "__main__":
    unittest.main()
