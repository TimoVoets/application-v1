from fastapi import FastAPI
from fastapi.testclient import TestClient
import types
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_KEY", "dummy")

import gmail_oauth
from gmail_oauth import router


def test_update_subject_filter(monkeypatch):
    class DummyQuery:
        def __init__(self):
            self.update_data = None
            self.conditions = []

        def update(self, data):
            self.update_data = data
            return self

        def eq(self, column, value):
            self.conditions.append((column, value))
            return self

        def execute(self):
            return types.SimpleNamespace(
                data=[
                    {
                        "id": 1,
                        "user_id": "user1",
                        "provider": "gmail",
                        "subject_filter": self.update_data.get("subject_filter"),
                    }
                ]
            )

    class DummySupabase:
        def table(self, name):
            self.table_name = name
            self.query = DummyQuery()
            return self.query

    dummy = DummySupabase()
    monkeypatch.setattr(gmail_oauth, "supabase", dummy)

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    payload = {"user_id": "user1", "subject_filter": "Invoices", "token_id": 1}
    resp = client.post("/oauth/gmail/settings", json=payload)

    assert resp.status_code == 200
    assert resp.json()["updated"][0]["subject_filter"] == "Invoices"
    assert dummy.table_name == "email_tokens"
    assert ("user_id", "user1") in dummy.query.conditions
    assert ("provider", "gmail") in dummy.query.conditions
    assert ("id", 1) in dummy.query.conditions

