import asyncio
import copy
import csv
import io
import unittest
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

from services.report_service import compile_snapshot, report_options, render_html, render_csv, snapshot_hash, local_day, read_all
from test_push_delivery import load_functions
from test_notification_reads import HTTPException


class ReportGenerationTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 28, 4, tzinfo=timezone.utc)
        self.user = dict(id='inspector', role='sanitization_inspector', full_name='Inspector Example')
        self.options = dict(title='Test report', barangay='Batuan', period_start='2026-10-26',
            period_end='2026-10-27', include_forecasts=False, notes='Review priorities')
        self.sources = [
            dict(id='a', full_name='Spring A', barangay='Batuan', created_at='2026-10-25T12:00:00Z', latitude=10.0, longitude=124.87),
            dict(id='b', full_name='Added later', barangay='Batuan', created_at='2026-10-28T00:00:00Z'),
            dict(id='c', full_name='Other area', barangay='Other', created_at='2026-10-25T00:00:00Z'),
            dict(id='d', full_name='No history', barangay='Batuan', status='safe', created_at='2026-10-26T00:00:00Z'),
        ]
        self.logs = [
            dict(id='1', location_id='a', effective_date='2026-10-25', status='safe', sample_date='2026-10-25', sample_time='09:00:00', bacteriological_exam='passed', recorded_at='2026-10-25T04:00:00Z'),
            dict(id='2', location_id='a', effective_date='2026-10-27', status='contaminated', sample_date='2026-10-27', sample_time='09:00:00', bacteriological_exam='failed', recorded_at='2026-10-27T04:00:00Z'),
            dict(id='3', location_id='a', effective_date='2026-10-27', status='warning', sample_date='2026-10-27', sample_time='09:00:00', bacteriological_exam='failed', recorded_at='2026-10-28T04:00:00Z', notes='Corrected record'),
            dict(id='4', location_id='a', effective_date='2026-10-28', status='safe', sample_date='2026-10-28', bacteriological_exam='passed'),
        ]
        self.concerns = [dict(id='r', title='Pipe leak', type='broken_pipe', barangay='Batuan',
            description='Please review', status='escalated', created_at='2026-10-27T15:59:59Z'),
            dict(id='late', barangay='Batuan', created_at='2026-10-27T16:00:00Z')]
        self.tickets = [dict(id='t', location_id='a', barangay='Batuan', status='in_progress', created_at='2026-10-01T00:00:00Z'),
                        dict(id='new', location_id='a', barangay='Batuan', status='completed', created_at='2026-10-26T01:00:00Z')]

    def compile(self, forecasts=None):
        return compile_snapshot(self.sources, self.logs, self.concerns, self.tickets, forecasts or [], self.options, self.user, self.now)

    def test_historical_inventory_uses_effective_day_and_latest_revision(self):
        report = self.compile()
        self.assertEqual(report['summary']['sources'], 2)
        self.assertEqual(report['sources'][0]['status'], 'unknown')  # sorted source name: No history
        spring = next(item for item in report['sources'] if item['id'] == 'a')
        self.assertEqual(spring['status'], 'warning')
        self.assertEqual(spring['notes'], 'Corrected record')
        self.assertEqual(report['summary']['source_status'], {'unknown': 1, 'warning': 1})
        self.assertEqual(report['summary']['missing_history'], 1)

    def test_period_counts_and_philippine_midnight_boundary(self):
        report = self.compile()
        self.assertEqual(local_day('2026-10-27T16:00:00Z'), '2026-10-28')
        self.assertEqual(report['summary']['new_sources'], 1)
        self.assertEqual(report['summary']['recorded_checks'], 1)
        self.assertEqual(report['summary']['change_days'], 1)
        self.assertEqual(report['summary']['concerns_submitted'], 1)
        self.assertEqual(report['summary']['inspections_created'], 1)
        self.assertEqual(report['summary']['open_inspections'], 1)
        self.assertEqual(len(report['inspections']), 2, 'Older open tickets remain in workload')

    def test_samples_are_not_inflated_by_notes_only_revisions(self):
        self.logs.append(dict(self.logs[2], id='5', effective_date='2026-10-26', notes='Same sample'))
        self.assertEqual(self.compile()['summary']['recorded_checks'], 1)

    def test_empty_report_does_not_invent_safe_sources(self):
        report = compile_snapshot([], [], [], [], [], self.options, self.user, self.now)
        self.assertEqual(report['summary']['sources'], 0)
        self.assertEqual(report['summary']['source_status'], {})
        self.assertIn('No matching records.', render_html(report))

    def test_archived_source_is_excluded_after_archive_day_but_kept_in_earlier_history(self):
        self.sources[0]['archived_at'] = '2026-10-27T02:00:00Z'
        self.assertEqual(self.compile()['summary']['sources'], 1)
        self.options['period_end'] = '2026-10-26'
        self.assertEqual(self.compile()['summary']['sources'], 2)

    def test_forecasts_are_separate_from_recorded_results_and_cover_all_eight_days(self):
        self.options['include_forecasts'] = True
        forecast = dict(station_id='a', forecast_date='2026-10-28', generated_at=self.now.isoformat(),
                        prediction=dict(predicted_status='undrinkable'))
        report = self.compile([forecast])
        self.assertEqual(len(report['forecasts']), 8)
        self.assertEqual(report['forecasts'][0]['high'], 1)
        self.assertEqual(report['forecasts'][0]['unavailable'], 1)
        self.assertEqual(report['forecasts'][7]['unavailable'], 2)
        self.assertEqual(report['summary']['source_status'].get('contaminated', 0), 0)

    def test_html_escapes_all_user_content_and_csv_handles_formulas_and_commas(self):
        self.options['title'] = '<script>alert(1)</script>'
        self.sources[0]['full_name'] = '=HYPERLINK("bad","click")'
        self.options['notes'] = 'Comma, quote " and\nnew line'
        report = self.compile()
        markup = render_html(report)
        self.assertNotIn('<script>', markup)
        self.assertIn('&lt;script&gt;', markup)
        rows = list(csv.reader(io.StringIO(render_csv(report, 'id', snapshot_hash(report)).lstrip('\ufeff'))))
        self.assertTrue(any(cell.startswith("'=HYPERLINK") for row in rows for cell in row))
        self.assertTrue(any('Comma, quote " and\nnew line' in row for row in rows))

    def test_snapshot_fingerprint_is_order_independent_and_jsonb_numeric_safe(self):
        self.assertEqual(snapshot_hash({'a': 10.0, 'b': [1, 2]}), snapshot_hash({'b': [1.0, 2], 'a': 10}))
        report = self.compile()
        self.assertNotEqual(snapshot_hash(report), snapshot_hash({**report, 'notes': 'Edited'}))

    def test_scope_is_forced_to_officials_own_barangay_and_dates_are_validated(self):
        body = SimpleNamespace(period_start=date(2026, 10, 1), period_end=date(2026, 10, 27),
            barangay='Other', title='', include_forecasts=False, notes='')
        options = report_options(body, dict(role='barangay_official', barangay='Batuan'), self.now)
        self.assertEqual(options['barangay'], 'Batuan')
        for start, end in [(date(2026, 10, 28), date(2026, 10, 27)), (date(2026, 10, 1), date(2026, 10, 29)), (date(2024, 1, 1), date(2026, 1, 1))]:
            body.period_start, body.period_end = start, end
            with self.assertRaises(ValueError):
                report_options(body, self.user, self.now)

    def test_pagination_collects_all_rows(self):
        query = Mock()
        rows = [{'id': i} for i in range(1100)]
        query.range.side_effect = lambda start, end: SimpleNamespace(execute=lambda: SimpleNamespace(data=rows[start:end + 1]))
        self.assertEqual(len(read_all(query)), 1100)

    def test_archived_download_never_queries_live_source_tables(self):
        report = self.compile()
        db = Mock()
        db.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
            dict(id='saved', barangay='Batuan', snapshot=copy.deepcopy(report), content_sha256=snapshot_hash(report), schema_version=1)]
        scope = load_functions('routers/reports.py', {'saved_report'}, get_supabase=lambda: db,
            snapshot_hash=snapshot_hash, HTTPException=HTTPException)
        self.sources[0]['full_name'] = 'Changed live name'
        result = scope['saved_report']('saved', self.user)
        self.assertEqual(result['snapshot'], report)
        db.table.assert_called_once_with('reports')

    def test_tampered_and_legacy_archives_are_rejected(self):
        db = Mock()
        scope = load_functions('routers/reports.py', {'saved_report'}, get_supabase=lambda: db,
            snapshot_hash=snapshot_hash, HTTPException=HTTPException)
        for record in [dict(snapshot=None), dict(snapshot=self.compile(), schema_version=1, content_sha256='wrong')]:
            db.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [record]
            with self.assertRaises(HTTPException) as error:
                scope['saved_report']('saved', self.user)
            self.assertEqual(error.exception.status_code, 409)

    def test_official_cannot_download_another_barangays_archive(self):
        db = Mock()
        db.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [{'barangay': 'Other'}]
        scope = load_functions('routers/reports.py', {'saved_report'}, get_supabase=lambda: db,
            snapshot_hash=snapshot_hash, HTTPException=HTTPException)
        with self.assertRaises(HTTPException) as error:
            scope['saved_report']('saved', dict(role='barangay_official', barangay='Batuan'))
        self.assertEqual(error.exception.status_code, 403)

    def test_generation_does_not_archive_a_failed_compilation(self):
        build = Mock(side_effect=HTTPException(503, 'Query failed'))
        db = Mock()
        scope = load_functions('routers/reports.py', {'generate_report'}, build_report=build, get_supabase=lambda: db)
        with self.assertRaises(HTTPException):
            asyncio.run(scope['generate_report'](None, self.user))
        db.table.assert_not_called()

    def test_preview_is_read_only_and_exports_include_all_operational_sections(self):
        report = self.compile()
        scope = load_functions('routers/reports.py', {'preview_report'},
            build_report=lambda *_: report, render_html=render_html,
            HTMLResponse=lambda content, **kwargs: content)
        preview = asyncio.run(scope['preview_report'](None, self.user))
        for title in ('Executive summary', 'Source inventory', 'New sources', 'Concerns submitted',
                      'Inspection workload', 'Follow-up register', 'Methods and data limitations'):
            self.assertIn(title, preview)
        self.assertNotIn('Changed live name', preview)

    def test_generate_archives_snapshot_only_after_compilation(self):
        report = self.compile()
        db = Mock()
        db.table.return_value.insert.return_value.execute.return_value.data = [dict(id='archive', title='Test report', snapshot=report)]
        scope = load_functions('routers/reports.py', {'generate_report'}, build_report=lambda *_: report,
            get_supabase=lambda: db, snapshot_hash=snapshot_hash)
        result = asyncio.run(scope['generate_report'](None, self.user))
        saved = db.table.return_value.insert.call_args.args[0]
        self.assertEqual(saved['snapshot'], report)
        self.assertEqual(saved['content_sha256'], snapshot_hash(report))
        self.assertEqual(saved['status'], 'completed')
        self.assertEqual(saved['generated_by'], 'inspector')
        self.assertNotIn('snapshot', result['data'], 'Listing response avoids duplicating the entire report')
