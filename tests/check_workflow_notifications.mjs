import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';
const { PGlite } = await import(pathToFileURL(process.argv[2]).href);
const db = new PGlite();
await db.exec(`CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role;
CREATE TABLE users(id uuid PRIMARY KEY);
CREATE TABLE notifications(id uuid PRIMARY KEY DEFAULT gen_random_uuid(), title text, message text, type text,
 barangay text, target_roles text[], created_by uuid REFERENCES users);
CREATE TABLE resident_reports(id uuid PRIMARY KEY DEFAULT gen_random_uuid(), title text, type text,
 status text, submitted_by uuid REFERENCES users, actioned_by uuid REFERENCES users, reason text);`);
const migration = await readFile(new URL('../migrations/20261030_workflow_notifications.sql', import.meta.url), 'utf8');
await db.exec(migration);
await db.exec(migration);
const owner = '00000000-0000-0000-0000-000000000001';
await db.query('INSERT INTO users VALUES ($1)', [owner]);
for (const type of ['concern', 'unregistered_source']) {
  for (const status of ['done', 'dismissed']) {
    const { rows: [report] } = await db.query("INSERT INTO resident_reports(title,type,status,submitted_by) VALUES ('Test',$1,'pending',$2) RETURNING id", [type, owner]);
    await db.query('UPDATE resident_reports SET status=$1 WHERE id=$2', [status, report.id]);
    await db.query('UPDATE resident_reports SET status=$1 WHERE id=$2', [status, report.id]);
  }
}
const { rows } = await db.query('SELECT * FROM notifications');
assert.equal(rows.length, 4, 'Repeating an outcome must not create duplicates');
assert.ok(rows.every(row => row.target_user_id === owner && row.barangay === null));
assert.equal(rows.filter(row => row.title.includes('Water source')).length, 2);
await db.exec('BEGIN');
await db.query("INSERT INTO resident_reports(title,type,status,submitted_by) VALUES ('rollback','concern','done',$1)", [owner]);
await db.exec('ROLLBACK');
assert.equal((await db.query('SELECT * FROM notifications')).rows.length, 4);
await db.close();
console.log('Workflow notification SQL checks passed: outcomes, owner, duplicate prevention, rollback, repeatable migration.');
