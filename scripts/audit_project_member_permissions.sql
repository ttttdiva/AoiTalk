-- Enterprise project ACL audit (read-only).
--
-- Run after taking a database backup:
--   psql "$DATABASE_URL" -X -v ON_ERROR_STOP=1 \
--     -f scripts/audit_project_member_permissions.sql
--
-- A row matching an old generated JSON value is only a correction candidate.
-- The same value may have been explicitly selected by an administrator, so
-- this script never updates data and must not be converted into a blanket fix.

\set ON_ERROR_STOP on

BEGIN TRANSACTION READ ONLY;

SELECT version_num AS alembic_revision
FROM alembic_version;

-- Rows whose value is indistinguishable from the faulty 20260804_0002
-- backfill. Review the membership creation/change audit trail and business
-- intent before placing any id in the confirmed correction transaction.
SELECT
    pm.id AS project_member_id,
    pm.project_id,
    p.name AS project_name,
    pm.user_id,
    u.username,
    pm.role,
    pm.permissions,
    CASE LOWER(TRIM(pm.role))
        WHEN 'admin' THEN
            '{"read": true, "write": true, "delete": true, "manage_members": true, "manage_settings": true}'::jsonb
        WHEN 'member' THEN
            '{"read": true, "write": false, "delete": false, "manage_members": false, "manage_settings": false}'::jsonb
    END AS reviewed_target_permissions,
    CASE LOWER(TRIM(pm.role))
        WHEN 'admin' THEN 'old admin default omitted manage_settings'
        WHEN 'member' THEN 'old member default granted write'
    END AS candidate_reason
FROM project_members AS pm
JOIN projects AS p ON p.id = pm.project_id
JOIN users AS u ON u.id = pm.user_id
WHERE (
        LOWER(TRIM(pm.role)) = 'admin'
        AND pm.permissions::jsonb =
            '{"read": true, "write": true, "delete": true, "manage_members": true, "manage_settings": false}'::jsonb
    )
    OR (
        LOWER(TRIM(pm.role)) = 'member'
        AND pm.permissions::jsonb =
            '{"read": true, "write": true, "delete": false, "manage_members": false, "manage_settings": false}'::jsonb
    )
ORDER BY p.name, u.username, pm.id;

-- Ownership is authoritative. The corrective migration repairs these cases,
-- but any remaining row indicates migration failure or later data drift.
SELECT
    p.id AS project_id,
    p.name AS project_name,
    p.owner_id,
    u.username AS owner_username,
    pm.id AS project_member_id,
    pm.role,
    pm.permissions,
    CASE
        WHEN pm.id IS NULL THEN 'owner membership missing'
        WHEN LOWER(TRIM(pm.role)) <> 'owner' THEN 'owner membership role mismatch'
        WHEN COALESCE(
            pm.permissions::jsonb @>
                '{"read": true, "write": true, "delete": true, "manage_members": true, "manage_settings": true}'::jsonb,
            false
        ) IS NOT TRUE
            THEN 'owner membership ACL is not full access'
    END AS anomaly
FROM projects AS p
JOIN users AS u ON u.id = p.owner_id
LEFT JOIN project_members AS pm
    ON pm.project_id = p.id
   AND pm.user_id = p.owner_id
WHERE pm.id IS NULL
   OR LOWER(TRIM(pm.role)) <> 'owner'
   OR COALESCE(
       pm.permissions::jsonb @>
           '{"read": true, "write": true, "delete": true, "manage_members": true, "manage_settings": true}'::jsonb,
       false
   ) IS NOT TRUE
ORDER BY p.name, p.id;

-- Unknown, empty, NULL, and structurally malformed roles/ACLs should remain
-- deny-all until an administrator explicitly assigns a supported role/ACL.
SELECT
    pm.id AS project_member_id,
    pm.project_id,
    p.name AS project_name,
    pm.user_id,
    u.username,
    pm.role,
    pm.permissions,
    CASE
        WHEN pm.role IS NULL OR TRIM(pm.role) = '' THEN 'empty role'
        WHEN LOWER(TRIM(pm.role)) NOT IN ('owner', 'admin', 'member', 'viewer')
            THEN 'unsupported role'
        WHEN pm.permissions IS NULL THEN 'permissions NULL'
        WHEN jsonb_typeof(pm.permissions::jsonb) <> 'object'
            THEN 'permissions is not an object'
    END AS anomaly
FROM project_members AS pm
JOIN projects AS p ON p.id = pm.project_id
JOIN users AS u ON u.id = pm.user_id
WHERE pm.role IS NULL
   OR TRIM(pm.role) = ''
   OR LOWER(TRIM(pm.role)) NOT IN ('owner', 'admin', 'member', 'viewer')
   OR pm.permissions IS NULL
   OR jsonb_typeof(pm.permissions::jsonb) <> 'object'
ORDER BY p.name, u.username, pm.id;

COMMIT;
