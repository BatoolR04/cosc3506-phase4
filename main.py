from fastapi import (
    FastAPI,
    UploadFile,
    File,
    HTTPException,
    Depends,
)

from pydantic import BaseModel
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from bs4 import BeautifulSoup
from typing import List, Optional
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
import os
import re
import logging


# ---------------------------------------------------------
# Application setup
# ---------------------------------------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="COSC 3506 Phase 4 API",
    version="4.0.0"
)


# This assignment currently uses in-memory storage.
# The data will reset whenever Render restarts or redeploys.
students = {}
catalog = {}
users = {}
rate_limits = {}


JWT_SECRET = os.getenv(
    "JWT_SECRET",
    "phase4-development-secret-key-123456"
)

JWT_ALGORITHM = "HS256"
JWT_EXPIRY_MINUTES = 60


# auto_error=False allows us to return our own 401 response
# when the Authorization header is missing.
bearer_scheme = HTTPBearer(auto_error=False)


# ---------------------------------------------------------
# Password helpers
# ---------------------------------------------------------

def hash_password(password: str) -> str:
    hashed_password = bcrypt.hashpw(
        password.encode("utf-8"),
        bcrypt.gensalt()
    )

    return hashed_password.decode("utf-8")


def verify_password(
    password: str,
    password_hash: str
) -> bool:
    return bcrypt.checkpw(
        password.encode("utf-8"),
        password_hash.encode("utf-8")
    )


# ---------------------------------------------------------
# Admin account
# ---------------------------------------------------------

def seed_admin():
    if "admin" not in users:
        users["admin"] = {
            "password_hash": hash_password("admin"),
            "role": "admin"
        }

        logger.info("Default admin account created.")


seed_admin()


# ---------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------

def create_access_token(
    username: str,
    role: str
) -> str:
    now = datetime.now(timezone.utc)

    payload = {
        "sub": username,
        "role": role,
        "iat": now,
        "exp": now + timedelta(
            minutes=JWT_EXPIRY_MINUTES
        )
    }

    return jwt.encode(
        payload,
        JWT_SECRET,
        algorithm=JWT_ALGORITHM
    )


def get_current_user(
    credentials: Optional[
        HTTPAuthorizationCredentials
    ] = Depends(bearer_scheme)
) -> dict:
    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized"
        )

    try:
        payload = jwt.decode(
            credentials.credentials,
            JWT_SECRET,
            algorithms=[JWT_ALGORITHM]
        )

        username = payload.get("sub")
        role = payload.get("role")

        if not username or not role:
            raise HTTPException(
                status_code=401,
                detail="Invalid authentication token"
            )

        return {
            "username": str(username).strip(),
            "role": str(role).strip().lower()
        }

    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=401,
            detail="Token has expired"
        )

    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=401,
            detail="Invalid authentication token"
        )


def require_owner_or_admin(
    student_id: str,
    current_user: dict
):
    username = current_user.get("username")
    role = current_user.get("role")

    if username != student_id and role != "admin":
        raise HTTPException(
            status_code=401,
            detail="Unauthorized"
        )


def require_admin(current_user: dict):
    role = str(
        current_user.get("role", "")
    ).strip().lower()

    if role != "admin":
        raise HTTPException(
            status_code=403,
            detail="Admin access required"
        )


# ---------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------

def check_rate_limit(user_id: str):
    now = datetime.now(timezone.utc)

    if user_id not in rate_limits:
        rate_limits[user_id] = []

    rate_limits[user_id] = [
        request_time
        for request_time in rate_limits[user_id]
        if (now - request_time).total_seconds() < 60
    ]

    if len(rate_limits[user_id]) >= 10:
        raise HTTPException(
            status_code=429,
            detail="Too Many Requests"
        )

    rate_limits[user_id].append(now)


# ---------------------------------------------------------
# Course and term helpers
# ---------------------------------------------------------

def normalize_course_code(
    course_code: str
) -> str:
    return re.sub(
        r"[\s-]",
        "",
        course_code
    ).upper()


SEASON_ORDER = {
    "W": 1,
    "SP": 2,
    "S": 3,
    "F": 4
}


def parse_term(term: str):
    term = term.upper().strip()

    match = re.fullmatch(
        r"(\d{2})(SP|W|S|F)",
        term
    )

    if not match:
        return 999, 999

    year = int(match.group(1))
    season = match.group(2)

    return year, SEASON_ORDER[season]


def is_earlier(
    term1: str,
    term2: str
) -> bool:
    return parse_term(term1) < parse_term(term2)


# ---------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------

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


# ---------------------------------------------------------
# Transcript parsing
# ---------------------------------------------------------

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


def parse_transcript(html):
    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    records = {}

    valid_statuses = {
        "Completed",
        "In-Progress",
        "Attempted"
    }

    for table in soup.find_all("table"):
        rows = table.find_all("tr")

        if not rows:
            continue

        headers = [
            cell.get_text(strip=True)
            for cell in rows[0].find_all(
                ["th", "td"]
            )
        ]

        if len(headers) < 6:
            continue

        if (
            headers[0] != "Status"
            or headers[1] != "Course"
        ):
            continue

        for row in rows[1:]:
            cols = [
                cell.get_text(strip=True)
                for cell in row.find_all("td")
            ]

            if len(cols) < 6:
                continue

            status = cols[0]
            course_code = cols[1]
            grade = cols[3]
            term = cols[4]
            credits = parse_credits(cols[5])

            if status not in valid_statuses:
                continue

            if not term:
                continue

            key = (
                course_code,
                term
            )

            current = records.get(key)

            new_record = {
                "course_code": course_code,
                "term": term,
                "credits_earned": credits,
                "status": status,
                "_grade_rank": grade_rank(grade)
            }

            if current is None:
                records[key] = new_record

            elif (
                new_record["_grade_rank"]
                > current["_grade_rank"]
            ):
                records[key] = new_record

            elif (
                new_record["_grade_rank"]
                == current["_grade_rank"]
                and credits
                > current["credits_earned"]
            ):
                records[key] = new_record

    final_records = []

    for record in records.values():
        record.pop(
            "_grade_rank",
            None
        )

        final_records.append(record)

    return final_records


# ---------------------------------------------------------
# Health and startup checks
# ---------------------------------------------------------

@app.on_event("startup")
def startup_event():
    logger.info(
        "COSC 3506 Phase 4 API started successfully."
    )

    logger.info(
        "Admin role currently stored as: %s",
        users.get("admin", {}).get("role")
    )


@app.get("/")
def root():
    return {
        "message": "COSC 3506 Phase 4 API is running",
        "version": "4.0.0"
    }


@app.get("/health")
def health():
    return {
        "status": "healthy",
        "students_loaded": len(students),
        "catalog_courses_loaded": len(catalog),
        "users_loaded": len(users)
    }


# ---------------------------------------------------------
# Authentication
# ---------------------------------------------------------

@app.post(
    "/api/v1/auth/register",
    status_code=201
)
def register(body: AuthBody):
    username = body.username.strip()

    if not username:
        raise HTTPException(
            status_code=400,
            detail="Username is required"
        )

    if not body.password:
        raise HTTPException(
            status_code=400,
            detail="Password is required"
        )

    if username in users:
        raise HTTPException(
            status_code=409,
            detail="Username already exists"
        )

    users[username] = {
        "password_hash": hash_password(
            body.password
        ),
        "role": "student"
    }

    return {
        "status": "registered"
    }


@app.post("/api/v1/auth/login")
def login(body: AuthBody):
    username = body.username.strip()
    user = users.get(username)

    if user is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid username or password"
        )

    if not verify_password(
        body.password,
        user["password_hash"]
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid username or password"
        )

    token = create_access_token(
        username,
        user["role"]
    )

    return {
        "access_token": token,
        "token_type": "bearer"
    }


# ---------------------------------------------------------
# Catalog
# ---------------------------------------------------------

@app.post("/api/v1/admin/catalog/import")
async def import_catalog(
    file: UploadFile = File(...),
    current_user: dict = Depends(
        get_current_user
    )
):
    require_admin(current_user)

    logger.info(
        "Catalog import requested by admin user: %s",
        current_user["username"]
    )

    content = await file.read()

    soup = BeautifulSoup(
        content,
        "html.parser"
    )

    table = soup.find("table")

    if table is None:
        raise HTTPException(
            status_code=400,
            detail="No table found"
        )

    rows = table.find_all("tr")

    catalog.clear()

    course_code_regex = re.compile(
        r"[A-Z]{4}[\s-]?\d{4}"
    )

    for row in rows[1:]:
        cols = row.find_all("td")

        if len(cols) < 5:
            continue

        course_code = cols[0].get_text(
            strip=True
        )

        title = cols[1].get_text(
            strip=True
        )

        credits_raw = cols[2].get_text(
            strip=True
        )

        prerequisites_raw = cols[3].get_text(
            strip=True
        )

        cross_listed_raw = cols[4].get_text(
            strip=True
        )

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

        key = normalize_course_code(
            course_code
        )

        catalog[key] = {
            "course_code": course_code,
            "title": title,
            "credits": credits,
            "prerequisites": prerequisites,
            "cross_listed": cross_listed
        }

    logger.info(
        "Catalog imported successfully. Courses loaded: %s",
        len(catalog)
    )

    return {
        "message": "Catalog imported",
        "courses_loaded": len(catalog)
    }


@app.get(
    "/api/v1/catalog/courses/{course_code}"
)
def get_course(course_code: str):
    key = normalize_course_code(
        course_code
    )

    if key not in catalog:
        raise HTTPException(
            status_code=404,
            detail="Course not found"
        )

    return catalog[key]


# ---------------------------------------------------------
# Student history
# ---------------------------------------------------------

@app.post(
    "/api/v1/students/{student_id}/history/import",
    status_code=201
)
async def import_history(
    student_id: str,
    file: UploadFile = File(...),
    current_user: dict = Depends(
        get_current_user
    )
):
    require_owner_or_admin(
        student_id,
        current_user
    )

    content = await file.read()
    history = parse_transcript(content)

    existing_plan = []

    if student_id in students:
        existing_plan = students[
            student_id
        ].get("plan", [])

    students[student_id] = {
        "history": history,
        "plan": existing_plan
    }

    return {
        "status": "success",
        "past_courses_imported": len(history)
    }


@app.put(
    "/api/v1/students/{student_id}/history"
)
def update_history(
    student_id: str,
    body: HistoryBody,
    current_user: dict = Depends(
        get_current_user
    )
):
    require_owner_or_admin(
        student_id,
        current_user
    )

    if student_id not in students:
        raise HTTPException(
            status_code=404,
            detail="Student not found"
        )

    students[student_id]["history"] = [
        course.model_dump()
        for course in body.history
    ]

    return {
        "status": "success",
        "message": (
            "Academic history updated successfully"
        )
    }


@app.delete(
    "/api/v1/students/{student_id}/history"
)
def delete_history(
    student_id: str,
    current_user: dict = Depends(
        get_current_user
    )
):
    require_owner_or_admin(
        student_id,
        current_user
    )

    if student_id not in students:
        raise HTTPException(
            status_code=404,
            detail="Student not found"
        )

    students[student_id]["history"] = []

    return {
        "status": "success",
        "message": "Academic history cleared"
    }


# ---------------------------------------------------------
# Student plan
# ---------------------------------------------------------

@app.post(
    "/api/v1/students/{student_id}/plan"
)
def create_plan(
    student_id: str,
    body: PlanBody,
    current_user: dict = Depends(
        get_current_user
    )
):
    require_owner_or_admin(
        student_id,
        current_user
    )

    if student_id not in students:
        raise HTTPException(
            status_code=404,
            detail="Student not found"
        )

    students[student_id]["plan"] = [
        course.model_dump()
        for course in body.planned_courses
    ]

    return {
        "status": "success",
        "planned_courses_saved": len(
            body.planned_courses
        )
    }


@app.put(
    "/api/v1/students/{student_id}/plan"
)
def update_plan(
    student_id: str,
    body: PlanBody,
    current_user: dict = Depends(
        get_current_user
    )
):
    require_owner_or_admin(
        student_id,
        current_user
    )

    if student_id not in students:
        raise HTTPException(
            status_code=404,
            detail="Student not found"
        )

    students[student_id]["plan"] = [
        course.model_dump()
        for course in body.planned_courses
    ]

    return {
        "status": "success",
        "planned_courses_saved": len(
            body.planned_courses
        )
    }


@app.delete(
    "/api/v1/students/{student_id}/plan"
)
def delete_plan(
    student_id: str,
    current_user: dict = Depends(
        get_current_user
    )
):
    require_owner_or_admin(
        student_id,
        current_user
    )

    if student_id not in students:
        raise HTTPException(
            status_code=404,
            detail="Student not found"
        )

    students[student_id]["plan"] = []

    return {
        "status": "success",
        "message": "Plan cleared"
    }


@app.get(
    "/api/v1/students/{student_id}/plan"
)
def get_plan(
    student_id: str,
    current_user: dict = Depends(
        get_current_user
    )
):
    require_owner_or_admin(
        student_id,
        current_user
    )

    if student_id not in students:
        raise HTTPException(
            status_code=404,
            detail="Student not found"
        )

    return {
        "student_id": student_id,
        "planned_courses": students[
            student_id
        ]["plan"]
    }


# ---------------------------------------------------------
# Student profile
# ---------------------------------------------------------

@app.get(
    "/api/v1/students/{student_id}/profile"
)
def get_profile(
    student_id: str,
    current_user: dict = Depends(
        get_current_user
    )
):
    require_owner_or_admin(
        student_id,
        current_user
    )

    if student_id not in students:
        raise HTTPException(
            status_code=404,
            detail="Student not found"
        )

    return {
        "student_id": student_id,
        "history": students[
            student_id
        ]["history"],
        "plan": students[
            student_id
        ]["plan"]
    }


# ---------------------------------------------------------
# Audit report
# ---------------------------------------------------------

@app.get(
    "/api/v1/students/{student_id}/audit-report"
)
def audit_report(
    student_id: str,
    strict: bool = False,
    current_user: dict = Depends(
        get_current_user
    )
):
    require_owner_or_admin(
        student_id,
        current_user
    )

    if student_id not in students:
        raise HTTPException(
            status_code=404,
            detail="Student not found"
        )

    check_rate_limit(
        current_user["username"]
    )

    history = students[
        student_id
    ]["history"]

    plan = students[
        student_id
    ]["plan"]

    timeline_validation = []
    cross_list_violations = []

    completed_courses = {}

    for course in history:
        if course["status"] == "Completed":
            code = normalize_course_code(
                course["course_code"]
            )

            completed_courses[code] = (
                course["credits_earned"]
            )

    total_earned = sum(
        completed_courses.values()
    )

    total_planned = 0

    for course in plan:
        code = normalize_course_code(
            course["course_code"]
        )

        if code in catalog:
            total_planned += catalog[
                code
            ]["credits"]

    total_remaining = max(
        0,
        120
        - total_earned
        - total_planned
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
            prerequisite_code = (
                normalize_course_code(
                    prerequisite
                )
            )

            prerequisite_completed_earlier = False

            for history_course in history:
                history_code = (
                    normalize_course_code(
                        history_course[
                            "course_code"
                        ]
                    )
                )

                if (
                    history_code
                    == prerequisite_code
                    and history_course["status"]
                    == "Completed"
                    and is_earlier(
                        history_course["term"],
                        planned_term
                    )
                ):
                    prerequisite_completed_earlier = True
                    break

            if not prerequisite_completed_earlier:
                if (
                    planned_term
                    not in timeline_errors
                ):
                    timeline_errors[
                        planned_term
                    ] = []

                timeline_errors[
                    planned_term
                ].append(
                    {
                        "course_code": (
                            planned_course[
                                "course_code"
                            ]
                        ),
                        "type": (
                            "MISSING_PREREQUISITE"
                        ),
                        "message": (
                            "Missing prerequisite: "
                            f"{prerequisite}"
                        )
                    }
                )

    for term in sorted(
        timeline_errors,
        key=parse_term
    ):
        timeline_validation.append(
            {
                "term": term,
                "errors": timeline_errors[
                    term
                ]
            }
        )

    completed_course_codes = {}

    for history_course in history:
        if (
            history_course["status"]
            == "Completed"
        ):
            normalized_code = (
                normalize_course_code(
                    history_course[
                        "course_code"
                    ]
                )
            )

            completed_course_codes[
                normalized_code
            ] = history_course[
                "course_code"
            ]

    for planned_course in plan:
        planned_code = normalize_course_code(
            planned_course["course_code"]
        )

        if planned_code not in catalog:
            continue

        cross_listed_courses = catalog[
            planned_code
        ]["cross_listed"]

        for cross_listed_course in (
            cross_listed_courses
        ):
            normalized_cross_listed = (
                normalize_course_code(
                    cross_listed_course
                )
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
                        "course_code": (
                            planned_course[
                                "course_code"
                            ]
                        ),
                        "type": (
                            "CROSS_LIST_CONFLICT"
                        ),
                        "message": (
                            "Cross-listed with "
                            "completed course "
                            f"{completed_display_code}"
                        )
                    }
                )

    has_issues = bool(
        timeline_validation
        or cross_list_violations
    )

    if has_issues:
        status_value = (
            "failed"
            if strict
            else "warning"
        )

    else:
        status_value = "ok"

    return {
        "student_id": student_id,
        "status": status_value,
        "timeline_validation": (
            timeline_validation
        ),
        "cross_list_violations": (
            cross_list_violations
        ),
        "credit_summary": {
            "total_earned": total_earned,
            "total_planned": total_planned,
            "total_remaining_for_graduation": (
                total_remaining
            )
        }
    }


# ---------------------------------------------------------
# Course recommendations
# ---------------------------------------------------------

@app.get(
    "/api/v1/students/{student_id}/recommendations"
)
def get_recommendations(
    student_id: str,
    current_user: dict = Depends(
        get_current_user
    )
):
    require_owner_or_admin(
        student_id,
        current_user
    )

    if student_id not in students:
        raise HTTPException(
            status_code=404,
            detail="Student not found"
        )

    history = students[
        student_id
    ]["history"]

    completed_courses = set()

    for course in history:
        if course["status"] == "Completed":
            completed_courses.add(
                normalize_course_code(
                    course["course_code"]
                )
            )

    graph = {}
    indegree = {}

    for course_code in catalog:
        normalized_course = (
            normalize_course_code(
                course_code
            )
        )

        if (
            normalized_course
            in completed_courses
        ):
            continue

        graph[normalized_course] = []
        indegree[normalized_course] = 0

    for course_code, course in (
        catalog.items()
    ):
        normalized_course = (
            normalize_course_code(
                course_code
            )
        )

        if (
            normalized_course
            in completed_courses
        ):
            continue

        for prerequisite in course[
            "prerequisites"
        ]:
            prerequisite_code = (
                normalize_course_code(
                    prerequisite
                )
            )

            if (
                prerequisite_code
                in completed_courses
            ):
                continue

            if (
                prerequisite_code in graph
                and normalized_course in graph
            ):
                graph[
                    prerequisite_code
                ].append(
                    normalized_course
                )

                indegree[
                    normalized_course
                ] += 1

    queue = [
        course_code
        for course_code in indegree
        if indegree[course_code] == 0
    ]

    recommended_pathway = []
    term_number = 1

    while queue:
        current_term_courses = queue
        queue = []

        term_course_codes = [
            catalog[current_course]["course_code"]
            for current_course in current_term_courses
        ]

        recommended_pathway.append(
            {
                "term": f"Term {term_number}",
                "courses": term_course_codes
            }
        )

        for current_course in (
            current_term_courses
        ):
            for dependent_course in graph[
                current_course
            ]:
                indegree[
                    dependent_course
                ] -= 1

                if (
                    indegree[
                        dependent_course
                    ] == 0
                ):
                    queue.append(
                        dependent_course
                    )

        term_number += 1

    return {
        "student_id": student_id,
        "recommended_pathway": (
            recommended_pathway
        )
    }
    