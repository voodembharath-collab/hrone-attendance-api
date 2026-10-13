# REVIEW.md

## Defects Found in the Starter Code

### 1. Employee Pagination
- **Where:** `list_employees`
- **Problem:** The pagination offset may skip the first page, and the total count may not apply the department filter.
- **How to notice:** Request page 1 and compare the results with the first employee records. Apply a department filter and check the total.
- **Fix:** Use `(page - 1) * page_size` for the offset and apply the same filters when counting records.

### 2. Employee Validation During Punch-In
- **Where:** `punch_in`
- **Problem:** The employee must be validated before accessing employee-specific shift details.
- **How to notice:** Send a punch-in request for an unknown employee code.
- **Fix:** Validate the employee and return an appropriate HTTP error when the employee does not exist.

### 3. Concurrent Punch-In Requests
- **Where:** `punch_in`
- **Problem:** An existence check alone cannot prevent two simultaneous requests from creating duplicate attendance records.
- **How to notice:** Send two punch-in requests for the same employee and date concurrently.
- **Fix:** Use a unique `(emp_code, date)` index and handle duplicate-key errors with HTTP 409.

### 4. Timestamp and Attendance-Date Handling
- **Where:** `punch_in` and timestamp helper functions
- **Problem:** Timestamp handling must follow the API contract's epoch-millisecond and IST attendance-date rules.
- **How to notice:** Test timestamps near midnight and invalid timestamp units.
- **Fix:** Validate timestamps and handle time zones and attendance dates consistently.

### 5. Late-Minute Calculation
- **Where:** `compute_late_minutes`
- **Problem:** The calculation must follow the shift boundaries, 10-minute grace period, and whole-minute flooring rules.
- **How to notice:** Test punch-ins exactly 10 minutes and 10 minutes plus one second after shift start.
- **Fix:** Apply the contract's grace-period and minute-calculation rules.

### 6. Work-Hour Rounding
- **Where:** `compute_work_hours`
- **Problem:** Work hours must use consistent time handling and half-up rounding to two decimal places.
- **How to notice:** Test durations near a two-decimal rounding boundary.
- **Fix:** Use consistent time calculations and explicit decimal half-up rounding.

### 7. Overtime Calculation
- **Where:** `compute_overtime`
- **Problem:** Overtime must handle overnight shifts and apply the 30-minute minimum.
- **How to notice:** Test punch-outs 29 minutes and 30 minutes after shift end.
- **Fix:** Calculate the correct shift end and enforce the threshold.

### 8. Attendance Query Efficiency
- **Where:** `list_attendance`
- **Problem:** Filtering, sorting, and pagination should be handled efficiently in MongoDB.
- **How to notice:** Test with a large attendance collection and compare response time.
- **Fix:** Use database queries for filtering, sorting, pagination, and counting.

### 9. API Contract Coverage
- **Where:** API endpoint implementation
- **Problem:** All endpoints required by the API contract must be implemented.
- **How to notice:** Compare the routes in `openapi.yaml` with the application's exposed routes.
- **Fix:** Implement the required attendance, analytics, and explain endpoints.

## Items Reviewed but Not Considered Defects

- Default page number 1 and page size 20 are consistent with the API contract.
- MongoDB is appropriate for employee and attendance storage in this assignment.
- HTTP 409 is appropriate when a duplicate attendance record is rejected.
