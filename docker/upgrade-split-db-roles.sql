-- One-time conversion of a bundled-compose PostgreSQL cluster created before
-- #324 into the split-role shape a fresh install has. Run by the operator,
-- never automatically. DEPLOYMENT.md, "Upgrading: split database roles", is
-- the procedure (take a backup first); this is the command it gives:
--
--   docker compose -f <bundle> stop obsidian-mcp
--   docker compose -f <bundle> exec -T postgres sh -c \
--     'psql -X -v ON_ERROR_STOP=1 -U obsidian_mcp -d obsidian_mcp \
--           -v admin_pw="$POSTGRES_PASSWORD" -v app_pw="$OBSIDIAN_DB_PASSWORD"' \
--     < docker/upgrade-split-db-roles.sql
--   docker compose -f <bundle> restart postgres
--   docker compose -f <bundle> up -d
--
-- The two passwords come from the NEW postgres container's environment
-- (postgres.env and .env, already validated by docker/postgres-entrypoint.sh),
-- so neither appears on the host command line. The connection is the
-- container's local socket, which the image's default pg_hba.conf trusts.
--
-- Before:  OID 10 (the bootstrap superuser) is named obsidian_mcp, and the app
--          connects as it.
-- After:   OID 10 is named postgres, with admin_pw. A new NOSUPERUSER role
--          obsidian_mcp, with app_pw, owns database obsidian_mcp, every user
--          object in it and every default-privilege entry the old role held
--          there. The vector extension and its member objects stay with
--          postgres, exactly as on a fresh install.
--
-- Why it is shaped like this (openspec/changes/compose-db-roles, design D7):
--   * OID 10 cannot lose SUPERUSER, and a session cannot rename its own role,
--     so the rename happens from a temporary superuser's session;
--   * REASSIGN OWNED refuses the bootstrap role (its objects are pinned and
--     have no pg_shdepend rows), so ownership moves catalog by catalog;
--   * all of it is ONE transaction ending in a self-check, so a failure
--     leaves the cluster exactly as it was (plus the temporary role, which a
--     re-run removes);
--   * a session that authenticated as the old obsidian_mcp before the commit
--     is still OID 10 afterwards, i.e. still a superuser under the new name,
--     so after the commit every other OID-10 client session is terminated and
--     the script fails unless none is left;
--   * every session that carries a password sets log_statement = none and
--     log_min_error_statement = panic first, so neither a logged statement nor
--     a failing ALTER/CREATE ROLE ... PASSWORD writes a password to the
--     server log.
--
-- On a cluster that is already split it removes a temporary role an
-- interrupted earlier run left behind, runs the same self-check as the
-- conversion (the fresh-install shape), prints "already split" and changes
-- nothing else; a partly-split cluster fails that check and is refused with
-- what is wrong.

\set ON_ERROR_STOP on
\set QUIET on

-- ── 1. Preflight ───────────────────────────────────────────────────────────

-- Input presence is checked client-side; no password is sent to the server
-- before logging is turned off below.
\if :{?admin_pw}
\else
DO $$ BEGIN RAISE EXCEPTION 'upgrade-split-db-roles: admin_pw is not set (pass -v admin_pw="$POSTGRES_PASSWORD"); nothing was changed'; END $$;
\endif
\if :{?app_pw}
\else
DO $$ BEGIN RAISE EXCEPTION 'upgrade-split-db-roles: app_pw is not set (pass -v app_pw="$OBSIDIAN_DB_PASSWORD"); nothing was changed'; END $$;
\endif

SELECT
    current_database() = 'obsidian_mcp' AS right_database,
    EXISTS (SELECT 1 FROM pg_roles WHERE oid = 10 AND rolname = 'postgres')
        AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'obsidian_mcp' AND NOT rolsuper)
        AS already_split,
    EXISTS (SELECT 1 FROM pg_roles WHERE oid = 10 AND rolname = 'obsidian_mcp')
        AND NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'postgres')
        AS pre_split,
    (SELECT rolname FROM pg_roles WHERE oid = 10) AS bootstrap_name,
    EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'postgres') AS has_postgres,
    COALESCE((SELECT rolsuper::text FROM pg_roles WHERE rolname = 'obsidian_mcp'), 'absent')
        AS app_role_super
\gset

\if :right_database
\else
DO $$ BEGIN RAISE EXCEPTION 'upgrade-split-db-roles: connect to database obsidian_mcp (-d obsidian_mcp); nothing was changed'; END $$;
\endif

\if :already_split
    -- The documented command connects as obsidian_mcp, which is no longer a
    -- superuser here; the check below needs one.
    \c obsidian_mcp postgres
    \set QUIET on
\elif :pre_split
\else
SELECT set_config('omcp.shape_message', format(
    'upgrade-split-db-roles: unrecognised cluster shape (bootstrap superuser named %s; role postgres %s; role obsidian_mcp superuser=%s). Expected either the pre-#324 shape (bootstrap superuser obsidian_mcp, no role postgres) or the split shape. Nothing was changed.',
    :'bootstrap_name',
    CASE WHEN :'has_postgres'::boolean THEN 'exists' ELSE 'absent' END,
    :'app_role_super'
), false) AS ignored
\gset
DO $$ BEGIN RAISE EXCEPTION '%', current_setting('omcp.shape_message'); END $$;
\endif

-- A superuser session from here on (OID 10 under either name).
SET client_min_messages = warning;
SET log_statement = 'none';
SET log_min_duration_statement = -1;
SET log_min_error_statement = 'panic';

SELECT length(:'admin_pw') = 0 AS admin_pw_empty,
       length(:'app_pw') = 0 AS app_pw_empty
\gset
\if :admin_pw_empty
DO $$ BEGIN RAISE EXCEPTION 'upgrade-split-db-roles: admin_pw is empty (is POSTGRES_PASSWORD set in postgres.env?); nothing was changed'; END $$;
\endif
\if :app_pw_empty
DO $$ BEGIN RAISE EXCEPTION 'upgrade-split-db-roles: app_pw is empty (is OBSIDIAN_DB_PASSWORD set in .env?); nothing was changed'; END $$;
\endif

-- The temporary role is a passwordless SUPERUSER reachable over the local
-- socket. A previous run may have committed the conversion and stopped before
-- its last step, so it is dropped on both paths.
DROP ROLE IF EXISTS obsidian_mcp_split_tmp;

\if :already_split
SELECT set_config('omcp.mode', 'check', false) AS ignored
\gset
BEGIN;
\else
CREATE ROLE obsidian_mcp_split_tmp LOGIN SUPERUSER;

-- ── 2. The conversion, as the temporary superuser, in one transaction ──────

\c obsidian_mcp obsidian_mcp_split_tmp
\set QUIET on
SET client_min_messages = warning;
SET log_statement = 'none';
SET log_min_duration_statement = -1;
SET log_min_error_statement = 'panic';
SELECT set_config('omcp.mode', 'convert', false) AS ignored
\gset

BEGIN;

ALTER ROLE obsidian_mcp RENAME TO postgres;
-- A rename clears an MD5 password; set the admin password explicitly either way.
ALTER ROLE postgres PASSWORD :'admin_pw';
CREATE ROLE obsidian_mcp LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD :'app_pw';
ALTER DATABASE obsidian_mcp OWNER TO obsidian_mcp;
\endif

-- Objects created by an extension (pg_depend deptype 'e'), and recursively
-- whatever is internally bound to them (deptype 'i', e.g. an extension type's
-- array type), stay with the superuser: that is the fresh-install shape.
CREATE TEMP TABLE omcp_ext_closure ON COMMIT DROP AS
WITH RECURSIVE closure(classid, objid) AS (
    SELECT classid, objid FROM pg_depend
     WHERE deptype = 'e' AND refclassid = 'pg_extension'::regclass
    UNION
    SELECT d.classid, d.objid
      FROM pg_depend d
      JOIN closure c ON d.refclassid = c.classid AND d.refobjid = c.objid
     WHERE d.deptype = 'i'
)
SELECT classid, objid FROM closure;

\if :already_split
\else
DO $convert$
DECLARE
    app oid := (SELECT oid FROM pg_roles WHERE rolname = 'obsidian_mcp');
    r record;
    obj_kw text;
    acl_code "char";
    old_acl aclitem[];
    new_acl aclitem[];
    item record;
    grantee_sql text;
BEGIN
    -- A user object: OID at or above FirstNormalObjectId (16384; everything
    -- initdb created is below it), owned by OID 10, not in the extension
    -- closure. Temporary-table schemas (pg_temp_N, pg_toast_temp_N) are also
    -- owned by OID 10 and are skipped by name.

    -- Schemas. `public` (OID 2200) is owned by pg_database_owner from PG 15 on
    -- and is then left alone; an older cluster's public moves.
    FOR r IN
        SELECT n.oid, n.nspname FROM pg_namespace n
         WHERE n.nspowner = 10
           AND (n.oid >= 16384 OR n.nspname = 'public')
           AND n.nspname !~ '^pg_'
           AND NOT EXISTS (SELECT 1 FROM omcp_ext_closure c
                            WHERE c.classid = 'pg_namespace'::regclass AND c.objid = n.oid)
    LOOP
        EXECUTE format('ALTER SCHEMA %I OWNER TO obsidian_mcp', r.nspname);
    END LOOP;

    -- Relations. Indexes, TOAST tables, row types and column-owned sequences
    -- (serial: deptype 'a', identity: deptype 'i') follow their table.
    FOR r IN
        SELECT c.oid, c.relkind FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE c.relowner = 10 AND c.oid >= 16384
           AND n.nspname !~ '^pg_'
           AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
           AND NOT EXISTS (SELECT 1 FROM omcp_ext_closure x
                            WHERE x.classid = 'pg_class'::regclass AND x.objid = c.oid)
           AND NOT (c.relkind = 'S' AND EXISTS (
                SELECT 1 FROM pg_depend d
                 WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid
                   AND d.refclassid = 'pg_class'::regclass AND d.refobjsubid > 0
                   AND d.deptype IN ('a', 'i')))
         ORDER BY c.relkind = 'S', c.oid
    LOOP
        obj_kw := CASE r.relkind
            WHEN 'v' THEN 'VIEW'
            WHEN 'm' THEN 'MATERIALIZED VIEW'
            WHEN 'f' THEN 'FOREIGN TABLE'
            WHEN 'S' THEN 'SEQUENCE'
            ELSE 'TABLE' END;
        EXECUTE format('ALTER %s %s OWNER TO obsidian_mcp', obj_kw, r.oid::regclass);
    END LOOP;

    -- Functions, procedures, aggregates.
    FOR r IN
        SELECT p.oid, p.prokind FROM pg_proc p
         WHERE p.proowner = 10 AND p.oid >= 16384
           AND NOT EXISTS (SELECT 1 FROM omcp_ext_closure x
                            WHERE x.classid = 'pg_proc'::regclass AND x.objid = p.oid)
    LOOP
        obj_kw := CASE r.prokind WHEN 'a' THEN 'AGGREGATE' WHEN 'p' THEN 'PROCEDURE' ELSE 'FUNCTION' END;
        EXECUTE format('ALTER %s %s OWNER TO obsidian_mcp', obj_kw, r.oid::regprocedure);
    END LOOP;

    -- Types: enums, domains, ranges, multiranges (PG 16 does not move a
    -- range's multirange with it), standalone composites. Array types follow
    -- their element type; a table's row type follows the table.
    FOR r IN
        SELECT t.oid, t.typtype FROM pg_type t
          JOIN pg_namespace n ON n.oid = t.typnamespace
         WHERE t.typowner = 10 AND t.oid >= 16384
           AND n.nspname !~ '^pg_'
           AND t.typtype IN ('e', 'd', 'r', 'm', 'c')
           AND (t.typtype <> 'c' OR EXISTS (SELECT 1 FROM pg_class k
                                            WHERE k.oid = t.typrelid AND k.relkind = 'c'))
           AND NOT EXISTS (SELECT 1 FROM omcp_ext_closure x
                            WHERE x.classid = 'pg_type'::regclass AND x.objid = t.oid)
    LOOP
        obj_kw := CASE r.typtype WHEN 'd' THEN 'DOMAIN' ELSE 'TYPE' END;
        EXECUTE format('ALTER %s %s OWNER TO obsidian_mcp', obj_kw, r.oid::regtype);
    END LOOP;

    FOR r IN
        SELECT c.oid, n.nspname, c.collname FROM pg_collation c
          JOIN pg_namespace n ON n.oid = c.collnamespace
         WHERE c.collowner = 10 AND c.oid >= 16384
           AND NOT EXISTS (SELECT 1 FROM omcp_ext_closure x
                            WHERE x.classid = 'pg_collation'::regclass AND x.objid = c.oid)
    LOOP
        EXECUTE format('ALTER COLLATION %I.%I OWNER TO obsidian_mcp', r.nspname, r.collname);
    END LOOP;

    FOR r IN
        SELECT c.oid, n.nspname, c.conname FROM pg_conversion c
          JOIN pg_namespace n ON n.oid = c.connamespace
         WHERE c.conowner = 10 AND c.oid >= 16384
           AND NOT EXISTS (SELECT 1 FROM omcp_ext_closure x
                            WHERE x.classid = 'pg_conversion'::regclass AND x.objid = c.oid)
    LOOP
        EXECUTE format('ALTER CONVERSION %I.%I OWNER TO obsidian_mcp', r.nspname, r.conname);
    END LOOP;

    FOR r IN
        SELECT o.oid FROM pg_operator o
         WHERE o.oprowner = 10 AND o.oid >= 16384
           AND NOT EXISTS (SELECT 1 FROM omcp_ext_closure x
                            WHERE x.classid = 'pg_operator'::regclass AND x.objid = o.oid)
    LOOP
        EXECUTE format('ALTER OPERATOR %s OWNER TO obsidian_mcp', r.oid::regoperator);
    END LOOP;

    FOR r IN
        SELECT f.oid, n.nspname, f.opfname, a.amname FROM pg_opfamily f
          JOIN pg_namespace n ON n.oid = f.opfnamespace
          JOIN pg_am a ON a.oid = f.opfmethod
         WHERE f.opfowner = 10 AND f.oid >= 16384
           AND NOT EXISTS (SELECT 1 FROM omcp_ext_closure x
                            WHERE x.classid = 'pg_opfamily'::regclass AND x.objid = f.oid)
    LOOP
        EXECUTE format('ALTER OPERATOR FAMILY %I.%I USING %I OWNER TO obsidian_mcp',
                       r.nspname, r.opfname, r.amname);
    END LOOP;

    FOR r IN
        SELECT c.oid, n.nspname, c.opcname, a.amname FROM pg_opclass c
          JOIN pg_namespace n ON n.oid = c.opcnamespace
          JOIN pg_am a ON a.oid = c.opcmethod
         WHERE c.opcowner = 10 AND c.oid >= 16384
           AND NOT EXISTS (SELECT 1 FROM omcp_ext_closure x
                            WHERE x.classid = 'pg_opclass'::regclass AND x.objid = c.oid)
    LOOP
        EXECUTE format('ALTER OPERATOR CLASS %I.%I USING %I OWNER TO obsidian_mcp',
                       r.nspname, r.opcname, r.amname);
    END LOOP;

    FOR r IN
        SELECT c.oid FROM pg_ts_config c
         WHERE c.cfgowner = 10 AND c.oid >= 16384
           AND NOT EXISTS (SELECT 1 FROM omcp_ext_closure x
                            WHERE x.classid = 'pg_ts_config'::regclass AND x.objid = c.oid)
    LOOP
        EXECUTE format('ALTER TEXT SEARCH CONFIGURATION %s OWNER TO obsidian_mcp', r.oid::regconfig);
    END LOOP;

    FOR r IN
        SELECT d.oid FROM pg_ts_dict d
         WHERE d.dictowner = 10 AND d.oid >= 16384
           AND NOT EXISTS (SELECT 1 FROM omcp_ext_closure x
                            WHERE x.classid = 'pg_ts_dict'::regclass AND x.objid = d.oid)
    LOOP
        EXECUTE format('ALTER TEXT SEARCH DICTIONARY %s OWNER TO obsidian_mcp', r.oid::regdictionary);
    END LOOP;

    FOR r IN
        SELECT s.oid, n.nspname, s.stxname FROM pg_statistic_ext s
          JOIN pg_namespace n ON n.oid = s.stxnamespace
         WHERE s.stxowner = 10 AND s.oid >= 16384
    LOOP
        EXECUTE format('ALTER STATISTICS %I.%I OWNER TO obsidian_mcp', r.nspname, r.stxname);
    END LOOP;

    FOR r IN
        SELECT l.oid FROM pg_largeobject_metadata l WHERE l.lomowner = 10
    LOOP
        EXECUTE format('ALTER LARGE OBJECT %s OWNER TO obsidian_mcp', r.oid);
    END LOOP;

    -- Default privileges (pg_default_acl) the old role held in this database
    -- move to obsidian_mcp. A database-wide row is a full ACL that replaces
    -- the built-in default (acldefault), so the difference against it is
    -- replayed for obsidian_mcp and undone for postgres, which makes the
    -- server delete postgres's row. A per-schema row only ever adds grants.
    FOR r IN
        SELECT d.defaclnamespace, d.defaclobjtype, d.defaclacl, n.nspname
          FROM pg_default_acl d
          LEFT JOIN pg_namespace n ON n.oid = d.defaclnamespace
         WHERE d.defaclrole = 10
    LOOP
        obj_kw := CASE r.defaclobjtype
            WHEN 'r' THEN 'TABLES' WHEN 'S' THEN 'SEQUENCES' WHEN 'f' THEN 'FUNCTIONS'
            WHEN 'T' THEN 'TYPES' WHEN 'n' THEN 'SCHEMAS' END;
        acl_code := CASE r.defaclobjtype WHEN 'S' THEN 's' ELSE r.defaclobjtype END;
        old_acl := r.defaclacl;
        -- The same ACL with OID 10 replaced by obsidian_mcp, as grantee and grantor.
        SELECT array_agg(makeaclitem(
                   CASE e.grantee WHEN 10 THEN app ELSE e.grantee END,
                   CASE e.grantor WHEN 10 THEN app ELSE e.grantor END,
                   e.privilege_type, e.is_grantable))
          INTO new_acl
          FROM aclexplode(old_acl) e;

        IF r.defaclnamespace = 0 THEN
            -- For each of (role, wanted ACL, built-in default): grant what the
            -- wanted ACL has beyond the default and revoke what it lacks.
            FOR item IN
                WITH spec(role_name, wanted, base) AS (
                    VALUES ('obsidian_mcp', new_acl, acldefault(acl_code, app)),
                           ('postgres', acldefault(acl_code, 10::oid), old_acl)
                ),
                w AS (SELECT s.role_name, e.grantee, e.privilege_type, e.is_grantable
                        FROM spec s, aclexplode(s.wanted) e),
                b AS (SELECT s.role_name, e.grantee, e.privilege_type, e.is_grantable
                        FROM spec s, aclexplode(s.base) e)
                SELECT 'GRANT' AS verb, w.role_name, w.grantee, w.privilege_type, w.is_grantable
                  FROM w
                 WHERE NOT EXISTS (SELECT 1 FROM b WHERE b.role_name = w.role_name
                                     AND b.grantee = w.grantee AND b.privilege_type = w.privilege_type
                                     AND (b.is_grantable OR NOT w.is_grantable))
                UNION ALL
                SELECT 'REVOKE', b.role_name, b.grantee, b.privilege_type,
                       EXISTS (SELECT 1 FROM w WHERE w.role_name = b.role_name
                                 AND w.grantee = b.grantee AND w.privilege_type = b.privilege_type)
                  FROM b
                 WHERE NOT EXISTS (SELECT 1 FROM w WHERE w.role_name = b.role_name
                                     AND w.grantee = b.grantee AND w.privilege_type = b.privilege_type
                                     AND (w.is_grantable OR NOT b.is_grantable))
                 ORDER BY 1
            LOOP
                grantee_sql := CASE item.grantee WHEN 0 THEN 'PUBLIC'
                    ELSE quote_ident((SELECT rolname FROM pg_roles WHERE oid = item.grantee)) END;
                IF item.verb = 'GRANT' THEN
                    EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I GRANT %s ON %s TO %s%s',
                        item.role_name, item.privilege_type, obj_kw, grantee_sql,
                        CASE WHEN item.is_grantable THEN ' WITH GRANT OPTION' ELSE '' END);
                ELSE
                    -- is_grantable here means "the privilege stays, only its
                    -- grant option goes".
                    EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I REVOKE %s%s ON %s FROM %s',
                        item.role_name,
                        CASE WHEN item.is_grantable THEN 'GRANT OPTION FOR ' ELSE '' END,
                        item.privilege_type, obj_kw, grantee_sql);
                END IF;
            END LOOP;
        ELSE
            FOR item IN SELECT e.grantee, e.privilege_type, e.is_grantable FROM aclexplode(new_acl) e LOOP
                grantee_sql := CASE item.grantee WHEN 0 THEN 'PUBLIC'
                    ELSE quote_ident((SELECT rolname FROM pg_roles WHERE oid = item.grantee)) END;
                EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE obsidian_mcp IN SCHEMA %I GRANT %s ON %s TO %s%s',
                    r.nspname, item.privilege_type, obj_kw, grantee_sql,
                    CASE WHEN item.is_grantable THEN ' WITH GRANT OPTION' ELSE '' END);
            END LOOP;
            FOR item IN SELECT e.grantee, e.privilege_type FROM aclexplode(old_acl) e LOOP
                grantee_sql := CASE item.grantee WHEN 0 THEN 'PUBLIC'
                    ELSE quote_ident((SELECT rolname FROM pg_roles WHERE oid = item.grantee)) END;
                EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA %I REVOKE %s ON %s FROM %s',
                    r.nspname, item.privilege_type, obj_kw, grantee_sql);
            END LOOP;
        END IF;
    END LOOP;
END
$convert$;
\endif

-- Self-check: the fresh-install shape. On the conversion path any failure
-- raises and the whole transaction rolls back; on the already-split path it
-- refuses a cluster that is only partly in that shape (database owned by
-- postgres, a privileged obsidian_mcp or one that is a member of a privileged
-- role, an extension not owned by postgres, objects or default privileges
-- left with the superuser), naming every problem it found.
DO $check$
DECLARE
    problems text[] := '{}';
    attrs text;
    found text;
BEGIN
    SELECT concat_ws(', ',
               CASE WHEN rolsuper THEN 'SUPERUSER' END,
               CASE WHEN rolcreatedb THEN 'CREATEDB' END,
               CASE WHEN rolcreaterole THEN 'CREATEROLE' END,
               CASE WHEN rolreplication THEN 'REPLICATION' END,
               CASE WHEN rolbypassrls THEN 'BYPASSRLS' END,
               CASE WHEN NOT rolcanlogin THEN 'NOLOGIN' END)
      INTO attrs
      FROM pg_roles WHERE rolname = 'obsidian_mcp';
    IF attrs IS NULL THEN
        problems := problems || 'role obsidian_mcp does not exist'::text;
    ELSIF attrs <> '' THEN
        problems := problems || format('role obsidian_mcp has %s (expected a plain LOGIN role)', attrs);
    END IF;

    -- A plain role that can SET ROLE to a privileged one is a privileged role
    -- (Codex review round 2). pg_has_role(..., 'MEMBER') follows direct and
    -- nested grants whatever their INHERIT/SET options.
    IF attrs IS NOT NULL THEN
        SELECT string_agg(format('%s (%s)', quote_ident(r.rolname), concat_ws(', ',
                   CASE WHEN r.rolsuper THEN 'SUPERUSER' END,
                   CASE WHEN r.rolcreatedb THEN 'CREATEDB' END,
                   CASE WHEN r.rolcreaterole THEN 'CREATEROLE' END,
                   CASE WHEN r.rolreplication THEN 'REPLICATION' END,
                   CASE WHEN r.rolbypassrls THEN 'BYPASSRLS' END)), ', ' ORDER BY r.rolname)
          INTO found
          FROM pg_roles r
         WHERE r.rolname <> 'obsidian_mcp'
           AND (r.rolsuper OR r.rolcreatedb OR r.rolcreaterole OR r.rolreplication OR r.rolbypassrls)
           AND pg_has_role('obsidian_mcp', r.oid, 'MEMBER');
        IF found IS NOT NULL THEN
            problems := problems || format('role obsidian_mcp is a member (directly or through other roles) of privileged role(s) %s', found);
        END IF;
    END IF;

    -- Extensions belong to the bootstrap superuser, as on a fresh install
    -- (plpgsql from initdb, vector from the init script).
    SELECT string_agg(format('extension %s is owned by %s, not postgres',
                             quote_ident(e.extname), pg_get_userbyid(e.extowner)), '; ' ORDER BY e.extname)
      INTO found
      FROM pg_extension e
     WHERE e.extowner <> 10;
    IF found IS NOT NULL THEN
        problems := problems || found;
    END IF;

    found := (SELECT rolname FROM pg_roles WHERE oid = 10);
    IF found <> 'postgres' THEN
        problems := problems || format('the bootstrap superuser (OID 10) is named %s, not postgres', found);
    END IF;

    found := (SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = current_database());
    IF found <> 'obsidian_mcp' THEN
        problems := problems || format('database obsidian_mcp is owned by %s, not obsidian_mcp', found);
    END IF;

    WITH owned(catalog, oid, owner) AS (
        SELECT 'pg_namespace'::regclass, oid, nspowner FROM pg_namespace
         WHERE nspname !~ '^pg_' AND (oid >= 16384 OR nspname = 'public')
        UNION ALL SELECT 'pg_class'::regclass, c.oid, c.relowner FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname !~ '^pg_'
        UNION ALL SELECT 'pg_proc'::regclass, oid, proowner FROM pg_proc
        UNION ALL SELECT 'pg_type'::regclass, t.oid, t.typowner FROM pg_type t
          JOIN pg_namespace n ON n.oid = t.typnamespace WHERE n.nspname !~ '^pg_'
        UNION ALL SELECT 'pg_collation'::regclass, oid, collowner FROM pg_collation
        UNION ALL SELECT 'pg_conversion'::regclass, oid, conowner FROM pg_conversion
        UNION ALL SELECT 'pg_operator'::regclass, oid, oprowner FROM pg_operator
        UNION ALL SELECT 'pg_opclass'::regclass, oid, opcowner FROM pg_opclass
        UNION ALL SELECT 'pg_opfamily'::regclass, oid, opfowner FROM pg_opfamily
        UNION ALL SELECT 'pg_ts_config'::regclass, oid, cfgowner FROM pg_ts_config
        UNION ALL SELECT 'pg_ts_dict'::regclass, oid, dictowner FROM pg_ts_dict
        UNION ALL SELECT 'pg_statistic_ext'::regclass, oid, stxowner FROM pg_statistic_ext
        UNION ALL SELECT 'pg_largeobject_metadata'::regclass, oid, lomowner FROM pg_largeobject_metadata
        UNION ALL SELECT 'pg_language'::regclass, oid, lanowner FROM pg_language
        UNION ALL SELECT 'pg_foreign_data_wrapper'::regclass, oid, fdwowner FROM pg_foreign_data_wrapper
        UNION ALL SELECT 'pg_foreign_server'::regclass, oid, srvowner FROM pg_foreign_server
        UNION ALL SELECT 'pg_event_trigger'::regclass, oid, evtowner FROM pg_event_trigger
        UNION ALL SELECT 'pg_publication'::regclass, oid, pubowner FROM pg_publication
    )
    SELECT string_agg(pg_describe_object(o.catalog, o.oid, 0), ', ' ORDER BY o.catalog::text, o.oid)
      INTO found
      FROM owned o
     WHERE o.owner = 10
       AND (o.oid >= 16384 OR (o.catalog = 'pg_namespace'::regclass))
       AND NOT EXISTS (SELECT 1 FROM omcp_ext_closure x
                        WHERE x.classid = o.catalog AND x.objid = o.oid);
    IF found IS NOT NULL THEN
        problems := problems || format('objects still owned by the superuser: %s', found);
    END IF;

    SELECT string_agg(format('%s %s',
               CASE d.defaclobjtype WHEN 'r' THEN 'TABLES' WHEN 'S' THEN 'SEQUENCES'
                    WHEN 'f' THEN 'FUNCTIONS' WHEN 'T' THEN 'TYPES' WHEN 'n' THEN 'SCHEMAS'
                    ELSE d.defaclobjtype::text END,
               CASE WHEN d.defaclnamespace = 0 THEN 'database-wide'
                    ELSE 'in schema ' || quote_ident(n.nspname) END), ', ' ORDER BY d.oid)
      INTO found
      FROM pg_default_acl d LEFT JOIN pg_namespace n ON n.oid = d.defaclnamespace
     WHERE d.defaclrole = 10;
    IF found IS NOT NULL THEN
        problems := problems || format('default privileges still held by the superuser: %s', found);
    END IF;

    IF cardinality(problems) > 0 THEN
        IF current_setting('omcp.mode') = 'check' THEN
            RAISE EXCEPTION 'upgrade-split-db-roles: the cluster looks split (bootstrap superuser postgres, obsidian_mcp not a superuser) but is not in the fresh-install shape: %. Nothing was converted. Fix these as postgres and run the script again; see DEPLOYMENT.md, "Upgrading: split database roles".',
                array_to_string(problems, '; ');
        ELSE
            RAISE EXCEPTION 'upgrade-split-db-roles: self-check failed: %; nothing was changed',
                array_to_string(problems, '; ');
        END IF;
    END IF;
END
$check$;

COMMIT;

\if :already_split
\echo 'upgrade-split-db-roles: already split (bootstrap superuser is postgres; obsidian_mcp is a plain login role that owns database obsidian_mcp and everything in it); nothing to do.'
\quit
\endif

-- ── 3. Disconnect the old superuser identity ───────────────────────────────

-- A session that authenticated as the old obsidian_mcp before the COMMIT is
-- still OID 10, and OID 10 is the superuser now named postgres: it would keep
-- superuser rights for as long as it stays connected. No legitimate OID-10
-- session can exist yet (its password was set by this transaction), so every
-- other OID-10 client backend is terminated, waiting up to 5 s for each.
DO $disconnect$
DECLARE
    r record;
BEGIN
    FOR r IN SELECT pid FROM pg_stat_activity
              WHERE usesysid = 10 AND backend_type = 'client backend'
                AND pid <> pg_backend_pid()
    LOOP
        PERFORM pg_terminate_backend(r.pid, 5000);
    END LOOP;
END
$disconnect$;

-- ── 4. Remove the temporary superuser, then verify ─────────────────────────

\c obsidian_mcp postgres
\set QUIET on
SET client_min_messages = warning;
DROP ROLE obsidian_mcp_split_tmp;

DO $verify$
DECLARE
    left_over int;
BEGIN
    SELECT count(*) INTO left_over FROM pg_stat_activity
     WHERE usesysid = 10 AND backend_type = 'client backend'
       AND pid <> pg_backend_pid();
    IF left_over > 0 THEN
        RAISE EXCEPTION 'upgrade-split-db-roles: the conversion is committed, but % session(s) of the old superuser identity are still connected and keep superuser rights. Restart the database before starting the app: docker compose -f <bundle> restart postgres', left_over;
    END IF;
END
$verify$;

\echo 'upgrade-split-db-roles: done. The bootstrap superuser is now postgres; obsidian_mcp is a non-superuser that owns database obsidian_mcp. Restart the database, then start the app: docker compose -f <bundle> restart postgres && docker compose -f <bundle> up -d'
