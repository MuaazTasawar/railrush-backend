

## Phase 7 note: sold-seat lookups

Every hold request now reads the sold and reserved seats of the run from PostgreSQL (one query on
`bookingseat_run_active_idx`). That is acceptable at the current rates (30 holds per minute per user, admission
paced by the waiting room), but it is the first thing to measure in the Phase 8 load test. If it shows up, keep a
Redis set of sold seats per run, update it after the booking commits, and rebuild it from the database when it is missing.
