\echo == busiest statements by total time
SELECT calls, round(total_exec_time::numeric) AS total_ms, round(mean_exec_time::numeric, 2) AS mean_ms, round(max_exec_time::numeric) AS max_ms, left(regexp_replace(query, '\s+', ' ', 'g'), 110) AS query FROM pg_stat_statements WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database()) AND query NOT ILIKE '%pg_stat_statements%' ORDER BY total_exec_time DESC LIMIT 12;
\echo == the sold-seat lookup behind every hold request
SELECT calls, round(total_exec_time::numeric) AS total_ms, round(mean_exec_time::numeric, 3) AS mean_ms, round(max_exec_time::numeric, 1) AS max_ms, left(regexp_replace(query, '\s+', ' ', 'g'), 110) AS query FROM pg_stat_statements WHERE query ILIKE 'SELECT%' AND query ILIKE '%bookings_bookingseat%' AND query ILIKE '%active%' AND query NOT ILIKE '%pg_stat_statements%' ORDER BY calls DESC LIMIT 3;
\echo == connections right now
SELECT state, count(*) AS connections FROM pg_stat_activity WHERE datname = current_database() GROUP BY state ORDER BY count(*) DESC;
\echo == cumulative deadlocks and temp files since PostgreSQL started
SELECT deadlocks, temp_files FROM pg_stat_database WHERE datname = current_database();
