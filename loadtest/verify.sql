\set ON_ERROR_STOP on
\echo == seats sold twice (must be 0 rows)
SELECT run_id, seat_label, count(*) AS row_count FROM bookings_bookingseat WHERE active AND run_id IN (__RUNS__) GROUP BY run_id, seat_label HAVING count(*) > 1;
\echo == bookings by status
SELECT status, count(*) AS bookings, coalesce(sum(total_pkr), 0) AS total_pkr FROM bookings_booking WHERE run_id IN (__RUNS__) GROUP BY status ORDER BY status;
\echo == payments by status
SELECT status, count(*) AS payments FROM bookings_payment WHERE booking_id IN (SELECT id FROM bookings_booking WHERE run_id IN (__RUNS__)) GROUP BY status ORDER BY status;
\echo == active seats per run
SELECT run_id, count(*) AS active_seats FROM bookings_bookingseat WHERE active AND run_id IN (__RUNS__) GROUP BY run_id ORDER BY run_id;
\echo == confirmed bookings without a succeeded payment (must be 0)
SELECT count(*) AS n FROM bookings_booking b WHERE b.run_id IN (__RUNS__) AND b.status = 'confirmed' AND NOT EXISTS (SELECT 1 FROM bookings_payment p WHERE p.booking_id = b.id AND p.status = 'succeeded');
\echo == confirmed bookings whose paid amount differs (must be 0)
SELECT count(*) AS n FROM bookings_booking b JOIN bookings_payment p ON p.booking_id = b.id AND p.status = 'succeeded' WHERE b.run_id IN (__RUNS__) AND b.status = 'confirmed' AND b.total_pkr <> p.amount_pkr;
\echo == active seat rows of bookings that are not live (must be 0)
SELECT count(*) AS n FROM bookings_bookingseat s JOIN bookings_booking b ON b.id = s.booking_id WHERE s.run_id IN (__RUNS__) AND s.active AND b.status NOT IN ('pending_payment', 'confirmed');
\echo == live bookings with an inactive seat (must be 0)
SELECT count(*) AS n FROM bookings_booking b WHERE b.run_id IN (__RUNS__) AND b.status IN ('pending_payment', 'confirmed') AND EXISTS (SELECT 1 FROM bookings_bookingseat s WHERE s.booking_id = b.id AND NOT s.active);
