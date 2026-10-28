// node tests/check_resident_email_auth.mjs <path to @electric-sql/pglite/dist/index.js>
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';
const { PGlite } = await import(pathToFileURL(process.argv[2]).href);
const db = new PGlite();
await db.exec(`CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role;
CREATE TABLE users(id uuid PRIMARY KEY DEFAULT gen_random_uuid(), full_name text NOT NULL,
 username text UNIQUE NOT NULL, email text UNIQUE, password_hash text NOT NULL, role text NOT NULL,
 barangay text, is_active boolean DEFAULT true);
INSERT INTO users(full_name,username,email,password_hash,role) VALUES('Old','old','old@example.com','oldhash','resident');`);
const sql = await readFile(new URL('../migrations/20261029_resident_email_auth.sql', import.meta.url), 'utf8');
await db.exec(sql);
await db.exec(sql);
assert.equal((await db.query('SELECT email_verified FROM users')).rows[0].email_verified, true);
const issue = async (hash, purpose, email, payload, ip=email) => (await db.query('SELECT issue_resident_email_token($1,$2,$3,$4,$5) AS ok', [hash,purpose,email,ip,JSON.stringify(payload)])).rows[0].ok;
const finish = async (hash, purpose, password=null) => (await db.query('SELECT complete_resident_email_action($1,$2,$3) AS ok', [hash,purpose,password])).rows[0].ok;
const pending = {full_name:'New Resident',username:'newresident',password_hash:'hashed',barangay:'Abgao',role:'admin'};
assert.equal(await issue('signup','register','new@example.com',pending), true);
assert.equal((await db.query("SELECT count(*)::int AS n FROM users WHERE username='newresident'")).rows[0].n, 0);
assert.equal(await issue('resend','register','new@example.com',pending), false);
assert.equal(await finish('signup','reset','evil'), false);
assert.equal(await finish('signup','register'), true);
assert.equal(await finish('signup','register'), false);
const user = (await db.query("SELECT * FROM users WHERE username='newresident'")).rows[0];
assert.equal(user.role,'resident');
assert.equal(user.email_verified,true);
assert.equal((await db.query("SELECT payload FROM resident_email_tokens WHERE token_hash='signup'")).rows[0].payload.password_hash, undefined);
await db.exec("UPDATE resident_email_tokens SET created_at=now()-interval '2 minutes'");
const payload = {user_id:user.id,auth_version:0};
assert.equal(await issue('reset1','reset',user.email,payload),true);
assert.equal(await finish('reset1','register'),false);
assert.equal(await finish('reset1','reset','newhash'),true);
assert.equal(await finish('reset1','reset','replayhash'),false);
const reset = (await db.query('SELECT * FROM users WHERE id=$1',[user.id])).rows[0];
assert.equal(reset.password_hash,'newhash'); assert.equal(reset.auth_version,1);
await db.exec("UPDATE resident_email_tokens SET created_at=now()-interval '2 minutes'");
assert.equal(await issue('stale','reset',user.email,payload),true);
assert.equal(await finish('stale','reset','bad'),false);
await db.exec("UPDATE resident_email_tokens SET created_at=now()-interval '2 minutes'");
assert.equal(await issue('expired','reset','old@example.com',{user_id:reset.id,auth_version:1}),true);
await db.exec("UPDATE resident_email_tokens SET expires_at=now()-interval '1 second' WHERE token_hash='expired'");
assert.equal(await finish('expired','reset','bad'),false);
assert.equal(await finish('missing','reset','bad'),false);
// Even a valid token cannot reset staff/admin accounts or deactivated accounts.
for (const [name,role,active] of [['admin','admin',true],['inactive','resident',false]]) {
  const row=(await db.query('INSERT INTO users(full_name,username,email,password_hash,role,is_active) VALUES($1,$1,$2,$3,$4,$5) RETURNING id',[name,`${name}@example.com`,'oldhash',role,active])).rows[0];
  assert.equal(await issue(name,'reset',`${name}@example.com`,{user_id:row.id,auth_version:0}),true);
  assert.equal(await finish(name,'reset','bad'),false);
}
// Cross-mailbox abuse is limited per client; grants protect pending password hashes.
for(let i=0;i<20;i++) assert.equal(await issue(`rate${i}`,'reset',`rate${i}@example.com`,{},'same-client'),true);
assert.equal(await issue('rate21','reset','last@example.com',{},'same-client'),false);
await db.exec('SET ROLE anon');
await assert.rejects(db.query('SELECT * FROM resident_email_tokens'), /permission denied/);
await assert.rejects(finish('signup','register'), /permission denied/);
await db.exec('RESET ROLE');
console.log('Resident email SQL checks passed: pending signup, verification, replay, purpose, expiry, reset, session version, eligibility, rate limits, privileges, repeatable migration.');
await db.close();
