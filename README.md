# Host Resource AI Agent

Hệ thống AI-powered xử lý alert Prometheus cho CPU / RAM / DISK ở mức host.

## Kiến trúc

```
Prometheus Alertmanager
  → FastAPI webhook (/api/v1/alerts/webhook)
  → Redis dedup + queue
  → Worker: collect evidence (Prometheus + SSH)
  → Rule-based RCA + Knowledge lookup
  → LLM one-shot (Gemini, nếu cần)
  → UI approval (http://localhost:8082)
  → SSH execution + verification
  → Knowledge learning
```

## Cài đặt và chạy

### 1. Cài dependencies

```bash
pip install -r requirements.txt
```

### 2. Cấu hình .env

Sửa file `.env` cho phù hợp:
- `DATABASE_URL` — MySQL connection
- `REDIS_URL` — Redis connection
- `PROMETHEUS_URL` — Prometheus server
- `SSH_USER` / `SSH_KEY_PATH` — SSH credentials
- `GEMINI_API_KEY` — Google Gemini API key

### 3. Tạo database

```sql
CREATE DATABASE IF NOT EXISTS host_resource_agent CHARACTER SET utf8mb4;
```

Tables sẽ tự tạo khi app khởi động.

### 4. Seed demo data

```bash
python scripts/seed_demo.py
```

### 5. Chạy app

```bash
# Chạy API + UI
./run.sh app
# hoặc
uvicorn app.main:app --host 0.0.0.0 --port 8082 --reload

# Chạy worker (terminal khác)
./run.sh worker

# Hoặc chạy cả 2
./run.sh all
```

### 6. Truy cập

- **UI:** http://localhost:8082
- **API Docs:** http://localhost:8082/docs
- **SSE Stream:** http://localhost:8082/api/v1/events/stream

## API Endpoints

| Method | Path | Mô tả |
|--------|------|--------|
| POST | /api/v1/alerts/webhook | Nhận alert từ Alertmanager |
| GET | /api/v1/incidents/stats | Thống kê tổng quan |
| GET | /api/v1/incidents | Danh sách incidents |
| GET | /api/v1/incidents/{id} | Chi tiết incident |
| POST | /api/v1/approvals | Approve/Cancel action |
| GET | /api/v1/audit | Audit log |
| GET | /api/v1/events/stream | SSE realtime |
| GET | /api/v1/health | Health check |

## Test gửi alert

```bash
curl -X POST http://localhost:8082/api/v1/alerts/webhook \
  -H "Content-Type: application/json" \
  -d '{
    "status": "firing",
    "alerts": [{
      "status": "firing",
      "labels": {
        "alertname": "HostCPUHigh",
        "instance": "10.0.1.50:9100",
        "severity": "warning",
        "job": "node-exporter"
      },
      "annotations": {"summary": "CPU usage is above 90%"},
      "fingerprint": "test-cpu-001"
    }]
  }'
```

## Cấu trúc thư mục

```
host_resource_ai_agent/
├── app/
│   ├── api/routers/incidents.py   # 8 API endpoints + SSE
│   ├── clients/
│   │   ├── llm_client.py          # Gemini one-shot
│   │   └── prometheus_client.py   # Prometheus queries
│   ├── collectors/
│   │   ├── ssh_collector.py       # 70+ SSH commands
│   │   └── evidence_builder.py    # Parse + build evidence pack
│   ├── core/
│   │   ├── config.py              # Settings from .env
│   │   ├── database.py            # SQLAlchemy async
│   │   ├── redis_client.py        # Redis dedup/queue/lock/events
│   │   └── logging.py             # Structured JSON logging
│   ├── models/models.py           # 12 DB tables
│   ├── prompts/rca_prompt.py      # LLM prompt template
│   ├── repositories/incident_repo.py
│   ├── schemas/schemas.py         # Pydantic schemas
│   ├── services/
│   │   ├── alert_intake.py        # Normalize + dedup + create
│   │   ├── rule_rca.py            # Rule-based RCA engine
│   │   ├── knowledge_service.py   # Knowledge lookup + learning
│   │   ├── execution_service.py   # SSH execution + safety
│   │   └── verification_service.py
│   ├── workers/incident_worker.py # Full 8-phase pipeline
│   └── main.py                    # FastAPI app
├── static/index.html              # Operations UI
├── scripts/seed_demo.py
├── tests/test_agent.py            # 25 test cases
├── migrations/
├── .env
├── run.sh
└── requirements.txt
```

## Workflow (8 phases)

1. **Alert Intake** → lưu raw + normalize + tạo incident
2. **Dedup/Suppress** → Redis TTL + pattern check
3. **Evidence Collection** → Prometheus + SSH crawl (70+ commands)
4. **RCA** → Rule engine → Knowledge lookup → LLM 1 lần duy nhất
5. **Approval** → UI hiển thị ≥3 phương án → operator chọn
6. **Execution** → SSH + safety checks + rollback
7. **Verification** → Prometheus + SSH read-only
8. **Knowledge Learning** → lưu tri thức tái sử dụng
