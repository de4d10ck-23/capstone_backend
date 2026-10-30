import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock
from test_push_delivery import load_functions
from test_notification_reads import HTTPException


class WorkflowTests(unittest.TestCase):
    def report_scope(self, status="validated", role="barangay_official"):
        db = Mock()
        db.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [
            {"id":"report", "status":status, "barangay":"Combado"}]
        db.rpc.return_value.execute.return_value.data = {"id":"report", "status":"escalated"}
        scope = load_functions("routers/resident_reports.py", {"update_resident_report"},
            get_supabase=lambda: db, HTTPException=HTTPException, notify_staff=AsyncMock(), notify_resident_outcome=AsyncMock(), notify_linked_resident_outcome=AsyncMock())
        return db, scope, {"id":"staff", "role":role, "barangay":"Combado"}

    def test_forwarding_uses_atomic_database_operation(self):
        db, scope, actor = self.report_scope()
        result = asyncio.run(scope["update_resident_report"]("report", {"status":"escalated"}, actor))
        self.assertTrue(result["success"])
        db.rpc.assert_called_once_with("forward_resident_report_to_chu", {
            "p_report_id":"report", "p_actor_id":"staff", "p_reason":None})
        db.table.return_value.update.assert_not_called()
        db.table.return_value.insert.assert_not_called()

    def test_forwarding_failure_is_not_reported_as_success(self):
        db, scope, actor = self.report_scope()
        db.rpc.return_value.execute.side_effect = RuntimeError("Missing migration")
        with self.assertRaises(HTTPException) as err:
            asyncio.run(scope["update_resident_report"]("report", {"status":"escalated"}, actor))
        self.assertEqual(err.exception.status_code, 503)

    def test_official_cannot_process_another_barangay(self):
        db, scope, actor = self.report_scope()
        actor["barangay"] = "Abgao"
        with self.assertRaises(HTTPException) as err:
            asyncio.run(scope["update_resident_report"]("report", {"status":"escalated"}, actor))
        self.assertEqual(err.exception.status_code, 403)
        db.rpc.assert_not_called()

    def test_done_report_cannot_be_forwarded_again(self):
        db, scope, actor = self.report_scope("done")
        with self.assertRaises(HTTPException) as err:
            asyncio.run(scope["update_resident_report"]("report", {"status":"escalated"}, actor))
        self.assertEqual(err.exception.status_code, 409)

    def test_chu_cannot_claim_a_report_not_endorsed(self):
        db, scope, actor = self.report_scope("pending", "sanitization_inspector")
        with self.assertRaises(HTTPException):
            asyncio.run(scope["update_resident_report"]("report", {"status":"in_progress"}, actor))
        db.table.return_value.update.assert_not_called()

    def test_pending_inspection_cannot_skip_processing(self):
        db = Mock()
        db.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [{"id":"ticket", "status":"pending"}]
        scope = load_functions("routers/inspections.py", {"update_inspection"}, get_supabase=lambda: db, HTTPException=HTTPException, notify_staff=AsyncMock(), notify_resident_outcome=AsyncMock(), notify_linked_resident_outcome=AsyncMock())
        with self.assertRaises(HTTPException) as err:
            asyncio.run(scope["update_inspection"]("ticket", {"status":"completed", "notes":"Findings"}, {"id":"inspector", "role":"sanitization_inspector"}))
        self.assertEqual(err.exception.status_code, 409)
        db.table.return_value.update.assert_not_called()

    def test_inspector_cannot_take_another_inspectors_ticket(self):
        db = Mock()
        db.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [{"id":"ticket", "status":"assigned", "assigned_to":"other"}]
        scope = load_functions("routers/inspections.py", {"update_inspection"}, get_supabase=lambda: db, HTTPException=HTTPException, notify_staff=AsyncMock(), notify_resident_outcome=AsyncMock(), notify_linked_resident_outcome=AsyncMock())
        with self.assertRaises(HTTPException) as err:
            asyncio.run(scope["update_inspection"]("ticket", {"status":"in_progress"}, {"id":"inspector", "role":"sanitization_inspector"}))
        self.assertEqual(err.exception.status_code, 409)

    def test_stale_inspection_update_is_reported(self):
        db = Mock()
        db.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [{"id":"ticket", "status":"in_progress", "assigned_to":"inspector"}]
        db.table.return_value.update.return_value.eq.return_value.eq.return_value.eq.return_value.execute.return_value.data = []
        scope = load_functions("routers/inspections.py", {"update_inspection"}, get_supabase=lambda: db, HTTPException=HTTPException, notify_staff=AsyncMock(), notify_resident_outcome=AsyncMock(), notify_linked_resident_outcome=AsyncMock())
        with self.assertRaises(HTTPException) as err:
            asyncio.run(scope["update_inspection"]("ticket", {"status":"completed", "notes":"Findings"}, {"id":"inspector", "role":"sanitization_inspector"}))
        self.assertEqual(err.exception.status_code, 409)

    def test_start_and_complete_share_the_same_update_path(self):
        update = AsyncMock(return_value={"success":True})
        scope = load_functions("routers/inspections.py", {"start_inspection", "complete_inspection"}, update_inspection=update)
        actor = {"id":"inspector", "role":"sanitization_inspector"}
        asyncio.run(scope["start_inspection"]("ticket", actor))
        update.assert_awaited_with("ticket", {"status":"in_progress"}, actor)
        asyncio.run(scope["complete_inspection"]("ticket", SimpleNamespace(notes="Findings"), actor))
        update.assert_awaited_with("ticket", {"status":"completed", "notes":"Findings"}, actor)

    def test_inspector_can_dismiss_pending_or_processing_ticket_without_updating_source(self):
        for status in ("pending", "in_progress"):
            with self.subTest(status=status):
                db = Mock()
                db.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [
                    {"id": "ticket", "status": status, "assigned_to": None}]
                db.table.return_value.update.return_value.eq.return_value.eq.return_value.is_.return_value.execute.return_value.data = [
                    {"id": "ticket", "status": "cancelled"}]
                scope = load_functions("routers/inspections.py", {"update_inspection"},
                    get_supabase=lambda: db, HTTPException=HTTPException, notify_staff=AsyncMock(), notify_resident_outcome=AsyncMock(), notify_linked_resident_outcome=AsyncMock())
                result = asyncio.run(scope["update_inspection"]("ticket",
                    {"status": "cancelled", "notes": "Dismissal reason: Duplicate request"},
                    {"id": "inspector", "role": "sanitization_inspector"}))
                self.assertEqual(result["data"]["status"], "cancelled")
                db.table.return_value.update.assert_called_once_with({
                    "status": "cancelled", "notes": "Dismissal reason: Duplicate request"})
                self.assertTrue(all(call.args == ("inspection_requests",) for call in db.table.call_args_list))

    def test_done_needs_no_water_findings_and_updates_only_the_ticket(self):
        db = Mock()
        db.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [
            {"id": "ticket", "status": "in_progress", "assigned_to": "inspector", "notes": None}]
        db.table.return_value.update.return_value.eq.return_value.eq.return_value.eq.return_value.execute.return_value.data = [
            {"id": "ticket", "status": "completed"}]
        scope = load_functions("routers/inspections.py", {"update_inspection"},
            get_supabase=lambda: db, HTTPException=HTTPException, notify_staff=AsyncMock(), notify_resident_outcome=AsyncMock(), notify_linked_resident_outcome=AsyncMock())
        result = asyncio.run(scope["update_inspection"]("ticket", {"status": "completed"},
            {"id": "inspector", "role": "sanitization_inspector"}))
        self.assertEqual(result["data"]["status"], "completed")
        db.table.return_value.update.assert_called_once_with({"status": "completed", "assigned_to": "inspector"})
        self.assertTrue(all(call.args == ("inspection_requests",) for call in db.table.call_args_list))

if __name__ == "__main__":
    unittest.main()
