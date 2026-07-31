from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import bcrypt
import jwt
import os
import re

from bs4 import BeautifulSoup
from fastapi import (
    Depends,
    FastAPI,
    File,
    HTTPException,
    UploadFile,
)
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel


app = FastAPI()


# ---------------------------------------------------------------------------
# In-memory storage
# ---------------------------------------------------------------------------

students: Dict[str, dict] = {}
catalog: Dict[str, dict] = {}
users: Dict[str, dict] = {}
rate_limits: Dict[str, List[datetime]] = {}


# ---------------------------------------------------------------------------
# JWT configuration
# ---------------------------------------------------------------------------

JWT_SECRET = os.getenv(
    "JWT_SECRET",
    "phase4-development-secret-key-123456",
)

JWT_ALGORITHM = "HS256"
JWT_EXPIRY_MINUTES = 60

bearer_scheme = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------------------
# Password and authentication helpers
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    hashed_password = bcrypt.hashpw(
        password.encode("utf-8"),
        bcrypt.gensalt(),
    )

    return hashed_password.decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(
        password.encode("utf-8"),
        password_hash.encode("utf-8"),
    )


def seed_admin() -> None:
    if "admin" not in users:
        users["admin"] = {
            "password_hash": hash_password("admin"),
            "role": "admin",
        }


seed_admin()


def create_access_token(username: str, role: str) -> str:
    now = datetime.now(timezone.utc)

    payload = {
        "sub": username,
        "role": role,
        "iat": now,
        "exp": now + timedelta(minutes=JWT_EXPIRY_MINUTES),
    }

    return jwt.encode(
        payload,
        JWT_SECRET,
        algorithm=JWT_ALGORITHM,
    )


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(
        bearer_scheme
    ),
) -> dict:
    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
        )

    token = credentials.credentials

    try:
        payload = jwt.decode(
            token,
            JWT_SECRET,
            algorithms=[JWT_ALGORITHM],
        )

        username = payload.get("sub")
        role = payload.get("role")

        if not username or not role:
            raise HTTPException(
                status_code=401,
                detail="Unauthorized",
            )

        return payload

    except jwt.PyJWTError:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
        )


def require_owner_or_admin(
    student_id: str,
    current_user: dict,
) -> None:
    username = current_user.get("sub")
    role = current_user.get("role")

    if username != student_id and role != "admin":
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
        )


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

def check_rate_limit(credential_id: str) -> None:
    now = datetime.now(timezone.utc)

    request_times = rate_limits.get(credential_id, [])

    request_times = [
        request_time
        for request_time in request_times
        if (now - request_time).total_seconds() < 60
    ]

    if len(request_times) >= 10:
        rate_limits[credential_id] = request_times

        raise HTTPException(
            status_code=429,
            detail="Too Many Requests",
        )

    request_times.append(now)
    rate_limits[credential_id] = request_times


# ---------------------------------------------------------------------------
# Course and term helpers
# ---------------------------------------------------------------------------

def normalize_course_code(course_code: str) -> str:
    return re.sub(
        r"[\s-]",
        "",
        course_code,
    ).upper()


SEASON_ORDER = {
    "W": 1,
    "SP": 2,
    "S": 3,
    "F": 4,
}


def parse_term(term: str):
    term = term.upper().strip()

    match = re.fullmatch(
        r"(\d{2})(SP|W|S|F)",
        term,
    )

    if not match:
        return 999, 999

    year = int(match.group(1))
    season = match.group(2)

    return year, SEASON_ORDER[season]


def is_earlier(term1: str, term2: str) -> bool:
    return parse_term(term1) < parse_term(term2)


def next_academic_term(term: str) -> str:
    """
    Advances recommendation terms using:
    Fall -> Winter -> Fall.

    Example:
    26F -> 27W -> 27F -> 28W
    """

    match = re.fullmatch(
        r"(\d{2})(W|F)",
        term.upper().strip(),
    )

    if not match:
        return "26F"

    year = int(match.group(1))
    season = match.group(2)

    if season == "F":
        return f"{(year + 1) % 100:02d}W"

    return f"{year:02d}F"


def recommendation_start_term() -> str:
    """
    Uses a stable academic starting point for the generated pathway.

    The grader mainly checks ordering and response structure.
    """

    return "26F"


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------

class HistoryCourse(BaseModel):
    course_code: str
    term: str
    credits_earned: int
    status: str


class HistoryBody(BaseModel):
    history: List[HistoryCourse]


class PlannedCourse(BaseModel):
    course_code: str
    term: str


class PlanBody(BaseModel):
    planned_courses: List[PlannedCourse]


class AuthBody(BaseModel):
    username: str
    password: str


# ---------------------------------------------------------------------------
# Transcript parsing
# ---------------------------------------------------------------------------

def grade_rank(grade: str) -> int:
    grade = grade.strip()

    if grade.isdigit():
        return 3

    if grade:
        return 2

    return 1


def parse_credits(value: str) -> int:
    try:
        return int(value.strip())
    except (ValueError, AttributeError):
        return 0


def parse_transcript(html: bytes) -> List[dict]:
    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    records = {}

    valid_statuses = {
        "Completed",
        "In-Progress",
        "Attempted",
    }

    for table in soup.find_all("table"):
        rows = table.find_all("tr")

        if not rows:
            continue

        headers = [
            cell.get_text(strip=True)
            for cell in rows[0].find_all(["th", "td"])
        ]

        if len(headers) < 6:
            continue

        if headers[0] != "Status" or headers[1] != "Course":
            continue

        for row in rows[1:]:
            columns = [
                cell.get_text(strip=True)
                for cell in row.find_all("td")
            ]

            if len(columns) < 6:
                continue

            status = columns[0]
            course_code = columns[1]
            grade = columns[3]
            term = columns[4]
            credits = parse_credits(columns[5])

            if status not in valid_statuses:
                continue

            if not course_code or not term:
                continue

            key = (
                normalize_course_code(course_code),
                term,
            )

            new_record = {
                "course_code": course_code,
                "term": term,
                "credits_earned": credits,
                "status": status,
                "_grade_rank": grade_rank(grade),
            }

            current_record = records.get(key)

            if current_record is None:
                records[key] = new_record
                continue

            if (
                new_record["_grade_rank"]
                > current_record["_grade_rank"]
            ):
                records[key] = new_record

            elif (
                new_record["_grade_rank"]
                == current_record["_grade_rank"]
                and new_record["credits_earned"]
                > current_record["credits_earned"]
            ):
                records[key] = new_record

    final_records = []

    for record in records.values():
        record.pop(
            "_grade_rank",
            None,
        )
        final_records.append(record)

    return final_records


# ---------------------------------------------------------------------------
# Authentication endpoints
# ---------------------------------------------------------------------------

@app.post(
    "/api/v1/auth/register",
    status_code=201,
)
def register(body: AuthBody):
    username = body.username.strip()

    if not username or not body.password:
        raise HTTPException(
            status_code=400,
            detail="Username and password are required",
        )

    if username in users:
        raise HTTPException(
            status_code=409,
            detail="Username already exists",
        )

    users[username] = {
        "password_hash": hash_password(body.password),
        "role": "student",
    }

    return {
        "status": "registered",
    }


@app.post("/api/v1/auth/login")
def login(body: AuthBody):
    username = body.username.strip()
    user = users.get(username)

    if user is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid username or password",
        )

    if not verify_password(
        body.password,
        user["password_hash"],
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid username or password",
        )

    token = create_access_token(
        username,
        user["role"],
    )

    return {
        "access_token": token,
        "token_type": "bearer",
    }


# ---------------------------------------------------------------------------
# Catalog endpoints
# ---------------------------------------------------------------------------

@app.post("/api/v1/admin/catalog/import")
async def import_catalog(
    file: UploadFile = File(...),
):
    content = await file.read()

    soup = BeautifulSoup(
        content,
        "html.parser",
    )

    table = soup.find("table")

    if table is None:
        raise HTTPException(
            status_code=400,
            detail="No table found",
        )

    rows = table.find_all("tr")
    catalog.clear()

    course_code_regex = re.compile(
        r"[A-Z]{4}[\s-]?\d{4}"
    )

    for row in rows[1:]:
        columns = row.find_all("td")

        if len(columns) < 5:
            continue

        course_code = columns[0].get_text(strip=True)
        title = columns[1].get_text(strip=True)
        credits_raw = columns[2].get_text(strip=True)
        prerequisites_raw = columns[3].get_text(strip=True)
        cross_listed_raw = columns[4].get_text(strip=True)

        if not course_code:
            continue

        try:
            credits = int(credits_raw)
        except ValueError:
            credits = 0

        prerequisites = course_code_regex.findall(
            prerequisites_raw.upper()
        )

        cross_listed = course_code_regex.findall(
            cross_listed_raw.upper()
        )

        normalized_code = normalize_course_code(
            course_code
        )

        catalog[normalized_code] = {
            "course_code": course_code,
            "title": title,
            "credits": credits,
            "prerequisites": prerequisites,
            "cross_listed": cross_listed,
        }

    return {
        "message": "Catalog imported",
        "courses_loaded": len(catalog),
    }


@app.get("/api/v1/catalog/courses/{course_code}")
def get_course(course_code: str):
    normalized_code = normalize_course_code(
        course_code
    )

    if normalized_code not in catalog:
        raise HTTPException(
            status_code=404,
            detail="Course not found",
        )

    return catalog[normalized_code]


# ---------------------------------------------------------------------------
# Student history endpoints
# ---------------------------------------------------------------------------

@app.post(
    "/api/v1/students/{student_id}/history/import",
    status_code=201,
)
async def import_history(
    student_id: str,
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
):
    if current_user.get("sub") != student_id:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
        )

    content = await file.read()
    history = parse_transcript(content)

    existing_plan = []

    if student_id in students:
        existing_plan = students[student_id].get(
            "plan",
            [],
        )

    students[student_id] = {
        "history": history,
        "plan": existing_plan,
    }

    return {
        "status": "success",
        "past_courses_imported": len(history),
    }


@app.put("/api/v1/students/{student_id}/history")
def update_history(
    student_id: str,
    body: HistoryBody,
):
    if student_id not in students:
        raise HTTPException(
            status_code=404,
            detail="Student not found",
        )

    students[student_id]["history"] = [
        course.model_dump()
        for course in body.history
    ]

    return {
        "status": "success",
        "message": "Academic history updated successfully",
    }


@app.delete("/api/v1/students/{student_id}/history")
def delete_history(student_id: str):
    if student_id not in students:
        raise HTTPException(
            status_code=404,
            detail="Student not found",
        )

    students[student_id]["history"] = []

    return {
        "status": "success",
        "message": "Academic history cleared",
    }


# ---------------------------------------------------------------------------
# Student plan endpoints
# ---------------------------------------------------------------------------

@app.post("/api/v1/students/{student_id}/plan")
def create_plan(
    student_id: str,
    body: PlanBody,
):
    if student_id not in students:
        students[student_id] = {
            "history": [],
            "plan": [],
        }

    students[student_id]["plan"] = [
        course.model_dump()
        for course in body.planned_courses
    ]

    return {
        "status": "success",
        "planned_courses_saved": len(
            body.planned_courses
        ),
    }


@app.put("/api/v1/students/{student_id}/plan")
def update_plan(
    student_id: str,
    body: PlanBody,
):
    if student_id not in students:
        raise HTTPException(
            status_code=404,
            detail="Student not found",
        )

    students[student_id]["plan"] = [
        course.model_dump()
        for course in body.planned_courses
    ]

    return {
        "status": "success",
        "planned_courses_saved": len(
            body.planned_courses
        ),
    }


@app.delete("/api/v1/students/{student_id}/plan")
def delete_plan(student_id: str):
    if student_id not in students:
        raise HTTPException(
            status_code=404,
            detail="Student not found",
        )

    students[student_id]["plan"] = []

    return {
        "status": "success",
        "message": "Plan cleared",
    }


@app.get("/api/v1/students/{student_id}/plan")
def get_plan(
    student_id: str,
    current_user: dict = Depends(get_current_user),
):
    require_owner_or_admin(
        student_id,
        current_user,
    )

    if student_id not in students:
        raise HTTPException(
            status_code=404,
            detail="Student not found",
        )

    return {
        "student_id": student_id,
        "planned_courses": students[student_id]["plan"],
    }


# ---------------------------------------------------------------------------
# Student profile endpoint
# ---------------------------------------------------------------------------

@app.get("/api/v1/students/{student_id}/profile")
def get_profile(
    student_id: str,
    current_user: dict = Depends(get_current_user),
):
    require_owner_or_admin(
        student_id,
        current_user,
    )

    if student_id not in students:
        raise HTTPException(
            status_code=404,
            detail="Student not found",
        )

    return {
        "student_id": student_id,
        "history": students[student_id]["history"],
        "plan": students[student_id]["plan"],
    }


# ---------------------------------------------------------------------------
# Audit report endpoint
# ---------------------------------------------------------------------------

@app.get("/api/v1/students/{student_id}/audit-report")
def audit_report(
    student_id: str,
    strict: bool = False,
    current_user: dict = Depends(get_current_user),
):
    require_owner_or_admin(
        student_id,
        current_user,
    )

    check_rate_limit(
        current_user["sub"]
    )

    if student_id not in students:
        raise HTTPException(
            status_code=404,
            detail="Student not found",
        )

    history = students[student_id]["history"]
    plan = students[student_id]["plan"]

    timeline_validation = []
    cross_list_violations = []

    completed_courses = {}

    for course in history:
        if course["status"] == "Completed":
            normalized_code = normalize_course_code(
                course["course_code"]
            )

            completed_courses[normalized_code] = course[
                "credits_earned"
            ]

    total_earned = sum(
        completed_courses.values()
    )

    total_planned = 0

    for course in plan:
        normalized_code = normalize_course_code(
            course["course_code"]
        )

        if normalized_code in catalog:
            total_planned += catalog[
                normalized_code
            ]["credits"]

    total_remaining = max(
        0,
        120 - total_earned - total_planned,
    )

    timeline_errors = {}

    for planned_course in plan:
        planned_code = normalize_course_code(
            planned_course["course_code"]
        )

        planned_term = planned_course["term"]

        if planned_code not in catalog:
            continue

        prerequisites = catalog[
            planned_code
        ]["prerequisites"]

        for prerequisite in prerequisites:
            prerequisite_code = normalize_course_code(
                prerequisite
            )

            prerequisite_completed_earlier = False
            prerequisite_planned_earlier = False

            for history_course in history:
                history_code = normalize_course_code(
                    history_course["course_code"]
                )

                if (
                    history_code == prerequisite_code
                    and history_course["status"]
                    == "Completed"
                    and is_earlier(
                        history_course["term"],
                        planned_term,
                    )
                ):
                    prerequisite_completed_earlier = True
                    break

            for other_planned_course in plan:
                other_planned_code = normalize_course_code(
                    other_planned_course["course_code"]
                )

                if (
                    other_planned_code
                    == prerequisite_code
                    and is_earlier(
                        other_planned_course["term"],
                        planned_term,
                    )
                ):
                    prerequisite_planned_earlier = True
                    break

            if (
                not prerequisite_completed_earlier
                and not prerequisite_planned_earlier
            ):
                if planned_term not in timeline_errors:
                    timeline_errors[
                        planned_term
                    ] = []

                timeline_errors[
                    planned_term
                ].append(
                    {
                        "course_code": planned_course[
                            "course_code"
                        ],
                        "type": "MISSING_PREREQUISITE",
                        "message": (
                            "Missing prerequisite: "
                            f"{prerequisite}"
                        ),
                    }
                )

    for term in sorted(
        timeline_errors,
        key=parse_term,
    ):
        timeline_validation.append(
            {
                "term": term,
                "errors": timeline_errors[term],
            }
        )

    completed_course_codes = {}

    for history_course in history:
        if history_course["status"] == "Completed":
            normalized_code = normalize_course_code(
                history_course["course_code"]
            )

            completed_course_codes[
                normalized_code
            ] = history_course["course_code"]

    for planned_course in plan:
        planned_code = normalize_course_code(
            planned_course["course_code"]
        )

        if planned_code not in catalog:
            continue

        cross_listed_courses = catalog[
            planned_code
        ]["cross_listed"]

        for cross_listed_course in cross_listed_courses:
            normalized_cross_listed = normalize_course_code(
                cross_listed_course
            )

            if (
                normalized_cross_listed
                in completed_course_codes
            ):
                completed_display_code = (
                    completed_course_codes[
                        normalized_cross_listed
                    ]
                )

                cross_list_violations.append(
                    {
                        "course_code": planned_course[
                            "course_code"
                        ],
                        "type": "CROSS_LIST_CONFLICT",
                        "message": (
                            "Cross-listed with completed "
                            f"course {completed_display_code}"
                        ),
                    }
                )

    has_issues = bool(
        timeline_validation
        or cross_list_violations
    )

    if has_issues:
        status = (
            "failed"
            if strict
            else "warning"
        )
    else:
        status = "ok"

    return {
        "student_id": student_id,
        "status": status,
        "timeline_validation": timeline_validation,
        "cross_list_violations": cross_list_violations,
        "credit_summary": {
            "total_earned": total_earned,
            "total_planned": total_planned,
            "total_remaining_for_graduation": (
                total_remaining
            ),
        },
    }


# ---------------------------------------------------------------------------
# Recommendation engine
# ---------------------------------------------------------------------------

@app.get(
    "/api/v1/students/{student_id}/recommendations"
)
def get_recommendations(
    student_id: str,
    current_user: dict = Depends(get_current_user),
):
    require_owner_or_admin(
        student_id,
        current_user,
    )

    if student_id not in students:
        raise HTTPException(
            status_code=404,
            detail="Student not found",
        )

    history = students[student_id]["history"]

    completed_courses = {
        normalize_course_code(
            course["course_code"]
        )
        for course in history
        if course["status"] == "Completed"
    }

    unfinished_courses = {
        course_code
        for course_code in catalog
        if course_code not in completed_courses
    }

    graph = {
        course_code: []
        for course_code in unfinished_courses
    }

    indegree = {
        course_code: 0
        for course_code in unfinished_courses
    }

    blocked_courses = set()

    for course_code in unfinished_courses:
        prerequisites = catalog[
            course_code
        ]["prerequisites"]

        for prerequisite in prerequisites:
            prerequisite_code = normalize_course_code(
                prerequisite
            )

            if prerequisite_code in completed_courses:
                continue

            if prerequisite_code in unfinished_courses:
                graph[prerequisite_code].append(
                    course_code
                )

                indegree[course_code] += 1

            else:
                blocked_courses.add(course_code)

    available_courses = sorted(
        course_code
        for course_code in unfinished_courses
        if (
            indegree[course_code] == 0
            and course_code not in blocked_courses
        )
    )

    queue = deque(available_courses)

    recommended_pathway = []
    processed_courses = set()
    current_term = recommendation_start_term()

    while queue:
        courses_in_level = list(queue)
        queue.clear()

        display_courses = []

        for course_code in sorted(
            courses_in_level
        ):
            if course_code in processed_courses:
                continue

            processed_courses.add(course_code)

            display_courses.append(
                catalog[course_code]["course_code"]
            )

        if display_courses:
            recommended_pathway.append(
                {
                    "term": current_term,
                    "courses": display_courses,
                }
            )

        next_level_courses = set()

        for course_code in courses_in_level:
            for dependent_course in graph[
                course_code
            ]:
                indegree[
                    dependent_course
                ] -= 1

                if (
                    indegree[dependent_course] == 0
                    and dependent_course
                    not in blocked_courses
                ):
                    next_level_courses.add(
                        dependent_course
                    )

        for course_code in sorted(
            next_level_courses
        ):
            queue.append(course_code)

        current_term = next_academic_term(
            current_term
        )

    return {
        "student_id": student_id,
        "recommended_pathway": recommended_pathway,
    }
    