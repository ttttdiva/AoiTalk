# Active Directory transport preflight

This document describes the **read-only transport gate** for Enterprise Active
Directory password authentication.  It is not an AD administration guide and
does not authorize access to a directory.  The directory remains the
credential and immutable-identity authority; AoiTalk only stores a local
ownership principal and an immutable objectGUID binding after a successful
user bind.

## Safety boundary

`scripts/validate_ad_transport.py` is a preflight utility.  With `--check` it
performs only the following bounded operations:

1. validate the operator-supplied `AOITALK_AD_*` configuration;
2. use at most `AOITALK_AD_MAX_ENDPOINTS` (hard maximum four) static FQDNs, or
   one configured `_ldaps._tcp` SRV name;
3. connect to TCP **636** with TLS 1.2 or newer and certificate/hostname
   validation; and
4. send one LDAP BASE-scope RootDSE read and stop at `SearchResultDone`.

The utility never accepts a username or password, performs a directory user search,
changes directory state, enumerates users/groups, follows referrals, exports
directory data, or obtains Kerberos/OIDC tokens.  No bind secret is mounted by
the Enterprise Compose file.  Application login supplies the user's password
directly to the canonical authentication service and must not persist it.

Configuration and TLS failures are fail-closed.  A successful TCP connection
or an open port alone is **not** evidence of a usable AD authentication path.

## Configuration contract

The Enterprise deployment exposes generic placeholders only; it contains no
company domain, controller name, IP address, CA, or credential.  Set these in
the target operator's protected environment/configuration:

| Variable | Meaning |
| --- | --- |
| `AOITALK_AD_ENABLED` | `false` by default; must be explicitly enabled. |
| `AOITALK_AD_DISCOVERY_MODE` | `static` (default) or `srv`. |
| `AOITALK_AD_SERVERS` | Comma-separated allow-listed controller FQDNs for static mode. IP literals are rejected. |
| `AOITALK_AD_SRV_RECORD` | One operator-approved `_ldaps._tcp` SRV FQDN for `srv` mode. With exactly one suffix, the conventional record is derived when this is blank. Targets are still suffix-checked and limited to four. |
| `AOITALK_AD_ALLOWED_DNS_SUFFIXES` | Required DNS suffix allowlist; matching is label-boundary exact. |
| `AOITALK_AD_PORT` | Must remain `636`; plaintext LDAP is unsupported. |
| `AOITALK_AD_BASE_DN` | Application search base (not read by the preflight RootDSE probe). |
| `AOITALK_AD_DOMAIN` | Optional DNS-shaped UPN domain used by the application bind template. |
| `AOITALK_AD_AUTHORITY` | Optional DNS-shaped immutable binding namespace; do not use a user/DN value. |
| `AOITALK_AD_LOGIN_ATTRIBUTE` | Application login attribute, normally `sAMAccountName`. |
| `AOITALK_AD_BIND_TEMPLATE` | Optional non-secret application bind-name template; never put a password here. |
| `AOITALK_AD_CA_FILE` | Optional target CA bundle. The path is read-only and must be a non-empty regular file. |
| `AOITALK_AD_USE_SYSTEM_CA` | Use the host/container trust store (`true` by default). |
| `AOITALK_AD_CONNECT_TIMEOUT_SECONDS` | Per-connect bound, 0.1–15 seconds. |
| `AOITALK_AD_OPERATION_TIMEOUT_SECONDS` | Per-operation bound, 0.1–30 seconds. |
| `AOITALK_AD_MAX_ENDPOINTS` | Per-run endpoint cap, 1–4. |

Use a secret manager or protected file for any approved test credential used
by the application login test.  Do not add it to `.env`, Compose, command
arguments, source, logs, screenshots, or bug reports.

## Operator procedure

1. Confirm the exact AD FQDN, DNS suffix, LDAPS certificate chain, and CA
   ownership/rotation with the Enterprise operator.  A repository snapshot or
   an old network note is not proof of current reachability.
2. Configure the variables above on the authorized Enterprise host.  Keep AD
   disabled until both endpoint and CA values have been reviewed.
3. Run a config-only check first:

   ```bash
   python3 scripts/validate_ad_transport.py --json
   ```

4. On the authorized network, run the bounded transport gate:

   ```bash
   python3 scripts/validate_ad_transport.py --check --json
   ```

5. Treat `status=ok` as transport evidence only.  It does not prove a user
   credential, authorization/group policy, JIT transaction, or production
   readiness.  Record timestamp, configured endpoint class, CA source, and
   result without recording secrets or directory entries.

If the target host lacks the optional `dnspython` dependency, use static mode
or install the dependency through the approved Enterprise image process; do
not replace SRV discovery with subnet scanning.  If no authorized AD test
credential exists, report `AUTHORIZED_AD_TEST_CREDENTIAL_UNAVAILABLE` and keep
the real-login gate pending rather than guessing or using a local credential.
