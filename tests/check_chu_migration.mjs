// Usage: node check_chu_migration.mjs <path-to-pglite-module>
// Runs against an isolated in-memory PostgreSQL instance; never uses project credentials.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";
const { PGlite } = await import(pathToFileURL(process.argv[2]).href);
const db = new PGlite();
const id = (n) => `00000000-0000-0000-0000-${String(n).padStart(12, "0")}`;
await db.exec(`
  CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role;
  CREATE TABLE users(id uuid PRIMARY KEY);
  CREATE TABLE water_locations(id uuid PRIMARY KEY);
  CREATE TABLE resident_reports(id uuid PRIMARY KEY, title text, description text, type text,
    barangay text, latitude float, longitude float, submitted_by uuid REFERENCES users,
    actioned_by uuid REFERENCES users, status text, reason text);
  CREATE TABLE inspection_requests(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    location_id uuid REFERENCES water_locations, description text, barangay text,
    latitude float, longitude float, requested_by uuid REFERENCES users, assigned_to uuid REFERENCES users,
    status text, notes text, created_at timestamptz DEFAULT now());
`);
await db.query("INSERT INTO users VALUES ($1),($2)", [id(1), id(2)]);
await db.query("INSERT INTO water_locations VALUES ($1)", [id(3)]);
for (const [n, status] of [[10, "validated"], [11, "escalated"], [12, "escalated"], [13, "rejected"]]) {
  await db.query("INSERT INTO resident_reports(id,title,description,type,barangay,submitted_by,actioned_by,status,reason) VALUES ($1,$2,$3,'concern','Combado',$4,$5,$6,$7)",
    [id(n), `Report ${n}`, `Concern ${n} (ID: ${id(3)})`, id(1), id(2), status, n === 13 ? "[DONE] Previously completed" : null]);
}
await db.query("INSERT INTO inspection_requests(description,barangay,requested_by,status,notes) VALUES ($1,'Combado',$2,'completed','Legacy findings')",
  [`[Concern passed to CHU by Brgy Official] Report 11: Concern 11 (ID: ${id(3)})`, id(1)]);
const migration = await readFile(new URL("../migrations/20261027_chu_report_workflow.sql", import.meta.url), "utf8");
await db.exec(migration);
const status = async (n) => (await db.query("SELECT status FROM resident_reports WHERE id=$1", [id(n)])).rows[0].status;
assert.equal(await status(11), "done", "Legacy completed inspection repairs resident status");
assert.equal(await status(13), "done", "Legacy DONE marker is converted");
assert.equal((await db.query("SELECT count(*) FROM inspection_requests WHERE resident_report_id=$1", [id(12)])).rows[0].count, 1, "Failed legacy forwarding is repaired");
for (let i = 0; i < 2; i++) await db.query("SELECT forward_resident_report_to_chu($1,$2,$3)", [id(10), id(2), "Forwarded"]);
assert.equal((await db.query("SELECT count(*) FROM inspection_requests WHERE resident_report_id=$1", [id(10)])).rows[0].count, 1, "Repeat forwarding is idempotent");
assert.equal((await db.query("SELECT location_id FROM inspection_requests WHERE resident_report_id=$1", [id(10)])).rows[0].location_id, id(3));
await db.query("UPDATE inspection_requests SET status='in_progress', assigned_to=$2 WHERE resident_report_id=$1", [id(10), id(2)]);
assert.equal(await status(10), "in_progress", "Processing synchronizes to resident");
await db.query("UPDATE inspection_requests SET status='completed', notes='Contamination found' WHERE resident_report_id=$1", [id(10)]);
assert.equal(await status(10), "done", "Completion synchronizes to resident");
assert.equal((await db.query("SELECT reason FROM resident_reports WHERE id=$1", [id(10)])).rows[0].reason, "Contamination found");
await db.query("UPDATE resident_reports SET status='done', reason='Source registered' WHERE id=$1", [id(12)]);
assert.equal((await db.query("SELECT status FROM inspection_requests WHERE resident_report_id=$1", [id(12)])).rows[0].status, "completed", "Registry completion synchronizes to CHU");
await assert.rejects(db.query("SELECT forward_resident_report_to_chu($1,$2,NULL)", [id(10), id(2)]), /Closed reports/);
await db.exec(migration);
assert.equal(await status(10), "done", "Reapplying migration preserves completion");
console.log("PASS: PostgreSQL migration, exact legacy linking, missing-ticket recovery, idempotent forwarding, source ID extraction, bidirectional status/notes sync, closed-report protection, and migration reapplication.");
await db.close();
