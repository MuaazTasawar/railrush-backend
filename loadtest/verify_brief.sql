\set ON_ERROR_STOP on
SELECT
 (SELECT count(*) FROM (SELECT 1 FROM bookings_bookingseat WHERE active AND run_id IN (__RUNS__) GROUP BY run_id, seat_label HAVING count(*) > 1) d) AS seats_sold_twice,
 (SELECT count(*) FROM bookings_booking b WHERE b.run_id IN (__RUNS__) AND b.status = 'confirmed' AND NOT EXISTS (SELECT 1 FROM bookings_payment p WHERE p.booking_id = b.id AND p.status = 'succeeded')) AS confirmed_without_payment,
 (SELECT count(*) FROM bookings_booking b JOIN bookings_payment p ON p.booking_id = b.id AND p.status = 'succeeded' WHERE b.run_id IN (__RUNS__) AND b.status = 'confirmed' AND b.total_pkr <> p.amount_pkr) AS amount_mismatch,
 (SELECT count(*) FROM bookings_bookingseat s JOIN bookings_booking b ON b.id = s.booking_id WHERE s.run_id IN (__RUNS__) AND s.active AND b.status NOT IN ('pending_payment', 'confirmed')) AS active_seats_of_dead_bookings,
 (SELECT count(*) FROM bookings_booking b WHERE b.run_id IN (__RUNS__) AND b.status IN ('pending_payment', 'confirmed') AND EXISTS (SELECT 1 FROM bookings_bookingseat s WHERE s.booking_id = b.id AND NOT s.active)) AS live_bookings_with_inactive_seat,
 (SELECT count(*) FROM bookings_booking WHERE run_id IN (__RUNS__)) AS bookings;
