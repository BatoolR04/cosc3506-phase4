from fastapi import FastAPI, UploadFile, File, HTTPException
from pydantic import BaseModel
from bs4 import BeautifulSoup
from typing import List
import re

app = FastAPI()

students = {}

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
    except:
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
                "_grade_rank": grade_rank(grade)
            }

            if current is None:
                records[key] = new_record
            else:
                if new_record["_grade_rank"] > current["_grade_rank"]:
                    records[key] = new_record
                elif new_record["_grade_rank"] == current["_grade_rank"] and credits > current["credits_earned"]:
                    records[key] = new_record

    final = []
    for record in records.values():
        record.pop("_grade_rank", None)
        final.append(record)

    return final

@app.post("/api/v1/students/{student_id}/history/import", status_code=201)
async def import_history(student_id: str, file: UploadFile = File(...)):
    content = await file.read()
    history = parse_transcript(content)

    students[student_id] = {
        "history": history,
        "plan": []
    }

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
        "plan": students[student_id]["plan"]
    }