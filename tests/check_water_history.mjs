// node tests/check_water_history.mjs <absolute path to PGlite dist/index.js>
// Executes the real migration in isolated PostgreSQL; no Supabase credentials.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';
const { PGlite } = await import(pathToFileURL(process.argv[2]).href);
const db = new PGlite();
await db.exec(`
  CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role;
  CREATE TABLE users(id uuid PRIMARY KEY, full_name text, role text, is_active boolean, barangay text);
  CREATE TABLE water_locations(id uuid PRIMARY KEY DEFAULT gen_random_uuid(), full_name text NOT NULL,
    barangay text, latitude float NOT NULL, longitude float NOT NULL, image_url text,
    status text, e_coli boolean, coliform_bacteria boolean, bacteriological_exam text,
    sample_date date, sample_time time, notes text, created_by uuid REFERENCES users,
    created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now());
  CREATE TABLE water_status_logs(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    location_id uuid REFERENCES water_locations, status text NOT NULL, previous_status text,
    bacteriological_exam text, coliform_count int DEFAULT 0, e_coli_count int DEFAULT 0,
    notes text, recorded_by uuid REFERENCES users, recorded_at timestamptz DEFAULT now());
`);
const actor = '00000000-0000-0000-0000-000000000001';
const station = '00000000-0000-0000-0000-000000000002';
await db.query('INSERT INTO users(id) VALUES ($1)', [actor]);
await db.query(`INSERT INTO water_locations(id,full_name,latitude,longitude,status,e_coli,coliform_bacteria,
  bacteriological_exam,sample_date,sample_time,notes,created_by)
  VALUES ($1,'Test source',10.13,124.87,'safe',false,false,'passed','2025-10-27','04:21:00','Initial notes',$2)`, [station, actor]);
const migration = await readFile(new URL('../migrations/20261028_water_status_history.sql', import.meta.url), 'utf8');
await db.exec(migration);
await db.exec(migration);
const logs = async () => (await db.query("SELECT *, to_char(effective_date, 'YYYY-MM-DD') AS day FROM water_status_logs WHERE location_id=$1 ORDER BY effective_date,recorded_at,id", [station])).rows;
const current = async () => (await db.query('SELECT * FROM water_locations WHERE id=$1', [station])).rows[0];
const update = (day, patch) => db.query('SELECT update_water_location_history($1,$2::jsonb,$3::date,$4)', [station, JSON.stringify(patch), day, actor]);
assert.equal((await logs()).length, 1, 'Migration is repeatable and backfills one baseline');
await update('2025-10-28', { status: 'contaminated', bacteriological_exam: 'failed', e_coli: false, notes: 'First change' });
assert.equal((await current()).status, 'contaminated');
assert.equal((await current()).e_coli, false, 'Contamination does not invent positive E. coli');
assert.equal((await logs()).length, 2);
await update('2025-10-29', { status: 'contaminated', bacteriological_exam: 'failed', e_coli: false, notes: 'First change' });
assert.equal((await logs()).length, 2, 'Unchanged next day creates no colored event');
assert.equal((await current()).notes, 'First change', 'Notes persist on gray days');
await update('2025-10-30', { status: 'safe', bacteriological_exam: 'passed', notes: 'Retested', sample_date: '2025-10-30' });
await update('2025-10-28', { status: 'warning', bacteriological_exam: 'untested', notes: 'Corrected historical note' });
assert.equal((await current()).status, 'safe', 'Backdated correction never overwrites the latest snapshot');
assert.equal((await current()).notes, 'Retested');
assert.equal((await logs()).filter(row => row.day === '2025-10-28').length, 2, 'Same-day revisions are preserved');
await update('2025-10-31', { notes: '' });
assert.equal((await current()).notes, '', 'Cleared notes stay cleared');
const before = (await logs()).length;
await assert.rejects(update('2025-11-01', { latitude: null, status: 'contaminated' }));
assert.equal((await logs()).length, before, 'Failed station update rolls back history insertion too');
assert.equal((await current()).status, 'safe');
await assert.rejects(update('2999-01-01', { status: 'safe' }));
await assert.rejects(update('2025-11-01', { sample_date: '2025-11-02' }));
const created = await db.query(`INSERT INTO water_locations(full_name,latitude,longitude,status,notes,created_by)
  VALUES ('New source',10.13,124.87,'unknown','Registration notes',$1) RETURNING id`, [actor]);
assert.equal((await db.query('SELECT notes FROM water_status_logs WHERE location_id=$1', [created.rows[0].id])).rows[0].notes, 'Registration notes');
// Source types must survive the real history RPC, migration reruns, and archive/restore.
const typesMigration = await readFile(new URL('../migrations/20261028_water_source_types.sql', import.meta.url), 'utf8');
await db.exec(typesMigration);
assert.equal((await current()).source_type, 'unknown', 'Do not guess historical source types');
const logCount = (await logs()).length;
await update('2025-11-01', { source_type: 'spring' });
assert.equal((await current()).source_type, 'spring');
assert.equal((await logs()).length, logCount, 'Metadata edits must not invent safety changes');
await update('2025-11-01', { full_name: 'Renamed source' });
assert.equal((await current()).source_type, 'spring', 'Omitted type is preserved');
await db.exec(typesMigration);
assert.equal((await current()).source_type, 'spring', 'Migration reruns preserve saved types');
await db.query("UPDATE users SET role='admin',is_active=true,full_name='Editor' WHERE id=$1", [actor]);
await db.exec(await readFile(new URL('../migrations/20261028_water_source_archive.sql', import.meta.url), 'utf8'));
await db.query('SELECT set_water_source_archived($1,$2,true)', [station,actor]);
await assert.rejects(update('2025-11-01', {source_type: 'deep_well'}), /Restore this water source/);
await db.query('SELECT set_water_source_archived($1,$2,false)', [station,actor]);
assert.equal((await current()).source_type, 'spring', 'Restoring retains the selected type');
console.log('PostgreSQL history checks passed: backfill, repeat migration, gray days, carry-forward notes, same-day revisions, backdating, atomic rollback, date validation, and registration trigger.');
await db.close();
