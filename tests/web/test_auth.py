def test_pages_redirect_to_login(web):
    response = web.client.get("/files", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/login"


def test_htmx_requests_get_401_with_redirect_header(web):
    response = web.client.get("/partials/status", headers={"HX-Request": "true"})
    assert response.status_code == 401 and response.headers["HX-Redirect"] == "/login"


def test_wrong_then_right_password(web):
    response = web.client.post("/login", data={"password": "nope"})
    assert response.status_code == 401 and "Wrong password." in response.text
    web.login()
    assert web.client.get("/healthz").json() == {"ok": True, "printer_online": True, "dead": []}


def test_lockout_after_five_failures(web):
    for _ in range(5):
        web.client.post("/login", data={"password": "nope"})
    response = web.client.post("/login", data={"password": "secret"}, follow_redirects=False)
    assert response.status_code == 429
    web.clock.advance(61)
    web.login()


def test_cross_site_post_is_refused(web):
    web.login()
    response = web.client.post("/logout", headers={"Origin": "http://evil.example"})
    assert response.status_code == 403


def test_health_is_public(web):
    assert web.client.get("/healthz").status_code == 200


def test_logout(web):
    web.login()
    web.client.post("/logout")
    assert web.client.get("/files", follow_redirects=False).status_code == 303


def test_empty_or_missing_password_never_logs_in(web):
    wrong = web.client.post("/login", data={"password": "nope"})
    for data in ({}, {"password": ""}):
        response = web.client.post("/login", data=data, follow_redirects=False)
        assert response.status_code == wrong.status_code == 401
        assert "Wrong password." in response.text
    assert web.client.get("/files", follow_redirects=False).status_code == 303


def test_empty_passwords_count_toward_lockout(web):
    for _ in range(5):
        web.client.post("/login", data={})
    assert web.client.post("/login", data={"password": "secret"}).status_code == 429


def test_empty_password_setting_does_not_open_the_login(web):
    from dataclasses import replace
    svc = web.client.app.state.services
    svc.settings = replace(svc.settings, app_password="")
    response = web.client.post("/login", data={}, follow_redirects=False)
    assert response.status_code == 401
    assert web.client.get("/files", follow_redirects=False).status_code == 303
