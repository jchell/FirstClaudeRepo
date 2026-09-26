-- Runs once, when the data volume is first initialized (as the bootstrap superuser).
-- Platform services never log in as a fixed user: Vault issues short-lived logins
-- that are members of dataplat_owner and act as it, so every object they create is
-- owned by this group role.
CREATE ROLE dataplat_owner NOLOGIN;
ALTER DATABASE dataplat OWNER TO dataplat_owner;
ALTER SCHEMA public OWNER TO dataplat_owner;
REVOKE ALL ON DATABASE dataplat FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE dataplat TO dataplat_owner;

-- Serving database for gold replicas (direct SQL/BI access, Phase 4).
CREATE DATABASE serving OWNER dataplat_owner;
