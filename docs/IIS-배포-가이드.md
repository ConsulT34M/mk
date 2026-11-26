# 말결 TM을 IIS에 배포하기

Windows Server 2022 x64 한 대에서 IIS 10, Flask, MySQL 8.4를 운영하는 예시다. 외부 접속 주소는 `https://desk.example.com`으로 표기했다. 실제 도메인과 그 도메인의 인증서를 준비한다.

```text
브라우저 → IIS (HTTPS 443) → Waitress / Flask (127.0.0.1:8000) → MySQL (127.0.0.1:3306)
```

Windows 서비스 `MKDeskWeb`은 직원·고객 화면을 제공한다. `MKDeskPurge`는 삭제 요청을 받은 녹취와 만료된 상담 내용·세션을 30초 간격으로 정리하고, 별도로 새 AI 녹취의 요약 요청을 처리한다. 아래 명령은 대상 서버의 관리자 권한 Windows PowerShell에서 실행한다.

## 1. 서버와 소스 준비

1. `Install-WindowsFeature Web-Server -IncludeManagementTools`로 IIS를 설치한다. 이어서 x64용 URL Rewrite 2.1, ARR 3.0을 이 순서로 설치한다. [URL Rewrite 다운로드](https://www.iis.net/downloads/microsoft/url-rewrite) · [ARR 다운로드](https://www.iis.net/downloads/microsoft/application-request-routing)
2. Python 3.12 x64를 모든 사용자용으로 설치하고 `py -3.12 --version`을 확인한다. MySQL Server 8.4 x64와 Configurator를 설치한다. MySQL은 TCP 3306, Windows 서비스 자동 시작으로 설정하고 실제 서비스 이름을 기록한다. 이 가이드에서는 `MySQL84`를 사용한다. 같은 서버의 앱만 DB를 쓰므로 MySQL 설정 파일 `my.ini`의 `[mysqld]`에 `bind-address=127.0.0.1`을 적용한다. 방화벽에는 IIS의 443만 공개한다. [MySQL Windows 설치](https://dev.mysql.com/doc/refman/8.4/en/windows-installation.html)
3. 새 전용 경로에 아래 폴더를 만든다. `C:\MKDesk`는 관리자·SYSTEM이 관리하고, 서비스 계정 `LocalService`는 소스와 설정을 읽고 로그·녹취에 쓸 수 있게 한다. IIS의 공개 폴더는 `C:\inetpub\MKDesk`다.

```powershell
New-Item -ItemType Directory -Path C:\MKDesk -ErrorAction Stop | Out-Null
icacls C:\MKDesk /inheritance:r /grant:r '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-18:(OI)(CI)F' '*S-1-5-19:(OI)(CI)RX'
if ($LASTEXITCODE -ne 0) { throw 'MKDesk 폴더 권한을 확인하세요.' }
'src','svc','logs','recordings' | ForEach-Object {
    New-Item -ItemType Directory -Path (Join-Path C:\MKDesk $_) | Out-Null
}
icacls C:\MKDesk\logs /grant:r '*S-1-5-19:(OI)(CI)M'
icacls C:\MKDesk\recordings /grant:r '*S-1-5-19:(OI)(CI)M'
New-Item -ItemType Directory -Path C:\inetpub\MKDesk | Out-Null
```

배포할 `mk` 버전의 `app`, `mysql` 폴더와 `requirements.txt`를 `C:\MKDesk\src`에 복사한다. `C:\MKDesk\src\app\__init__.py`가 보여야 한다. Windows에서 Flask를 실행할 WSGI 서버는 Waitress다. [Flask의 Windows 배포 안내](https://flask.palletsprojects.com/en/stable/deploying/waitress/)

```powershell
Set-Location C:\MKDesk\src
py -3.12 -m venv C:\MKDesk\venv
$mkPython = 'C:\MKDesk\venv\Scripts\python.exe'
$mkDeps = @(Get-Content .\requirements.txt | Where-Object { $_.Trim() -and $_ -notmatch '^\s*(#|gunicorn\b)' })
& $mkPython -m pip install @mkDeps 'waitress==3.0.2'
& $mkPython -m pip check
```

## 2. DB 초기화 또는 기존 데이터 이전

MySQL 클라이언트를 열고 DBA 계정으로 접속한다.

```powershell
& 'C:\Program Files\MySQL\MySQL Server 8.4\bin\mysql.exe' --host=127.0.0.1 --user=root --password --default-character-set=utf8mb4
```

새 서버의 MySQL 프롬프트에서 DB와 앱 전용 계정을 만든다. `CREATE USER` 결과의 임의 비밀번호는 3단계에서 사용한다. [MySQL 계정 생성](https://dev.mysql.com/doc/refman/8.4/en/create-user.html)

```sql
CREATE DATABASE tm_portal CHARACTER SET utf8mb4;
CREATE USER 'tm_app'@'127.0.0.1' IDENTIFIED BY RANDOM PASSWORD;
GRANT SELECT, INSERT, UPDATE, DELETE ON tm_portal.* TO 'tm_app'@'127.0.0.1';
USE tm_portal;
```

| 설치 상황 | 여기서 할 일 |
|---|---|
| 신규 설치 | MySQL 프롬프트에서 `SOURCE C:/MKDesk/src/mysql/schema.sql`을 한 번 실행한다. |
| 기존 `mk` 이전 | 원본의 앱 쓰기와 만료 정리를 멈춘 시점에 `tm_portal` 덤프와 녹취를 함께 백업한다. 대상의 빈 DB에 덤프를 복원한다. 원본 앱에서 보이던 `/var/lib/tm-recordings/`의 전체 내용을 `C:\MKDesk\recordings\`에 같은 상대 경로로 복사한다. `files/` 아래 WAV 외에도 이전 형식의 키와 진행 중인 `.part`가 있을 수 있다. |

두 경우 모두 MySQL 프롬프트에서 `exit`로 나온다. 새 PowerShell 창에서 다음 명령으로 뷰·조직·공지·통계 스키마를 적용한다. `init-admin`에는 DDL 권한이 필요하므로 이 창에서만 DBA 자격을 사용한다.

```powershell
Set-Location C:\MKDesk\src
$mkPython = 'C:\MKDesk\venv\Scripts\python.exe'
$env:MYSQL_HOST = '127.0.0.1'
$env:MYSQL_DATABASE = 'tm_portal'
$env:MYSQL_USER = 'root'
$env:MYSQL_PASSWORD = [Net.NetworkCredential]::new('', (Read-Host 'MySQL DBA 비밀번호' -AsSecureString)).Password
& $mkPython -m flask --app app init-admin
if ($LASTEXITCODE -ne 0) { throw '스키마 적용 오류를 먼저 확인하세요.' }
```

신규 설치에서는 같은 창에서 `& $mkPython -m flask --app app shell`을 실행한 뒤 아래 Python 코드를 입력한다. 첫 관리자 아이디는 `mkadmin`이다. `seed-demo`는 시연 계정과 샘플 상담을 생성하므로 운영 초기화에서는 제외한다. 기존 DB를 복원했다면 복원한 관리자 계정을 사용한다.

```python
from getpass import getpass
from werkzeug.security import generate_password_hash
from app.db import db, new_id, one, run

if one("SELECT 1 FROM staff_account WHERE role='ADMIN' LIMIT 1"):
    print("기존 관리자 계정을 사용하세요.")
else:
    password = getpass("새 관리자 비밀번호: ")
    if not 1 <= len(password) <= 512 or password != getpass("비밀번호 확인: "):
        raise ValueError("비밀번호 길이 또는 확인 입력을 확인하세요.")
    run("INSERT INTO staff_account (staff_id,login_id,password_hash,role) VALUES (%s,'mkadmin',%s,'ADMIN')",
        (new_id(), generate_password_hash(password)))
    db().commit()

exit()
```

DB 작업을 마치면 이 PowerShell 창을 닫는다. 관리자 로그인 후 `계정 관리 → 상담원 추가`에서 실제 상담원 계정을 만든다. 기존 시연 DB를 이전했다면 서비스 공개 전에 시연 관리자 비밀번호를 교체하고 사용하지 않을 시연 상담원은 비활성화한다.

## 3. 서비스 설정과 보관 기간

새 관리자 PowerShell 창에서 다음 블록을 실행한다. 앱 전용 DB 비밀번호와 파트너 연동용 비밀 토큰을 입력한다. 기존 거래소 연동을 이어갈 때는 거래소가 사용 중인 `PARTNER_TOKEN`을 입력한다. 신규 연동이면 비밀번호 관리 도구에서 임의 토큰을 생성해 거래소와 같은 값으로 설정한다.

녹취와 요약은 관리자 삭제 또는 동의 철회 시까지 보관한다. 종결 상담 본문과 메모는 기본 5분간 보관하며, 아래에서 운영 기간을 분 단위로 설정한다. 기존 상담 본문에는 DB에 기록된 만료 시각이 적용된다.

```powershell
function Read-MkSecret([string]$label) {
    [Net.NetworkCredential]::new('', (Read-Host $label -AsSecureString)).Password
}
[int]$mkCaseMinutes = Read-Host '종결 상담 내용 보관 기간(분)'
if ($mkCaseMinutes -lt 1) { throw '양의 분 단위 값을 입력하세요.' }
$mkSettings = [ordered]@{
    MYSQL_HOST = '127.0.0.1'
    MYSQL_DATABASE = 'tm_portal'
    MYSQL_USER = 'tm_app'
    MYSQL_PASSWORD = Read-MkSecret 'tm_app 비밀번호'
    PARTNER_TOKEN = Read-MkSecret '거래소 연동 토큰'
    RECORDING_DIR = 'C:\MKDesk\recordings'
    CASE_RETENTION_MINUTES = [string]$mkCaseMinutes
    OPENAI_API_KEY = Read-MkSecret 'AI 음성 상담 키(미사용 시 Enter)'
}
if (-not $mkSettings.MYSQL_PASSWORD -or -not $mkSettings.PARTNER_TOKEN) { throw 'DB 비밀번호와 연동 토큰이 필요합니다.' }
$mkSettings | ConvertTo-Json | Set-Content C:\MKDesk\svc\settings.json -Encoding UTF8
```

`settings.json`에는 실제 비밀값이 들어간다. 1단계에서 권한을 지정한 `svc` 폴더에 보관한다. 서버에서 `api.openai.com`으로 HTTPS 및 음성 사용량 수집용 WebSocket 접속이 가능해야 한다. 관리자의 ‘AI → AI 설정’에서 OpenAI 키와 채팅·통화·녹취 요약 모델을 변경하면 다음 요청부터 적용된다. 화면에서 등록한 키는 DB와 `RECORDING_DIR\.openai-settings.key`를 함께 백업해야 복원할 수 있다. 서버 기본값은 `OPENAI_API_KEY`, `OPENAI_CHAT_MODEL`, `OPENAI_REALTIME_MODEL`, `OPENAI_CALL_SUMMARY_MODEL`로 지정하며, 이 환경 변수를 변경하면 두 앱 서비스를 다시 시작한다.

## 4. 웹과 만료 정리를 Windows 서비스로 등록

아래 코드를 `C:\MKDesk\svc\run.py`로 저장한다. `web`은 서버 내부에서만 접속할 수 있고, `purge`는 만료 정리와 AI 통화 요약 작업을 실행한다. IIS가 외부 HTTPS를 처리하므로 Waitress가 Flask에 전달하는 요청 스킴은 `https`로 지정한다. [Waitress 프록시 설정](https://docs.pylonsproject.org/projects/waitress/en/latest/reverse-proxy.html)

```python
import json
import os
import sys
from pathlib import Path

svc = Path(__file__).resolve().parent
os.environ.update(json.loads((svc / "settings.json").read_text(encoding="utf-8-sig")))
sys.path.insert(0, str(svc.parent / "src"))

from app import app

if sys.argv[1] == "web":
    from waitress import serve
    serve(app, listen="127.0.0.1:8000", url_scheme="https")
elif sys.argv[1] == "purge":
    from app import run_maintenance
    run_maintenance(app)
else:
    raise SystemExit("web 또는 purge 모드를 지정하세요.")
```

WinSW 2.14.0의 `WinSW-x64.exe`를 `C:\MKDesk\svc`에 저장한다. 아래 블록은 신규 서비스 등록용이다. `$mkDbService`를 실제 MySQL 서비스 이름으로 맞춘다. 두 서비스는 `LocalService`로 자동 시작하고 프로세스 오류 시 30초 후 다시 시작한다. [WinSW 배포 파일](https://github.com/winsw/winsw/releases/tag/v2.14.0) · [WinSW 2.14 설정](https://github.com/winsw/winsw/blob/v2.14.0/doc/xmlConfigFile.md)

```powershell
Set-Location C:\MKDesk\svc
$mkDbService = 'MySQL84'
Get-Service -Name $mkDbService -ErrorAction Stop | Out-Null
foreach ($mode in 'web','purge') {
    $name = if ($mode -eq 'web') { 'MKDeskWeb' } else { 'MKDeskPurge' }
    Copy-Item .\WinSW-x64.exe ".\$name.exe" -ErrorAction Stop
@"
<service>
  <id>$name</id>
  <name>$name</name>
  <description>MKDesk $mode service</description>
  <executable>C:\MKDesk\venv\Scripts\python.exe</executable>
  <arguments>-B -u "%BASE%\run.py" $mode</arguments>
  <workingdirectory>C:\MKDesk\src</workingdirectory>
  <serviceaccount><domain>NT AUTHORITY</domain><user>LocalService</user></serviceaccount>
  <depend>$mkDbService</depend>
  <startmode>Automatic</startmode>
  <onfailure action="restart" delay="30 sec"/>
  <logpath>C:\MKDesk\logs</logpath>
  <log mode="roll-by-size"/>
</service>
"@ | Set-Content ".\$name.xml" -Encoding UTF8
    & ".\$name.exe" install
    if ($LASTEXITCODE -ne 0) { throw "$name 서비스 등록 오류" }
}
Start-Service MKDeskWeb
(Invoke-RestMethod 'http://127.0.0.1:8000/health').status
```

`ok`가 나오면 Flask에서 운영 DB에 연결된 것이다. 서비스 시작 직후 연결이 거부되면 몇 초 뒤 다시 조회한다. 기존 DB 이전 시 백업·복원과 만료 시각 점검까지 마치고 `Start-Service MKDeskPurge`를 실행한다. 신규 설치에서도 이 명령으로 정리를 시작한다.

## 5. IIS에 HTTPS 사이트 연결

1. DNS에서 운영 도메인이 IIS 서버를 가리키게 한다. IIS 관리자에서 사이트 `MKDesk`를 만들고 실제 경로를 `C:\inetpub\MKDesk`로 지정한다. 애플리케이션 풀은 `관리 코드 없음`, 인증은 `익명 인증`으로 설정한다.
2. `바인딩`에서 `https`, 포트 `443`, 실제 호스트 이름과 인증서를 선택한다. 여러 사이트가 한 IP의 443을 쓰면 SNI를 활성화한다. 이 사이트에서는 HTTPS 바인딩만 사용한다. [Microsoft HTTPS 바인딩](https://learn.microsoft.com/en-us/iis/manage/configuring-security/how-to-set-up-ssl-on-iis)
3. 아래 명령으로 ARR 프록시를 켜고 외부 `Host`를 보존한다. 이 설정은 IIS 서버 전체에 적용된다. 다른 사이트와 공유하는 호스트라면 기존 ARR 값을 먼저 확인한다. `mk`는 로그인 등 POST 요청의 `Origin`을 실제 도메인과 스킴에 대조한다. [Microsoft ARR 프록시](https://learn.microsoft.com/en-us/iis/extensions/url-rewrite-module/reverse-proxy-with-url-rewrite-v2-and-application-request-routing) · [Host 보존 설정](https://learn.microsoft.com/en-us/iis/extensions/configuring-application-request-routing-arr/arr-as-generic-proxy-in-hotmail-and-skydrive)

```powershell
$mkAppCmd = "$env:windir\System32\inetsrv\appcmd.exe"
& $mkAppCmd list config /section:system.webServer/proxy
& $mkAppCmd add backup ('MKDesk-' + (Get-Date -Format yyyyMMdd-HHmmss))
& $mkAppCmd set config /section:system.webServer/proxy /enabled:true /preserveHostHeader:true /commit:apphost
```

다음을 `C:\inetpub\MKDesk\web.config`로 저장한다. 사이트 루트의 모든 경로와 쿼리 문자열이 Flask로 전달된다. 로그인, 정적 파일, 녹취 재생도 같은 도메인을 사용한다.

```xml
<?xml version="1.0" encoding="utf-8"?>
<configuration>
  <system.webServer>
    <rewrite>
      <rules>
        <rule name="MKDesk HTTPS proxy" stopProcessing="true">
          <match url="(.*)" />
          <conditions><add input="{HTTPS}" pattern="^ON$" /></conditions>
          <action type="Rewrite" url="http://127.0.0.1:8000/{R:1}" appendQueryString="true" />
        </rule>
      </rules>
    </rewrite>
    <httpErrors existingResponse="PassThrough" />
  </system.webServer>
</configuration>
```

## 6. 배포 확인과 장애 점검

```powershell
Get-Service MySQL84,MKDeskWeb,MKDeskPurge,W3SVC
(Invoke-RestMethod 'http://127.0.0.1:8000/health').status
(Invoke-RestMethod 'https://desk.example.com/health').status
```

실제 도메인으로 바꿔 두 주소 모두 `ok`인지 확인한다. 브라우저에서 `/`로 로그인해 관리자는 `/admin`, 상담원은 `/portal`로 이동하는지 확인한다. 검증용 상담원과 별도 브라우저의 `/inquiry`에서 채팅 한 건을 주고받는다. 녹취를 사용하면 동의한 테스트 통화 한 건으로 저장·재생을 확인한다.

| 증상 | 먼저 확인할 곳 |
|---|---|
| 내부 `/health` 실패 또는 IIS 502 | `Get-Service MKDeskWeb`과 `C:\MKDesk\logs\MKDeskWeb.err.log`를 본다. 내부 주소는 응답하지만 외부만 실패하면 ARR 활성화, Rewrite 대상 포트, HTTPS 바인딩을 확인한다. |
| 로그인 화면은 열리는데 로그인 POST가 403 | 실제 도메인으로 접속했는지, ARR의 `preserveHostHeader`와 Waitress의 `url_scheme="https"`가 적용됐는지 확인한다. |
| IIS 500.19 | URL Rewrite 설치 여부, `web.config` XML, 사이트 풀의 공개 폴더 읽기 권한을 확인한다. |
| WAV 업로드가 크기 제한으로 실패 | 앱의 `WAV_MAX_MIB` 기본값은 10이다. 값을 올리면 IIS `maxAllowedContentLength`도 최소 `WAV_MAX_MIB × 1,048,576 + 131,072`바이트로 맞춘다. [IIS 요청 크기 제한](https://learn.microsoft.com/en-us/iis/configuration/system.webserver/security/requestfiltering/requestlimits/) |
| 녹취 저장·정리 실패 | `RECORDING_DIR`의 수정 권한, `MKDeskPurge`의 실행 상태, `C:\MKDesk\logs\MKDeskPurge.err.log`를 확인한다. |

다음 버전을 배포할 때는 요청 유입을 잠시 멈추고 두 앱 서비스를 중지한 뒤 DB와 녹취를 같은 시점에 백업한다. 소스·의존성을 갱신하고 2단계의 `init-admin`을 실행한 다음 두 서비스를 시작해 위 점검을 반복한다. 스키마까지 되돌려야 한다면 쓰기를 재개하기 전에 이전 소스와 같은 시점의 DB·녹취 백업을 함께 복원한다.
