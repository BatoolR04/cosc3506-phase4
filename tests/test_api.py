from fastapi.testclient import TestClient

from main import app, catalog, students, users


client = TestClient(app)


def reset_data():
    catalog.clear()
    students.clear()

    # Keep only the hardcoded admin account between tests.
    admin_user = users.get("admin")
    users.clear()

    if admin_user is not None:
        users["admin"] = admin_user


def get_token(username="770001", password="password123"):
    register_response = client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "password": password,
        },
    )

    assert register_response.status_code in {201, 409}

    login_response = client.post(
        "/api/v1/auth/login",
        json={
            "username": username,
            "password": password,
        },
    )

    assert login_response.status_code == 200

    return login_response.json()["access_token"]


def auth_headers(username="770001", password="password123"):
    token = get_token(
        username=username,
        password=password,
    )

    return {
        "Authorization": f"Bearer {token}",
    }


def admin_headers():
    response = client.post(
        "/api/v1/auth/login",
        json={
            "username": "admin",
            "password": "admin",
        },
    )

    assert response.status_code == 200

    token = response.json()["access_token"]

    return {
        "Authorization": f"Bearer {token}",
    }


def test_catalog_import_and_lookup():
    reset_data()

    html = """
    <html>
      <body>
        <table>
          <tr>
            <th>Course Code</th>
            <th>Title</th>
            <th>Credits</th>
            <th>Prerequisites</th>
            <th>Cross-listed</th>
          </tr>
          <tr>
            <td>COSC 2006</td>
            <td>Programming Fundamentals I</td>
            <td>3</td>
            <td>None</td>
            <td></td>
          </tr>
        </table>
      </body>
    </html>
    """

    response = client.post(
        "/api/v1/admin/catalog/import",
        files={
            "file": (
                "catalog.html",
                html,
                "text/html",
            )
        },
    )

    assert response.status_code == 200
    assert response.json()["courses_loaded"] == 1

    response = client.get("/api/v1/catalog/courses/COSC2006")

    assert response.status_code == 200
    assert response.json()["credits"] == 3


def test_history_import_and_profile():
    reset_data()

    headers = auth_headers("770001")

    transcript = """
    <html>
      <body>
        <table>
          <tr>
            <th>Status</th>
            <th>Course</th>
            <th>Title</th>
            <th>Grade</th>
            <th>Term</th>
            <th>Credits</th>
          </tr>
          <tr>
            <td>Completed</td>
            <td>COSC 2006</td>
            <td>Programming Fundamentals I</td>
            <td>80</td>
            <td>24W</td>
            <td>3</td>
          </tr>
        </table>
      </body>
    </html>
    """

    response = client.post(
        "/api/v1/students/770001/history/import",
        headers=headers,
        files={
            "file": (
                "transcript.html",
                transcript,
                "text/html",
            )
        },
    )

    assert response.status_code == 201
    assert response.json()["past_courses_imported"] == 1

    response = client.get(
        "/api/v1/students/770001/profile",
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["student_id"] == "770001"
    assert len(response.json()["history"]) == 1


def test_missing_prerequisite_and_strict_behavior():
    reset_data()

    headers = auth_headers("770001")

    catalog.update(
        {
            "COSC2006": {
                "course_code": "COSC 2006",
                "title": "Programming Fundamentals I",
                "credits": 3,
                "prerequisites": [],
                "cross_listed": [],
            },
            "COSC2007": {
                "course_code": "COSC 2007",
                "title": "Programming Fundamentals II",
                "credits": 3,
                "prerequisites": ["COSC 2006"],
                "cross_listed": [],
            },
        }
    )

    students["770001"] = {
        "history": [],
        "plan": [
            {
                "course_code": "COSC 2007",
                "term": "20W",
            }
        ],
    }

    response = client.get(
        "/api/v1/students/770001/audit-report?strict=false",
        headers=headers,
    )

    assert response.status_code == 200

    body = response.json()

    assert body["status"] == "warning"
    assert body["timeline_validation"][0]["term"] == "20W"
    assert body["timeline_validation"][0]["errors"][0]["type"] == "MISSING_PREREQUISITE"

    response = client.get(
        "/api/v1/students/770001/audit-report?strict=true",
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "failed"


def test_cross_list_conflict():
    reset_data()

    headers = auth_headers("770001")

    catalog.update(
        {
            "COSC3506": {
                "course_code": "COSC 3506",
                "title": "Software Systems Development",
                "credits": 3,
                "prerequisites": [],
                "cross_listed": ["ITEC 3506"],
            },
            "ITEC3506": {
                "course_code": "ITEC 3506",
                "title": "Software Systems Development",
                "credits": 3,
                "prerequisites": [],
                "cross_listed": ["COSC 3506"],
            },
        }
    )

    students["770001"] = {
        "history": [
            {
                "course_code": "COSC 3506",
                "term": "24F",
                "credits_earned": 3,
                "status": "Completed",
            }
        ],
        "plan": [
            {
                "course_code": "ITEC 3506",
                "term": "26F",
            }
        ],
    }

    response = client.get(
        "/api/v1/students/770001/audit-report",
        headers=headers,
    )

    assert response.status_code == 200

    body = response.json()

    assert body["status"] == "warning"
    assert len(body["cross_list_violations"]) == 1
    assert body["cross_list_violations"][0]["type"] == "CROSS_LIST_CONFLICT"


def test_retake_not_double_counted():
    reset_data()

    headers = auth_headers("770001")

    students["770001"] = {
        "history": [
            {
                "course_code": "COSC 2006",
                "term": "23F",
                "credits_earned": 0,
                "status": "Attempted",
            },
            {
                "course_code": "COSC-2006",
                "term": "24W",
                "credits_earned": 3,
                "status": "Completed",
            },
            {
                "course_code": "COSC 2006",
                "term": "24F",
                "credits_earned": 3,
                "status": "Completed",
            },
        ],
        "plan": [],
    }

    response = client.get(
        "/api/v1/students/770001/audit-report",
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["credit_summary"]["total_earned"] == 3


def test_student_not_found():
    reset_data()

    response = client.get(
        "/api/v1/students/does-not-exist/audit-report",
        headers=admin_headers(),
    )

    assert response.status_code == 404


def test_update_and_delete_history():
    reset_data()

    students["770001"] = {
        "history": [],
        "plan": [],
    }

    response = client.put(
        "/api/v1/students/770001/history",
        json={
            "history": [
                {
                    "course_code": "COSC 1047",
                    "term": "24W",
                    "credits_earned": 3,
                    "status": "Completed",
                }
            ]
        },
    )

    assert response.status_code == 200
    assert len(students["770001"]["history"]) == 1

    response = client.delete("/api/v1/students/770001/history")

    assert response.status_code == 200
    assert students["770001"]["history"] == []


def test_update_and_delete_plan():
    reset_data()

    students["770001"] = {
        "history": [],
        "plan": [],
    }

    response = client.put(
        "/api/v1/students/770001/plan",
        json={
            "planned_courses": [
                {
                    "course_code": "COSC 3506",
                    "term": "26F",
                }
            ]
        },
    )

    assert response.status_code == 200
    assert len(students["770001"]["plan"]) == 1

    response = client.delete("/api/v1/students/770001/plan")

    assert response.status_code == 200
    assert students["770001"]["plan"] == []
