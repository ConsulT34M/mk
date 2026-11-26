"""직원 계정, 세션, 팀, 목표와 공지."""

from ..db import one, rows, run


def lock_login(login_id):
    # 로그인할 활성 직원 계정을 잠근다.
    return one("SELECT * FROM staff_account WHERE login_id=%s AND is_active=1 FOR UPDATE", (login_id,))


def session_user(token_hash):
    # 유효한 세션의 직원 정보와 권한을 조회한다.
    return one(
        "SELECT a.staff_id, a.login_id, a.role, a.display_name, a.team_id FROM staff_session s "
        "JOIN staff_account a ON a.staff_id=s.staff_id "
        "WHERE s.token_hash=%s AND s.expires_at>UTC_TIMESTAMP(6) AND a.is_active=1",
        (token_hash,),
    )


def create_session(token_hash, staff_id, expires_at):
    # 직원 로그인 세션을 만든다.
    run(
        "INSERT INTO staff_session (token_hash,staff_id,expires_at) VALUES (%s,%s,%s)",
        (token_hash, staff_id, expires_at),
    )


def delete_session(token_hash):
    # 직원 로그인 세션 한 건을 삭제한다.
    run("DELETE FROM staff_session WHERE token_hash=%s", (token_hash,))


def revoke_sessions(staff_id):
    # 직원의 모든 로그인 세션을 종료한다.
    run("DELETE FROM staff_session WHERE staff_id=%s", (staff_id,))


def delete_expired_sessions():
    # 만료된 직원 세션을 삭제한다.
    run("DELETE FROM staff_session WHERE expires_at<=UTC_TIMESTAMP(6)")


def active_agents():
    # 배정 가능한 활성 상담원 목록을 조회한다.
    return rows("SELECT staff_id, login_id FROM staff_account WHERE role='AGENT' AND is_active=1 ORDER BY login_id")


def login_for_staff(staff_id):
    # 직원 ID로 로그인 아이디를 찾는다.
    return one("SELECT login_id FROM staff_account WHERE staff_id=%s", (staff_id,))


def lock_active_agent(staff_id):
    # 배정할 활성 상담원 계정을 잠근다.
    return one(
        "SELECT login_id FROM staff_account WHERE staff_id=%s AND role='AGENT' AND is_active=1 FOR UPDATE",
        (staff_id,),
    )


def agent_account(staff_id, *, lock=False):
    # 상담원 계정을 조회하고 필요하면 잠근다.
    return one(
        "SELECT staff_id,login_id,display_name,is_active FROM staff_account WHERE staff_id=%s AND role='AGENT'"
        + (" FOR UPDATE" if lock else ""), (staff_id,),
    )


def create_agent(staff_id, login_id, display_name, password_hash):
    # 새 상담원 계정을 등록한다.
    run(
        "INSERT INTO staff_account (staff_id,login_id,display_name,password_hash,role) VALUES (%s,%s,%s,%s,'AGENT')",
        (staff_id, login_id, display_name, password_hash),
    )
    run("INSERT INTO agent_quota (staff_id) VALUES (%s)", (staff_id,))


def set_display_name(staff_id, display_name):
    # 직원의 표시 이름을 바꾼다.
    run("UPDATE staff_account SET display_name=%s WHERE staff_id=%s", (display_name, staff_id))


def set_login(staff_id, login_id):
    # 직원의 로그인 아이디를 바꾼다.
    run("UPDATE staff_account SET login_id=%s WHERE staff_id=%s", (login_id, staff_id))


def set_password(staff_id, password_hash):
    # 직원 계정에 새 비밀번호 해시를 저장한다.
    run("UPDATE staff_account SET password_hash=%s WHERE staff_id=%s", (password_hash, staff_id))


def set_active(staff_id, enabled):
    # 직원 계정을 활성화하거나 비활성화한다.
    run("UPDATE staff_account SET is_active=%s WHERE staff_id=%s", (enabled, staff_id))


def quota(staff_id):
    # 상담원의 일·주·월별 목표를 조회한다.
    return one("SELECT daily,weekly,monthly FROM agent_quota WHERE staff_id=%s", (staff_id,))


def set_quota(staff_id, daily, weekly, monthly):
    # 상담원의 일·주·월별 목표를 저장한다.
    run(
        "REPLACE INTO agent_quota (staff_id,daily,weekly,monthly) VALUES (%s,%s,%s,%s)",
        (staff_id, daily, weekly, monthly),
    )


def teams():
    # 팀 목록을 이름순으로 조회한다.
    return rows("SELECT team_id,name FROM staff_team ORDER BY name")


def team_name(team_id):
    # 팀 ID로 이름을 조회한다.
    return one("SELECT name FROM staff_team WHERE team_id=%s", (team_id,))


def lock_team(team_id):
    # 수정할 팀을 잠그고 현재 이름을 조회한다.
    return one("SELECT name FROM staff_team WHERE team_id=%s FOR UPDATE", (team_id,))


def create_team(team_id, name):
    # 새 팀을 등록한다.
    run("INSERT INTO staff_team (team_id,name) VALUES (%s,%s)", (team_id, name))


def rename_team(team_id, name):
    # 팀 이름을 바꾼다.
    run("UPDATE staff_team SET name=%s WHERE team_id=%s", (name, team_id))


def team_roster():
    # 상담원별 소속 팀과 계정 활성 여부를 조회한다.
    return rows(
        "SELECT staff_id,login_id,display_name,team_id,is_active FROM staff_account WHERE role='AGENT' ORDER BY login_id",
    )


def lock_team_memberships():
    # 팀 배정을 변경하기 전에 상담원 행을 ID순으로 잠근다.
    return rows("SELECT staff_id,team_id FROM staff_account WHERE role='AGENT' ORDER BY staff_id FOR UPDATE")


def set_team(staff_id, team_id):
    # 직원의 소속 팀을 지정하거나 해제한다.
    run("UPDATE staff_account SET team_id=%s WHERE staff_id=%s", (team_id, staff_id))


def notices(latest=False):
    # 공지를 최신순으로 조회하고 필요하면 한 건만 가져온다.
    return rows("SELECT n.notice_id,n.title,n.body,n.published_at,COALESCE(a.display_name,a.login_id) AS author "
                "FROM staff_notice n JOIN staff_account a ON a.staff_id=n.author_id ORDER BY published_at DESC,notice_id DESC"
                + (" LIMIT 1" if latest else ""))


def notice_by_id(notice_id):
    # 공지의 작성자, 제목과 본문을 조회한다.
    return one("SELECT author_id,title,body FROM staff_notice WHERE notice_id=%s", (notice_id,))


def publish_notice(notice_id, author_id, title, body):
    # 작성자와 함께 새 공지를 게시한다.
    run(
        "INSERT INTO staff_notice (notice_id,author_id,title,body) VALUES (%s,%s,%s,%s)",
        (notice_id, author_id, title, body),
    )


def add_admin_change(actor_id, action, target, case_id):
    # 관리자가 변경한 대상과 작업을 기록한다.
    run(
        "INSERT INTO admin_change (actor_id, action, target, case_id) VALUES (%s,%s,%s,%s)",
        (actor_id, action, target, case_id),
    )


def any_account():
    # 초기 계정을 만들기 전에 직원 계정이 있는지 확인한다.
    return one("SELECT staff_id FROM staff_account LIMIT 1")


def seed_account(staff_id, login_id, password_hash, role):
    # 데모 직원을 지정한 권한으로 등록한다.
    if role == "AGENT":
        return create_agent(staff_id, login_id, None, password_hash)
    run(
        "INSERT INTO staff_account (staff_id,login_id,password_hash,role) VALUES (%s,%s,%s,%s)",
        (staff_id, login_id, password_hash, role),
    )
