-- roles for one application database, run by the bootstrap superuser against that database.
-- idempotent: converges role attributes, passwords, memberships, grants and ownership, so it also takes over a volume whose tables the bootstrap superuser created
--
-- env: DB_MIGRATOR_ROLE DB_RUNTIME_ROLE DB_MIGRATOR_PASSWORD DB_RUNTIME_PASSWORD, plus the libpq PGHOST PGDATABASE PGUSER PGPASSWORD of the bootstrap superuser. DB_PRODUCTION=1 holds the bootstrap password to the production rules as well
\set ON_ERROR_STOP on
\getenv migrator DB_MIGRATOR_ROLE
\getenv runtime DB_RUNTIME_ROLE
\getenv migrator_password DB_MIGRATOR_PASSWORD
\getenv runtime_password DB_RUNTIME_PASSWORD
\getenv production DB_PRODUCTION
\getenv bootstrap_password PGPASSWORD

SELECT :{?migrator} AND :{?runtime} AND :{?migrator_password} AND :{?runtime_password} AS vars_set,
       (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) AS is_superuser
\gset
\if :vars_set
\else
DO $$ BEGIN RAISE EXCEPTION 'DB_MIGRATOR_ROLE, DB_RUNTIME_ROLE, DB_MIGRATOR_PASSWORD and DB_RUNTIME_PASSWORD must all be set'; END $$;
\endif
\if :is_superuser
\else
DO $$ BEGIN RAISE EXCEPTION 'run as the bootstrap superuser'; END $$;
\endif

-- a failed statement is logged with its text, and the statements below carry passwords
SET log_min_error_statement = panic;
SET log_statement = none;
-- taking ownership locks a relation exclusively and a release runs while the previous one still serves traffic: wait briefly, then fail the release instead of queueing behind a long query and stalling the service
SET lock_timeout = '5s';
SET statement_timeout = '60s';

SELECT :'migrator' ~ '^[a-z_][a-z0-9_]*$' AND :'runtime' ~ '^[a-z_][a-z0-9_]*$'
         AND :'migrator' <> :'runtime' AND current_user NOT IN (:'migrator', :'runtime') AS roles_ok,
       :'migrator_password' ~ '^[A-Za-z0-9_-]{16,}$' AND :'runtime_password' ~ '^[A-Za-z0-9_-]{16,}$'
         AND :'migrator_password' <> :'runtime_password'
         AND position('change-me' in :'migrator_password' || :'runtime_password') = 0 AS passwords_ok
\gset
\if :roles_ok
\else
DO $$ BEGIN RAISE EXCEPTION 'the migrator and runtime roles must be two distinct lowercase names, neither of them the bootstrap user'; END $$;
\endif
\if :passwords_ok
\else
DO $$ BEGIN RAISE EXCEPTION 'the two passwords must differ, be 16 or more characters of [A-Za-z0-9_-], they go into a connection url, and not be placeholders'; END $$;
\endif

-- production also refuses a weak or placeholder bootstrap password and dev passwords for the roles, the bootstrap one is rotated first (DEPLOY.md)
\set production_mode false
\if :{?production}
SELECT :'production' = '1' AS production_mode
\gset
\endif
\if :production_mode
\if :{?bootstrap_password}
\else
DO $$ BEGIN RAISE EXCEPTION 'production provisioning authenticates the bootstrap superuser by PGPASSWORD'; END $$;
\endif
SELECT length(:'bootstrap_password') >= 16 AND position('change-me' in :'bootstrap_password') = 0
         AND :'bootstrap_password' !~ '^(dev-|ops-dev)'
         AND :'migrator_password' !~ '^(dev-|ops-dev)' AND :'runtime_password' !~ '^(dev-|ops-dev)' AS production_passwords_ok
\gset
\if :production_passwords_ok
\else
DO $$ BEGIN RAISE EXCEPTION 'production needs a bootstrap password of 16 or more characters and no placeholder or dev password, rotate the bootstrap password first (DEPLOY.md)'; END $$;
\endif
\endif

-- a process of an older release still connected as the bootstrap superuser keeps that access, and nothing here can revoke it. a health probe connects only for a moment
SELECT count(*) AS bootstrap_sessions, coalesce(string_agg(DISTINCT coalesce(host(client_addr), 'local socket'), ', '), '') AS bootstrap_clients
FROM pg_stat_activity
WHERE usename = current_user AND backend_type = 'client backend' AND pid <> pg_backend_pid()
  AND backend_start < now() - interval '2 seconds'
\gset
SELECT :bootstrap_sessions = 0 AS no_bootstrap_sessions
\gset
\if :no_bootstrap_sessions
\else
\warn :bootstrap_sessions open sessions of the bootstrap superuser, from :bootstrap_clients
DO $$ BEGIN RAISE EXCEPTION 'stop every process that still connects as the bootstrap superuser before provisioning (DEPLOY.md)'; END $$;
\endif

SELECT format('CREATE ROLE %I', r) FROM unnest(ARRAY[:'migrator', :'runtime']) AS r
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = r)
\gexec
ALTER ROLE :"migrator" WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD :'migrator_password';
ALTER ROLE :"runtime" WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD :'runtime_password';
-- membership in any other role hands over its rights, the migrator's ownership included
SELECT format('REVOKE %I FROM %I GRANTED BY %I', g.rolname, m.rolname, gr.rolname)
FROM pg_auth_members a
JOIN pg_roles g ON g.oid = a.roleid JOIN pg_roles m ON m.oid = a.member JOIN pg_roles gr ON gr.oid = a.grantor
WHERE m.rolname IN (:'migrator', :'runtime')
\gexec

SELECT current_database() AS dbname
\gset
REVOKE ALL ON DATABASE :"dbname" FROM PUBLIC, :"migrator", :"runtime";
GRANT CONNECT ON DATABASE :"dbname" TO :"migrator", :"runtime";
-- the maintenance and template databases admit every role by default
SELECT format('REVOKE CONNECT, TEMPORARY ON DATABASE %I FROM PUBLIC', datname)
FROM pg_database WHERE datname IN ('postgres', 'template1') AND datname <> current_database()
\gexec

REVOKE ALL ON SCHEMA public FROM PUBLIC, :"runtime";
GRANT USAGE ON SCHEMA public TO :"runtime";
GRANT USAGE, CREATE ON SCHEMA public TO :"migrator";

-- the migrator alters what migrations created, so it owns every relation; sequences owned by a column follow their table
SELECT format('ALTER %s %I.%I OWNER TO %I',
              CASE c.relkind WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW'
                             WHEN 'S' THEN 'SEQUENCE' WHEN 'f' THEN 'FOREIGN TABLE' ELSE 'TABLE' END,
              n.nspname, c.relname, :'migrator')
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
  AND c.relowner <> :'migrator'::regrole
  AND NOT EXISTS (SELECT FROM pg_depend d
                  WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype IN ('e', 'a', 'i'))
\gexec

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO :"runtime";
REVOKE TRUNCATE, REFERENCES, TRIGGER ON ALL TABLES IN SCHEMA public FROM :"runtime";
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO :"runtime";
ALTER DEFAULT PRIVILEGES FOR ROLE :"migrator" IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"runtime";
ALTER DEFAULT PRIVILEGES FOR ROLE :"migrator" IN SCHEMA public REVOKE TRUNCATE, REFERENCES, TRIGGER ON TABLES FROM :"runtime";
ALTER DEFAULT PRIVILEGES FOR ROLE :"migrator" IN SCHEMA public GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO :"runtime";

\echo database roles converged for :dbname
