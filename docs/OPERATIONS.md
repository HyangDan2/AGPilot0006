# HD2Lab Document Intelligence Service (AGPilot0006) — 운영 및 배포 가이드 (OPERATIONS)

> **버전**: v2.0.0  
> **기준 일자**: 2026-11-13  
> **시스템 서비스**: `hd2lab-agtools.service` (포트: `8006`)  
> **서버 경로**: `/var/www/services/AGTools`

---

## 1. 서비스 구성 및 systemd 관리

AGPilot0006은 라즈베리파이 운영 서버에서 `systemd` 서비스(`hd2lab-agtools.service`)로 상시 가동됩니다.

### 1.1 서비스 유닛 파일 (`/etc/systemd/system/hd2lab-agtools.service`)
```ini
[Unit]
Description=HD2Lab Document Intelligence Service (AGTools / AGPilot0006)
After=network.target

[Service]
Type=simple
User=pi
Group=pi
WorkingDirectory=/var/www/services/AGTools
Environment=PYTHONPATH=/var/www/services/AGTools
Environment=PORT=8006
ExecStart=/opt/hd2lab-agcore-venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8006
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

### 1.2 주요 관리 명령어
```bash
# 서비스 상태 확인
sudo systemctl status hd2lab-agtools.service

# 서비스 재시작 (코드 업데이트 후)
sudo systemctl restart hd2lab-agtools.service

# 실시간 로그 모니터링
sudo journalctl -u hd2lab-agtools.service -f -n 50
```

---

## 2. Nginx 역방향 프록시 연동 (Reverse Proxy)

외부 요청은 Nginx SSL 레이어(`:443`)를 통해 단일 창구로 진입하며, SSO 인증 및 세션 검증 후 내부 `127.0.0.1:8006`으로 프록시됩니다.

### 2.1 Nginx 설정 조각 (`/etc/nginx/snippets/hd2lab-ag-server.conf`)
```nginx
location = /admin/agtools { return 308 /admin/agtools/; }
location ^~ /admin/agtools/ {
    include /etc/nginx/snippets/admin-sso-page.conf;
    proxy_pass http://127.0.0.1:8006/;
    proxy_http_version 1.1;
    proxy_set_header Host $http_host;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header Authorization "";
    proxy_set_header X-HD2Lab-User $hd2lab_user;
    proxy_set_header X-HD2Lab-Role $hd2lab_role;
    proxy_cache off;
    proxy_buffering off;
    client_max_body_size 64m;
    proxy_read_timeout 300s;
}
```

- **Swagger UI 접속**: `https://hd2lab.duckdns.org/admin/agtools/docs`
- **상태 대시보드 접속**: `https://hd2lab.duckdns.org/admin/agtools/`

---

## 3. 환경 변수 설정 (Configuration & Environment)

| 환경 변수명 | 기본값 | 설명 |
| :--- | :--- | :--- |
| `DOCUMENT_SERVICE_TOKEN` | `hd2-doc-secret-token-key-2026` | 서비스 간 직접 통신용 Bearer 인증 토큰 |
| `DOCUMENT_DATA_DIR` | `./data` | 전체 데이터 보관 루트 디렉터리 |
| `DOCUMENT_MAX_UPLOAD_MB` | `100` | 스트리밍 업로드 최대 허용 용량 (MB) |
| `DOCUMENT_MAX_PAGES` | `500` | 단일 문서 최대 처리 페이지/슬라이드 한도 |
| `DOCUMENT_JOB_TIMEOUT_SEC`| `300` | 비동기 파싱 Job 타임아웃 제한 (초) |
| `PORT` | `8006` | Uvicorn 수신 포트 |

---

## 4. 디렉터리 및 데이터 백업 관리

문서 데이터와 검색 인덱스는 영구 볼륨 경로에 보관됩니다:

```text
/var/www/services/AGTools/data/
├── metadata.sqlite3               # 문서 메타데이터, 작업 큐 및 FTS5 색인 (SQLite WAL)
└── documents/                     # 문서별 원본 및 파싱 산출물
    └── {document_id}/
        ├── original/              # 업로드 원본 파일
        ├── parsed/                # document.json, document.md, chunks.jsonl
        └── assets/                # figures/, pages/, slides/
```

### 4.1 SQLite 백업 권장 사항
`metadata.sqlite3`는 동시성 향상을 위해 WAL 모드로 동작하므로, 안전한 백업을 위해 SQLite Online Backup 기능을 사용합니다:
```bash
sqlite3 /var/www/services/AGTools/data/metadata.sqlite3 ".backup /backup/agtools/metadata_$(date +%Y%m%d).sqlite3"
```

---

## 5. 정기 점검 및 트러블슈팅

1. **포트 충돌 확인**:
   ```bash
   sudo netstat -tlpn | grep 8006
   ```
2. **미완료 작업 수동 복구**:
   - 서버 불시 종료 시 `JobManager.recover_on_startup()`에 의해 서비스 재시작 시 자동 복구되지만, 특정 문서의 비정상 처리로 큐가 멈춘 경우 `jobs` 테이블의 상태를 직접 점검할 수 있습니다:
   ```bash
   sqlite3 /var/www/services/AGTools/data/metadata.sqlite3 "SELECT job_id, document_id, status, error_message FROM jobs WHERE status != 'completed';"
   ```
