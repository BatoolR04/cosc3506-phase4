from fastapi.testclient import TestClient

from main import app, catalog, rate_limits, students, users


client = TestClient(app)


def reset_all():
    catalog.clear()
    students.clear()
    rate_limits.clear()

    admin_user = users.get("admin")
    users.clear()

    if admin_user is not None:
        users["admin"] = admin_user


def register_and_login(username="s12345", password="MyPass1!"):
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


def auth_headers(token):
    return {
        "Authorization": f"Bearer {token}",
    }


def test_register_duplicate_and_bad_login():
    reset_all()

    response = client.post(
        "/api/v1/auth/register",
        json={
            "username": "s11111",
            "password": "Password1!",
        },
    )

    assert response.status_code == 201

    duplicate = client.post(
        "/api/v1/auth/register",
        json={
            "username": "s11111",
            "password": "Password1!",
        },
    )

    assert duplicate.status_code == 409

    wrong_password = client.post(
        "/api/v1/auth/login",
        json={
            "username": "s11111",
            "password": "wrong",
        },
    )

    assert wrong_password.status_code == 401

    missing_user = client.post(
        "/api/v1/auth/login",
        json={
            "username": "does-not-exist",
            "password": "wrong",
        },
    )

    assert missing_user.status_code == 401


def test_history_import_bola():
    reset_all()

    token = register_and_login("s12345")

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

    no_token = client.post(
        "/api/v1/students/s12345/history/import",
        files={
            "file": (
                "transcript.html",
                transcript,
                "text/html",
            )
        },
    )

    assert no_token.status_code == 401

    wrong_student = client.post(
        "/api/v1/students/s99999/history/import",
        headers=auth_headers(token),
        files={
            "file": (
                "transcript.html",
                transcript,
                "text/html",
            )
        },
    )

    assert wrong_student.status_code == 401

    owner = client.post(
        "/api/v1/students/s12345/history/import",
        headers=auth_headers(token),
        files={
            "file": (
                "transcript.html",
                transcript,
                "text/html",
            )
        },
    )

    assert owner.status_code == 201


def test_profile_owner_admin_and_wrong_user():
    reset_all()

    owner_token = register_and_login("s12345")
    wrong_token = register_and_login("s99999")

    students["s12345"] = {
        "history": [],
        "plan": [],
    }

    no_token = client.get("/api/v1/students/s12345/profile")

    assert no_token.status_code == 401

    wrong_user = client.get(
        "/api/v1/students/s12345/profile",
        headers=auth_headers(wrong_token),
    )

    assert wrong_user.status_code == 401

    owner = client.get(
        "/api/v1/students/s12345/profile",
        headers=auth_headers(owner_token),
    )

    assert owner.status_code == 200

    admin_login = client.post(
        "/api/v1/auth/login",
        json={
            "username": "admin",
            "password": "admin",
        },
    )

    admin_token = admin_login.json()["access_token"]

    admin = client.get(
        "/api/v1/students/s12345/profile",
        headers=auth_headers(admin_token),
    )

    assert admin.status_code == 200


def test_recommendations_ordering_and_completed_filter():
    reset_all()

    token = register_and_login("s12345")

    catalog.update(
        {
            "COSC1000": {
                "course_code": "COSC 1000",
                "title": "Intro",
                "credits": 3,
                "prerequisites": [],
                "cross_listed": [],
            },
            "COSC2000": {
                "course_code": "COSC 2000",
                "title": "Intermediate",
                "credits": 3,
                "prerequisites": ["COSC 1000"],
                "cross_listed": [],
            },
            "COSC3000": {
                "course_code": "COSC 3000",
                "title": "Advanced",
                "credits": 3,
                "prerequisites": ["COSC 2000"],
                "cross_listed": [],
            },
        }
    )

    students["s12345"] = {
        "history": [
            {
                "course_code": "COSC 1000",
                "term": "24W",
                "credits_earned": 3,
                "status": "Completed",
            }
        ],
        "plan": [],
    }

    response = client.get(
        "/api/v1/students/s12345/recommendations",
        headers=auth_headers(token),
    )

    assert response.status_code == 200

    pathway = response.json()["recommended_pathway"]

    all_courses = [course for term in pathway for course in term["courses"]]

    assert "COSC 1000" not in all_courses
    assert "COSC 2000" in pathway[0]["courses"]
    assert "COSC 3000" in pathway[1]["courses"]


def test_rate_limit_eleventh_request_returns_429():
    reset_all()

    token = register_and_login("s12345")

    students["s12345"] = {
        "history": [],
        "plan": [],
    }

    for _ in range(10):
        response = client.get(
            "/api/v1/students/s12345/audit-report",
            headers=auth_headers(token),
        )

        assert response.status_code == 200

    eleventh = client.get(
        "/api/v1/students/s12345/audit-report",
        headers=auth_headers(token),
    )

    assert eleventh.status_code == 429
