# Database migrations

Apply the SQL files in the connected Supabase project's SQL Editor before deploying the corresponding code.

## Resident email authentication (20261029_resident_email_auth.sql)

Apply before deploying the resident email-verification/password-reset backend. Adds private pending email tokens, atomic confirmation/reset functions, cross-worker request limits, verified-account metadata, and a session version for reset revocation. Existing accounts remain verified; admin-created accounts bypass confirmation. Public registration creates an account only after confirmation. See [RESIDENT_EMAIL_SETUP.md](../RESIDENT_EMAIL_SETUP.md) for Resend sender verification, backend secrets, and deployment steps.

## Recoverable water source archive (20261028_water_source_archive.sql)

Apply after `20261028_water_status_history.sql`, then deploy the backend, staff, and admin frontends together. The Water Sources Registry has an Archived Stations view with Restore. Archiving keeps the station row and original ID, photos, dated status logs, linked concerns/inspections, and saved forecasts. A restored source returns with its prior status and history. Archive and restore actions record the actor and time; repeated requests do not create extra events. Archived sources are excluded from active maps, analytics, forecast runs, concern selection, and the current station registry. Historical report periods ending before the archive day can still include the source.

Only active editors may archive or restore. Barangay officials are limited to their assigned barangay. An old edit tab cannot modify an archived station. This migration cannot recover stations physically deleted before it was applied, because the old delete operation cascaded through dependent history.

## Report doctor review (20261028_report_review_workflow.sql)

Apply after `20261028_report_snapshots.sql`, then deploy the backend and staff frontend together. Existing archives remain Ready to send. Only their author (or an administrator) can send them to the CHU doctor's inbox. Sending stores the sender, receipt time, review event, and in-app notification atomically. Retrying a send preserves the original receipt and does not create another notification.

Doctor queues are Received, Under Review, and Done Archive. Starting review assigns the report to that doctor; another doctor can read it but cannot finish their review. An administrator can act when reassignment is necessary. Completion requires review notes and records the doctor and completion time. Done means document review is complete, not that the water is safe. Completed notes cannot be overwritten through the workflow. Opening, printing, and downloading never change review status.

Review metadata and audit events are separate from the fixed report snapshot. Printable HTML and CSV append the current review record without changing the archived data fingerprint. Doctors cannot open unsent archives. The inspector archive refreshes review progress on focus and every minute. Checks: `tests/test_report_review.py` and `tests/check_report_review.mjs` (execute with an installed PGlite module path); the latter runs the real SQL functions, retry/ownership checks, rollback checks, and repeatable migrations in an isolated database.

## Report snapshots (20261028_report_snapshots.sql)

Adds a versioned JSON snapshot and data fingerprint to each generated report. Apply after the water-status-history migration (and water-forecast migration if including the optional model outlook), then deploy backend, staff, and admin frontends together. Existing report metadata is preserved; old records without a snapshot cannot produce a truthful data export and must be regenerated.

Report periods use Asia/Manila. Source status is reconstructed from effective-day snapshots on or before the period end. Concern and inspection workflow states are labeled as current at generation; the system does not have historical completion timestamps. Deleted sources cannot be reconstructed. Sample checks are deduplicated by source/date/time; no missing chlorine or chemical measurements are invented. Resident names and photo URLs are omitted from exports. Generated reports become available in the authorized report archive; generation does not send email or assert delivery to a named officer.

Preview is read-only. Generate archives the data. CSV and printable HTML downloads use the stored snapshot and verify its SHA-256 fingerprint; database JSON numeric normalization does not change this fingerprint. CSV values are protected against spreadsheet formula injection and HTML escapes user content. Regression tests: `tests/test_report_generation.py`.

## Water status history (20261028_water_status_history.sql)

Adds explicit effective day, sample date/time, and nullable bacteria results to history. Existing rows are backfilled using Philippine time. Each save preserves a complete snapshot and keeps same-day revisions. The `update_water_location_history` function atomically saves changes and synchronizes the current station from the newest effective snapshot, so backdated edits cannot replace newer results. Unchanged days create no event; the UI carries the last snapshot forward. Registration history is created by a database trigger.

Apply the migration before deploying the backend and staff frontend. This migration preserves existing notes but cannot recover notes or revisions deleted by the old implementation. History reads never update the station. `tests/check_water_history.mjs` executes the actual migration and update logic in isolated PGlite PostgreSQL (pass the module path as its argument).

## CHU workflow (20261027_chu_report_workflow.sql)

- Adds a unique resident-report link to each CHU inspection ticket.
- Creates an atomic forwarding operation and triggers that synchronize Processing, Done, and Dismissed statuses.
- Recovers old tickets only when the report and ticket have one unambiguous content/submitter/barangay match.
- Creates missing tickets for already-forwarded reports when no legacy ticket matches.
- Converts legacy [DONE] and [DISMISSED] markers to explicit statuses.
- Keeps ambiguous legacy tickets unchanged for manual review; the final commented query lists unlinked old endorsements.

Apply this migration, then deploy the backend, staff frontend, and resident frontend together. The new CHU list requires the new relationship; forwarding requires the database function. No live schema changes are made by editing these files.

Workflow: resident submits -> barangay reviews/validates -> Pass to CHU -> CHU starts Processing -> CHU records findings -> Done. Registering a forwarded new source also closes its linked ticket. Dismissed stays separate from Done. Done describes case processing, not water safety.

Checks: Python API regression tests are in tests/test_chu_workflow.py. tests/check_chu_migration.mjs runs the migration and synchronization checks in an isolated PGlite PostgreSQL instance (provide its module path as the command-line argument). No project credentials are used by these tests.
