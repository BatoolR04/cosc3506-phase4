from fastapi import FastAPI, UploadFile, File, HTTPException
from pydantic import BaseModel
from bs4 import BeautifulSoup
from typing import List
import re

app = FastAPI()

students = {}
catalog = {}


def normalize_course_code(course_code: str) -> str:
    return re.sub(r"[\s-]", "", course_code).upper()


SEASON_ORDER = {"W": 1, "SP": 2, "S": 3, "F": 4}


def parse_term(term):
    term = term.upper().strip()

    match = re.fullmatch(r"(\d{2})(SP|W|S|F)", term)

    if not match:
        return (999, 999)

    year = int(match.group(1))
    season = match.group(2)

    return (year, SEASON_ORDER[season])


def is_earlier(term1, term2):
    return parse_term(term1) < parse_term(term2)


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


def grade_rank(grade):
    grade = grade.strip()
    if grade.isdigit():
        return 3
    if grade:
        return 2
    return 1


def parse_credits(value):
    try:
        return int(value.strip())
    except (ValueError, AttributeError):
        return 0


def parse_transcript(html):
    soup = BeautifulSoup(html, "html.parser")
    records = {}

    valid_statuses = {"Completed", "In-Progress", "Attempted"}

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue

        headers = [cell.get_text(strip=True) for cell in rows[0].find_all(["th", "td"])]
        if len(headers) < 6:
            continue

        if headers[0] != "Status" or headers[1] != "Course":
            continue

        for row in rows[1:]:
            cols = [cell.get_text(strip=True) for cell in row.find_all("td")]
            if len(cols) < 6:
                continue

            status = cols[0]
            course_code = cols[1]
            grade = cols[3]
            term = cols[4]
            credits = parse_credits(cols[5])

            if status not in valid_statuses:
                continue
            if term == "":
                continue

            key = (course_code, term)

            current = records.get(key)
            new_record = {
                "course_code": course_code,
                "term": term,
                "credits_earned": credits,
                "status": status,
                "_grade_rank": grade_rank(grade),
            }

            if current is None:
                records[key] = new_record
            else:
                if new_record["_grade_rank"] > current["_grade_rank"]:
                    records[key] = new_record
                elif (
                    new_record["_grade_rank"] == current["_grade_rank"]
                    and credits > current["credits_earned"]
                ):
                    records[key] = new_record

    final = []
    for record in records.values():
        record.pop("_grade_rank", None)
        final.append(record)

    return final


@app.post("/api/v1/admin/catalog/import")
async def import_catalog(file: UploadFile = File(...)):
    content = await file.read()
    soup = BeautifulSoup(content, "html.parser")
    table = soup.find("table")

    if table is None:
        raise HTTPException(status_code=400, detail="No table found")

    rows = table.find_all("tr")
    catalog.clear()

    course_code_regex = re.compile(r"[A-Z]{4}[\s-]?\d{4}")

    for row in rows[1:]:
        cols = row.find_all("td")

        if len(cols) < 5:
            continue

        course_code = cols[0].get_text(strip=True)
        title = cols[1].get_text(strip=True)
        credits_raw = cols[2].get_text(strip=True)
        prerequisites_raw = cols[3].get_text(strip=True)
        cross_listed_raw = cols[4].get_text(strip=True)

        try:
            credits = int(credits_raw)
        except ValueError:
            credits = 0

        prerequisites = course_code_regex.findall(prerequisites_raw.upper())

        cross_listed = course_code_regex.findall(cross_listed_raw.upper())

        key = normalize_course_code(course_code)

        catalog[key] = {
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
    key = normalize_course_code(course_code)

    if key not in catalog:
        raise HTTPException(
            status_code=404,
            detail="Course not found",
        )

    return catalog[key]


@app.post("/api/v1/students/{student_id}/history/import", status_code=201)
async def import_history(student_id: str, file: UploadFile = File(...)):
    content = await file.read()
    history = parse_transcript(content)

    students[student_id] = {"history": history, "plan": []}

    return {"status": "success", "past_courses_imported": len(history)}


@app.put("/api/v1/students/{student_id}/history")
def update_history(student_id: str, body: HistoryBody):
    if student_id not in students:
        raise HTTPException(status_code=404, detail="Student not found")

    students[student_id]["history"] = [course.dict() for course in body.history]
    return {"status": "success", "message": "Academic history updated successfully"}


@app.delete("/api/v1/students/{student_id}/history")
def delete_history(student_id: str):
    if student_id not in students:
        raise HTTPException(status_code=404, detail="Student not found")

    students[student_id]["history"] = []
    return {"status": "success", "message": "Academic history cleared"}


@app.post("/api/v1/students/{student_id}/plan")
def create_plan(student_id: str, body: PlanBody):
    if student_id not in students:
        raise HTTPException(status_code=404, detail="Student not found")

    students[student_id]["plan"] = [course.dict() for course in body.planned_courses]
    return {"status": "success", "planned_courses_saved": len(body.planned_courses)}


@app.put("/api/v1/students/{student_id}/plan")
def update_plan(student_id: str, body: PlanBody):
    if student_id not in students:
        raise HTTPException(status_code=404, detail="Student not found")

    students[student_id]["plan"] = [course.dict() for course in body.planned_courses]
    return {"status": "success", "planned_courses_saved": len(body.planned_courses)}


@app.delete("/api/v1/students/{student_id}/plan")
def delete_plan(student_id: str):
    if student_id not in students:
        raise HTTPException(status_code=404, detail="Student not found")

    students[student_id]["plan"] = []
    return {"status": "success", "message": "Plan cleared"}


@app.get("/api/v1/students/{student_id}/profile")
def get_profile(student_id: str):
    if student_id not in students:
        raise HTTPException(status_code=404, detail="Student not found")

    return {
        "student_id": student_id,
        "history": students[student_id]["history"],
        "plan": students[student_id]["plan"],
    }


@app.get("/api/v1/students/{student_id}/audit-report")
def audit_report(student_id: str, strict: bool = False):

    if student_id not in students:
        raise HTTPException(status_code=404, detail="Student not found")

    history = students[student_id]["history"]
    plan = students[student_id]["plan"]

    timeline_validation = []
    cross_list_violations = []

    completed_courses = {}

    for course in history:
        if course["status"] == "Completed":
            code = normalize_course_code(course["course_code"])
            completed_courses[code] = course["credits_earned"]

    total_earned = sum(completed_courses.values())

    total_planned = 0

    for course in plan:
        code = normalize_course_code(course["course_code"])

        if code in catalog:
            total_planned += catalog[code]["credits"]

    total_remaining = max(0, 120 - total_earned - total_planned)

    timeline_errors = {}

    for planned_course in plan:
        planned_code = normalize_course_code(planned_course["course_code"])
        planned_term = planned_course["term"]

        if planned_code not in catalog:
            continue

        prerequisites = catalog[planned_code]["prerequisites"]

        for prerequisite in prerequisites:
            prerequisite_code = normalize_course_code(prerequisite)

            prerequisite_completed_earlier = False

            for history_course in history:
                history_code = normalize_course_code(history_course["course_code"])

                if (
                    history_code == prerequisite_code
                    and history_course["status"] == "Completed"
                    and is_earlier(history_course["term"], planned_term)
                ):
                    prerequisite_completed_earlier = True
                    break

            if not prerequisite_completed_earlier:
                if planned_term not in timeline_errors:
                    timeline_errors[planned_term] = []

                timeline_errors[planned_term].append(
                    {
                        "course_code": planned_course["course_code"],
                        "type": "MISSING_PREREQUISITE",
                        "message": (f"Missing prerequisite: {prerequisite}"),
                    }
                )

    for term in sorted(timeline_errors, key=parse_term):
        timeline_validation.append({"term": term, "errors": timeline_errors[term]})
    completed_course_codes = {}

    for history_course in history:
        if history_course["status"] == "Completed":
            normalized_code = normalize_course_code(history_course["course_code"])

            completed_course_codes[normalized_code] = history_course["course_code"]

    for planned_course in plan:
        planned_code = normalize_course_code(planned_course["course_code"])

        if planned_code not in catalog:
            continue

        cross_listed_courses = catalog[planned_code]["cross_listed"]

        for cross_listed_course in cross_listed_courses:
            normalized_cross_listed = normalize_course_code(cross_listed_course)

            if normalized_cross_listed in completed_course_codes:
                completed_display_code = completed_course_codes[normalized_cross_listed]

                cross_list_violations.append(
                    {
                        "course_code": planned_course["course_code"],
                        "type": "CROSS_LIST_CONFLICT",
                        "message": (
                            "Cross-listed with completed course "
                            f"{completed_display_code}"
                        ),
                    }
                )

    has_issues = bool(timeline_validation or cross_list_violations)

    if has_issues:
        status = "failed" if strict else "warning"
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
            "total_remaining_for_graduation": total_remaining,
        },
    }
