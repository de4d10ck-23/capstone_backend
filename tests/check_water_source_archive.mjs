// node tests/check_water_source_archive.mjs <absolute path to PGlite dist/index.js>
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';

const { PGlite } = await import(pathToFileURL(process.argv[2]).href);
const db = new PGlite();
await db.exec(`
  CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role;
  CREATE TABLE public.users(id uuid PRIMARY KEY, full_name text, role text, is_active boolean, barangay text);
  CREATE TABLE public.water_locations(id uuid PRIMARY KEY, full_name text NOT NULL, barangay text,
    latitude float NOT NULL, longitude float NOT NULL, status text, image_url text, notes text,
    created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now());
  CREATE TABLE public.water_status_logs(id uuid PRIMARY KEY, location_id uuid REFERENCES public.water_locations(id) ON DELETE CASCADE,
    status text, notes text);
  CREATE TABLE public.water_forecasts(station_id uuid REFERENCES public.water_locations(id) ON DELETE CASCADE,
    forecast_date date, prediction jsonb);
  CREATE TABLE public.linked_concern(id uuid PRIMARY KEY, location_id uuid REFERENCES public.water_locations(id));
`);
const actor = '00000000-0000-0000-0000-000000000001';
const other = '00000000-0000-0000-0000-000000000002';
const station = '00000000-0000-0000-0000-000000000003';
await db.query(`INSERT INTO public.users VALUES
  ($1, 'Barangay editor', 'barangay_official', true, 'Hantag'),
  ($2, 'Other editor', 'barangay_official', true, 'Batuan')`, [actor, other]);
await db.query(`INSERT INTO public.water_locations(id,full_name,barangay,latitude,longitude,status,image_url,notes)
  VALUES($1,'Original station','Hantag',10.1,124.8,'contaminated','photo.jpg','Original note')`, [station]);
await db.query(`INSERT INTO public.water_status_logs VALUES
  ('00000000-0000-0000-0000-000000000004',$1,'safe','Earlier test'),
  ('00000000-0000-0000-0000-000000000005',$1,'contaminated','Latest test')`, [station]);
await db.query(`INSERT INTO public.water_forecasts VALUES($1,'2026-10-28','{}')`, [station]);
await db.query(`INSERT INTO public.linked_concern VALUES('00000000-0000-0000-0000-000000000006',$1)`, [station]);
const migration = await readFile(new URL('../migrations/20261028_water_source_archive.sql', import.meta.url), 'utf8');
await db.exec(migration);
await db.exec(migration);

const row = async () => (await db.query('SELECT * FROM public.water_locations WHERE id=$1', [station])).rows[0];
const events = async () => (await db.query('SELECT action FROM public.water_source_archive_events WHERE location_id=$1 ORDER BY occurred_at', [station])).rows;
const change = (person, archive) => db.query('SELECT public.set_water_source_archived($1,$2,$3)', [station, person, archive]);

await assert.rejects(change(other, true), /assigned barangay/);
assert.equal((await row()).archived_at, null);
await change(actor, true);
const archived = await row();
assert.ok(archived.archived_at);
assert.equal(archived.status, 'contaminated');
assert.equal(archived.image_url, 'photo.jpg');
assert.equal((await db.query('SELECT count(*)::int AS total FROM public.water_status_logs WHERE location_id=$1', [station])).rows[0].total, 2);
assert.equal((await db.query('SELECT count(*)::int AS total FROM public.water_forecasts WHERE station_id=$1', [station])).rows[0].total, 1);
assert.equal((await db.query('SELECT count(*)::int AS total FROM public.linked_concern WHERE location_id=$1', [station])).rows[0].total, 1);
await assert.rejects(db.query("UPDATE public.water_locations SET status='safe' WHERE id=$1", [station]), /Restore this water source/);
await change(actor, true);
assert.deepEqual((await events()).map(({ action }) => action), ['archived']);
await assert.rejects(change(other, false), /assigned barangay/);
await change(actor, false);
const restored = await row();
assert.equal(restored.archived_at, null);
assert.equal(restored.id, station);
assert.equal(restored.status, 'contaminated');
assert.equal(restored.notes, 'Original note');
assert.equal((await db.query('SELECT count(*)::int AS total FROM public.water_status_logs WHERE location_id=$1', [station])).rows[0].total, 2);
await change(actor, false);
assert.deepEqual((await events()).map(({ action }) => action), ['archived', 'restored']);
await db.exec(migration);
assert.deepEqual((await events()).map(({ action }) => action), ['archived', 'restored']);
console.log('Water source archive PostgreSQL checks passed: scope, preserved history/links/photo, restore, edit guard, idempotency, repeatable migration.');
