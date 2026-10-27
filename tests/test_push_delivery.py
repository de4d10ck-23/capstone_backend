"""Isolated regression checks; no real databases, credentials, or push deliveries."""
import ast
import asyncio
import json
import logging
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import Mock, AsyncMock

ROOT = Path(__file__).resolve().parents[1]


def load_functions(path, names, **overrides):
    # Avoid module startup loading production keys or local subscription files.
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    for node in nodes:
        node.decorator_list = []
        for arg in node.args.args:
            arg.annotation = None
        node.returns = None
    scope = dict(asyncio=asyncio, json=json, datetime=datetime, Dict=Dict, Any=Any,
                 List=List, Optional=Optional, logger=logging.getLogger("push-test"))
    scope.update(overrides)
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), path, "exec"), scope)
    return scope


class DeliveryTests(unittest.TestCase):
    def test_merge_keeps_local_only_devices_and_deduplicates(self):
        db = Mock()
        db.table.return_value.select.return_value.execute.return_value.data = [
            dict(endpoint="shared", subscription_json={}, user_id="old", updated_at="2026-01-01"),
            dict(endpoint="remote", subscription_json={}, user_id="remote")]
        scope = load_functions("services/push_service.py", {"get_active_subscriptions", "_normalize_barangay"},
            get_supabase=lambda: db, _load_local_subscriptions=lambda: None,
            _local_subscriptions={"shared": dict(endpoint="shared", user_id="new", updated_at="2026-02-01"),
                                  "local": dict(endpoint="local", user_id="local")})
        result = scope["get_active_subscriptions"]()
        self.assertEqual(len(result), 3)
        self.assertEqual(next(s for s in result if s["endpoint"] == "shared")["user_id"], "new")

    def test_send_uses_fresh_claims_ttl_timeout_and_user_filter(self):
        calls = []
        def send(**kwargs):
            self.assertNotIn("aud", kwargs["vapid_claims"])
            kwargs["vapid_claims"]["aud"] = "provider-mutated-value"
            self.assertEqual(kwargs["ttl"], 3600)
            self.assertEqual(kwargs["timeout"], 15)
            calls.append(kwargs)
        subscriptions = [dict(endpoint=str(i), user_id=user, subscription={"endpoint": str(i)})
                         for i, user in enumerate(["me", "me", "other"])]
        scope = load_functions("services/push_service.py", {"broadcast_push_notification"},
            get_active_subscriptions=lambda _: subscriptions, _vapid_instance=object(), _vapid_keys={},
            pywebpush=SimpleNamespace(webpush=send, WebPushException=RuntimeError))
        result = asyncio.run(scope["broadcast_push_notification"]("test", "body", user_id="me"))
        self.assertEqual(result, dict(total=2, sent=2, failed=0))
        self.assertEqual(len(calls), 2)

    def test_expired_and_invalid_subscriptions_count_as_failed(self):
        class Expired(Exception):
            response = SimpleNamespace(status_code=410)
        removed = []
        scope = load_functions("services/push_service.py", {"broadcast_push_notification"},
            get_active_subscriptions=lambda _: [dict(endpoint="expired", subscription={"endpoint":"expired"}), dict(endpoint="invalid")],
            _vapid_instance=object(), _vapid_keys={}, remove_push_subscription=removed.append,
            pywebpush=SimpleNamespace(webpush=Mock(side_effect=Expired("expired")), WebPushException=Expired))
        result = asyncio.run(scope["broadcast_push_notification"]("test", "body"))
        self.assertEqual(result, dict(total=2, sent=0, failed=2))
        self.assertEqual(removed, ["expired"])

    def test_zero_sends_do_not_report_pushed(self):
        db = Mock()
        db.table.return_value.insert.return_value.execute.return_value.data = [{"id":"test"}]
        scope = load_functions("routers/notifications.py", {"create_notification"}, get_supabase=lambda: db,
            broadcast_push_notification=AsyncMock(return_value=dict(total=0, sent=0, failed=0)))
        body = SimpleNamespace(title="test", message="test", type="critical", barangay=None, target_roles=["resident"])
        result = asyncio.run(scope["create_notification"](body, {"role":"admin", "id":"me"}))
        self.assertFalse(result["pushed_to_devices"])
        self.assertEqual(result["push_stats"]["sent"], 0)

    def test_test_push_is_scoped_to_authenticated_user(self):
        send = AsyncMock(return_value=dict(total=0, sent=0, failed=0))
        scope = load_functions("routers/notifications.py", {"send_test_push"}, broadcast_push_notification=send)
        result = asyncio.run(scope["send_test_push"]({"id":"me"}))
        self.assertEqual(send.call_args.kwargs["user_id"], "me")
        self.assertFalse(result["success"])
        source = (ROOT / "routers/notifications.py").read_text(encoding="utf-8")
        node = next(n for n in ast.parse(source).body if isinstance(n, ast.AsyncFunctionDef) and n.name == "send_test_push")
        self.assertIn("Depends(get_current_user)", ast.unparse(node.args.args[0].annotation))

    def test_registration_does_not_trust_client_user_id(self):
        save = Mock(return_value=True)
        scope = load_functions("routers/notifications.py", {"subscribe_device"}, save_push_subscription=save)
        body = SimpleNamespace(subscription={"endpoint":"test"}, user_id="spoofed", barangay="spoofed")
        asyncio.run(scope["subscribe_device"](body, {"id":"me", "barangay":"Combado"}))
        self.assertEqual(save.call_args.kwargs["user_id"], "me")
        self.assertEqual(save.call_args.kwargs["barangay"], "Combado")

    def test_surveillance_retries_failed_sends_and_suppresses_success(self):
        for sent, failed in [(0, 0), (0, 2), (1, 1), (2, 0)]:
            with self.subTest(sent=sent, failed=failed):
                db = Mock()
                db.table.return_value.select.return_value.execute.return_value.data = [{"id":"station", "barangay":"Combado"}]
                db.table.return_value.insert.return_value.execute.return_value.data = [{"id":"notice"}]
                save = Mock()
                prediction = dict(station_id="station", station_name="Test", barangay="Combado",
                                  predicted_status="undrinkable", risk_percentage=90, risk_level="high")
                scope = load_functions("services/forecast_service.py", {"run_barangay_surveillance_and_alert"},
                    get_supabase=lambda: db, predict_station_risk=AsyncMock(return_value=prediction),
                    _load_push_alert_log=lambda: {}, _today_str=lambda: "2026-10-27", _save_push_alert_log=save,
                    broadcast_push_notification=AsyncMock(return_value=dict(sent=sent, failed=failed)))
                result = asyncio.run(scope["run_barangay_surveillance_and_alert"]())
                self.assertEqual(bool(result["alerted_station_ids"]), sent > 0 and failed == 0)
                self.assertEqual(save.called, sent > 0 and failed == 0)


if __name__ == "__main__":
    unittest.main()
