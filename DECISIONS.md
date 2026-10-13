# DECISIONS.md

1. **Indexes.** The unique employee-code index prevents duplicate employees. The department and joining-date index supports department headcount queries. The unique `(emp_code, date)` attendance index prevents duplicate daily records. The date/employee and status/date/employee indexes support attendance queries. I would avoid adding indexes without checking real query plans because indexes consume storage and slow writes.

2. **Punch-in race.** Two identical punch-in requests can both check for an existing record before either inserts. The unique `(emp_code, date)` index prevents duplicates: one insert succeeds and the other receives a duplicate-key error, which the API converts to HTTP 409.

3. **Ties.** The leaderboard sorts by total late minutes in descending order, then employee code. Employees with equal late minutes receive the same competition rank. The limit applies to rank, so all employees tied at the cutoff rank are included.

4. **Headcount.** The department summary counts employees who joined by the end of the period, including employees without attendance logs.

5. **Scaling.** For 100 times more data, I would measure query performance, review execution plans, and consider pre-aggregated daily summaries for expensive repeated analytics.
