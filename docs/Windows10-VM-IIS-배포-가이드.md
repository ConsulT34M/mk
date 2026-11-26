# 말결 TM Windows 10 VM IIS 배포 가이드

이 문서는 말결 TM을 Docker 없이 Windows 10 VM 한 대에 설치하고 `https://localhost`로 제공하는 절차를 설명한다. 신규 MySQL 데이터베이스를 만드는 경우를 기준으로 하며, 실제 설치 중 확인된 오류와 해결 방법까지 포함한다.

완성된 구성은 다음과 같다.

```text
브라우저 https://localhost
  → IIS 10 HTTPS 443
  → URL Rewrite 2.1과 ARR 3.0
  → Waitress 127.0.0.1:8000
  → Flask 앱
  → MySQL 127.0.0.1:3306
```

`app/templates/agent.html`은 IIS의 기본 문서로 직접 여는 정적 HTML이 아니다. Flask가 로그인 사용자와 상담 데이터를 넣어 렌더링하는 Jinja 템플릿이다. 따라서 IIS의 `main.html` 기본 문서 설정은 최종 구성에서 사용하지 않는다.

## 1. 설치 전 준비

### 1.1 필요한 프로그램

- Windows 10 x64 VM
- IIS 10과 IIS 관리 콘솔
- Python 3.12 x64
- MySQL Server 8.x와 MySQL 명령줄 클라이언트
- IIS URL Rewrite 2.1 x64
- IIS Application Request Routing 3.0 x64
- Waitress 3.0.2
- WinSW 2.14.0 x64

Python은 반드시 **모든 사용자용**으로 설치한다. 사용자 전용 경로인 `C:\Users\<사용자>\AppData\Local\Programs\Python`에 설치하면 `LocalService`로 실행하는 Windows 서비스가 Python을 읽지 못할 수 있다. 권장 경로는 `C:\Program Files\Python312`이다.

### 1.2 IIS 활성화

관리자 PowerShell에서 실행한다.

```powershell
Enable-WindowsOptionalFeature -Online -All -FeatureName IIS-WebServerRole
Enable-WindowsOptionalFeature -Online -All -FeatureName IIS-WebServer
Enable-WindowsOptionalFeature -Online -All -FeatureName IIS-ManagementConsole
Enable-WindowsOptionalFeature -Online -All -FeatureName IIS-StaticContent
Enable-WindowsOptionalFeature -Online -All -FeatureName IIS-DefaultDocument
Enable-WindowsOptionalFeature -Online -All -FeatureName IIS-HttpErrors
```

설치 후 VM을 재부팅하고 서비스를 확인한다.

```powershell
Get-Service W3SVC
```

### 1.3 배포 폴더 생성

```powershell
New-Item -ItemType Directory -Force C:\MKDesk\src | Out-Null
New-Item -ItemType Directory -Force C:\MKDesk\svc | Out-Null
New-Item -ItemType Directory -Force C:\MKDesk\logs | Out-Null
New-Item -ItemType Directory -Force C:\MKDesk\recordings | Out-Null
New-Item -ItemType Directory -Force C:\inetpub\wwwroot | Out-Null
```

저장소에서 다음 항목을 `C:\MKDesk\src`로 복사한다.

```text
app\
mysql\
requirements.txt
```

복사 결과를 확인한다.

```powershell
Test-Path C:\MKDesk\src\app\__init__.py
Test-Path C:\MKDesk\src\app\templates\agent.html
Test-Path C:\MKDesk\src\mysql\schema.sql
Test-Path C:\MKDesk\src\requirements.txt
```

네 결과가 모두 `True`여야 한다.

## 2. Python 환경 구성

### 2.1 모든 사용자용 Python 확인

```powershell
Test-Path 'C:\Program Files\Python312\python.exe'
& 'C:\Program Files\Python312\python.exe' --version
```

사용자 전용 Python만 있다면 Python 3.12 설치 프로그램에서 `Customize installation → Install for all users`를 선택해 다시 설치한다.

### 2.2 가상환경 생성

```powershell
& 'C:\Program Files\Python312\python.exe' -m venv C:\MKDesk\venv
$mkPython = 'C:\MKDesk\venv\Scripts\python.exe'
```

기반 Python을 확인한다.

```powershell
Get-Content C:\MKDesk\venv\pyvenv.cfg
```

`home`이 `C:\Program Files\Python312`를 가리켜야 한다.

### 2.3 Windows용 패키지 설치

저장소의 `requirements.txt`에는 Linux용 Gunicorn이 포함되어 있으므로 Windows에서는 제외하고 Waitress를 설치한다.

```powershell
Set-Location C:\MKDesk\src
& $mkPython -m pip install --upgrade pip
$mkDeps = @(
    Get-Content .\requirements.txt |
    Where-Object { $_.Trim() -and $_ -notmatch '^\s*(#|gunicorn\b)' }
)
& $mkPython -m pip install @mkDeps 'waitress==3.0.2'
& $mkPython -m pip check
```

## 3. MySQL 구성

### 3.1 MySQL 서비스와 클라이언트 위치 확인

```powershell
Get-Service |
    Where-Object { $_.Name -match 'mysql' -or $_.DisplayName -match 'mysql' } |
    Format-Table Name, DisplayName, Status
```

서비스 이름은 설치 버전과 다를 수 있다. 실제 사례에서는 서버가 MySQL 8.0.45였지만 서비스 이름은 `MySQL84`였다. 이후 WinSW의 `<depend>`에는 버전이 아니라 실제 서비스 이름을 사용한다.

`mysql.exe` 위치를 검색한다.

```powershell
Get-ChildItem `
    'C:\Program Files\MySQL', `
    'C:\Program Files (x86)\MySQL' `
    -Filter mysql.exe `
    -File `
    -Recurse `
    -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty FullName
```

MySQL Workbench에 포함된 클라이언트도 사용할 수 있다.

```powershell
$mysql = 'C:\Program Files\MySQL\MySQL Workbench 8.0\mysql.exe'
Test-Path $mysql
```

### 3.2 root로 접속

비밀번호를 명령줄에 직접 쓰지 않는다. `--password=1234` 형태는 PowerShell 기록과 프로세스 정보에 비밀번호가 남는다.

```powershell
& $mysql `
    --host=127.0.0.1 `
    --user=root `
    --password `
    --default-character-set=utf8mb4
```

### 3.3 DB와 앱 계정 생성

MySQL 프롬프트에서 실행한다. 비밀번호는 실제 강력한 값으로 바꾸고 작은따옴표로 감싼다.

```sql
CREATE DATABASE IF NOT EXISTS tm_portal
CHARACTER SET utf8mb4
COLLATE utf8mb4_0900_ai_ci;

CREATE USER IF NOT EXISTS 'tm_app'@'127.0.0.1'
IDENTIFIED BY '강력한_tm_app_비밀번호';

ALTER USER 'tm_app'@'127.0.0.1'
IDENTIFIED BY '강력한_tm_app_비밀번호';

GRANT SELECT, INSERT, UPDATE, DELETE
ON tm_portal.*
TO 'tm_app'@'127.0.0.1';

USE tm_portal;
SOURCE C:/MKDesk/src/mysql/schema.sql;
SHOW TABLES;
```

`IDENTIFIED BY test123`처럼 따옴표 없이 입력하면 `ERROR 1064`가 발생한다. 올바른 형식은 `IDENTIFIED BY 'test123'`이다. 단, 짧고 노출된 비밀번호는 실제 환경에서 사용하지 않는다.

### 3.4 추가 스키마 적용

MySQL을 `exit`로 종료하고 관리자 PowerShell에서 실행한다.

```powershell
Set-Location C:\MKDesk\src
$mkPython = 'C:\MKDesk\venv\Scripts\python.exe'
$env:MYSQL_HOST = '127.0.0.1'
$env:MYSQL_DATABASE = 'tm_portal'
$env:MYSQL_USER = 'root'
$env:MYSQL_PASSWORD = [Net.NetworkCredential]::new(
    '',
    (Read-Host 'MySQL root 비밀번호' -AsSecureString)
).Password
& $mkPython -m flask --app app init-admin
$LASTEXITCODE
```

`init-admin`은 성공해도 메시지를 출력하지 않는다. `$LASTEXITCODE`가 `0`이면 정상이다.

### 3.5 최초 관리자 생성

root 환경변수를 제거하고 앱 계정으로 전환한다.

```powershell
Remove-Item Env:MYSQL_PASSWORD -ErrorAction SilentlyContinue
Remove-Item Env:MYSQL_USER -ErrorAction SilentlyContinue
$env:MYSQL_HOST = '127.0.0.1'
$env:MYSQL_DATABASE = 'tm_portal'
$env:MYSQL_USER = 'tm_app'
$env:MYSQL_PASSWORD = [Net.NetworkCredential]::new(
    '',
    (Read-Host 'tm_app 비밀번호' -AsSecureString)
).Password
& $mkPython -m flask --app app shell
```

Flask 셸에서 실행한다.

```python
from getpass import getpass
from werkzeug.security import generate_password_hash
from app.db import db, new_id, one, run

if one("SELECT 1 FROM staff_account WHERE role='ADMIN' LIMIT 1"):
    print("기존 관리자 계정을 사용하세요.")
else:
    password = getpass("mkadmin 비밀번호: ")
    password_check = getpass("비밀번호 확인: ")
    if password != password_check:
        raise ValueError("비밀번호가 일치하지 않습니다.")
    run(
        "INSERT INTO staff_account "
        "(staff_id, login_id, password_hash, role) "
        "VALUES (%s, 'mkadmin', %s, 'ADMIN')",
        (new_id(), generate_password_hash(password)),
    )
    db().commit()

print(one("SELECT login_id,role,is_active FROM staff_account WHERE login_id='mkadmin'"))
exit()
```

`NameError: name 'run' is not defined`가 나오면 INSERT는 실행되지 않은 것이다. `from app.db import db, new_id, one, run`을 실행한 뒤 INSERT와 `db().commit()`을 다시 실행한다.

## 4. 앱 설정과 Waitress 시험

### 4.1 settings.json 생성

관리자 PowerShell에서 실행한다.

```powershell
$mkDbPassword = [Net.NetworkCredential]::new(
    '',
    (Read-Host 'tm_app 비밀번호' -AsSecureString)
).Password
$mkSettings = [ordered]@{
    MYSQL_HOST = '127.0.0.1'
    MYSQL_DATABASE = 'tm_portal'
    MYSQL_USER = 'tm_app'
    MYSQL_PASSWORD = $mkDbPassword
    PARTNER_TOKEN = ([guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N'))
    RECORDING_DIR = 'C:\MKDesk\recordings'
    CASE_RETENTION_MINUTES = '1440'
    OPENAI_API_KEY = ''
}
$mkSettings | ConvertTo-Json |
    Set-Content C:\MKDesk\svc\settings.json -Encoding UTF8
```

`settings.json`은 비밀번호를 포함하므로 IIS 공개 폴더에 두지 않는다.

### 4.2 run.py 생성

`C:\MKDesk\svc\run.py`에 다음 내용을 저장한다.

```python
import json
import os
import sys
import time
from pathlib import Path

service_dir = Path(__file__).resolve().parent
settings = json.loads((service_dir / "settings.json").read_text(encoding="utf-8-sig"))
os.environ.update(settings)
sys.path.insert(0, str(service_dir.parent / "src"))

from app import app

if len(sys.argv) < 2:
    raise SystemExit("web 또는 purge 모드를 지정하세요.")

if sys.argv[1] == "web":
    from waitress import serve
    serve(app, listen="127.0.0.1:8000", url_scheme="https")
elif sys.argv[1] == "purge":
    from app.recordings import purge
    while True:
        with app.app_context():
            purge()
        time.sleep(30)
else:
    raise SystemExit("web 또는 purge 모드를 지정하세요.")
```

### 4.3 수동 실행과 내부 상태 확인

```powershell
& $mkPython -m py_compile C:\MKDesk\svc\run.py
& $mkPython -B -u C:\MKDesk\svc\run.py web
```

별도 PowerShell에서 확인한다.

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

`status: ok`가 나오면 Flask와 MySQL 연결이 정상이다. 로그인은 Secure 쿠키를 사용하므로 이 HTTP 주소에서는 시험하지 않는다.

## 5. URL Rewrite와 ARR 설치

IIS 관리자를 종료하고 Microsoft 공식 x64 설치 파일을 다음 순서로 설치한다.

1. URL Rewrite 2.1 x64
2. Application Request Routing 3.0 x64

`Start-Process -Verb RunAs`와 상대 경로를 함께 쓰면 관리자 프로세스의 작업 폴더가 바뀌어 “설치 패키지를 열 수 없음” 오류가 날 수 있다. 절대 경로를 사용한다.

```powershell
$rewriteMsi = Get-ChildItem "$env:USERPROFILE\Downloads" `
    -Filter 'rewrite_*amd64*.msi' |
    Select-Object -First 1
Test-Path $rewriteMsi.FullName
Unblock-File -LiteralPath $rewriteMsi.FullName
& msiexec.exe /i $rewriteMsi.FullName
```

ARR도 같은 방식으로 실제 MSI 절대 경로를 사용한다. 설치 후 확인한다.

```powershell
iisreset
& "$env:windir\System32\inetsrv\appcmd.exe" list modules |
    Select-String RewriteModule
& "$env:windir\System32\inetsrv\appcmd.exe" `
    list config /section:system.webServer/proxy
```

ARR 설치 전에는 `Unknown config section "system.webServer/proxy"`가 발생한다. URL Rewrite를 먼저 설치한 뒤 ARR을 설치하면 해결된다.

프록시를 활성화한다.

```powershell
$mkAppCmd = "$env:windir\System32\inetsrv\appcmd.exe"
& $mkAppCmd set config `
    /section:system.webServer/proxy `
    /enabled:true `
    /preserveHostHeader:true `
    /commit:apphost
```

## 6. localhost HTTPS 구성

### 6.1 인증서 생성과 신뢰 등록

```powershell
$mkCertificate = New-SelfSignedCertificate `
    -DnsName 'localhost' `
    -CertStoreLocation 'Cert:\LocalMachine\My' `
    -FriendlyName 'MKDesk localhost' `
    -KeyAlgorithm RSA `
    -KeyLength 2048 `
    -HashAlgorithm SHA256 `
    -KeyExportPolicy Exportable `
    -NotAfter (Get-Date).AddYears(3)

Export-Certificate `
    -Cert $mkCertificate `
    -FilePath C:\MKDesk\svc\MKDesk-localhost.cer `
    -Force | Out-Null

Import-Certificate `
    -FilePath C:\MKDesk\svc\MKDesk-localhost.cer `
    -CertStoreLocation Cert:\LocalMachine\Root
```

### 6.2 IIS HTTPS 바인딩

IIS 관리자에서 `Default Web Site → 바인딩`에 다음 HTTPS 바인딩을 추가한다.

```text
종류: https
IP 주소: 127.0.0.1
포트: 443
호스트 이름: 빈칸
SNI: 체크 해제
SSL 인증서: MKDesk localhost
```

localhost가 IPv6 `::1`로 해석되는 VM에서는 같은 인증서로 IPv6 루프백 바인딩도 추가하거나 hosts 파일에서 `127.0.0.1 localhost`를 우선시한다. 외부 인터페이스에 공개하지 않으려면 `할당되지 않은 모든 IP`보다 루프백 바인딩을 사용한다.

### 6.3 web.config 구성

기존 `main.html` 기본 문서용 `web.config`를 백업한다.

```powershell
Copy-Item C:\inetpub\wwwroot\web.config `
    C:\inetpub\wwwroot\web.config.main-html-backup `
    -ErrorAction SilentlyContinue
```

`C:\inetpub\wwwroot\web.config`를 다음 내용으로 교체한다.

```xml
<?xml version="1.0" encoding="utf-8"?>
<configuration>
  <system.webServer>
    <rewrite>
      <rules>
        <clear />
        <rule name="MKDesk proxy all requests" stopProcessing="true">
          <match url=".*" />
          <action type="Rewrite"
                  url="http://127.0.0.1:8000/{R:0}"
                  appendQueryString="true" />
        </rule>
      </rules>
    </rewrite>
    <httpErrors existingResponse="PassThrough" />
  </system.webServer>
</configuration>
```

IIS 사이트 경로가 `%SystemDrive%\inetpub\wwwroot`로 표시될 때 `Test-Path`는 환경변수를 자동 확장하지 않아 거짓 `False`를 낼 수 있다. 실제 경로로 변환해 확인한다.

```powershell
$site = Get-Website -Name 'Default Web Site'
$sitePath = [Environment]::ExpandEnvironmentVariables($site.PhysicalPath)
Test-Path (Join-Path $sitePath 'web.config')
```

규칙 로드를 확인한다.

```powershell
& "$env:windir\System32\inetsrv\appcmd.exe" `
    list config "Default Web Site" `
    /section:system.webServer/rewrite/rules
iisreset
```

### 6.4 HTTPS 전체 경로 확인

```powershell
curl.exe -vk `
    --resolve localhost:443:127.0.0.1 `
    https://localhost/health
```

정상 응답에는 다음 항목이 나타난다.

```text
HTTP/1.1 200 OK
X-Powered-By: ARR/3.0
{"status":"ok"}
```

`Invoke-RestMethod https://localhost/health`가 TLS 오류를 내더라도 위 curl 요청이 200이고 브라우저가 접속된다면 서버 경로는 정상이다. 이는 Windows PowerShell 클라이언트의 인증서 또는 TLS 처리 문제일 수 있다.

브라우저에서 `https://localhost/`에 접속해 `mkadmin`으로 로그인한다. 관리자 화면에서 상담원을 생성한 뒤 상담원으로 로그인해 `/portal` 화면을 확인한다.

## 7. Windows 서비스 등록

### 7.1 LocalService 권한

```powershell
icacls C:\MKDesk\src /grant '*S-1-5-19:(OI)(CI)RX'
icacls C:\MKDesk\venv /grant '*S-1-5-19:(OI)(CI)RX'
icacls C:\MKDesk\svc /grant '*S-1-5-19:(OI)(CI)RX'
icacls C:\MKDesk\logs /grant '*S-1-5-19:(OI)(CI)M'
icacls C:\MKDesk\recordings /grant '*S-1-5-19:(OI)(CI)M'
```

### 7.2 WinSW 서비스 생성

WinSW 2.14.0의 `WinSW-x64.exe`를 `C:\MKDesk\svc`에 저장한다. 관리자 PowerShell에서 실행한다.

```powershell
Set-Location C:\MKDesk\svc
$mkDbService = 'MySQL84'
Get-Service $mkDbService -ErrorAction Stop | Out-Null

foreach ($mode in 'web','purge') {
    $name = if ($mode -eq 'web') { 'MKDeskWeb' } else { 'MKDeskPurge' }
    Copy-Item .\WinSW-x64.exe ".\$name.exe" -Force
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
```

수동 Waitress가 포트 8000을 사용 중이면 서비스를 시작할 수 없다. 소유 프로세스를 확인한다.

```powershell
$mkPort = Get-NetTCPConnection `
    -LocalAddress 127.0.0.1 `
    -LocalPort 8000 `
    -State Listen `
    -ErrorAction SilentlyContinue

$mkPort | Select-Object LocalAddress,LocalPort,OwningProcess
Get-CimInstance Win32_Process `
    -Filter "ProcessId=$($mkPort.OwningProcess)" |
    Select-Object ProcessId,Name,ExecutablePath,CommandLine
```

명령줄에 `C:\MKDesk\svc\run.py web`이 확인되면 해당 실행 창에서 `Ctrl+C`로 종료한다. 창을 찾을 수 없을 때만 확인된 PID를 `Stop-Process -Id <PID>`로 종료한다.

서비스를 시작한다.

```powershell
Start-Service MKDeskWeb
Start-Service MKDeskPurge
Get-Service MySQL84,MKDeskWeb,MKDeskPurge,W3SVC |
    Format-Table Name,Status,StartType
curl.exe -k https://localhost/health
```

## 8. 최종 검증

다음 항목을 순서대로 확인한다.

- `MySQL84`, `MKDeskWeb`, `MKDeskPurge`, `W3SVC`가 `Running`
- `http://127.0.0.1:8000/health`가 `ok`
- `https://localhost/health`가 `ok`
- `mkadmin` 로그인 후 `/admin` 이동
- 상담원 로그인 후 `/portal` 이동
- `/inquiry`에서 테스트 채팅 생성
- 상담원 화면에서 티켓 수신과 종결 확인
- 녹취 사용 시 파일 저장과 재생 확인

마지막으로 VM을 재부팅한다.

```powershell
Restart-Computer
```

재부팅 후 서비스와 브라우저 접속을 다시 확인한다. 모두 정상이라면 배포가 완료된 것이다.

## 9. 실제 발생 오류와 해결 요약

| 증상 | 원인 | 해결 |
|---|---|---|
| `mysql.exe`를 찾을 수 없음 | 가이드의 MySQL Server 경로와 실제 클라이언트 위치가 다름 | `Get-ChildItem`로 검색하고 Workbench의 `mysql.exe` 등 실제 절대 경로 사용 |
| `CREATE USER ... IDENTIFIED BY test123`에서 `ERROR 1064` | 비밀번호 문자열 따옴표 누락 | `IDENTIFIED BY '비밀번호'` 사용 |
| `init-admin` 실행 후 아무 출력 없음 | 성공 시 출력하지 않는 명령 | 즉시 `$LASTEXITCODE` 확인, `0`이면 성공 |
| Flask 셸에서 `NameError: run is not defined` | DB 함수 import 누락 | `from app.db import db, new_id, one, run` 실행 후 INSERT 재시도 |
| `system.webServer/proxy`를 알 수 없음 | ARR 미설치 | URL Rewrite 2.1 설치 후 ARR 3.0 설치 |
| MSI 패키지를 열 수 없음 | 관리자 실행 시 상대 경로의 작업 폴더가 바뀜 | MSI 절대 경로와 `msiexec.exe /i <절대경로>` 사용 |
| `WebConfig=False`인데 파일은 존재 | `%SystemDrive%`가 `Test-Path`에서 자동 확장되지 않음 | `ExpandEnvironmentVariables`로 실제 경로 변환 |
| IIS `/health`가 404 | Rewrite 조건 또는 다른 바인딩으로 요청 유입 | 조건 없는 최소 프록시 규칙으로 분리 시험, 사이트 경로와 바인딩 확인 |
| HTTPS 연결이 갑자기 닫힘 | SSL 인증서가 IP:포트에 연결되지 않았거나 TLS 클라이언트 문제 | `netsh http show sslcert`, IIS 바인딩에서 인증서 재선택, `curl -vk`로 확인 |
| curl의 `--resolve`만 성공하고 브라우저 실패 | 브라우저가 `localhost`를 IPv6 `::1`로 해석 | IPv6 루프백 바인딩 추가 또는 hosts에서 IPv4 localhost 우선 |
| `Invoke-RestMethod`만 TLS 오류 | Windows PowerShell 인증서/TLS 처리 차이 | 브라우저와 `curl.exe`로 서버 검증, 필요 시 인증서를 LocalMachine Root에 등록 |
| 서비스 시작 전 포트 8000이 계속 Listen | 수동 Waitress 프로세스가 실행 중 | PID와 명령줄을 확인한 뒤 `Ctrl+C` 또는 해당 PID만 종료 |
| 서비스에서 Python을 실행하지 못함 | 가상환경이 사용자 전용 Python을 참조 | Python을 모든 사용자용으로 설치하고 가상환경 재생성 |
| 로그인 POST가 403 | 외부 Host 또는 스킴이 Flask가 보는 값과 다름 | ARR `preserveHostHeader=true`, Waitress `url_scheme="https"` 확인 |
| IIS 502 | Waitress 미실행 또는 8000 연결 실패 | `MKDeskWeb` 상태와 `C:\MKDesk\logs\MKDeskWeb.err.log` 확인 |
| IIS 500.19 | URL Rewrite 미설치 또는 `web.config` XML 오류 | 모듈 설치와 `appcmd list config` 확인 |

## 10. 업데이트와 운영 시 주의 사항

새 버전을 배포할 때는 다음 순서를 사용한다.

1. 요청 유입을 중지한다.
2. `MKDeskWeb`과 `MKDeskPurge`를 중지한다.
3. DB와 `C:\MKDesk\recordings`를 같은 시점에 백업한다.
4. `app`, `mysql`, `requirements.txt`를 갱신한다.
5. 가상환경 의존성을 갱신한다.
6. DBA 계정으로 `flask --app app init-admin`을 실행한다.
7. 서비스를 시작한다.
8. 내부와 HTTPS `/health`를 확인한다.
9. 관리자, 상담원, 문의, 녹취 흐름을 확인한다.

`settings.json`과 인증서 개인 키, MySQL 비밀번호를 저장소에 커밋하지 않는다. 운영 도메인을 사용하게 되면 자체 서명 localhost 인증서를 공인 인증서로 교체하고 IIS 바인딩과 DNS를 실제 도메인에 맞춘다.
