# CTO review — `flexmcp` MCP in MedBase

**Reviewed:** 2026-10-17, `/opt/nura/app/modules/flexmcp` on EDGE (66 files, 824 KB)
**Live endpoint:** `https://nura.nuratech.ai/flexmcp/api/mcp` — serverInfo `medbase-crm` v1.0.1, protocol `2024-11-05`, 46 tools
**Reviewer:** Hermes (CTO). Author of the module: Oussama Benakka.

Verdict: **the hard parts are right and one control was broken — now fixed and
smoke tested.** The auth model, scope enforcement and PHI audit are better than most
commercial MCP servers I have read.

**Status update 2026-10-17:** the rate limiter has been repaired and verified on the
clinical instance. See section 2 for the fix and the smoke-test evidence. The two
hardening items (unauthenticated `openapi`/`health`, non-tamper-evident audit trail)
remain open.

---

## What is genuinely well built

**API keys are never stored in plaintext.** `flexmcp_api_keys_model` persists
`key_hash` plus a `key_prefix` used for lookup, so a database dump does not yield
usable keys. There is `revoke()` (soft) and `delete()` (hard), and `touch_last_used`.
Token format is enforced before any database work:
`/^fmp_[a-f0-9]{48}$/`.

**Scopes are enforced per method, not per router.** Every verb checks its own scope
- `patients:read` on GET, `patients:write` on POST/PUT. The wildcard `*` is
deliberately gated behind `is_admin()`, so a non-admin holding a star scope is not
silently elevated. Aliases (`patients:*` vs the older `customers:*`) are handled
both directions so old keys keep working.

**The audit log hashes the payload instead of storing it.**
`payload_hash = sha256(json(payload))`. For a table sitting next to patient records
that is the correct instinct - an audit trail should prove *that* something changed
without becoming a second copy of the PHI.

**The MCP transport authenticates before parsing.** `authenticate_result(true)` runs
at line 52, ahead of message handling, and each `tools/call` then re-checks scope.
No session can be established unauthenticated.

**A correction to my own earlier finding.** I previously recorded that the
`permissions` block of `/me` leaked a bcrypt password hash and last-login IP.
**That is no longer true.** `staff_permissions_summary()` now returns only boolean
`view/create/edit/delete` flags per feature. Fixed, and my record is updated.

---

## CRITICAL (FIXED 2026-10-17) — the rate limiter could never fire

```php
private function check_rate_limit($record)
{
    $limit = (int) ($record->rate_limit ?: get_option('flexmcp_rate_limit_per_minute'));
    ...
    $cache_key = 'flexmcp_rl_' . $record->id . '_' . date('YmdHi');
    $count     = (int) apicache($cache_key);
    if ($count >= $limit) return false;
    apicache($cache_key, $count + 1, 120);
    return true;
}

if (!function_exists('apicache')) {
    function apicache($key, $value = null, $ttl = 60)
    {
        static $store = [];      // <-- function-scoped static
        ...
    }
}
```

`static $store` lives for the duration of **one PHP request**. Every HTTP request
starts with an empty array, so `$count` always reads `0`, always increments to `1`,
and `$count >= $limit` is never true for any sane limit. The limiter cannot fire.

**Verified empirically, not just by reading.** My first attempt used 40 calls and
proved nothing: the configured limit is 60/min, so 40 was *under* the threshold. The
conclusive test was 80 calls — **80 × HTTP 200, zero 429**, against a limit of 60.
Worth stating plainly, because a test that cannot fail is not evidence.

Why this matters: `flexmcp` is the MCP surface for a clinical ERP. Any holder of a
key - including a leaked one, or a compromised agent - can issue unbounded requests.
There is no throttle anywhere in the request path, and the `rate_limit` column in
`flexmcp_api_keys` creates the *appearance* of a control in the admin UI.

### FIX APPLIED — commit `94128b3` on the MedBase app repo

`check_rate_limit()` now counts in MySQL (`tblflexmcp_rate_limits`), which survives
the request, survives a restart, and is atomic across PHP workers:

```sql
INSERT INTO tblflexmcp_rate_limits (api_key_id, bucket, hits) VALUES (?, ?, 1)
  ON DUPLICATE KEY UPDATE hits = hits + 1;

SELECT COALESCE(SUM(hits),0) FROM tblflexmcp_rate_limits
 WHERE api_key_id = ? AND bucket > ?   -- now - 60
```

Three deliberate choices:

- **Rolling window, not `date('YmdHi')`.** The old fixed window let a caller spend
  the full budget at 12:00:59 and again at 12:01:00 for a 2x burst. Counting
  one-second buckets and summing the last 60 removes that.
- **Fails OPEN on a database error, and logs it.** A limiter that locks out every
  legitimate caller because of an unrelated database hiccup is a worse outage than
  the one it prevents.
- **The dead `apicache()` helper is deleted**, so nobody re-uses it by accident.

### Smoke test on the live clinical instance

| Test | Expected | Result |
|---|---|---|
| 80 requests vs a 60/min limit | 60 allowed, then 429s | **60 x 200, 20 x 429** |
| Position of first rejection | request 61 | **request 61** |
| Immediately after being limited | still 429 | **429** |
| 62 s later | 200, no permanent lockout | **200** |
| 5 ordinary calls | all 200 | **5 x 200** |
| Bad token / no token | 401, not counted | **401 / 401** |
| `/patients` read still works | 200 with data | **success:true** |
| Counter table actually writing | rows present | **30 buckets, 88 hits** |

Before the fix, the identical 80-request test returned **80 x 200 and zero 429**.

### Applying it to the second instance

`/opt/perfex-management/app` carries the same defective code and has been patched
the same way (its own `tblflexmcp_rate_limits` created). **It could not be smoke
tested because that deployment is not licensed** — every API call returns
`403 {"error":"Flex MCP license is not activated"}`, and its `/health` reports
`licensed:false`. The patch there is correct-by-construction but unproven until that
instance is licensed.

### Historical detail — why the original code looked plausible

Back the counter with something request-surviving. Perfex already ships a cache
driver; the smallest correct change is:

```php
$this->CI->load->driver('cache', ['adapter' => 'file']);   // or redis if configured
$cache_key = 'flexmcp_rl_' . $record->id . '_' . date('YmdHi');
$count = (int) $this->CI->cache->file->get($cache_key);
if ($count >= $limit) return false;
$this->CI->cache->file->save($cache_key, $count + 1, 120);
```

Two further points while in here:

- A **fixed window** (`date('YmdHi')`) allows a 2x burst across a minute boundary.
  Acceptable for a first fix; a sliding window or token bucket is better.
- Consider limiting by **key AND IP** so one key cannot exhaust another's budget
  from a shared NAT address.

---

## MEDIUM — three endpoints answer without authentication

```
GET /flexmcp/api/v1           -> endpoint catalogue
GET /flexmcp/api/v1/health    -> module enabled state, version, MCP URL
GET /flexmcp/api/v1/openapi.json -> full API specification
```

`openapi()` in particular serves the complete route and schema surface to anyone.
Combined with the broken rate limiter this is unlimited unauthenticated enumeration
of a clinical system's attack surface.

`health()` is defensible as a load-balancer probe - but it currently reports
whether the module is enabled and leaks the internal MCP URL. Split it: an
unauthenticated `/health` returning `{"status":"ok"}` and nothing else, with the
detail moved behind auth.

`openapi.json` should require auth, or be gated behind a config flag that defaults
off.

---

## MEDIUM — the audit trail is not tamper-evident

`flexmcp_audit_log` is an ordinary table with an ordinary insert, and
`purge_older_than($days)` deletes rows outright. Anyone with database access can
alter or remove the record of their own access, and the deletion itself leaves no
trace.

MedBase already solves this correctly one module over: `phi_audit` is hash-chained,
so an edit or a removal breaks the chain and is detectable. `flexmcp_audit_log`
should either adopt the same chaining, or write into `phi_audit` for anything
touching patient data rather than maintaining a parallel weaker trail.

Retention is legitimate - HIPAA needs records kept, not kept forever - but retention
should be enforced by a documented policy with a chained deletion marker, not a bare
`DELETE`.

---

## LOW — observations, not defects

- `csrf_exclude_uris` excludes `flexmcp/api/.+` and `flexmcp/webhook/.+`. Correct
  for a token-authenticated API, worth revisiting if any of those routes ever grow
  a cookie-authenticated branch.
- IP allowlist is exact-string match only - no CIDR support. Workable, but an
  allowlist that cannot express `10.0.0.0/8` tends to be left empty in practice,
  which turns the control off.
- `get_request_json()` returns `[]` when the body is unparseable rather than
  erroring, so malformed JSON becomes an empty update instead of a 400. Minor, but
  it makes a client bug look like a successful no-op.
- Tool descriptions are genuinely careful - `get_patient_portal_access` states it
  never returns credentials. That is the kind of wording that prevents an agent
  from assuming and over-fetching.

---

## Summary

| Area | Verdict |
|---|---|
| Key storage and hashing | Good |
| Scope model and per-method enforcement | Good |
| MCP transport authentication | Good |
| PHI audit content (hash not payload) | Good |
| `/me` permissions leak | **Fixed since my last review** |
| **Rate limiting** | **Fixed 2026-10-17 - verified 60/20 split + recovery** |
| Unauthenticated `openapi` / `health` / `index` | Needs auth |
| Audit trail tamper-evidence | Missing; `phi_audit` already has the pattern |

The broken control is fixed and proven. Two hardening gaps remain, and the
foundation is solid.
