
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError
from pymongo.write_concern import WriteConcern

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "hrone_attendance")

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

PRESENT_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = PRESENT_STATUSES | {"ABSENT", "LEAVE"}


def now_utc():
    return datetime.now(UTC).replace(microsecond=0)


def truncate_seconds(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)


def epoch_ms_to_datetime(value: int) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int):
        raise HTTPException(422, "Timestamp must be an integer in epoch milliseconds")
    if not 100000000000 <= value <= 4102444800000:
        raise HTTPException(422, "Timestamp is outside the permitted range")
    return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)


def datetime_to_epoch_ms(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp()) * 1000


def as_utc(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def ist_datetime(value):
    return as_utc(value).astimezone(IST)


def money_round(value, places=2):
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        raise HTTPException(422, "Date must use YYYY-MM-DD format")


def parse_month(value: str) -> Tuple[date, date]:
    if not re.fullmatch(r"\d{4}-\d{2}", value or ""):
        raise HTTPException(422, "Month must use YYYY-MM format")
    try:
        first = date.fromisoformat(value + "-01")
    except ValueError:
        raise HTTPException(422, "Month must use YYYY-MM format")
    if first.month == 12:
        next_month = date(first.year + 1, 1, 1)
    else:
        next_month = date(first.year, first.month + 1, 1)
    return first, next_month - timedelta(days=1)


def ist_clock(value: str) -> time:
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value or ""):
        raise HTTPException(422, "Time must use HH:MM format")
    h, m = map(int, value.split(":"))
    return time(h, m)


def shift_bounds(attendance_date: str, shift_start: str, shift_end: str):
    day = date.fromisoformat(attendance_date)
    start_clock = ist_clock(shift_start)
    end_clock = ist_clock(shift_end)
    start = datetime.combine(day, start_clock, IST)
    end_day = day + timedelta(days=1) if end_clock <= start_clock else day
    end = datetime.combine(end_day, end_clock, IST)
    return start, end


def attendance_date_for(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local = ist_datetime(punch_in)
    start_clock = ist_clock(shift_start)
    end_clock = ist_clock(shift_end)
    day = local.date()

    if end_clock <= start_clock and local.time().replace(tzinfo=None) < end_clock:
        day -= timedelta(days=1)

    return day.isoformat()


def compute_late_minutes(punch_in, attendance_date, shift_start, shift_end):
    if punch_in is None:
        return 0
    start, _ = shift_bounds(attendance_date, shift_start, shift_end)
    local_in = ist_datetime(punch_in)
    difference = (local_in - start).total_seconds()
    return int(difference // 60) if difference > 600 else 0


def compute_overtime(punch_out, attendance_date, shift_start, shift_end):
    if punch_out is None:
        return 0
    _, end = shift_bounds(attendance_date, shift_start, shift_end)
    difference = (ist_datetime(punch_out) - end).total_seconds()
    minutes = int(difference // 60)
    return minutes if minutes >= 30 else 0


def compute_work_hours(punch_in, punch_out):
    if punch_in is None or punch_out is None:
        return None
    seconds = (as_utc(punch_out) - as_utc(punch_in)).total_seconds()
    return money_round(seconds / 3600, 2)


def derived_fields(record, employee):
    punch_in = record.get("punch_in")
    punch_out = record.get("punch_out")
    hours = compute_work_hours(punch_in, punch_out)

    record["work_hours"] = hours
    record["late_minutes"] = compute_late_minutes(
        punch_in, record["date"], employee["shift_start"], employee["shift_end"]
    )
    record["overtime_minutes"] = compute_overtime(
        punch_out, record["date"], employee["shift_start"], employee["shift_end"]
    )
    record["half_day"] = hours is not None and hours < 4.50
    return record


def serialize_employee(doc):
    result = {k: v for k, v in doc.items() if k != "_id"}
    if "created_at" in result:
        result["created_at"] = datetime_to_epoch_ms(result["created_at"])
    return result


def serialize_attendance(doc):
    result = {k: v for k, v in doc.items() if k != "_id"}
    for field in ("punch_in", "punch_out"):
        if field in result:
            result[field] = datetime_to_epoch_ms(result[field])
    for item in result.get("history", []):
        if "at" in item:
            item["at"] = datetime_to_epoch_ms(item["at"])
        for field in ("punch_in", "punch_out"):
            if field in item.get("changes", {}):
                item["changes"][field] = datetime_to_epoch_ms(
                    item["changes"][field]
                )
    return result


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.command("ping")
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)]
    )
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)]
    )
    yield
    client.close()


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: EmailStr
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_time(cls, value):
        ist_clock(value)
        return value

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value):
        try:
            parsed = date.fromisoformat(value)
            if parsed.isoformat() != value:
                raise ValueError
        except (ValueError, TypeError):
            raise ValueError("Date must use YYYY-MM-DD format")
        return value

    @field_validator("shift_end")
    @classmethod
    def validate_shift_difference(cls, value, info):
        start = info.data.get("shift_start")
        if start == value:
            raise ValueError("shift_start and shift_end must differ")
        return value
@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "MongoDB is unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["email"] = str(body.email)
    doc["created_at"] = now_utc()

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")

    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {}
    if department:
        query["department"] = department

    total = db.employees.count_documents(query)
    cursor = (
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    return {
        "items": [serialize_employee(doc) for doc in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }

class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, value):
        if not re.fullmatch(r"EMP\d{4,6}", value):
            raise ValueError("emp_code must look like EMP0001")
        return value

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, value):
        if value is not None:
            epoch_ms_to_datetime(value)
        return value

    @field_validator("status")
    @classmethod
    def validate_status(cls, value):
        if value not in PRESENT_STATUSES:
            raise ValueError("status must be PRESENT, WFH, or ON_DUTY")
        return value


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, value):
        if not re.fullmatch(r"EMP\d{4,6}", value):
            raise ValueError("emp_code must look like EMP0001")
        return value

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, value):
        if value is not None:
            epoch_ms_to_datetime(value)
        return value


def find_employee(emp_code: str):
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    return employee



def attendance_document(employee, punched_at: datetime, status: str):
    punched_at = truncate_seconds(punched_at)

    attendance_date = attendance_date_for(
        punched_at,
        employee["shift_start"],
        employee["shift_end"],
    )

    return {
        "emp_code": employee["emp_code"],
        "date": attendance_date,
        "status": status,
        "punch_in": punched_at,
        "punch_out": None,
        "shift_start": employee["shift_start"],
        "shift_end": employee["shift_end"],
        "work_hours": None,
        "late_minutes": compute_late_minutes(
            punched_at,
            attendance_date,
            employee["shift_start"],
            employee["shift_end"],
        ),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
        "created_at": now_utc(),
        "updated_at": now_utc(),
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(request: PunchInRequest):
    employee = find_employee(request.emp_code)

    punched_at = (
        epoch_ms_to_datetime(request.punched_at)
        if request.punched_at is not None
        else truncate_seconds(now_utc())
    )

    document = attendance_document(employee, punched_at, request.status)

    try:
        db.attendance_logs.insert_one(document)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="Attendance already exists for this employee and date",
        )

    saved = db.attendance_logs.find_one(
        {"emp_code": request.emp_code, "date": document["date"]}
    )
    return serialize_attendance(saved)




@app.post("/attendance/punch-out")
def punch_out(request: PunchOutRequest):
    employee = find_employee(request.emp_code)

    punched_at = (
        epoch_ms_to_datetime(request.punched_at)
        if request.punched_at is not None
        else truncate_seconds(now_utc())
    )

    open_record = db.attendance_logs.find_one(
        {
            "emp_code": request.emp_code,
            "punch_in": {"$ne": None, "$lte": punched_at},
            "punch_out": None,
            "status": {"$in": list(PRESENT_STATUSES)},
        },
        sort=[("punch_in", DESCENDING)],
    )

    if open_record is None:
        raise HTTPException(
            status_code=404,
            detail="No open punch-in found for this employee",
        )

    punch_in_at = as_utc(open_record["punch_in"])
    elapsed_seconds = (punched_at - punch_in_at).total_seconds()

    if elapsed_seconds <= 0 or elapsed_seconds > 24 * 60 * 60:
        raise HTTPException(
            status_code=422,
            detail="Punch-out must be after punch-in and within 24 hours",
        )

    attendance_date = open_record["date"]
    updated = db.attendance_logs.update_one(
        {"_id": open_record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punched_at,
                "work_hours": compute_work_hours(punch_in_at, punched_at),
                "half_day": compute_work_hours(punch_in_at, punched_at) < 4.50,
                "late_minutes": compute_late_minutes(
                    punch_in_at,
                    attendance_date,
                    employee["shift_start"],
                    employee["shift_end"],
                ),
                "overtime_minutes": compute_overtime(
                    punched_at,
                    attendance_date,
                    employee["shift_start"],
                    employee["shift_end"],
                ),
                "updated_at": now_utc(),
            }
        },
    )

    if updated.modified_count != 1:
        raise HTTPException(
            status_code=409,
            detail="Attendance was already updated",
        )

    saved = db.attendance_logs.find_one({"_id": open_record["_id"]})
    return serialize_attendance(saved)

class AttendanceCorrection(BaseModel):
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, value):
        if value is not None and value not in ALL_STATUSES:
            raise ValueError("Invalid attendance status")
        return value

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_timestamp(cls, value):
        if value is not None:
            epoch_ms_to_datetime(value)
        return value


@app.patch("/attendance/{emp_code}/{attendance_date}")
def correct_attendance(
    emp_code: str,
    attendance_date: str,
    correction: AttendanceCorrection,
):
    if not re.fullmatch(r"EMP\d{4,6}", emp_code):
        raise HTTPException(status_code=422, detail="Invalid emp_code")

    try:
        parsed_date = parse_date(attendance_date)
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="Date must use YYYY-MM-DD")

    find_employee(emp_code)

    query = {"emp_code": emp_code, "date": parsed_date.isoformat()}
    existing = db.attendance_logs.find_one(query)

    if existing is None:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    changes = {}
    new_status = correction.status if correction.status is not None else existing["status"]

    if new_status in {"ABSENT", "LEAVE"}:
        if correction.punch_in is not None or correction.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="ABSENT and LEAVE cannot have punch times",
            )
        new_punch_in = None
        new_punch_out = None
    else:
        new_punch_in = (
            epoch_ms_to_datetime(correction.punch_in)
            if correction.punch_in is not None
            else existing.get("punch_in")
        )
        new_punch_out = (
            epoch_ms_to_datetime(correction.punch_out)
            if correction.punch_out is not None
            else existing.get("punch_out")
        )

        if new_punch_in is None:
            raise HTTPException(
                status_code=422,
                detail="Presence status requires punch_in",
            )

        if ist_datetime(new_punch_in).date() != parsed_date:
            raise HTTPException(
                status_code=422,
                detail="punch_in must belong to the attendance date",
            )

        if new_punch_out is not None:
            elapsed = (as_utc(new_punch_out) - as_utc(new_punch_in)).total_seconds()
            if elapsed <= 0 or elapsed > 24 * 60 * 60:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out must be after punch_in and within 24 hours",
                )

    if new_status != existing["status"]:
        changes["status"] = new_status

    old_in = existing.get("punch_in")
    if (old_in is None) != (new_punch_in is None) or (
        old_in is not None and new_punch_in is not None
        and as_utc(old_in) != as_utc(new_punch_in)
    ):
        changes["punch_in"] = (
            datetime_to_epoch_ms(new_punch_in) if new_punch_in is not None else None
        )

    old_out = existing.get("punch_out")
    if (old_out is None) != (new_punch_out is None) or (
        old_out is not None and new_punch_out is not None
        and as_utc(old_out) != as_utc(new_punch_out)
    ):
        changes["punch_out"] = (
            datetime_to_epoch_ms(new_punch_out) if new_punch_out is not None else None
        )

    if not changes:
        raise HTTPException(status_code=422, detail="No changes supplied")

    employee = db.employees.find_one({"emp_code": emp_code})
    shift_start, shift_end = employee["shift_start"], employee["shift_end"]

    update_fields = {
        "status": new_status,
        "punch_in": new_punch_in,
        "punch_out": new_punch_out,
        "updated_at": now_utc(),
    }


    if new_status in PRESENT_STATUSES and new_punch_in is not None:
        update_fields["late_minutes"] = compute_late_minutes(
            new_punch_in,
            parsed_date,
            employee["shift_start"],
            employee["shift_end"],
        )
        update_fields["overtime_minutes"] = (
            compute_overtime(
                new_punch_out,
                parsed_date,
                employee["shift_start"],
                employee["shift_end"],
            )
            if new_punch_out is not None else 0
        )
        update_fields["work_hours"] = (
            compute_work_hours(new_punch_in, new_punch_out)
            if new_punch_out is not None else None
        )
        work_hours = update_fields["work_hours"]
        update_fields["half_day"] = work_hours is not None and work_hours < 4.50
    else:
        update_fields.update({
            "late_minutes": 0,
            "overtime_minutes": 0,
            "work_hours": None,
            "half_day": False,
        })

    history_entry = {
        "at": now_utc(),
        "by": correction.regularized_by,
        "reason": correction.reason,
        "changes": changes,
    }

    result = db.attendance_logs.update_one(
        query,
        {
            "$set": update_fields,
            "$push": {"history": history_entry},
        },
    )

    if result.modified_count != 1:
        raise HTTPException(status_code=409, detail="Attendance changed concurrently")

    return serialize_attendance(db.attendance_logs.find_one(query))

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly_analytics(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
):
    employee = find_employee(emp_code)

    try:
        month_start, month_end = parse_month(month)
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="month must use YYYY-MM")

    # Determine the inclusive calendar range for this month.
    joined_on = parse_date(employee["joined_on"])

    # Count weekdays from the employee's joining date through month-end.
    first_workday_check = max(month_start, joined_on)
    working_days = 0
    current_day = first_workday_check
    while current_day <= month_end:
        if current_day.weekday() < 5:
            working_days += 1
        current_day += timedelta(days=1)

    # Aggregate attendance records in MongoDB for this employee and month.
    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": month_start.isoformat(),
                    "$lte": month_end.isoformat(),
                },
            }
        },
        {
            "$addFields": {
                "_attendance_day": {
                    "$dateFromString": {"dateString": "$date"}
                }
            }
        },
        {
            "$addFields": {
                "_is_weekday": {
                    "$not": [
                        {"$in": [{"$dayOfWeek": "$_attendance_day"}, [1, 7]]}
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", list(PRESENT_STATUSES)]},
                                    "$_is_weekday",
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1.0,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]

    result = next(db.attendance_logs.aggregate(pipeline), {})

    present_days = float(result.get("present_days", 0))

    attendance_pct = (
        money_round(
            Decimal(str(present_days)) * Decimal("100")
            / Decimal(working_days),
            4,
        )
        if working_days > 0
        else None
    )


    return {
        "emp_code": emp_code,
        "name": employee["name"],
        "department": employee["department"],
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": result.get("leave_days", 0),
        "late_count": result.get("late_count", 0),
        "total_late_minutes": result.get("total_late_minutes", 0),
        "total_overtime_minutes": result.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }

@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    department: Optional[str] = None,
):

    try:
        month_start, month_end = parse_month(month)
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="month must use YYYY-MM")

    employee_filter = {"joined_on": {"$lte": month_end.isoformat()}}
    if department is not None:
        employee_filter["department"] = department

    employees = list(db.employees.find(employee_filter, {"_id": 0}))
    if not employees:
        return []

    employee_codes = [employee["emp_code"] for employee in employees]
    departments = sorted({employee["department"] for employee in employees})

    pipeline = [
        {
            "$match": {
                "emp_code": {"$in": employee_codes},
                "date": {
                    "$gte": month_start.isoformat(),
                    "$lte": month_end.isoformat(),
                },
            }
        },
        {
            "$addFields": {
                "_attendance_day": {"$dateFromString": {"dateString": "$date"}}
            }
        },
        {
            "$addFields": {
                "_is_weekday": {
                    "$not": [
                        {"$in": [{"$dayOfWeek": "$_attendance_day"}, [1, 7]]}
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", list(PRESENT_STATUSES)]},
                                    "$_is_weekday",
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1.0,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", list(PRESENT_STATUSES)]},
                                    {"$ne": ["$work_hours", None]},
                                ]
                            },
                            "$work_hours",
                            0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", list(PRESENT_STATUSES)]},
                                    {"$ne": ["$work_hours", None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}
                },
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
        {"$group": {
            "_id": "$employee.department",
            "present_days": {"$sum": "$present_days"},
            "work_hours_sum": {"$sum": "$work_hours_sum"},
            "work_hours_count": {"$sum": "$work_hours_count"},
            "late_count": {"$sum": "$late_count"},
            "total_late_minutes": {"$sum": "$total_late_minutes"},
            "leave_count": {"$sum": "$leave_count"},
            "on_duty_count": {"$sum": "$on_duty_count"},
        }},
    ]

    aggregated = {
        row["_id"]: row
        for row in db.attendance_logs.aggregate(pipeline)
    }

    output = []
    for dept in departments:
        dept_employees = [e for e in employees if e["department"] == dept]
        row = aggregated.get(dept, {})
        work_count = row.get("work_hours_count", 0)
        avg_hours = (
            float(money_round(
                Decimal(str(row.get("work_hours_sum", 0))) / Decimal(work_count), 2
            ))
            if work_count else None
        )
        output.append({
            "department": dept,
            "headcount": len(dept_employees),
            "present_days": float(row.get("present_days", 0)),
            "avg_work_hours": avg_hours,
            "late_count": row.get("late_count", 0),
            "total_late_minutes": row.get("total_late_minutes", 0),
            "leave_count": row.get("leave_count", 0),
            "on_duty_count": row.get("on_duty_count", 0),
        })

    return output

@app.get("/analytics/leaderboard/late")
def late_arrival_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    try:
        month_start, month_end = parse_month(month)
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=422,
            detail="month must use YYYY-MM",
        )

    employee_filter = {}
    if department is not None:
        employee_filter["department"] = department

    employees = list(
        db.employees.find(
            employee_filter,
            {"_id": 0, "emp_code": 1, "name": 1, "department": 1},
        )
    )

    employee_by_code = {
        employee["emp_code"]: employee for employee in employees
    }
    codes = list(employee_by_code)

    if not codes:
        return []

    pipeline = [
        {
            "$match": {
                "emp_code": {"$in": codes},
                "date": {
                    "$gte": month_start.isoformat(),
                    "$lte": month_end.isoformat(),
                },
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
    ]

    rows = list(db.attendance_logs.aggregate(pipeline))
    output = []
    previous_minutes = None
    rank = 0

    for position, row in enumerate(rows, start=1):
        minutes = row["total_late_minutes"]

        if minutes != previous_minutes:
            rank = position
            previous_minutes = minutes

        if rank > limit:
            break

        employee = employee_by_code.get(row["_id"])
        if employee is None:
            continue

        output.append({
            "rank": rank,
            "emp_code": employee["emp_code"],
            "name": employee["name"],
            "department": employee["department"],
            "total_late_minutes": minutes,
        })

    return output

@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    try:
        start = parse_date(from_date)
        end = parse_date(to_date)
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=422,
            detail="from and to must use YYYY-MM-DD format",
        )

    if end < start:
        raise HTTPException(status_code=422, detail="to must be on or after from")

    if (end - start).days + 1 > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    department_employees = list(
        db.employees.find(
            {"department": department},
            {"_id": 0, "emp_code": 1, "joined_on": 1},
        )
    )

    if not department_employees:
        raise HTTPException(status_code=404, detail="Department not found")

    employee_codes = [e["emp_code"] for e in department_employees]

    # Aggregate records by calendar date, then densify the date series.
    pipeline = [
        {
            "$match": {
                "emp_code": {"$in": employee_codes},
                "date": {
                    "$gte": start.isoformat(),
                    "$lte": end.isoformat(),
                },
            }
        },
        {
            "$addFields": {
                "_day": {"$dateFromString": {"dateString": "$date"}}
            }
        },
        {
            "$group": {
                "_id": "$_day",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", list(PRESENT_STATUSES)]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "_day": "$_id",
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "_day",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [
                        datetime.combine(start, time.min),
                        datetime.combine(end + timedelta(days=1), time.min),
                    ],
                },
            }
        },
        {
            "$set": {
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {"$sort": {"_day": 1}},
    ]

    rows = list(db.attendance_logs.aggregate(pipeline))

    # Build the result for the requested calendar dates.
    # Headcount is based on employees who had joined by each date.
    output = []
    rates = []

    for row in rows:
        day = row["_day"].date()
        is_working_day = day.weekday() < 5
        headcount = sum(
            1 for employee in department_employees
            if parse_date(employee["joined_on"]) <= day
        )
        present_count = float(row.get("present_count", 0))
        late_count = int(row.get("late_count", 0))

        rate = (
            float(money_round(
                Decimal(str(present_count)) * Decimal("100") /
                Decimal(headcount), 4
            ))
            if is_working_day and headcount > 0
            else None
        )

        rates.append(rate)
        recent_rates = [value for value in rates[-7:] if value is not None]
        moving_avg = (
            float(money_round(
                sum(Decimal(str(value)) for value in recent_rates) /
                Decimal(len(recent_rates)), 4
            ))
            if recent_rates else None
        )

        output.append({
            "date": day.isoformat(),
            "is_working_day": is_working_day,
            "headcount": headcount,
            "present_count": present_count,
            "late_count": late_count,
            "attendance_rate": rate,
            "moving_avg_7d": moving_avg,
        })

    return output

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
):
    allowed = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }
    if endpoint not in allowed:
        raise HTTPException(
            status_code=422,
            detail=f"endpoint must be one of {sorted(allowed)}",
        )

    if endpoint == "attendance_list":
        filters = {}

        if emp_code is not None:
            if not re.fullmatch(r"EMP\d{4,6}", emp_code):
                raise HTTPException(status_code=422, detail="Invalid emp_code")
            filters["emp_code"] = emp_code

        try:
            start = parse_date(date_from) if date_from else None
            end = parse_date(date_to) if date_to else None
        except (ValueError, TypeError):
            raise HTTPException(status_code=422, detail="Invalid date format")

        if start and end and start > end:
            raise HTTPException(status_code=422, detail="date_from must not exceed date_to")

        if start or end:
            date_filter = {}
            if start:
                date_filter["$gte"] = start.isoformat()
            if end:
                date_filter["$lte"] = end.isoformat()
            filters["date"] = date_filter

        if status is not None:
            if status not in ALL_STATUSES:
                raise HTTPException(status_code=422, detail="Invalid status")
            filters["status"] = status

        command = {
            "find": "attendance_logs",
            "filter": filters,
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size,
            "limit": page_size,
            "projection": {"_id": 0},
        }
        collection = "attendance_logs"

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                status_code=422,
                detail="emp_code and month=YYYY-MM are required",
            )
        try:
            month_start = parse_month(month)
        except (ValueError, TypeError):
            raise HTTPException(status_code=422, detail="Invalid month")

        next_month = (
            date(month_start.year + 1, 1, 1)
            if month_start.month == 12
            else date(month_start.year, month_start.month + 1, 1)
        )
        month_end = next_month - timedelta(days=1)

        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {
                        "$gte": month_start.isoformat(),
                        "$lte": month_end.isoformat(),
                    },
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "records": {"$sum": 1},
                    "total_late_minutes": {
                        "$sum": {"$ifNull": ["$late_minutes", 0]}
                    },
                }
            },
        ]
        command = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        collection = "attendance_logs"


    elif endpoint in {"department_summary", "late_leaderboard"}:
        if not month:
            raise HTTPException(status_code=422, detail="month=YYYY-MM is required")
        try:
            month_start, month_end = parse_month(month)
        except (ValueError, TypeError):
            raise HTTPException(status_code=422, detail="month must use YYYY-MM")
        employee_filter = {}
        if department is not None:
            employee_filter["department"] = department

        employees = list(
            db.employees.find(
                employee_filter,
                {"_id": 0, "emp_code": 1},
            )
        )
        codes = [item["emp_code"] for item in employees]

        pipeline = [
            {
                "$match": {
                    "emp_code": {"$in": codes},
                    "date": {
                        "$gte": month_start.isoformat(),
                        "$lte": month_end.isoformat(),
                    },
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {
                        "$sum": {"$ifNull": ["$late_minutes", 0]}
                    },
                    "records": {"$sum": 1},
                }
            },
        ]

        if endpoint == "late_leaderboard":
            pipeline.extend([
                {"$match": {"total_late_minutes": {"$gt": 0}}},
                {"$sort": {"total_late_minutes": -1, "_id": 1}},
                {"$limit": limit},
            ])

        command = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        collection = "attendance_logs"

    else:  # department_trend
        if not department or not from_date or not to_date:
            raise HTTPException(
                status_code=422,
                detail="department, from=YYYY-MM-DD and to=YYYY-MM-DD are required",
            )
        try:
            start = parse_date(from_date)
            end = parse_date(to_date)
        except (ValueError, TypeError):
            raise HTTPException(status_code=422, detail="Invalid date format")

        if end < start or (end - start).days + 1 > 92:
            raise HTTPException(status_code=422, detail="Invalid trend date range")

        employees = list(
            db.employees.find(
                {"department": department},
                {"_id": 0, "emp_code": 1},
            )
        )
        codes = [item["emp_code"] for item in employees]

        pipeline = [
            {
                "$match": {
                    "emp_code": {"$in": codes},
                    "date": {
                        "$gte": start.isoformat(),
                        "$lte": end.isoformat(),
                    },
                }
            },
            {
                "$group": {
                    "_id": "$date",
                    "present_count": {
                        "$sum": {
                            "$cond": [
                                {"$in": ["$status", list(PRESENT_STATUSES)]},
                                1,
                                0,
                            ]
                        }
                    },
                }
            },
            {"$sort": {"_id": 1}},
        ]
        command = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        collection = "attendance_logs"

    try:
        explain = db.command(
            "explain",
            command,
            verbosity="executionStats",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"MongoDB explain failed: {type(exc).__name__}",
        )

    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": explain,
    }
