// node tests/check_report_review.mjs <absolute path to PGlite dist/index.js>
// Runs the real report migrations and workflow functions without project credentials.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';
const { PGlite } = await import(pathToFileURL(process.argv[2]).href);
const db = new PGlite();
const id = (n) => '00000000-0000-0000-0000-' + String(n).padStart(12, '0');
await db.exec(`
CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role;
CREATE TABLE users(id uuid PRIMARY KEY, role text, full_name text, is_active boolean DEFAULT true);
CREATE TABLE reports(id uuid PRIMARY KEY DEFAULT gen_random_uuid(), title text NOT NULL, generated_by uuid REFERENCES users,
  barangay text, status text DEFAULT 'completed');
CREATE TABLE notifications(id uuid PRIMARY KEY DEFAULT gen_random_uuid(), title text, message text,
  type text, barangay text, target_roles text[], created_by uuid REFERENCES users);
`);
for (const [n, role] of [[1, 'sanitization_inspector'], [2, 'city_health_officer'], [3, 'city_health_officer'], [4, 'sanitization_inspector'], [5, 'admin']]) {
  await db.query('INSERT INTO users(id,role,full_name) VALUES($1,$2,$3)', [id(n), role, 'User ' + n]);
}
await db.query("INSERT INTO reports(id,title,generated_by) VALUES($1,'Legacy report',$2)", [id(10), id(1)]);
for (const file of ['20261028_report_snapshots.sql', '20261028_report_review_workflow.sql']) {
  await db.exec(await readFile(new URL('../migrations/' + file, import.meta.url), 'utf8'));
}
// Reapplying the workflow must preserve receipts and states, not reset them.
const migration = await readFile(new URL('../migrations/20261028_report_review_workflow.sql', import.meta.url), 'utf8');
await db.exec(migration);
const snapshot = { prepared_by: 'User 1', summary: { sources: 12 }, notes: 'Archived records' };
for (const n of [11, 12, 13]) {
  await db.query("INSERT INTO reports(id,title,generated_by,snapshot,schema_version,content_sha256) VALUES($1,'Water report',$2,$3,1,'fingerprint')", [id(n), id(1), JSON.stringify(snapshot)]);
}
const report = async (n) => (await db.query('SELECT * FROM reports WHERE id=$1', [id(n)])).rows[0];
const send = (n, actor = 1) => db.query('SELECT send_report_to_chu($1,$2)', [id(n), id(actor)]);
const review = (n, actor, action, notes = null) => db.query('SELECT review_archived_report($1,$2,$3,$4)', [id(n), id(actor), action, notes]);
assert.equal((await report(10)).review_status, 'ready');
await assert.rejects(send(10), /archived snapshot/);
await assert.rejects(send(11, 4), /Only the report author/);
await assert.rejects(send(11, 2), /Only active inspectors/);
await assert.rejects(review(11, 2, 'start'), /not been sent/);
await send(11);
const sentAt = (await report(11)).submitted_at;
await Promise.all([send(11), send(11)]);
assert.equal((await report(11)).submitted_at.toISOString(), sentAt.toISOString());
assert.equal((await db.query('SELECT count(*) FROM notifications')).rows[0].count, 1, 'A sent report produces only one notification');
assert.equal((await db.query("SELECT count(*) FROM report_review_events WHERE event='sent'")).rows[0].count, 1);
await assert.rejects(review(11, 2, 'complete', 'Read it'), /Start review/);
await assert.rejects(review(11, 1, 'start'), /Only an active CHU doctor/);
await review(11, 2, 'start');
const startedAt = (await report(11)).review_started_at;
await review(11, 2, 'start');
assert.equal((await report(11)).review_started_at.toISOString(), startedAt.toISOString());
await assert.rejects(review(11, 3, 'start'), /another doctor/);
await assert.rejects(review(11, 3, 'complete', 'Override'), /another doctor/);
await assert.rejects(review(11, 2, 'complete', '   '), /Record review notes/);
assert.equal((await report(11)).review_status, 'in_review');
await review(11, 2, 'complete', '  Reviewed. Verify the missing sample dates.  ');
const completed = await report(11);
assert.equal(completed.review_status, 'done');
assert.equal(completed.review_notes, 'Reviewed. Verify the missing sample dates.');
assert.equal(completed.reviewed_by, id(2));
assert.equal(completed.reviewed_by_name, 'User 2');
assert.deepEqual(completed.snapshot, snapshot, 'Review never changes the source-data snapshot');
assert.equal(completed.content_sha256, 'fingerprint');
await review(11, 2, 'complete', 'Attempt to overwrite');
await send(11);
assert.equal((await report(11)).review_notes, completed.review_notes, 'Completed review cannot be overwritten or resent');
assert.equal((await db.query('SELECT count(*) FROM report_review_events WHERE report_id=$1', [id(11)])).rows[0].count, 3);
await db.exec(migration);
assert.equal((await report(11)).review_status, 'done', 'Reapplying the migration preserves the archive');
await db.query('UPDATE users SET is_active=false WHERE id=$1', [id(1)]);
await assert.rejects(send(12), /Only active inspectors/);
// A failed notification must also roll back the receipt and audit event.
await db.exec("ALTER TABLE notifications ADD CONSTRAINT reject_report_notification CHECK(title <> 'Water surveillance report received') NOT VALID");
await assert.rejects(send(13, 5));
assert.equal((await report(13)).review_status, 'ready');
assert.equal((await db.query('SELECT count(*) FROM report_review_events WHERE report_id=$1', [id(13)])).rows[0].count, 0);
console.log('Report review PostgreSQL checks passed: one-time send, notification/audit atomicity, role/author checks, doctor ownership, explicit Done, immutable snapshot and review, safe retries, and repeatable migrations.');
await db.close();
