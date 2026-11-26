# AWS Windows IIS 배포 가이드

이 문서는 로컬 Windows 10 VM에서 검증한 MKDesk 웹 서비스를 AWS의 Windows 환경으로 이전하기 위한 운영 가이드다. 기존 [Windows 10 VM 배포 가이드](./Windows10-VM-IIS-배포-가이드.md)는 수정하지 않으며, 로컬 VM에서 이미 확인한 `IIS → ARR → Waitress → Flask → MySQL` 구조를 AWS 환경에 맞게 확장한다.

## 1. 목표 구성

운영 환경에서는 다음 구성을 권장한다.

```text
사용자 브라우저
    │ HTTPS 443
    ▼
Route 53 또는 외부 DNS
    │
    ▼
Application Load Balancer
  - 공개 서브넷
  - ACM 인증서
  - HTTPS 종료
    │ HTTP 80, 보안 그룹으로 제한
    ▼
Windows Server EC2
  - 비공개 서브넷 권장
  - IIS + URL Rewrite + ARR
  - Waitress 127.0.0.1:8000
  - Flask 애플리케이션
  - MySQL Server 127.0.0.1:3306
  - 웹과 DB를 한 EC2에서 운영
    │
    └─ HTTPS 443, IAM 역할 ───────────────▶ Amazon S3
                                           - 비공개 녹취 버킷
                                           - 암호화 및 수명주기
                                           - 퍼블릭 액세스 차단
```

녹취 원본은 Amazon S3에 저장한다. EC2에는 음성 수집 중인 임시 조각만 두고, 녹취가 완성되면 S3에 업로드한 뒤 로컬 임시 파일을 삭제한다. 데이터베이스에는 파일 자체가 아니라 `files/<recording_id>.wav` 형식의 S3 객체 키와 메타데이터만 저장한다.

## 2. 로컬 VM 구성과 AWS 구성의 차이

| 항목 | 로컬 Windows 10 VM | AWS 권장 구성 |
|---|---|---|
| 운영체제 | Windows 10 | Windows Server 2022 EC2 |
| 외부 HTTPS | IIS의 localhost 인증서 | ALB + ACM 공개 인증서 |
| 웹 진입점 | `https://localhost` | 실제 도메인의 `https://서비스주소` |
| 애플리케이션 서버 | Waitress `127.0.0.1:8000` | 동일 |
| 데이터베이스 | 로컬 MySQL | 같은 Windows EC2의 로컬 MySQL |
| 녹취 저장소 | VM 로컬 디스크 | 비공개 Amazon S3 버킷 |
| 비밀값 | 로컬 설정 파일 | Secrets Manager 또는 Parameter Store 권장 |
| 원격 관리 | VM 콘솔/RDP | Systems Manager 권장, 필요 시 제한된 RDP |
| 모니터링 | 수동 확인 | CloudWatch 로그·메트릭·경보 |
| 백업 | 수동 | MySQL 덤프·EBS 스냅샷, S3 버전 관리·보존 정책 |

로컬 VM에서 검증한 `IIS → Waitress → Flask → MySQL` 구조를 같은 Windows EC2 안에 유지한다. 외부 HTTPS, 접근 제어, S3 저장, 백업과 모니터링만 AWS 환경에 맞게 변경한다.

## 3. 배포 방식 선택

### 3.1 이 가이드의 기준: ALB + 단일 Windows EC2 + S3

Windows 10 VM에서 검증한 구성을 최대한 동일하게 옮기기 위해 다음 방식을 사용한다.

- ALB가 공개 HTTPS를 처리한다.
- 인증서는 AWS Certificate Manager에서 관리한다.
- EC2에는 외부에서 직접 접속할 수 없도록 구성한다.
- Windows Server EC2 한 대에 IIS, Waitress, Flask, MySQL을 모두 설치한다.
- Flask는 `127.0.0.1:3306`으로 같은 서버의 MySQL에 연결한다.
- 녹취 완성본만 S3에 저장한다.
- MySQL 3306과 Waitress 8000은 AWS 보안 그룹에 공개하지 않는다.

### 3.2 이 구성의 한계

단일 EC2 구성은 웹과 DB가 한 장비에 있으므로 EC2 장애 시 두 기능이 함께 중단된다. 다음 보완이 필요하다.

- 암호화된 EBS 사용
- 정기 MySQL 논리 백업과 EBS 스냅샷
- 다른 위치에 백업 사본 보관
- Windows 서비스 자동 시작과 상태 경보
- 장애 시 새 EC2에 복원하는 절차 정기 검증

고가용성이나 무중단 운영이 필요해지면 이후 단계에서 DB를 RDS로 분리할 수 있다. 이 문서의 구축 절차는 분리형이 아니라 단일 Windows EC2를 기준으로 한다.

## 4. 준비 사항

다음을 미리 준비한다.

- AWS 계정과 배포 대상 리전
- 실제 서비스 도메인
- ACM 인증서 검증에 사용할 DNS 권한
- EC2 접속 권한 또는 Systems Manager 사용 권한
- Git 저장소 접근 방법
- MySQL 관리자 계정과 애플리케이션 전용 계정 정책
- 녹취 파일 보존 기간 및 백업 정책
- 운영자가 접근할 고정 공인 IP

AWS 리소스 이름 예시는 다음과 같이 통일하면 관리하기 쉽다.

```text
VPC                 mkdesk-vpc
ALB                 mkdesk-alb
EC2                 mkdesk-app-db-01
ALB 보안 그룹       mkdesk-alb-sg
EC2 보안 그룹       mkdesk-web-sg
대상 그룹           mkdesk-web-tg
```

## 5. VPC와 네트워크 구성

### 5.1 권장 서브넷

최소 2개 가용 영역에 다음 서브넷을 만든다.

```text
가용 영역 A
  - 공개 서브넷: ALB
  - 비공개 애플리케이션 서브넷: Windows EC2

가용 영역 B
  - 공개 서브넷: ALB
  - 비공개 애플리케이션 서브넷: 향후 EC2 확장
```

ALB는 최소 두 개 가용 영역의 공개 서브넷을 지정한다. Windows EC2는 비공개 애플리케이션 서브넷 한 곳에 배치한다.

비공개 EC2가 Windows Update, Python 패키지 설치, Git 접근 등 인터넷 통신을 해야 한다면 NAT Gateway를 사용한다. 비용을 줄인 시험 환경에서는 EC2를 공개 서브넷에 둘 수 있지만, 애플리케이션 포트는 보안 그룹으로 반드시 제한한다.

### 5.2 보안 그룹

#### `mkdesk-alb-sg`

인바운드:

| 프로토콜 | 포트 | 소스 | 목적 |
|---|---:|---|---|
| TCP | 443 | 서비스 이용 대상 대역 또는 `0.0.0.0/0` | 공개 HTTPS |
| TCP | 80 | 필요 시 `0.0.0.0/0` | HTTPS 리디렉션 전용 |

아웃바운드:

| 프로토콜 | 포트 | 대상 |
|---|---:|---|
| TCP | 80 | `mkdesk-web-sg` |

#### `mkdesk-web-sg`

인바운드:

| 프로토콜 | 포트 | 소스 | 목적 |
|---|---:|---|---|
| TCP | 80 | `mkdesk-alb-sg` | ALB에서 IIS로 전달 |
| TCP | 3389 | 관리자 고정 IP `/32` | RDP가 반드시 필요할 때만 |

아웃바운드:

| 프로토콜 | 포트 | 대상 | 목적 |
|---|---:|---|---|
| TCP | 443 | 필요한 AWS 서비스·인터넷 | 업데이트, API, 관리 |

Waitress의 8000 포트와 MySQL의 3306 포트는 보안 그룹에 추가하지 않는다. 두 서비스 모두 `127.0.0.1`에서만 수신하도록 유지한다.

## 6. Windows Server EC2 생성

EC2 인스턴스를 다음 기준으로 생성한다.

- AMI: Microsoft Windows Server 2022 Base
- 인스턴스 유형: 시험은 소형으로 시작하되 CPU·메모리 사용량에 따라 조정
- VPC/서브넷: 비공개 애플리케이션 서브넷 권장
- 보안 그룹: `mkdesk-web-sg`
- IAM 인스턴스 프로파일: Systems Manager, CloudWatch, 필요한 비밀값 읽기 권한
- 루트 EBS: 암호화 활성화
- S3 녹취 버킷 접근 권한이 포함된 IAM 역할 연결
- 종료 방지: 운영 환경에서 활성화 검토

### 7.1 원격 관리

가능하면 AWS Systems Manager Session Manager 또는 Fleet Manager를 사용한다. RDP를 사용할 경우에는 다음 원칙을 지킨다.

- 3389 포트를 인터넷 전체에 공개하지 않는다.
- 관리자 고정 공인 IP의 `/32`만 허용한다.
- 작업 종료 후 RDP 규칙을 제거하는 방식을 고려한다.
- 기본 Administrator 비밀번호를 안전하게 관리한다.

## 7. 같은 EC2에 MySQL Server 설치

Windows 10 VM에서 사용한 것과 같은 방식으로 Windows Server EC2에 MySQL Community Server를 설치한다. 설치 유형에서 MySQL Server가 포함됐는지 확인하고 Windows 서비스로 등록한다.

권장 설정:

```text
서버 유형          Server Computer
TCP/IP 포트        3306
외부 공개          사용하지 않음
Windows 서비스명   MySQL84 또는 설치 시 지정한 이름
서비스 시작 방식   Automatic
문자 집합          utf8mb4
```

MySQL 설정에서 바인드 주소를 `127.0.0.1`로 제한한다. 설치 버전에 따라 `my.ini` 위치가 다르므로 실제 서비스 명령행에서 설정 파일을 확인한다.

```powershell
Get-Service |
    Where-Object {
        $_.Name -match 'mysql' -or
        $_.DisplayName -match 'mysql'
    } |
    Format-Table Name, DisplayName, Status, StartType

Get-CimInstance Win32_Service |
    Where-Object { $_.Name -match 'mysql' } |
    Select-Object Name, State, StartMode, PathName
```

`my.ini`의 네트워크 설정 예시:

```ini
[mysqld]
port=3306
bind-address=127.0.0.1
```

설정 변경 후 실제 서비스 이름으로 재시작한다.

```powershell
Restart-Service MySQL84
```

로컬 수신 상태를 확인한다.

```powershell
Get-NetTCPConnection `
    -LocalAddress 127.0.0.1 `
    -LocalPort 3306 `
    -State Listen
```

`0.0.0.0:3306` 또는 EC2 사설 IP로 수신하면 설정을 다시 확인한다. Windows Defender Firewall에도 외부 3306 허용 규칙을 만들지 않는다.

### 7.1 데이터베이스와 전용 사용자 생성

MySQL 실행 파일 위치를 먼저 찾는다.

```powershell
$mysql = Get-ChildItem 'C:\Program Files\MySQL' `
    -Filter mysql.exe `
    -Recurse `
    -ErrorAction SilentlyContinue |
    Select-Object -First 1 -ExpandProperty FullName

$mysql
Test-Path $mysql
```

서비스 폴더가 아니라 Workbench 폴더에서 `mysql.exe`가 발견될 수도 있다. `Test-Path`가 `True`인 실제 경로를 사용한다.

```powershell
& $mysql `
    --host=127.0.0.1 `
    --port=3306 `
    --user=root `
    --password `
    --default-character-set=utf8mb4
```

MySQL 프롬프트에서 로컬 애플리케이션 전용 사용자를 만든다. 비밀번호는 반드시 따옴표로 감싼다.

```sql
CREATE DATABASE IF NOT EXISTS tm_portal
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_0900_ai_ci;

CREATE USER IF NOT EXISTS 'tm_app'@'127.0.0.1'
  IDENTIFIED BY '충분히-긴-임의의-비밀번호';

GRANT ALL PRIVILEGES ON tm_portal.* TO 'tm_app'@'127.0.0.1';
FLUSH PRIVILEGES;

SHOW GRANTS FOR 'tm_app'@'127.0.0.1';
```

`IDENTIFIED BY test123`처럼 비밀번호의 따옴표를 생략하면 SQL 구문 오류가 발생한다. 애플리케이션에는 root가 아니라 `tm_app@127.0.0.1` 계정을 사용한다.

## 8. Windows 기본 설치

관리자 PowerShell에서 IIS를 설치한다.

```powershell
Install-WindowsFeature `
    Web-Server, `
    Web-Default-Doc, `
    Web-Static-Content, `
    Web-Http-Errors, `
    Web-Http-Logging, `
    Web-Request-Monitor, `
    Web-Mgmt-Console `
    -IncludeManagementTools
```

설치 확인:

```powershell
Get-WindowsFeature Web-Server
Get-Service W3SVC
```

이어서 다음 프로그램을 설치한다.

- Git for Windows
- Python 3.12 64-bit 또는 프로젝트에서 검증한 버전
- IIS URL Rewrite Module 2
- IIS Application Request Routing 3
- MySQL Workbench 또는 MySQL Shell/클라이언트
- WinSW 2.14.0

URL Rewrite와 ARR 설치 파일을 실행할 때 현재 디렉터리의 파일명이 실제 파일과 일치하는지 먼저 확인한다.

```powershell
Get-ChildItem $env:USERPROFILE\Downloads -Filter '*.msi'
```

그 후 절대 경로로 설치한다.

```powershell
$installer = 'C:\Users\Administrator\Downloads\rewrite_amd64_en-US.msi'
Test-Path $installer

Start-Process msiexec.exe `
    -ArgumentList '/i', "`"$installer`"" `
    -Verb RunAs `
    -Wait
```

`설치 패키지를 열 수 없습니다`가 나타나면 파일명, 다운로드 위치, 차단 여부, 파일 크기를 확인한다.

```powershell
Get-Item $installer | Select-Object FullName, Length
Unblock-File $installer
```

ARR 프록시가 활성화됐는지 확인한다.

```powershell
& "$env:windir\System32\inetsrv\appcmd.exe" `
    list config `
    /section:system.webServer/proxy
```

정상 예시는 다음과 같다.

```xml
<proxy enabled="true" preserveHostHeader="true">
```

프록시 섹션을 찾을 수 없다면 ARR 설치 또는 서버 프록시 활성화가 완료되지 않은 것이다.

## 9. 애플리케이션 배치

권장 디렉터리 구조:

```text
C:\MKDesk\src       소스 코드
C:\MKDesk\venv      Python 가상환경
C:\MKDesk\svc       WinSW 서비스 파일
C:\MKDesk\logs      서비스 로그
C:\MKDesk\temp      녹취 완성 전 임시 조각
```

디렉터리를 생성한다.

```powershell
New-Item -ItemType Directory -Force `
    C:\MKDesk\src, `
    C:\MKDesk\svc, `
    C:\MKDesk\logs, `
    C:\MKDesk\temp
```

Git으로 소스를 내려받는다.

```powershell
git clone https://github.com/ConsulT34M/mk.git C:\MKDesk\src
Set-Location C:\MKDesk\src
git switch main
git pull --ff-only origin main
```

비공개 저장소라면 장기 토큰을 서버 파일에 평문 저장하지 말고 승인된 배포 방법을 사용한다.

### 9.1 Python 환경

Python 실행 경로를 먼저 확인한다.

```powershell
Get-Command python
python --version
```

가상환경과 패키지를 설치한다.

```powershell
python -m venv C:\MKDesk\venv

$mkPython = 'C:\MKDesk\venv\Scripts\python.exe'
& $mkPython -m pip install --upgrade pip
& $mkPython -m pip install -r C:\MKDesk\src\requirements.txt
& $mkPython -m pip install waitress
```

Windows에서는 Waitress로 실행한다. `requirements.txt`를 설치하면 `boto3`와 `python-dotenv`도 설치된다.

## 10. 애플리케이션 설정

환경별 비밀값을 Git에 커밋하지 않는다. 운영 환경에서는 Secrets Manager 또는 Systems Manager Parameter Store를 권장한다. 먼저 시험할 때는 서비스 계정만 읽을 수 있는 별도 설정 파일을 사용할 수 있다.

필수 설정 예시:

```text
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_DATABASE=tm_portal
MYSQL_USER=tm_app
MYSQL_PASSWORD=강력한-비밀번호
SECRET_KEY=충분히-긴-임의값
S3_RECORDING_BUCKET=mkdesk-recordings-prod-고유값
AWS_REGION=ap-northeast-2
RECORDING_DIR=C:\MKDesk\temp
```

주의 사항:

- MySQL root 사용자를 애플리케이션에서 사용하지 않는다.
- `SECRET_KEY`를 개발 환경과 공유하지 않는다.
- 설정 파일에 `Users` 그룹의 읽기 권한이 남지 않도록 ACL을 제한한다.
- 환경변수 변경 후에는 MKDeskWeb 서비스를 재시작해야 한다.
- 비밀값을 PowerShell 명령 기록이나 문서에 그대로 남기지 않는다.

## 11. DB 초기화와 관리자 생성

현재 PowerShell 세션에서 먼저 환경변수를 설정해 연결을 검증한다.

```powershell
$env:MYSQL_HOST = '127.0.0.1'
$env:MYSQL_PORT = '3306'
$env:MYSQL_DATABASE = 'tm_portal'
$env:MYSQL_USER = 'tm_app'
$env:MYSQL_PASSWORD = [Net.NetworkCredential]::new(
    '',
    (Read-Host 'MySQL 애플리케이션 비밀번호' -AsSecureString)
).Password
```

프로젝트가 제공하는 초기화 명령을 실행한다.

```powershell
Set-Location C:\MKDesk\src
$mkPython = 'C:\MKDesk\venv\Scripts\python.exe'

& $mkPython -m flask --app app init-admin
$LASTEXITCODE
```

`$LASTEXITCODE`가 `0`이면 명령이 정상 종료된 것이다. 출력이 없다는 사실만으로 실패라고 판단하지 않는다.

관리자 계정을 직접 생성해야 한다면 Flask shell에서 DB 함수를 먼저 import해야 한다.

```powershell
& $mkPython -m flask --app app shell
```

```python
from app.db import db, new_id, one, run
from werkzeug.security import generate_password_hash

password = input("관리자 비밀번호: ")

run(
    "INSERT INTO staff_account "
    "(staff_id, login_id, password_hash, role) "
    "VALUES (%s, 'mkadmin', %s, 'ADMIN')",
    (new_id(), generate_password_hash(password)),
)
db().commit()

one(
    "SELECT login_id, role, is_active "
    "FROM staff_account "
    "WHERE login_id='mkadmin'"
)
```

`NameError: name 'run' is not defined`는 `from app.db import ... run`을 실행하지 않았을 때 발생한다.

## 12. Waitress 실행 파일

`C:\MKDesk\src\run.py`를 준비한다. 저장소에 이미 같은 목적의 파일이 있다면 기존 파일을 사용하고 중복 생성하지 않는다.

```python
from waitress import serve
from app import create_app

app = create_app()

if __name__ == "__main__":
    serve(
        app,
        host="127.0.0.1",
        port=8000,
        threads=8,
        url_scheme="https",
    )
```

외부 사용자는 ALB에서 HTTPS로 접속하고 ALB에서 IIS까지는 HTTP를 사용할 수 있다. 애플리케이션의 보안 쿠키와 URL 생성을 위해 외부 스킴이 HTTPS임을 올바르게 전달해야 한다.

수동 실행으로 먼저 확인한다.

```powershell
Set-Location C:\MKDesk\src
& C:\MKDesk\venv\Scripts\python.exe C:\MKDesk\src\run.py
```

다른 PowerShell 창에서 확인한다.

```powershell
curl.exe http://127.0.0.1:8000/health
```

정상 결과:

```json
{"status":"ok"}
```

포트 확인:

```powershell
Get-NetTCPConnection `
    -LocalAddress 127.0.0.1 `
    -LocalPort 8000 `
    -State Listen
```

여기서는 출력이 있는 것이 정상이다. `LocalAddress`가 `127.0.0.1`이어야 하며 `0.0.0.0`이면 외부 인터페이스에도 노출된 것이다.

## 13. IIS 리버스 프록시 설정

AWS 권장 구성에서는 ALB가 HTTPS를 종료하고 EC2의 IIS에는 HTTP 80으로 전달한다. 따라서 EC2의 IIS에 localhost용 HTTPS 인증서를 만들 필요가 없다.

`C:\inetpub\wwwroot\web.config` 예시:

```xml
<?xml version="1.0" encoding="utf-8"?>
<configuration>
  <system.webServer>
    <rewrite>
      <rules>
        <clear />

        <rule name="MKDesk proxy all requests" stopProcessing="true">
          <match url=".*" />
          <action
            type="Rewrite"
            url="http://127.0.0.1:8000/{R:0}"
            appendQueryString="true" />
        </rule>
      </rules>
    </rewrite>

    <httpErrors existingResponse="PassThrough" />
  </system.webServer>
</configuration>
```

기존 `main.html` 기본 문서 설정은 전체 요청 프록시 규칙으로 교체한다. 정적 `main.html`과 Flask 프록시 규칙을 동시에 기본 진입점으로 운영하면 요청 경로가 혼동될 수 있다.

ARR의 `preserveHostHeader`를 활성화한다.

```powershell
& "$env:windir\System32\inetsrv\appcmd.exe" `
    set config `
    /section:system.webServer/proxy `
    /enabled:true `
    /preserveHostHeader:true `
    /commit:apphost
```

설정을 확인하고 IIS를 재시작한다.

```powershell
& "$env:windir\System32\inetsrv\appcmd.exe" `
    list config `
    /section:system.webServer/proxy

& "$env:windir\System32\inetsrv\appcmd.exe" `
    list config "Default Web Site" `
    /section:system.webServer/rewrite/rules

iisreset
```

EC2 내부 확인:

```powershell
curl.exe http://localhost/health
```

여기서 `{"status":"ok"}`가 반환돼야 ALB 연결 전 단계가 완료된 것이다.

## 14. WinSW로 Windows 서비스 등록

수동 실행한 Python 프로세스는 로그아웃이나 재부팅 시 중단될 수 있다. WinSW로 `MKDeskWeb` 서비스를 등록한다.

서비스 설정에는 다음 내용이 포함돼야 한다.

- 실행 파일: `C:\MKDesk\venv\Scripts\python.exe`
- 인수: `C:\MKDesk\src\run.py`
- 작업 디렉터리: `C:\MKDesk\src`
- 시작 방식: Automatic
- 장애 시 재시작
- 로그 디렉터리: `C:\MKDesk\logs`
- DB, SECRET_KEY, S3 버킷·리전, RECORDING_DIR 설정

서비스를 설치하고 시작한 뒤 확인한다.

```powershell
Get-Service MKDeskWeb

Get-NetTCPConnection `
    -LocalAddress 127.0.0.1 `
    -LocalPort 8000 `
    -State Listen

curl.exe http://localhost/health
```

프로세스 경로도 확인한다.

```powershell
$mkPort = Get-NetTCPConnection `
    -LocalAddress 127.0.0.1 `
    -LocalPort 8000 `
    -State Listen

$mkProcessId = $mkPort.OwningProcess

Get-CimInstance Win32_Process `
    -Filter "ProcessId=$mkProcessId" |
    Select-Object ProcessId, Name, ExecutablePath, CommandLine
```

서비스 등록 전 수동으로 실행한 Python이 남아 있으면 포트 충돌이 날 수 있다. 해당 PID가 수동 프로세스인지 확인한 후에만 종료한다.

정리 작업 서비스 `MKDeskPurge`가 프로젝트 운영에 필요하다면 같은 방식으로 별도 서비스로 등록하고, 최소 권한 계정을 사용한다.

## 15. ALB 대상 그룹 생성

EC2의 IIS가 정상 동작한 다음 대상 그룹을 만든다.

권장 설정:

```text
Target type        Instances
Protocol           HTTP
Port               80
VPC                mkdesk-vpc
Health check path  /health
Success codes      200
```

Windows EC2를 대상 그룹에 등록한다. 대상 상태가 `Healthy`가 되지 않으면 다음 순서로 확인한다.

1. EC2 내부에서 `curl.exe http://localhost/health`
2. IIS 사이트와 App Pool 상태
3. `MKDeskWeb` 서비스 상태
4. EC2 보안 그룹 80 포트의 소스가 ALB 보안 그룹인지
5. 대상 그룹 포트와 IIS 바인딩이 모두 80인지
6. `/health`가 인증 없이 200을 반환하는지

## 16. ACM 인증서와 ALB 생성

### 16.1 ACM 인증서

`AWS Certificate Manager → Request certificate`에서 실제 서비스 도메인의 공개 인증서를 요청한다.

```text
예: tm.example.com
```

DNS 검증을 권장한다. Route 53을 사용하면 검증 레코드를 자동으로 만들 수 있다. 인증서 상태가 `Issued`가 될 때까지 기다린다.

### 16.2 Application Load Balancer

ALB를 다음과 같이 생성한다.

- Scheme: Internet-facing
- IP address type: IPv4 또는 필요에 따라 Dualstack
- Network mapping: 공개 서브넷 2개 이상
- Security group: `mkdesk-alb-sg`
- Listener: HTTPS 443
- Default action: `mkdesk-web-tg`로 전달
- Certificate: 앞서 발급한 ACM 인증서

HTTP 80 리스너를 사용하는 경우 HTTPS 443으로 리디렉션한다. 애플리케이션으로 평문 HTTP 요청을 직접 전달하지 않는다.

## 17. DNS 연결

Route 53을 사용한다면 호스팅 영역에 Alias 레코드를 만든다.

```text
Name    tm.example.com
Type    A
Alias   Yes
Target  mkdesk-alb
```

외부 DNS를 사용한다면 공급자 지침에 따라 ALB DNS 이름을 가리키는 CNAME을 만든다. 루트 도메인은 DNS 공급자의 ALIAS/ANAME 지원 여부를 확인한다.

DNS 반영 후 확인한다.

```powershell
Resolve-DnsName tm.example.com
curl.exe -v https://tm.example.com/health
```

브라우저에서 인증서 경고 없이 다음 경로를 확인한다.

```text
https://tm.example.com/
https://tm.example.com/health
https://tm.example.com/portal
```

## 18. S3 녹취 저장소

녹취 파일은 전용 비공개 S3 버킷에 저장한다. 버킷은 웹 정적 파일 제공용이 아니며 사용자에게 직접 공개하지 않는다.

### 18.1 S3 버킷 생성

`S3 → Create bucket`에서 다음 기준으로 생성한다.

```text
버킷 이름       mkdesk-recordings-prod-고유값
AWS 리전        EC2와 같은 리전 권장
Object Ownership Bucket owner enforced
Block Public Access 모든 항목 활성화
Versioning       활성화 권장
Default encryption SSE-S3 또는 SSE-KMS
```

버킷 이름은 전 세계에서 고유해야 한다. 환경이 여러 개면 운영과 시험 버킷을 분리한다.

```text
mkdesk-recordings-prod-고유값
mkdesk-recordings-staging-고유값
```

녹취는 다음 키 규칙으로 저장한다.

```text
s3://mkdesk-recordings-prod-고유값/files/<recording_id>.wav
```

`recording.object_key`에는 `files/<recording_id>.wav`를, `recording.storage_bucket`에는 녹취 예약 시 선택한 버킷명을 저장한다. 로컬 녹취의 버킷명은 NULL이다.

### 18.2 암호화 선택

기본 선택은 다음 두 가지다.

- SSE-S3: 구성이 단순하며 S3가 키를 관리한다.
- SSE-KMS: 별도 KMS 키 권한, 키 사용 감사, 조직별 키 정책이 필요할 때 사용한다.

SSE-KMS를 사용하면 EC2 역할에 S3 권한 외에도 해당 KMS 키의 `kms:Decrypt`, `kms:Encrypt`, `kms:GenerateDataKey` 권한이 필요하다. KMS 키 정책에도 EC2 역할 사용이 허용돼야 한다.

버킷 기본 암호화를 활성화하고, 애플리케이션 업로드 시에도 조직 정책에 맞는 암호화 방식을 명시한다.

### 18.3 EC2 IAM 역할

EC2에 액세스 키를 저장하지 않고 인스턴스 프로파일의 IAM 역할로 S3에 접근한다. 역할에 다음과 같은 최소 권한 정책을 연결한다. 버킷 이름은 실제 값으로 바꾼다.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ListRecordingPrefix",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::mkdesk-recordings-prod-고유값",
      "Condition": {
        "StringLike": {
          "s3:prefix": ["files/*"]
        }
      }
    },
    {
      "Sid": "ManageRecordingObjects",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject"
      ],
      "Resource": "arn:aws:s3:::mkdesk-recordings-prod-고유값/files/*"
    }
  ]
}
```

애플리케이션이 `ListBucket`을 사용하지 않는 구현이면 해당 권한을 제거한다. 버킷 생성·삭제, 퍼블릭 ACL 변경 같은 관리 권한은 애플리케이션 역할에 부여하지 않는다.

IAM 역할 연결 후 EC2에서 자격 증명 파일을 만들지 않은 상태로 호출을 확인한다.

```powershell
aws sts get-caller-identity
aws s3api head-bucket --bucket 'mkdesk-recordings-prod-고유값'
```

AWS CLI는 점검용일 뿐이며 애플리케이션은 `boto3`의 기본 자격 증명 체인을 통해 EC2 역할을 자동 사용한다.

### 18.4 녹취 저장소 설정

`C:\MKDesk\src\.env` 또는 웹·유지보수 서비스 환경에 버킷명과 리전을 지정한다. 서비스 환경변수가 우선한다. 새 녹취는 버킷명이 있으면 S3, 비어 있으면 로컬에 저장한다.

```text
S3_RECORDING_BUCKET=mkdesk-recordings-prod-고유값
AWS_REGION=ap-northeast-2
RECORDING_DIR=C:\MKDesk\temp
```

기존 설치는 새 의존성을 설치하고 `flask --app app init-admin`으로 녹취 버킷 열을 추가한 뒤 웹·유지보수 서비스를 재시작한다. 녹취 중인 `.part`는 로컬 `RECORDING_DIR`에 쓴다. 서비스 계정에 이 폴더의 수정 권한을 준다. 기존 로컬 녹취가 있으면 해당 디렉터리를 계속 사용한다.

완성한 WAV는 `put_object`로 업로드한다. 요약과 재생은 DB에 기록된 버킷과 키를 사용한다. Flask가 권한과 삭제 상태를 확인하고, 브라우저가 요청한 구간을 S3에서 읽어 전송한다. 정리 작업은 삭제 요청을 받은 키에 `delete_object`를 호출한다.

### 18.5 수명주기와 삭제 정책

녹취는 관리자 삭제 또는 동의 철회 시까지 보관한다. 정리 작업은 `DELETE_PENDING`인 녹취의 객체를 삭제한 뒤 DB 상태를 `DESTROYED`로 바꾼다. 녹취 경로 `files/`에는 현재 버전을 기간 만료시키는 S3 수명주기 규칙을 적용하지 않는다.

- 보존 기간은 개인정보·녹취 보존 정책과 일치시킨다.
- Versioning을 사용하면 삭제 마커만 생성되고 이전 버전이 남을 수 있다.
- 비현재 버전 만료 규칙을 함께 설정하지 않으면 삭제된 녹취가 계속 저장된다.
- Multipart Upload를 사용한다면 중단된 업로드 정리 규칙을 설정한다.
- Object Lock은 법적 보존 요구가 있을 때만 사용하며 일반 삭제 정책과 충돌 여부를 먼저 검토한다.

S3 수명주기만으로 DB 상태가 자동 변경되지는 않는다. 애플리케이션 purge 작업과 S3 정책을 함께 검증한다.

### 18.6 S3 접근 경로

비공개 서브넷의 EC2가 NAT Gateway 없이 S3에 접근해야 한다면 S3 Gateway VPC Endpoint를 생성하고 애플리케이션 서브넷의 라우팅 테이블에 연결한다. 엔드포인트 정책과 버킷 정책은 `mkdesk-recordings-prod-고유값/files/*` 접근만 허용하도록 제한한다.

### 18.7 기능 검증

시험용 녹취를 업로드한 뒤 다음을 확인한다.

1. DB의 `object_key`가 `files/<recording_id>.wav` 형식이다.
2. S3 객체의 Content-Type이 `audio/wav`다.
3. S3 객체가 퍼블릭 URL로 열리지 않는다.
4. 인증된 웹 화면에서는 녹취 재생이 된다.
5. 권한 없는 사용자는 재생할 수 없다.
6. 삭제 요청 후 S3 객체와 필요한 객체 버전이 정책대로 제거된다.
7. 실패한 음성 세션의 로컬 `.part` 파일이 정리된다.
8. EC2 재부팅 후에도 기존 READY 녹취를 재생할 수 있다.

AWS CLI로 객체 메타데이터를 확인할 수 있다.

```powershell
aws s3api head-object `
    --bucket 'mkdesk-recordings-prod-고유값' `
    --key 'files/RECORDING-ID.wav'
```

## 19. 비밀값과 IAM

운영 환경 권장 방식:

1. DB 암호와 `SECRET_KEY`를 Secrets Manager 또는 Parameter Store SecureString에 저장한다.
2. EC2에 IAM 인스턴스 프로파일을 연결한다.
3. 해당 인스턴스가 필요한 비밀값만 읽도록 IAM 정책을 제한한다.
4. 애플리케이션 시작 시 비밀값을 읽어 환경변수로 전달한다.
5. 비밀값 회전 절차와 서비스 재시작 절차를 문서화한다.

AWS 액세스 키를 EC2의 소스 폴더, 설정 파일 또는 Git 저장소에 저장하지 않는다. 가능하면 IAM 역할을 사용한다.

## 20. 로그, 모니터링, 경보

CloudWatch Agent를 설치하여 다음 로그와 지표를 수집한다.

- Windows 이벤트 로그
- IIS 액세스 로그
- WinSW/Waitress 애플리케이션 로그
- CPU 사용률
- 메모리 사용률
- 루트 EBS 여유 공간과 임시 녹취 디렉터리 사용량
- 서비스 중단 여부
- ALB의 대상 응답 시간과 4xx/5xx
- MySQL Windows 서비스 상태, 연결 수, 오류 로그, 데이터 디렉터리 사용량
- S3 4xx/5xx 요청 오류와 저장 용량

권장 경보:

| 대상 | 조건 예시 |
|---|---|
| ALB | HealthyHostCount가 0 |
| ALB | 5xx 오류 급증 |
| EC2 | CPU가 일정 시간 높음 |
| EC2 | 메모리 또는 디스크 여유 부족 |
| Windows 서비스 | `MKDeskWeb` 중지 |
| Windows 서비스 | MySQL 서비스 중지 |
| MySQL | 연결 수 급증 또는 오류 로그 증가 |
| 인증서 | ACM 관리 범위를 벗어난 인증서 만료 임박 |

로그에는 DB 비밀번호, 세션 쿠키, 사용자 민감정보가 기록되지 않도록 한다.

## 21. 백업과 복구

### 21.1 로컬 MySQL

- `mysqldump` 또는 MySQL Shell로 정기 논리 백업 생성
- 백업 파일을 운영 EC2와 다른 위치에 암호화하여 보관
- Windows 작업 스케줄러로 백업 실행과 실패 로그 관리
- 애플리케이션 배포와 DB 스키마 변경 전에 추가 백업 생성
- 암호화된 EBS 스냅샷을 보조 백업으로 사용
- 새 EC2의 MySQL에 백업을 복원하는 전체 절차 정기 시험

예시 백업 명령은 비밀번호를 명령행에 직접 넣지 않는다.

```powershell
$mysqlDump = Get-ChildItem 'C:\Program Files\MySQL' `
    -Filter mysqldump.exe `
    -Recurse `
    -ErrorAction SilentlyContinue |
    Select-Object -First 1 -ExpandProperty FullName

Test-Path $mysqlDump

$backupDir = 'C:\MKDesk\backups'
New-Item -ItemType Directory -Force $backupDir

& $mysqlDump `
    --host=127.0.0.1 `
    --user=root `
    --password `
    --single-transaction `
    --routines `
    --triggers `
    --databases tm_portal `
    --result-file="$backupDir\tm_portal.sql"
```

자동화할 때는 Secrets Manager 또는 접근 권한이 제한된 MySQL 옵션 파일을 사용하고, 백업 파일의 ACL과 암호화를 확인한다. 동일 EC2 내부에만 있는 백업은 EC2 장애에 대비한 백업이 아니다.

### 21.2 EC2와 S3

- 소스는 Git을 원본으로 관리
- OS와 설정 복구를 위해 AMI 또는 Infrastructure as Code 검토
- S3 버전 관리와 비현재 버전 만료 기간을 보존 정책에 맞게 설정
- 버킷 정책, IAM 정책, 수명주기 구성도 코드 또는 별도 백업으로 관리
- 복구 시험은 삭제된 객체 버전 복원과 애플리케이션 재생까지 확인

백업이 존재하는 것과 실제 복구 가능한 것은 다르다. 별도 시험 환경에서 복구 훈련을 수행한다.

## 22. 애플리케이션 업데이트 절차

단일 EC2 기준 안전한 기본 절차:

1. 변경 내용을 검토하고 유지보수 시간을 정한다.
2. MySQL 논리 백업과 필요한 EBS 스냅샷을 생성한다.
3. 현재 Git 커밋 ID와 배포 버전을 기록한다.
4. `MKDeskWeb` 서비스를 중지한다.
5. `git pull --ff-only origin main`으로 소스를 갱신한다.
6. 가상환경에서 `pip install -r requirements.txt`를 실행한다.
7. DB 마이그레이션 또는 초기화 명령이 있다면 실행한다.
8. `MKDeskWeb` 서비스를 시작한다.
9. EC2 내부 `/health`를 확인한다.
10. ALB 대상이 Healthy인지 확인한다.
11. 외부 HTTPS 로그인과 주요 기능을 점검한다.

예시:

```powershell
Set-Location C:\MKDesk\src

git status --short
git rev-parse HEAD

Stop-Service MKDeskWeb
git pull --ff-only origin main

& C:\MKDesk\venv\Scripts\python.exe `
    -m pip install `
    -r C:\MKDesk\src\requirements.txt

Start-Service MKDeskWeb

curl.exe http://localhost/health
curl.exe https://tm.example.com/health
```

운영 서버에서 수정한 파일이 있으면 `git pull` 전에 원인을 확인한다. 사용자 변경 사항을 강제로 삭제하기 위해 `git reset --hard`를 사용하지 않는다.

트래픽 중단을 줄이려면 EC2를 두 대 이상 운영하고 ALB에서 한 대씩 대상 제외, 업데이트, 검증, 재등록하는 방식을 사용한다.

## 23. 롤백 절차

문제가 생기면 다음 순서로 대응한다.

1. ALB에서 문제가 있는 EC2를 대상 제외한다.
2. 애플리케이션 로그와 Windows 이벤트 로그를 보존한다.
3. 이전에 기록한 정상 커밋 또는 이전 AMI로 복구한다.
4. DB 스키마 변경이 포함됐다면 사전 정의된 DB 롤백 절차를 따른다.
5. 내부 `/health`, 대상 그룹 상태, 외부 기능을 다시 확인한다.

DB 복원을 위해 운영 MySQL을 즉시 덮어쓰지 않는다. 별도 EC2 또는 격리된 MySQL 인스턴스에 논리 백업을 복원해 데이터와 애플리케이션 호환성을 검증한 후 전환한다.

## 24. 주요 오류와 해결 방법

### 24.1 ALB 대상이 Unhealthy

```powershell
Get-Service W3SVC, MKDeskWeb
curl.exe http://127.0.0.1:8000/health
curl.exe http://localhost/health
```

- 첫 번째 curl 실패: Waitress 또는 Flask 문제
- 첫 번째 성공, 두 번째 실패: IIS/ARR/rewrite 문제
- 둘 다 성공, ALB만 실패: 보안 그룹, 대상 그룹 포트, 상태 확인 경로 문제

### 24.2 IIS에서 404 반환

`web.config`이 실제 IIS 사이트의 물리 경로에 있는지 확인한다.

```powershell
Import-Module WebAdministration

Get-Website |
    Format-Table Name, State, PhysicalPath

Test-Path C:\inetpub\wwwroot\web.config

& "$env:windir\System32\inetsrv\appcmd.exe" `
    list config "Default Web Site" `
    /section:system.webServer/rewrite/rules
```

PowerShell에서 `%SystemDrive%`가 포함된 IIS 경로가 그대로 표시되는 것은 설정 표현 방식일 수 있다. 실제 경로인 `C:\inetpub\wwwroot`도 함께 확인한다.

### 24.3 IIS에서 502 반환

- `MKDeskWeb` 서비스가 실행 중인지 확인한다.
- `127.0.0.1:8000`이 Listen 상태인지 확인한다.
- 수동 프로세스와 서비스가 포트를 동시에 사용하지 않는지 확인한다.
- `run.py` 경로와 가상환경 Python 경로를 확인한다.

### 24.4 로컬 MySQL 연결 실패

```powershell
Get-Service MySQL84

Test-NetConnection `
    -ComputerName 127.0.0.1 `
    -Port 3306
```

- `False`: MySQL 서비스 상태, `my.ini`, 포트 3306 충돌, 오류 로그 확인
- `True`인데 로그인 실패: 사용자명, 암호, 사용자 host, DB 이름 확인
- 권한 오류: `SHOW GRANTS FOR 'tm_app'@'127.0.0.1';` 확인
- 서비스 이름이 다르면 `Get-Service *mysql*`로 실제 이름 확인

### 24.5 외부 HTTPS는 열리지만 로그인 반복

- ALB의 HTTPS 리스너와 인증서를 확인한다.
- ARR의 `preserveHostHeader="true"`를 확인한다.
- Waitress의 `url_scheme="https"`를 확인한다.
- Flask의 프록시 헤더 처리와 Secure 쿠키 설정을 확인한다.
- 브라우저에서 과거 localhost 쿠키를 제거하고 실제 도메인으로 다시 시험한다.

### 24.6 브라우저에서 인증서 오류

운영 AWS 환경에서는 localhost 자체 서명 인증서를 사용하지 않는다.

- 접속 도메인이 ACM 인증서의 도메인과 일치하는지
- ALB 443 리스너에 올바른 인증서가 연결됐는지
- DNS가 올바른 ALB를 가리키는지
- 인증서 상태가 `Issued`인지 확인한다.

### 24.7 MySQL 실행 파일을 찾을 수 없음

설치 버전과 실제 경로가 다를 수 있다. 예를 들어 MySQL 서비스 이름이 `MySQL84`여도 클라이언트는 Workbench 폴더에 있을 수 있다.

```powershell
Get-ChildItem 'C:\Program Files\MySQL' `
    -Filter mysql.exe `
    -Recurse `
    -ErrorAction SilentlyContinue

Get-Service |
    Where-Object {
        $_.Name -match 'mysql' -or
        $_.DisplayName -match 'mysql'
    }
```

### 24.8 `Invoke-RestMethod`만 TLS 오류

`curl.exe -v https://실제도메인/health`가 200이고 브라우저도 정상이라면 구형 PowerShell/.NET TLS 동작 문제일 수 있다. 운영 판단은 실제 도메인의 공개 인증서, 브라우저, ALB 상태, `curl.exe` 결과를 함께 사용한다.

## 25. 전체 검증 체크리스트

### AWS 인프라

- [ ] ALB가 공개 서브넷 2개 이상에 배치됨
- [ ] ALB 443 리스너에 ACM 인증서가 연결됨
- [ ] ALB 80은 사용하지 않거나 443으로 리디렉션됨
- [ ] EC2 80은 ALB 보안 그룹에서만 접근 가능
- [ ] RDP 3389는 닫혀 있거나 관리자 IP로 제한됨
- [ ] Waitress 8000은 외부에 공개되지 않음
- [ ] MySQL 3306이 `127.0.0.1`에서만 Listen
- [ ] AWS 보안 그룹과 Windows Firewall에 3306 인바운드 규칙이 없음
- [ ] EC2 EBS와 S3 암호화가 활성화됨
- [ ] S3 퍼블릭 액세스 차단이 모두 활성화됨
- [ ] EC2 역할은 녹취 버킷의 `files/*`에만 필요한 권한을 가짐

### Windows와 애플리케이션

- [ ] IIS, URL Rewrite, ARR가 설치됨
- [ ] ARR 프록시와 `preserveHostHeader`가 활성화됨
- [ ] `MKDeskWeb` 서비스가 Automatic 및 Running 상태
- [ ] MySQL 서비스가 Automatic 및 Running 상태
- [ ] Waitress가 `127.0.0.1:8000`에서만 Listen
- [ ] `http://localhost/health`가 200을 반환
- [ ] ALB 대상 상태가 Healthy
- [ ] 실제 도메인의 `/health`가 HTTPS 200을 반환
- [ ] `/portal`과 관리자 로그인이 정상
- [ ] 녹취 업로드·재생·삭제가 정상
- [ ] READY 녹취가 S3에 있고 EC2 로컬 원본에 의존하지 않음
- [ ] 미인증 사용자는 S3 객체와 웹 재생 경로에 접근할 수 없음
- [ ] 실패·만료된 `.part` 임시 파일이 정리됨
- [ ] 재부팅 후 자동으로 서비스가 복구됨

### 운영

- [ ] 비밀값이 Git과 문서에 저장되지 않음
- [ ] 애플리케이션이 MySQL root 계정을 사용하지 않음
- [ ] MySQL 논리 백업이 EC2 외부의 안전한 위치에 보관됨
- [ ] 새 EC2에서 MySQL 백업 복원 시험을 완료함
- [ ] S3 버전 관리, 수명주기, 비현재 버전 만료 정책이 설정됨
- [ ] 애플리케이션 purge와 S3 객체 삭제가 함께 검증됨
- [ ] CloudWatch 로그와 주요 경보가 설정됨
- [ ] 배포 및 롤백 절차를 시험함
- [ ] DB 및 파일 복구 시험을 수행함

## 26. 권장 구축 순서 요약

```text
1. VPC, 서브넷, 라우팅 구성
2. ALB와 EC2 보안 그룹 생성
3. 비공개 S3 녹취 버킷과 수명주기 정책 생성
4. S3 최소 권한이 있는 EC2 IAM 역할 생성
5. Windows Server EC2 생성 및 IAM 역할 연결
6. 같은 EC2에 MySQL Server 설치 및 loopback 제한
7. IIS, URL Rewrite, ARR, Python, Git, WinSW 설치
8. S3 저장 코드와 Python 가상환경 배치
9. 로컬 MySQL DB·전용 계정 생성 및 DB 초기화
10. MySQL 외부 백업과 복원 절차 검증
11. S3 업로드·조회·삭제와 임시 파일 정리 검증
12. Waitress를 loopback에서 수동 검증
13. IIS에서 Waitress로 리버스 프록시 구성
14. WinSW로 MKDeskWeb 서비스 등록
15. EC2 내부 http://localhost/health 검증
16. ALB 대상 그룹 생성 및 Healthy 확인
17. ACM 인증서와 ALB HTTPS 리스너 연결
18. 실제 도메인을 ALB에 연결
19. 외부 HTTPS, 로그인, S3 녹취 기능 검증
20. CloudWatch, 백업, 경보, 복구 절차 구성
```

## 27. 참고 자료

- [AWS Application Load Balancer HTTPS 리스너 인증서](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/https-listener-certificates.html)
- [AWS Application Load Balancer HTTPS 리스너 생성](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/create-https-listener.html)
- [Windows EC2에 RDP로 연결](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/connect-rdp.html)
- [Amazon EC2 인프라 보안](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/infrastructure-security.html)
- [Amazon S3 퍼블릭 액세스 차단](https://docs.aws.amazon.com/AmazonS3/latest/userguide/access-control-block-public-access.html)
- [Amazon S3 기본 암호화](https://docs.aws.amazon.com/AmazonS3/latest/userguide/default-bucket-encryption.html)
- [Amazon S3 수명주기 구성](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lifecycle-mgmt.html)
- [Amazon S3 Gateway VPC Endpoint](https://docs.aws.amazon.com/vpc/latest/privatelink/vpc-endpoints-s3.html)

이 가이드는 로컬 VM에서 검증한 웹 서버와 MySQL 통합 구성을 단일 Windows EC2로 옮기는 기준 문서다. 실제 운영 전에는 조직의 보안 정책, 개인정보·녹취 데이터 보존 기준, 예상 사용자 수, 장애 허용 수준에 맞춰 인스턴스 크기, MySQL 외부 백업, S3 보존 기간, 접근 제어를 확정해야 한다.
