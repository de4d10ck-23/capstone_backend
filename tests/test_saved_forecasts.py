import asyncio
import copy
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

from test_push_delivery import load_functions


class Query:
    def __init__(self, db, table):
        self.db, self.table_name = db, table
        self.filters = []
        self.bounds = (0, 499)
        self.operation = "read"
        self.payload = None
    def select(self, *_): return self
    def order(self, *_): return self
    def limit(self, count): self.bounds = (0, count - 1); return self
    def range(self, start, end): self.bounds = (start, end); return self
    def eq(self, key, value): self.filters.append(lambda row: row[key] == value); return self
    def lt(self, key, value): self.filters.append(lambda row: row[key] < value); return self
    def ilike(self, key, value): self.filters.append(lambda row: row[key].lower() == value.lower()); return self
    def is_(self, key, value):
        if value != 'null': raise ValueError(value)
        self.filters.append(lambda row: row.get(key) is None)
        return self
    def delete(self): self.operation = "delete"; return self
    def upsert(self, rows, **_): self.operation = "upsert"; self.payload = rows; return self
    def execute(self):
        rows = self.db[self.table_name]
        matches = [row for row in rows if all(f(row) for f in self.filters)]
        if self.operation == "upsert":
            for new in self.payload:
                rows[:] = [row for row in rows if (row['station_id'], row['forecast_date']) != (new['station_id'], new['forecast_date'])]
                rows.append(copy.deepcopy(new))
        elif self.operation == "delete":
            rows[:] = [row for row in rows if row not in matches]
        return SimpleNamespace(data=copy.deepcopy(matches[self.bounds[0]:self.bounds[1] + 1]))


class SavedForecastTests(unittest.TestCase):
    def setUp(self):
        self.db = {"water_locations": [dict(id="one", latitude=10, longitude=124, barangay="Batuan")], "water_forecasts": []}
        client = SimpleNamespace(table=lambda name: Query(self.db, name))
        async def predict(station_id, forecast_date, **_):
            return {"station_id": station_id, "forecast_date": forecast_date, "risk_percentage": 25}
        self.predict = AsyncMock(side_effect=predict)
        self.scope = load_functions('services/saved_forecast_service.py',
            {'read_all', 'refresh_saved_forecasts', 'get_saved_forecasts'}, timedelta=timedelta, timezone=timezone,
            MANILA=timezone(timedelta(hours=8)), _refresh_lock=asyncio.Lock(),
            get_supabase=lambda: client, load_model_payload=lambda: {'pipeline': True},
            get_active_config=lambda: {}, predict_station_risk=self.predict)

    def refresh(self): return asyncio.run(self.scope['refresh_saved_forecasts']())

    def test_saves_all_eight_dates_including_today(self):
        result = self.refresh()
        today = datetime.now(timezone(timedelta(hours=8))).date()
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['saved'], 8)
        self.assertEqual({row['forecast_date'] for row in self.db['water_forecasts']},
                         {(today + timedelta(days=i)).isoformat() for i in range(8)})

    def test_failed_refresh_preserves_previous_successes(self):
        self.refresh()
        saved = copy.deepcopy(self.db['water_forecasts'])
        self.predict.side_effect = RuntimeError('Weather unavailable')
        result = self.refresh()
        self.assertEqual(result['failed'], 8)
        self.assertEqual(result['saved'], 0)
        self.assertEqual(self.db['water_forecasts'], saved)

    def test_partial_failure_updates_only_successful_dates(self):
        self.refresh()
        saved = copy.deepcopy(self.db['water_forecasts'])
        failed_date = saved[-1]['forecast_date']
        async def predict(station_id, forecast_date, **_):
            if forecast_date == failed_date: raise RuntimeError('Missing weather')
            return dict(station_id=station_id, forecast_date=forecast_date, risk_percentage=60)
        self.predict.side_effect = predict
        result = self.refresh()
        self.assertEqual((result['saved'], result['failed']), (7, 1))
        self.assertEqual(next(row for row in self.db['water_forecasts'] if row['forecast_date'] == failed_date), saved[-1])

    def test_pagination_does_not_drop_stations_after_first_page(self):
        self.db['water_locations'] = [dict(id=str(i)) for i in range(1001)]
        rows = self.scope['read_all'](Query(self.db, 'water_locations'))
        self.assertEqual(len(rows), 1001)

    def test_missing_model_leaves_saved_rows_intact(self):
        self.refresh()
        saved = copy.deepcopy(self.db['water_forecasts'])
        self.scope['load_model_payload'] = lambda: {}
        self.assertEqual(self.refresh()['status'], 'failed')
        self.assertEqual(self.db['water_forecasts'], saved)

    def test_saved_reads_report_staleness_and_missing_stations_without_inference(self):
        self.refresh()
        day = self.db['water_forecasts'][0]['forecast_date']
        self.db['water_forecasts'][0]['generated_at'] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        self.db['water_locations'].extend([
            dict(id='two', barangay='Batuan'), dict(id='outside', barangay='Other')])
        self.predict.reset_mock()
        scheduler = SimpleNamespace(get_run_interval_minutes=lambda: 60)
        with patch.dict('sys.modules', {'services.heatmap_service': scheduler}):
            result = self.scope['get_saved_forecasts'](day, 'Batuan')
        self.assertEqual(result['stale_count'], 1)
        self.assertEqual(result['unavailable_station_ids'], ['two'])
        self.assertEqual(len(result['results']), 1)
        self.predict.assert_not_awaited()

    def test_archived_station_disappears_from_forecasts_and_returns_after_restore(self):
        self.refresh()
        day = self.db['water_forecasts'][0]['forecast_date']
        station = self.db['water_locations'][0]
        scheduler = SimpleNamespace(get_run_interval_minutes=lambda: 60)
        station['archived_at'] = datetime.now(timezone.utc).isoformat()
        with patch.dict('sys.modules', {'services.heatmap_service': scheduler}):
            archived = self.scope['get_saved_forecasts'](day, 'Batuan')
            self.assertEqual(archived['results'], [])
            self.assertEqual(archived['unavailable_station_ids'], [])
            station['archived_at'] = None
            restored = self.scope['get_saved_forecasts'](day, 'Batuan')
        self.assertEqual(len(restored['results']), 1)
        self.assertEqual(restored['results'][0]['station_id'], station['id'])


if __name__ == '__main__': unittest.main()
