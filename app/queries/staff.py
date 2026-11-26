"""직원 계정, 세션, 팀, 목표와 공지."""

from ..db import one, rows, run


def lock_login(login_id):
    return one("SELECT * FROM staff_account WHERE login_id=%s AND is_active=1 FOR UPDATE", (login_id,))


def session_user(token_hash):
    return one(
        "SELECT a.staff_id, a.login_id, a.role, a.display_name, a.team_id FROM staff_session s "
        "JOIN staff_account a ON a.staff_id=s.staff_id "
        "WHERE s.token_hash=%s AND s.expires_at>UTC_TIMESTAMP(6) AND a.is_active=1",
        (token_hash,),
    )


def create_session(token_hash, staff_id, expires_at):
    run(
        "INSERT INTO staff_session (token_hash,staff_id,expires_at) VALUES (%s,%s,%s)",
        (token_hash, staff_id, expires_at),
    )


def delete_session(token_hash):
    run("DELETE FROM staff_session WHERE token_hash=%s", (token_hash,))


def revoke_sessions(staff_id):
    run("DELETE FROM staff_session WHERE staff_id=%s", (staff_id,))


def delete_expired_sessions():
    run("DELETE FROM staff_session WHERE expires_at<=UTC_TIMESTAMP(6)")


def active_agents():
    return rows("SELECT staff_id, login_id FROM staff_account WHERE role='AGENT' AND is_active=1 ORDER BY login_id")


def login_for_staff(staff_id):
    return one("SELECT login_id FROM staff_account WHERE staff_id=%s", (staff_id,))


def lock_active_agent(staff_id):
    return one(
        "SELECT login_id FROM staff_account WHERE staff_id=%s AND role='AGENT' AND is_active=1 FOR UPDATE",
        (staff_id,),
    )


def agent_account(staff_id, *, lock=False):
    return one(
        "SELECT staff_id,login_id,display_name,is_active FROM staff_account WHERE staff_id=%s AND role='AGENT'"
        + (" FOR UPDATE" if lock else ""), (staff_id,),
    )


def create_agent(staff_id, login_id, display_name, password_hash):
    run(
        "INSERT INTO staff_account (staff_id,login_id,display_name,password_hash,role) VALUES (%s,%s,%s,%s,'AGENT')",
        (staff_id, login_id, display_name, password_hash),
    )


def set_display_name(staff_id, display_name):
    run("UPDATE staff_account SET display_name=%s WHERE staff_id=%s", (display_name, staff_id))


def set_login(staff_id, login_id):
    run("UPDATE staff_account SET login_id=%s WHERE staff_id=%s", (login_id, staff_id))


def set_password(staff_id, password_hash):
    run("UPDATE staff_account SET password_hash=%s WHERE staff_id=%s", (password_hash, staff_id))


def set_active(staff_id, enabled):
    run("UPDATE staff_account SET is_active=%s WHERE staff_id=%s", (enabled, staff_id))


def quota(staff_id):
    return one("SELECT daily,weekly,monthly FROM agent_quota WHERE staff_id=%s", (staff_id,))


def set_quota(staff_id, daily, weekly, monthly):
    run(
        "REPLACE INTO agent_quota (staff_id,daily,weekly,monthly) VALUES (%s,%s,%s,%s)",
        (staff_id, daily, weekly, monthly),
    )


def teams():
    return rows("SELECT team_id,name FROM staff_team ORDER BY name")


def team_name(team_id):
    return one("SELECT name FROM staff_team WHERE team_id=%s", (team_id,))


def lock_team(team_id):
    return one("SELECT name FROM staff_team WHERE team_id=%s FOR UPDATE", (team_id,))


def create_team(team_id, name):
    run("INSERT INTO staff_team (team_id,name) VALUES (%s,%s)", (team_id, name))


def rename_team(team_id, name):
    run("UPDATE staff_team SET name=%s WHERE team_id=%s", (name, team_id))


def team_roster():
    return rows(
        "SELECT staff_id,login_id,display_name,team_id,is_active FROM staff_account WHERE role='AGENT' ORDER BY login_id",
    )


def lock_team_memberships():
    return rows("SELECT staff_id,team_id FROM staff_account WHERE role='AGENT' ORDER BY staff_id FOR UPDATE")


def set_team(staff_id, team_id):
    run("UPDATE staff_account SET team_id=%s WHERE staff_id=%s", (team_id, staff_id))


def notices(latest=False):
    # 최근 공지가 먼저 오도록 정렬한다. 데스크 요약은 한 건만, 공지 탭은 전체 목록을 사용한다.
    return rows("SELECT n.notice_id,n.title,n.body,n.published_at,COALESCE(a.display_name,a.login_id) AS author "
                "FROM staff_notice n JOIN staff_account a ON a.staff_id=n.author_id ORDER BY published_at DESC,notice_id DESC"
                + (" LIMIT 1" if latest else ""))


def notice_by_id(notice_id):
    return one("SELECT author_id,title,body FROM staff_notice WHERE notice_id=%s", (notice_id,))


def publish_notice(notice_id, author_id, title, body):
    run(
        "INSERT INTO staff_notice (notice_id,author_id,title,body) VALUES (%s,%s,%s,%s)",
        (notice_id, author_id, title, body),
    )


def add_admin_change(actor_id, action, target, case_id):
    run(
        "INSERT INTO admin_change (actor_id, action, target, case_id) VALUES (%s,%s,%s,%s)",
        (actor_id, action, target, case_id),
    )


def any_account():
    return one("SELECT staff_id FROM staff_account LIMIT 1")


def seed_account(staff_id, login_id, password_hash, role):
    run(
        "INSERT INTO staff_account (staff_id,login_id,password_hash,role) VALUES (%s,%s,%s,%s)",
        (staff_id, login_id, password_hash, role),
    )
