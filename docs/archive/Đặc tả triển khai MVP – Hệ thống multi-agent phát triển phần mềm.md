# Đặc tả triển khai MVP – Hệ thống multi-agent phát triển phần mềm

Phiên bản 0.2 · Kiến trúc lai: lõi LangGraph + n8n làm lớp chat và webhook · LLM: model free qua OpenRouter · Mục tiêu: chạy thông **một flow end-to-end**

## 1. Mục tiêu và phạm vi

### 1.1 Mục tiêu

Xây dựng hệ thống trong đó **owner** (người dùng) giao một yêu cầu phát triển phần mềm, một **Supervisor** phân việc cho một nhóm agent theo vai trò, owner theo dõi và can thiệp qua chat, và mọi hoạt động của agent được ghi lại dưới dạng sự kiện có cấu trúc.

MVP chỉ cần chứng minh **một flow chạy được từ đầu đến cuối** với model free. Độ đẹp, độ tự động hóa cao và hiệu năng chưa phải mục tiêu.

### 1.2 Trong phạm vi MVP

- 4 agent: Supervisor/PM, Architect, Developer, QA.
- Một flow duy nhất (mục 2).
- Cổng duyệt của owner sau bước thiết kế; owner có thể đổi spec giữa chừng (mức đơn giản).
- Chat với owner qua **Telegram, do n8n làm cầu nối** với Gateway (mục 9); CLI giữ lại để debug.
- Ghi event vào SQLite, có lệnh xem lại (replay) bằng log; Gateway đẩy event sang n8n qua webhook có ký tên.
- Chạy code và test của agent trong thư mục workspace riêng, có timeout.

### 1.2.1 Để giai đoạn sau

- Reviewer/Security và DevOps agent, chạy song song nhiều Dev.
- Dashboard web và cảnh Three.js (mục 10).
- Discord (thêm nhánh trong workflow n8n), đa người dùng, đa dự án, Postgres, Redis.

### 1.3 Thuật ngữ

| Thuật ngữ | Ý nghĩa |
| --- | --- |
| Run | Một lần thực hiện yêu cầu của owner, từ lúc giao đến lúc báo cáo cuối |
| Spec | Đặc tả yêu cầu của run, có version (v1, v2...) |
| Task | Một đơn vị việc giao cho một agent trong một run |
| Event | Bản ghi có cấu trúc về mọi việc xảy ra trong run |
| Gate | Điểm dừng chờ owner approve/reject |
| Gateway | API FastAPI, điểm vào duy nhất của mọi lệnh và nơi phát event |
| Workflow n8n | Luồng tự động hóa trong n8n; ở đây chỉ làm giao tiếp với owner, không điều phối agent |

## 2. Flow mục tiêu (flow duy nhất của MVP)

**Yêu cầu mẫu:** "Xây module thanh toán đơn giản: tạo giao dịch, hoàn tiền một phần hoặc toàn bộ, không cho hoàn quá số tiền đã thanh toán, lưu trong bộ nhớ." Đầu ra là một package Python nhỏ kèm unit test chạy bằng pytest. Yêu cầu này đủ nhỏ để model free xử lý được và đủ giàu logic để kiểm tra việc phối hợp.

```
Owner ──(yêu cầu)──► Supervisor
  Supervisor ─► Architect : thiết kế module + API + danh sách test case
  Supervisor ─► [GATE 1: owner duyệt thiết kế]
  Supervisor ─► Developer : viết code theo thiết kế
  Supervisor ─► QA        : viết test từ thiết kế, chạy pytest
     ├─ pass ─► Supervisor : báo cáo cuối cho owner
     └─ fail ─► Developer  : sửa (tối đa 3 vòng) ─► QA
               └─ quá 3 vòng ─► [GATE 2: owner quyết định tiếp tục / dừng / đổi spec]
```

### 2.1 Kịch bản đổi spec

Trong lúc run đang chạy, owner gửi "thêm: hỗ trợ ví điện tử". Hệ thống tạo spec v2 và quay lại bước Architect với spec mới (giữ cùng run). Mức nâng cao (chỉ làm lại phần bị ảnh hưởng) để giai đoạn sau.

## 3. Kiến trúc

```
Owner ── Telegram ──► n8n (chat adapter)
   │                    │ gọi REST: run, approve, spec, ask      ▲ nhận event
   │                    │                                         │ (webhook ký HMAC)
   └── CLI (debug) ─────┼────────────┐                            │
                        ▼            ▼                            │
                    Gateway (FastAPI) ◄──────── Outbox + Pub/Sub ───┘
                        │ lệnh
                        ▼
                Orchestrator (LangGraph)
      ┌───────────┬─────┴──────┬───────────┐
  Supervisor   Architect    Developer      QA
      └───────────┴─────┬──────┴───────────┘
                        ▼
              LLM layer (OpenRouter, fallback)
                        ▼
              Sandbox runner (workspace/<run_id>/)

Mọi node ─► Event Store (SQLite) ─► Pub/Sub ─► n8n (Telegram) / WebSocket (dashboard)
```

### 3.1 Thành phần

| Thành phần | Công nghệ MVP | Ghi chú |
| --- | --- | --- |
| Orchestrator | Python 3.11+, LangGraph | Graph có state, checkpoint, `interrupt` cho gate |
| Gateway | FastAPI | REST cho lệnh, WebSocket/SSE cho event, webhook đi sang n8n |
| Lưu trữ | SQLite (file) | Đổi sang Postgres khi cần nhiều người dùng |
| Pub/Sub | `asyncio.Queue` trong tiến trình | Đổi sang Redis Streams/NATS khi tách tiến trình |
| LLM | OpenRouter (API tương thích OpenAI) | Mục 5 |
| Chat adapter | n8n (Telegram Trigger, node Telegram, Webhook, HTTP Request, IF/Switch) | Tự host (Docker) hoặc n8n cloud; mục 9 |
| CLI | `typer` | Giữ để debug, cùng giao diện Gateway |
| Sandbox | Thư mục workspace + `subprocess` có timeout | Nâng lên Docker khi chạy code không tin cậy |

### 3.2 Nguyên tắc thiết kế

1. **Event là nguồn sự thật duy nhất.** Chat, dashboard và Three.js sau này chỉ là view trên event.
2. **Agent không gọi trực tiếp nhau.** Mọi chuyển giao đi qua graph của Supervisor.
3. **Agent trả về dữ liệu có cấu trúc (JSON), orchestrator mới là bên ghi file và chạy lệnh.** Cách này tránh phụ thuộc tool calling, vốn không ổn định ở model free (mục 5.4).
4. **Mọi vòng lặp đều có giới hạn** về số lần và số request.
5. **n8n chỉ làm giao tiếp và thông báo:** không giữ state của run, không gọi LLM. Nếu n8n ngừng chạy, run vẫn tiếp tục và event được gửi lại sau.
6. **Điểm nối Gateway–n8n là webhook có ký tên và idempotent** (mục 9.4).

### 3.3 Phân chia trách nhiệm giữa LangGraph và n8n

| Việc | LangGraph + Gateway | n8n |
| --- | --- | --- |
| Điều phối agent, vòng Dev–QA | Có | Không |
| State, checkpoint, spec version | Có | Không |
| Gọi LLM (OpenRouter) | Có | Không |
| Chạy pytest trong sandbox | Có | Không |
| Event store và replay | Có | Không |
| Nhận tin Telegram và nút bấm, định tuyến lệnh | Không | Có |
| Gửi thông báo, lọc event đáng gửi | Không | Có |
| Nhắc owner khi gate chờ lâu | Không | Có |
| Kết nối dịch vụ ngoài (Discord, email, Sheets) | Không | Có, giai đoạn sau |

## 4. Đặc tả agent

Mỗi agent gồm: system prompt, schema đầu vào/đầu ra (Pydantic), model ưu tiên và danh sách fallback. Đầu ra bắt buộc là JSON hợp lệ theo schema; nếu không hợp lệ thì thử lại tối đa 2 lần với thông báo lỗi parse.

### 4.1 Supervisor/PM

- **Việc:** diễn giải yêu cầu thành spec có cấu trúc; chia task; quyết định bước tiếp theo; tổng hợp báo cáo; phân loại tin nhắn của owner.
- **Đầu vào:** yêu cầu của owner, trạng thái run, kết quả các agent.
- **Đầu ra:** `Plan {goal, requirements[], acceptance_criteria[], out_of_scope[]}` và `Report {summary, files[], test_result, open_issues[]}`.
- **Phân loại tin nhắn owner:** `status_query | spec_change | ask_agent(agent, question) | approve | reject | other`. Dùng quy tắc theo lệnh trước (`/status`, `/approve`...), chỉ dùng LLM phân loại cho tin nhắn tự do.

### 4.2 Architect

- **Việc:** thiết kế module từ spec.
- **Đầu ra:** `Design {files[{path, purpose}], public_api[{signature, behavior}], data_models[], test_cases[{name, given, expect}], assumptions[]}`.
- **Ràng buộc:** tối đa khoảng 4 file cho flow MVP; mọi hành vi trong acceptance criteria phải có ít nhất một test case.

### 4.3 Developer

- **Việc:** hiện thực code theo `Design`.
- **Đầu vào:** `Design`, (khi sửa lỗi) log pytest lần chạy trước và các file hiện tại.
- **Đầu ra:** `CodeChange {files[{path, content}], notes}`. Orchestrator ghi nguyên file vào workspace; từ chối đường dẫn nằm ngoài workspace hoặc có `..`.

### 4.4 QA

- **Việc:** viết test từ `Design.test_cases` (không nhìn code của Developer ở lần viết đầu), rồi orchestrator chạy pytest và QA diễn giải kết quả.
- **Đầu ra:** `TestSuite {files[{path, content}]}` và `QAVerdict {passed: bool, failures[{test, reason, suspected_file}], advice_for_dev}`.
- **Lưu ý:** kết quả pass/fail dựa trên **mã thoát của pytest**, không dựa vào lời của model.

## 5. Lớp LLM (OpenRouter, model free)

### 5.1 Kết nối

OpenRouter tương thích OpenAI API, chỉ cần đổi `base_url` và tên model.

```python
from openai import OpenAI
client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=os.environ["OPENROUTER_API_KEY"])
resp = client.chat.completions.create(
    model="<model-id>:free",
    messages=[...],
    extra_body={"models": ["<model-a>:free", "<model-b>:free"]},  # fallback
)
```

### 5.2 Cấu hình model theo vai trò (`config/models.yaml`)

```yaml
supervisor: ["<model-1>:free", "<model-2>:free"]
architect:  ["<model-1>:free", "<model-3>:free"]
developer:  ["<coder-model>:free", "<model-2>:free"]
qa:         ["<model-2>:free", "<model-3>:free"]
```

Không hardcode một model duy nhất, vì model free có thể bị gỡ hoặc ngừng phục vụ bất cứ lúc nào. Tên model thật chọn ở bước 5.3.

### 5.3 Cách chọn model từ danh sách của OpenRouter

Danh sách bạn chụp lọc theo "Newest" nên chủ yếu là model mới ra, và nhiều model trong đó không phù hợp để làm agent lập trình (tạo ảnh, giọng nói, embedding, rerank). Cách chọn:

1. Chọn tab **Text**, lọc **Free**, rồi sắp xếp theo mức dùng (Weekly Tokens) thay vì Newest.
2. Mở trang từng model, kiểm tra: có hỗ trợ **structured output / response\_format** hay không, ngữ cảnh đủ lớn (từ khoảng 32K trở lên là đủ cho MVP), và có dòng chữ ghi dữ liệu có thể được dùng để huấn luyện hay không.
3. Ưu tiên model chuyên code cho Developer, model "suy luận" cho Architect và Supervisor.
4. Chạy bài thử nhanh (mục 13, tiêu chí T1) trên 3–5 model rồi chốt danh sách.

### 5.4 Giới hạn của model free và cách xử lý

| Giới hạn | Tác động | Cách xử lý trong thiết kế |
| --- | --- | --- |
| Giới hạn theo **số request**: 20 request/phút; 50 request/ngày nếu tài khoản chưa nạp đủ 10 USD, 1.000 request/ngày nếu đã nạp tổng cộng từ 10 USD (kiểm tra lại trên trang OpenRouter vì có thể thay đổi) | Một run đủ 4 agent có thể dùng 15–40 request; với 50 request/ngày chỉ chạy được 1–2 run | Bộ đếm ngân sách request (5.5); cân nhắc nạp 10 USD một lần để nâng giới hạn |
| Request lỗi vẫn bị tính vào hạn mức | Retry bừa sẽ cạn quota nhanh | Retry có giới hạn, backoff theo phút |
| Model có thể biến mất hoặc nghẽn vào giờ cao điểm | Run đang chạy bị lỗi | Danh sách fallback, lưu checkpoint để chạy tiếp |
| Tool calling kém ổn định | Agent gọi tool sai | Đầu ra JSON + Pydantic, orchestrator thực thi hành động |
| Một số nhà cung cấp free có thể dùng prompt để huấn luyện | Lộ code hoặc dữ liệu | Chỉ dùng cho dự án demo; không đưa bí mật, khóa API, mã nguồn riêng tư |
| Chất lượng thấp hơn model trả phí | Thiết kế/code kém, test fail nhiều | Spec nhỏ, prompt chặt, schema chặt, giới hạn 3 vòng sửa |

### 5.5 Bộ đếm ngân sách request

- Đếm số request mỗi run và mỗi agent trong SQLite.
- Trước mỗi run, gọi endpoint thông tin key của OpenRouter (`GET /api/v1/key`) để đọc số request còn lại trong ngày; nếu không đủ cho ước tính của một run, từ chối và báo cho owner.
- Giới hạn cứng mỗi run (ví dụ 40 request) và mỗi agent (ví dụ 12). Vượt thì dừng và chuyển sang Gate 2.
- Giới hạn 20 request/phút: dùng rate limiter (token bucket) ở lớp LLM.

### 5.6 Phương án thay thế khi hết hạn mức

Có thể thêm provider thứ hai có gói free (qua cùng giao diện OpenAI-compatible) làm fallback ở cấp ứng dụng, hoặc dùng model chạy local qua Ollama cho Supervisor. Lớp LLM phải trừu tượng hóa provider ngay từ đầu để đổi dễ.

## 6. Orchestrator (LangGraph)

### 6.1 State

```python
class RunState(TypedDict):
    run_id: str
    spec_version: int
    request: str
    plan: dict | None
    design: dict | None
    code_files: dict[str, str]
    test_files: dict[str, str]
    test_result: dict | None   # exit_code, stdout, failures
    fix_rounds: int
    status: str                # running | waiting_approval | done | failed | cancelled
    request_budget_used: int
```

### 6.2 Node và cạnh

```
START → pm_plan → architect_design → gate_design (interrupt)
  gate_design --approve--> dev_implement → qa_run
  gate_design --reject---> architect_design (kèm góp ý của owner)
  qa_run --pass--> pm_report → END
  qa_run --fail & fix_rounds<3--> dev_fix → qa_run
  qa_run --fail & fix_rounds>=3--> gate_escalate (interrupt)
  gate_escalate --continue--> dev_fix | --change_spec--> pm_plan | --stop--> END
```

Mỗi node, khi bắt đầu và kết thúc, phát event `task.started` và `task.done` (hoặc `error`). Dùng checkpointer của LangGraph (SQLite) để dừng ở gate và chạy tiếp được sau khi khởi động lại tiến trình.

### 6.3 Đổi spec

`/spec <nội dung>` làm tăng `spec_version`, cập nhật `request`, và điều hướng graph về `pm_plan`. Task đang chạy được đánh dấu `cancelled` bằng event; kết quả cũ giữ lại làm lịch sử. Mỗi artifact lưu `spec_version` mà nó được tạo ra.

## 7. Event và lưu trữ

### 7.1 Schema event

```json
{
  "id": "evt_000123",
  "ts": "2026-11-15T10:00:00Z",
  "run_id": "run_001",
  "task_id": "task_004",
  "spec_version": 1,
  "agent": "developer",
  "type": "task.started",
  "payload": {}
}
```

### 7.2 Các loại event

| type | Khi nào | payload chính |
| --- | --- | --- |
| `run.created` / `run.finished` | Bắt đầu, kết thúc run | request, status |
| `spec.updated` | Owner đổi spec | version, diff ngắn |
| `task.started` / `task.done` | Agent bắt đầu, xong | tên task, tóm tắt |
| `task.progress` | Tiến độ trung gian | progress 0–1, text |
| `llm.call` | Mỗi lần gọi model | model, token, thời gian, thành công hay lỗi |
| `handoff` | Chuyển việc giữa agent | from, to, artifact |
| `artifact.created` | Sinh ra thiết kế, code, test | loại, đường dẫn, version |
| `test.result` | Sau khi chạy pytest | exit\_code, số pass/fail |
| `approval.requested` / `approval.resolved` | Gate | gate, quyết định |
| `message` | Chat giữa owner và agent | người gửi, nội dung |
| `blocked` / `error` | Bị chặn, lỗi | lý do |

### 7.3 Bảng SQLite

- `runs(id, request, status, spec_version, created_at, finished_at)`
- `events(id, ts, run_id, task_id, spec_version, agent, type, payload_json)`
- `artifacts(id, run_id, spec_version, kind, path, content_hash, created_at)`
- `llm_usage(id, run_id, agent, model, prompt_tokens, completion_tokens, latency_ms, ok)`
- `outbox(event_id, target, status, attempts, next_retry_at)`: hàng đợi gửi event sang n8n
- Bảng checkpoint do LangGraph quản lý.

`events` chỉ ghi thêm, không sửa, để phục vụ replay và audit.

## 8. Gateway API

| Phương thức | Đường dẫn | Chức năng |
| --- | --- | --- |
| POST | `/runs` | Tạo run từ yêu cầu của owner |
| GET | `/runs?status=<s>` | Liệt kê run theo trạng thái (n8n dùng cho nhắc gate) |
| GET | `/runs/{id}` | Trạng thái run, task hiện tại, kết quả cuối |
| GET | `/runs/{id}/events?after=<id>` | Lấy event (phân trang) |
| WS | `/runs/{id}/stream` | Nhận event theo thời gian thực (CLI, dashboard) |
| POST | `/runs/{id}/spec` | Đổi spec |
| POST | `/runs/{id}/approve` · `/reject` | Quyết định tại gate (kèm nhận xét) |
| POST | `/runs/{id}/ask` | Hỏi một agent cụ thể |
| POST | `/runs/{id}/messages` | Tin nhắn tự do của owner; Supervisor phân loại (mục 4.1) |
| POST | `/runs/{id}/cancel` | Dừng run |

MVP chỉ có một owner nên REST xác thực bằng một Bearer token cấu hình qua biến môi trường; n8n và CLI dùng chung token đó. Chiều ngược lại, webhook Gateway gửi sang n8n được ký riêng (mục 9.4).

## 9. Kênh chat: n8n làm lớp giao tiếp

### 9.1 Nguyên tắc

n8n là lớp mỏng giữa Telegram và Gateway: dịch tin nhắn và nút bấm thành lời gọi REST, và dịch event thành tin nhắn. n8n không gọi LLM, để không tiêu hạn mức request của OpenRouter, và không giữ state của run. Tin nhắn tự do của owner được chuyển nguyên văn tới `POST /runs/{id}/messages` để Supervisor phân loại.

### 9.2 Các workflow n8n

- **WF-IN (owner → hệ thống):** Telegram Trigger → kiểm tra `chat_id` thuộc danh sách trắng → Switch theo lệnh (`/run`, `/status`, `/spec`, `/approve`, `/reject`, `/ask`, `/log`) hoặc `callback_query` của nút bấm → HTTP Request tới Gateway → gửi xác nhận cho owner. Tin không khớp lệnh nào đi vào `/messages`.
- **WF-OUT (hệ thống → owner):** Webhook nhận event từ Gateway (kiểm tra chữ ký HMAC) → IF lọc loại event cần đẩy (`approval.requested`, `task.done` ở mốc lớn, `blocked`, `error`, `run.finished`) → node Telegram, gắn nhãn vai trò ở đầu tin, ví dụ `[QA] 12/15 test pass`. Log chi tiết xem bằng `/log`.
- **Gate:** khi nhận `approval.requested`, WF-OUT gửi tin kèm nút inline Approve/Reject; bấm nút kích WF-IN gọi `POST /runs/{id}/approve` hoặc `/reject`. Không dùng Wait node của n8n để giữ trạng thái chờ, vì trạng thái đó nằm trong checkpoint của LangGraph; n8n chỉ chuyển quyết định của owner.
- **Hỏi riêng một agent:** `@qa vì sao test fail?` hoặc `/ask qa ...` gọi `POST /runs/{id}/ask`; câu trả lời quay về qua WF-OUT dưới dạng event `message`.
- **WF-REMIND (tùy chọn):** Schedule Trigger mỗi 15 phút gọi `GET /runs?status=waiting_approval` và nhắc owner nếu gate chờ lâu.

### 9.3 CLI (giữ để debug)

Lệnh: `run "<yêu cầu>"`, `status`, `spec "<nội dung>"`, `approve`, `reject "<lý do>"`, `ask <agent> "<câu hỏi>"`. CLI gọi cùng REST của Gateway như n8n và in event theo thời gian thực qua WebSocket, mỗi dòng gắn nhãn agent. Có CLI thì kiểm thử lõi mà không cần n8n hay Telegram, nên làm CLI trước.

### 9.4 Giao kèo giữa Gateway và n8n

- **Gateway → n8n:** `POST $N8N_EVENT_WEBHOOK_URL`, body là event theo schema mục 7.1, kèm header `X-Signature` (HMAC-SHA256 của body bằng secret chung) và `X-Event-Id`. Thử lại tối đa 3 lần với backoff; n8n bỏ qua `X-Event-Id` đã xử lý (idempotent).
- **n8n → Gateway:** gọi REST ở mục 8 với header `Authorization: Bearer <token>`.
- **Khi n8n không nhận được event:** Gateway giữ event trong bảng `outbox` và gửi lại khi n8n hoạt động trở lại; run không bị chặn.
- **Phiên bản workflow:** export JSON của các workflow vào `n8n/workflows/` trong repo để quản lý bằng git.

## 10. Giao diện trực quan (giai đoạn sau MVP)

### 10.1 Dashboard 2D (giai đoạn 2)

React + Vite; kết nối WebSocket; gồm timeline event, danh sách task theo trạng thái, panel chat, nút approve/reject, thống kê token và số request. Làm trước 3D vì 3D không thay được bảng chi tiết.

### 10.2 Cảnh Three.js (giai đoạn 3)

- `@react-three/fiber` + `drei`, state bằng Zustand, mỗi event cập nhật store.
- Mỗi agent là một khối hoặc nhân vật trên bàn; trạng thái thể hiện bằng màu: idle (xám), working (xanh), waiting approval (vàng), error (đỏ).
- Event `handoff` vẽ gói công việc bay từ agent này sang agent khác; `task.progress` là vòng tiến độ quanh agent; `message` hiện bong bóng thoại ngắn.
- Bấm vào agent mở panel chi tiết. Hỗ trợ replay từ log event.
- Cảnh chỉ **đọc** event, không phát lệnh điều khiển, nên có thể làm sau mà không đổi lõi.

## 11. Sandbox và bảo mật

- Mỗi run có thư mục `workspace/<run_id>/`; chỉ ghi file trong thư mục đó, chuẩn hóa đường dẫn và từ chối `..` hoặc đường dẫn tuyệt đối.
- Chạy pytest bằng `subprocess` với timeout (ví dụ 60 giây), giới hạn bộ nhớ, tắt biến môi trường nhạy cảm (không truyền `OPENROUTER_API_KEY` vào tiến trình con).
- Code do model sinh ra là mã không tin cậy. Nếu có chạy trên máy cá nhân, nên đặt trong Docker hoặc VM khi mở rộng.
- Khóa API lưu trong biến môi trường hoặc `.env` (không commit); nhật ký không ghi khóa.
- Gate bắt buộc trước mọi hành động ngoài phạm vi workspace (MVP không có hành động như vậy).
- Webhook Gateway → n8n ký HMAC-SHA256 và được kiểm tra ở n8n; dùng đường dẫn webhook ngẫu nhiên, chỉ bật webhook production.
- Telegram: chỉ `chat_id` trong danh sách trắng được xử lý; bot token và secret lưu trong credential của n8n, không nằm trong file workflow đã export.
- n8n không có quyền vào thư mục workspace và không dùng node Execute Command trong các workflow chat.

## 12. Cấu trúc repo

```
maf/
  config/models.yaml
  src/maf/
    llm/        client.py, budget.py, parse.py
    agents/     supervisor.py, architect.py, developer.py, qa.py, prompts/
    graph/      state.py, build.py, gates.py
    events/     store.py, bus.py, schema.py, outbox.py
    sandbox/    runner.py, paths.py
    api/        app.py, routes.py, ws.py, n8n_hook.py
    chat/       cli.py
  n8n/
    workflows/  wf_in.json, wf_out.json, wf_remind.json
    README.md   (cách import workflow, tạo credential)
  workspace/    (do hệ thống tạo)
  tests/
  docker-compose.yml   (gateway + n8n)
  .env.example
```

## 13. Tiêu chí nghiệm thu

| Mã | Tiêu chí |
| --- | --- |
| T1 | Mỗi agent, khi gọi riêng với model được chọn, trả về JSON hợp lệ theo schema ít nhất 8/10 lần thử |
| T2 | Flow mục 2 chạy từ `run` đến `run.finished` mà không cần can thiệp ngoài việc approve Gate 1 |
| T3 | Pytest chạy trong sandbox, kết quả pass/fail lấy từ mã thoát; tạo được vòng sửa lỗi ít nhất một lần khi test fail |
| T4 | Dừng ở Gate 1, tắt và bật lại tiến trình, approve (từ CLI hoặc nút Telegram) thì run chạy tiếp (checkpoint hoạt động) |
| T5 | Lệnh `/spec` giữa chừng tạo spec v2 và run quay về bước thiết kế |
| T6 | Một model bị lỗi hoặc bị giới hạn thì hệ thống chuyển sang model fallback và ghi event `llm.call` thất bại |
| T7 | Vượt giới hạn request của run thì dừng ở Gate 2, không tiếp tục gọi model |
| T8 | Từ log event có thể dựng lại tuyến thời gian của run (ai làm gì, lúc nào, dùng model nào) |
| T9 | Từ Telegram, owner chạy được `/run`, `/status`, `/spec`, `/approve` qua WF-IN và nhận event qua WF-OUT với nhãn agent |
| T10 | Tắt n8n giữa chừng thì run vẫn chạy; bật lại thì event trong `outbox` được gửi bù, không trùng tin nhắn |
| T11 | Webhook sai chữ ký bị WF-OUT từ chối; `chat_id` lạ bị WF-IN bỏ qua |

## 14. Lộ trình

| Giai đoạn | Nội dung | Kết quả |
| --- | --- | --- |
| 0 – Chuẩn bị (1–2 ngày) | Tạo key OpenRouter, thử 3–5 model free với prompt của từng agent (T1), chốt `models.yaml`; cài n8n bằng Docker, tạo bot Telegram qua BotFather | Danh sách model chạy được; n8n và bot sẵn sàng |
| 1 – Lõi (khoảng 1 tuần) | LLM layer, event store, graph 4 agent, sandbox, CLI | Flow chạy qua CLI, chưa cần n8n (T2, T3, T6, T7, T8) |
| 2 – Gateway + n8n (khoảng 1 tuần) | Gateway, gate với checkpoint, đổi spec, outbox và webhook, WF-IN, WF-OUT | T4, T5, T9, T10, T11: chat hai chiều qua Telegram |
| 3 – Giao diện | Dashboard 2D, sau đó Three.js | Theo dõi trực quan |
| 4 – Mở rộng | Reviewer, DevOps, chạy song song, Docker, Postgres/Redis, WF-REMIND, Discord | Phiên bản gần thực tế |

## 15. Rủi ro và cách giảm

| Rủi ro | Giảm thiểu |
| --- | --- |
| Cạn hạn mức request/ngày | Bộ đếm ngân sách, nạp 10 USD một lần, provider dự phòng |
| Model free trả JSON sai hoặc kém chất lượng | Schema chặt, retry có giới hạn, prompt kèm ví dụ, spec nhỏ |
| Vòng lặp Dev–QA không hội tụ | Tối đa 3 vòng, sau đó Gate 2 |
| Model bị gỡ hoặc nghẽn | Fallback, cấu hình model tách khỏi code |
| Code sinh ra gây hại | Sandbox, timeout, thư mục riêng, không truyền khóa |
| Rò rỉ dữ liệu qua nhà cung cấp free | Chỉ dùng dữ liệu demo |
| Phạm vi phình to | Giữ đúng một flow ở MVP; mọi thứ khác ghi vào giai đoạn sau |
| n8n ngừng chạy hoặc mất kết nối | Outbox gửi lại sau; run không phụ thuộc n8n |
| Webhook bị giả mạo hoặc event gửi trùng | Ký HMAC, `X-Event-Id` idempotent, đường dẫn webhook ngẫu nhiên |
| Logic rải ở hai nơi (graph và workflow n8n) khó theo dõi | n8n chỉ làm giao tiếp (mục 3.3); export workflow vào git |
| Webhook cần địa chỉ công khai khi chạy local | Dùng ngrok hoặc reverse proxy, hoặc n8n cloud; xem câu hỏi mở 2 |

## 16. Câu hỏi mở

1. Ngôn ngữ cho flow demo là Python (đề xuất) hay ngôn ngữ khác?
2. n8n tự host bằng Docker (kiểm soát nhiều hơn, nhưng webhook cần địa chỉ công khai qua ngrok hoặc reverse proxy) hay dùng n8n cloud (có sẵn URL công khai)?
3. Có nạp 10 USD một lần để nâng giới hạn free lên 1.000 request/ngày hay giữ hoàn toàn miễn phí?
4. Gateway chạy trên máy cá nhân hay trên VPS (ảnh hưởng đến cách làm sandbox và địa chỉ webhook)?
