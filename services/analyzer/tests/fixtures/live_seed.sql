-- Seed for tests/test_live_targets.py
--
-- The application user is deliberately granted FULL WRITE ACCESS. That is the
-- point of these tests: it means the only thing preventing a write is the
-- service's own enforcement, not the database role. A read-only role is what
-- the README tells operators to use in production, but a test relying on one
-- would prove nothing about this code.
--
-- PostgreSQL:
--   docker run -d --name fae-pg -e POSTGRES_PASSWORD=rootpw -e POSTGRES_DB=fraud \
--       -p 55432:5432 postgres:16-alpine
--   PGPASSWORD=rootpw psql -h 127.0.0.1 -p 55432 -U postgres -d fraud \
--       -f tests/fixtures/live_seed.sql
--
-- MySQL (skip the postgres-only lines; see the MySQL block at the bottom):
--   docker run -d --name fae-my -e MYSQL_ROOT_PASSWORD=rootpw \
--       -e MYSQL_DATABASE=fraud -p 53306:3306 mysql:8

-- ============================== PostgreSQL ==============================
CREATE TABLE payments (
  id serial PRIMARY KEY, day date, merchant text, amount numeric(12,2),
  chargeback boolean DEFAULT false, comment text,
  created_at timestamptz DEFAULT now()
);
INSERT INTO payments (day, merchant, amount, chargeback, comment) VALUES
 ('2026-08-19','acme',120.00,true,'card testing'),
 ('2026-08-19','acme',80.00,false,NULL),
 ('2026-08-20','globex',4300.00,true,'velocity'),
 ('2026-08-21','initech',990.00,true,'geo mismatch');
CREATE VIEW flagged AS SELECT * FROM payments WHERE chargeback;

CREATE ROLE app_rw LOGIN PASSWORD 'apppw';
GRANT ALL ON SCHEMA public TO app_rw;
GRANT ALL ON ALL TABLES IN SCHEMA public TO app_rw;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO app_rw;

-- ================================ MySQL =================================
-- docker exec -i fae-my mysql -uroot -prootpw fraud <<'EOF'
-- CREATE TABLE payments (
--   id INT AUTO_INCREMENT PRIMARY KEY, day DATE, merchant VARCHAR(64),
--   amount DECIMAL(12,2), chargeback TINYINT(1) DEFAULT 0, comment TEXT,
--   created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
-- );
-- INSERT INTO payments (day, merchant, amount, chargeback, comment) VALUES
--  ('2026-08-19','acme',120.00,1,'card testing'),
--  ('2026-08-19','acme',80.00,0,NULL),
--  ('2026-08-20','globex',4300.00,1,'velocity'),
--  ('2026-08-21','initech',990.00,1,'geo mismatch');
-- CREATE VIEW flagged AS SELECT * FROM payments WHERE chargeback = 1;
-- CREATE USER 'app_rw'@'%' IDENTIFIED BY 'apppw';
-- GRANT ALL PRIVILEGES ON fraud.* TO 'app_rw'@'%';
-- FLUSH PRIVILEGES;
-- EOF
