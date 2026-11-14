import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from leitor_dados.api import MAX_REQUEST_BODY_BYTES, create_app
from leitor_dados.query import CursorExpired, DatasetUnavailable, QueryBusy, QueryTimeout, ResultTooLarge


API_KEY = "test-api-key-" + "a" * 32
CURSOR_SECRET = "test-cursor-" + "b" * 32
HEADERS = {"X-API-Key": API_KEY}


class FakeStore:
    def __init__(self, **kwargs):
        self.available = True
        self.failure = None
        self.last_search = None

    def ready(self):
        return self.available

    def info(self):
        if not self.available:
            raise DatasetUnavailable("/private/data/path")
        return {"version": "version-1", "records": 2}

    def search(self, **kwargs):
        if self.failure:
            raise self.failure
        if not self.available:
            raise DatasetUnavailable("/private/data/path")
        self.last_search = kwargs
        return {
            "version": "version-1",
            "field": kwargs["field"],
            "items": [{"record_id": 7, "data": {"name": "<script>alert(1)</script>"}}],
            "next_cursor": "next-signed-cursor",
        }


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.store_patch = patch("leitor_dados.api.DatasetStore", FakeStore)
        self.store_patch.start()
        self.addCleanup(self.store_patch.stop)
        self.app = create_app(Path(self.temp_dir.name), API_KEY, CURSOR_SECRET)
        self.client = self.enterContext(TestClient(self.app))

    def test_authentication_and_readiness(self):
        self.assertEqual(self.client.get("/health/live").status_code, 200)
        for path in ("/health/ready", "/v1/info"):
            self.assertEqual(self.client.get(path).status_code, 401)
            self.assertEqual(self.client.get(path, headers={"X-API-Key": "wrong"}).status_code, 401)
            self.assertEqual(self.client.get(path, headers=HEADERS).status_code, 200)
        self.assertEqual(
            self.client.post("/v1/search", json={"field": "code", "value": "A"}).status_code,
            401,
        )
        self.app.state.store.available = False
        self.assertEqual(self.client.get("/health/live").status_code, 200)
        self.assertEqual(self.client.get("/health/ready", headers=HEADERS).status_code, 503)
        response = self.client.get("/v1/info", headers=HEADERS)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("/private", response.text)

    def test_search_forwards_bounded_values_and_cursor(self):
        body = {"field": "email", "value": "person@example.invalid", "limit": 12, "cursor": "signed"}
        response = self.client.post("/v1/search", json=body, headers=HEADERS)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.app.state.store.last_search, body)
        self.assertEqual(response.json()["next_cursor"], "next-signed-cursor")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")

    def test_validation_does_not_echo_personal_data(self):
        sensitive = "private-person@example.invalid"
        cases = [
            {"field": "unknown", "value": sensitive},
            {"field": "email", "value": {"email": sensitive}},
            {"field": "email", "value": ""},
            {"field": "email", "value": sensitive, "limit": 101},
            {"field": "email", "value": sensitive, "limit": True},
            {"field": "email", "value": sensitive, "cursor": "x" * 4097},
            {"field": "email", "value": sensitive, "extra": sensitive},
            {"field": "email", "value": sensitive * 200},
        ]
        for body in cases:
            with self.subTest(body=list(body)):
                response = self.client.post("/v1/search", json=body, headers=HEADERS)
                self.assertEqual(response.status_code, 422)
                self.assertNotIn(sensitive, response.text)
        self.assertIsNone(self.app.state.store.last_search)

    def test_large_body_and_malformed_json_are_safe(self):
        response = self.client.post(
            "/v1/search", content=b"x" * (MAX_REQUEST_BODY_BYTES + 1),
            headers={**HEADERS, "Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, 413)
        response = self.client.post(
            "/v1/search", content=iter((b"x" * 40_000, b"x" * 40_000)),
            headers={**HEADERS, "Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, 413)
        response = self.client.post(
            "/v1/search", content=b'{"value":"private-email@example.invalid",',
            headers={**HEADERS, "Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("private-email", response.text)

    def test_query_failures_are_sanitized(self):
        for error_type, status in (
            (ValueError, 400), (CursorExpired, 409),
            (ResultTooLarge, 413),
            (DatasetUnavailable, 503), (QueryTimeout, 503), (QueryBusy, 503), (RuntimeError, 503),
        ):
            self.app.state.store.failure = error_type("private-email@example.invalid /private/data")
            response = self.client.post(
                "/v1/search", json={"field": "code", "value": "A"}, headers=HEADERS,
            )
            with self.subTest(error=error_type.__name__):
                self.assertEqual(response.status_code, status)
                self.assertNotIn("private-email", response.text)
                self.assertNotIn("/private", response.text)
                if error_type is ResultTooLarge:
                    self.assertEqual(response.json()["detail"], "Reduza a quantidade de registros por página.")

    def test_ui_has_nonce_and_no_public_schema(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn('lang="pt-BR"', response.text)
        self.assertIn("script-src 'nonce-", response.headers["content-security-policy"])
        self.assertNotIn("__CSP_NONCE__", response.text)
        self.assertIn("content.textContent = JSON.stringify", response.text)
        self.assertIn('id="limit"', response.text)
        self.assertNotIn("localStorage", response.text)
        self.assertNotIn(API_KEY, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")
        for path in ("/docs", "/redoc", "/openapi.json"):
            self.assertEqual(self.client.get(path).status_code, 404)

    def test_empty_or_short_keys_fail_startup(self):
        for key, secret in (("", CURSOR_SECRET), ("short", CURSOR_SECRET), (API_KEY, "short")):
            with self.subTest(key_length=len(key), secret_length=len(secret)):
                with self.assertRaises(RuntimeError):
                    with TestClient(create_app(Path(self.temp_dir.name), key, secret)):
                        pass

    def test_secret_files_take_precedence_over_environment(self):
        key_file = Path(self.temp_dir.name) / "api-key"
        cursor_file = Path(self.temp_dir.name) / "cursor-key"
        key_file.write_text(API_KEY + "\n", encoding="utf-8")
        cursor_file.write_text(CURSOR_SECRET + "\n", encoding="utf-8")
        environment = {
            "API_KEY_FILE": str(key_file), "CURSOR_SECRET_FILE": str(cursor_file),
            "API_KEY": "bad", "CURSOR_SECRET": "bad", "DATA_DIR": self.temp_dir.name,
        }
        with patch.dict("os.environ", environment):
            with TestClient(create_app()) as client:
                self.assertEqual(client.get("/health/ready", headers=HEADERS).status_code, 200)


class ApiIntegrationTests(unittest.TestCase):
    def test_real_build_authentication_and_cursor_pagination(self):
        from leitor_dados.builder import build_dataset

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = directory / "input.jsonl"
            records = [
                {"cpf": "000.000.000-01", "email": "first@example.invalid", "phone": "+55 (11) 90000-0001", "code": "A", "name": "<script>alert(1)</script>"},
                {"cpf": "000.000.000-02", "email": "second@example.invalid", "phone": "5511900000002", "code": "A", "name": "Segunda pessoa"},
                {"cpf": "000.000.000-03", "email": "third@example.invalid", "phone": "5511900000003", "code": "B", "name": "Terceira pessoa"},
            ]
            source.write_text("\n".join(json.dumps(item) for item in records), encoding="utf-8")
            dataset = directory / "dataset"
            build_dataset(
                [source],
                {"format": "jsonl", "fields": {name: name for name in ("cpf", "email", "phone", "code")},
                 "data_shards": 2, "lookup_shards": 3, "batch_size": 2},
                dataset, "real-v1", publish=True,
            )
            with TestClient(create_app(dataset, API_KEY, CURSOR_SECRET)) as client:
                self.assertEqual(client.get("/health/ready").status_code, 401)
                self.assertEqual(client.get("/health/ready", headers=HEADERS).status_code, 200)
                self.assertEqual(client.get("/v1/info", headers=HEADERS).json()["row_count"], 3)
                first = client.post(
                    "/v1/search", json={"field": "code", "value": "A", "limit": 1}, headers=HEADERS,
                )
                self.assertEqual(first.status_code, 200)
                first_page = first.json()
                self.assertEqual(first_page["version"], "real-v1")
                self.assertEqual(first_page["items"][0]["data"], records[0])
                cursor = first_page["next_cursor"]
                self.assertIsInstance(cursor, str)
                second = client.post(
                    "/v1/search", json={"field": "code", "value": "A", "limit": 1, "cursor": cursor}, headers=HEADERS,
                )
                self.assertEqual(second.status_code, 200)
                self.assertEqual(second.json()["items"][0]["data"], records[1])
                self.assertIsNone(second.json()["next_cursor"])
                self.assertNotEqual(first_page["items"][0]["record_id"], second.json()["items"][0]["record_id"])
                for field, value in (("cpf", "00000000001"), ("email", "first@example.invalid"), ("phone", "5511900000001")):
                    response = client.post("/v1/search", json={"field": field, "value": value}, headers=HEADERS)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()["items"][0]["data"], records[0])
                changed = client.post(
                    "/v1/search", json={"field": "code", "value": "B", "cursor": cursor}, headers=HEADERS,
                )
                self.assertEqual(changed.status_code, 400)

    def test_real_missing_dataset_starts_with_live_health_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            with TestClient(create_app(Path(temporary) / "missing", API_KEY, CURSOR_SECRET)) as client:
                self.assertEqual(client.get("/health/live").status_code, 200)
                self.assertEqual(client.get("/health/ready", headers=HEADERS).status_code, 503)
                self.assertEqual(client.get("/v1/info", headers=HEADERS).status_code, 503)
                response = client.post("/v1/search", json={"field": "code", "value": "A"}, headers=HEADERS)
                self.assertEqual(response.status_code, 503)
                self.assertNotIn(temporary, response.text)


if __name__ == "__main__":
    unittest.main()
