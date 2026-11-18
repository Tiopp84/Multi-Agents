# Phòng phát triển phần mềm theo project

Supervisor/PM nhận PRD, lập kế hoạch task cho BA, Dev và QA, theo dõi kết quả và xử lý yêu cầu thay đổi. Các model chính và fallback đều đi qua 9Router.

## Khởi động

Chuẩn bị Python từ 3.11, Docker, Node.js cho project Node và Zenity trên Linux. Cài dependency trong môi trường Python:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e . 'langgraph-cli[inmem]'
```

Điền `.env` theo `.env.example`, rồi chạy:

```bash
make run
```

Dashboard ở http://127.0.0.1:8000/dashboard. Studio API ở cổng 2025 và in URL mở Studio. Dùng `make stop` hoặc Ctrl+C để dừng. Có thể chạy riêng `make gateway` hoặc `make studio`. n8n và 9Router được quản lý riêng.

Gateway nghe trên localhost và Docker host 172.17.0.1 để n8n gọi qua host.docker.internal. Có thể đổi `GATEWAY_PORT`, `STUDIO_PORT`, `GATEWAY_DOCKER_HOST`. `MAF_GATEWAY_URL` chỉ địa chỉ Gateway khi Studio/CLI dùng cổng khác. Chạy một Gateway worker để điều phối workspace.

LangSmith tracing được bật mặc định và dùng `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT` trong `.env`. Trace gửi dữ liệu graph tới LangSmith. Dùng `make run LANGSMITH_TRACING=false` để tắt.

## Quy trình làm việc

1. Tạo project trong dashboard, chọn workspace bằng hộp thoại hệ điều hành và nhập mục tiêu. Gateway cần quyền truy cập phiên desktop. `MAF_WORKSPACE_ROOT` giới hạn vùng workspace, mặc định home của người chạy Gateway.
2. Tạo run, chọn project và dán PRD/yêu cầu. Gateway trả ID ngay, PM lập kế hoạch ở background. Lời chào và yêu cầu chưa rõ được trả lời hoặc hỏi làm rõ.
3. Xem Gantt và task trên dashboard. Mỗi task có vai trò BA, Dev hoặc QA, tiêu chí nghiệm thu, phụ thuộc, ước lượng và ngân sách gọi model. Duyệt kế hoạch trước khi worker thực thi.
4. Các task chạy lần lượt theo phụ thuộc. Mỗi lượt agent chọn một công cụ hoặc báo kết quả. Timeline ghi bàn giao, thông điệp, gọi model, đọc/ghi file, command và output. Telegram nhận thông báo hoàn tất từng task và báo cáo cuối.
5. Có thể tạm dừng, tiếp tục, dừng hoặc gửi thay đổi cho PM qua chat/dashboard. Bước đang thực hiện được chốt trước khi lập lại kế hoạch. Kế hoạch mới cần được duyệt. Một run cập nhật workspace tại một thời điểm.

BA dùng cấu hình vai trò `architect` để phân tích yêu cầu và lưu tài liệu. Dev triển khai trực tiếp trên workspace, hỗ trợ nhiều stack. QA phải có bằng chứng lệnh kiểm thử thành công trước khi hoàn tất. Khi không đạt tiêu chí, agent đánh dấu blocked để owner/PM xử lý.

## Quyền công cụ và hạn mức

Trong **Cấu hình agent**, chỉnh rules, skills, model/fallback và công cụ của từng vai trò. Supervisor có quyền đọc project/run, tạo run, gửi thay đổi và điều khiển run. BA, Dev, QA có công cụ liệt kê/đọc/ghi file và terminal. Danh sách cho phép được kiểm tra ở schema trước khi thực thi.

Command chạy Bash trên máy Gateway, thư mục làm việc là workspace project, với timeout tối đa 300 giây. Agent có thể cài dependency và chạy test/ứng dụng theo task. Đây là quyền thực thi trên máy host. Các biến API key của Gateway không được truyền qua môi trường command. Các lệnh nên chạy tới khi kết thúc, không tạo dịch vụ chạy ngầm.

Mặc định mỗi run có 200 lượt gọi và tối đa 6 lượt/phút. Có thể chỉnh khi tạo run. Mỗi task có ngân sách riêng do PM đề xuất. Retry và fallback cũng được tính. Hết ngân sách hoặc lỗi thì run blocked, owner xem log rồi cấp thêm lượt bằng **Tiếp tục**. Đây là hạn mức nội bộ cho từng run, cần chọn phù hợp quota thực tế của 9Router. Gantt là ước lượng, có tính thời gian tối thiểu theo lượt gọi, không phải cam kết deadline.

## Dữ liệu và khôi phục

Trong `<workspace>/.maf/`:

- `department.sqlite` lưu kế hoạch, task, kết quả công cụ, lượt gọi và trạng thái tiếp tục.
- `events.sqlite` lưu timeline của project.
- `plans/<run_id>/` lưu các phiên bản kế hoạch.
- `commands/<run_id>/` lưu toàn bộ command output.
- `changes/<run_id>/` lưu bản trước/sau mỗi lần agent ghi file bằng công cụ.

Gateway giữ danh mục project, chỉ mục event và outbox trong `.maf/` của ứng dụng. File nguồn và tài liệu được agent ghi trực tiếp vào workspace. Sau khi Gateway khởi động lại, task đã hoàn tất được giữ lại. Bước bị ngắt giữa chừng được đánh dấu blocked để kiểm tra trước khi tiếp tục, tránh tự chạy lại một command có tác dụng phụ.

## PM, CLI và Studio

Trên Telegram, gửi `/projects` lấy ID, `/run PROJECT_ID yêu cầu` để bắt đầu hoặc `/pm` rồi trao đổi tự nhiên. PM có thể tra kế hoạch và task, ghi nhận thay đổi yêu cầu, tạm dừng hoặc tiếp tục run theo chỉ đạo. Xem [cấu hình n8n](n8n/README.md).

```bash
PYTHONPATH=src .venv/bin/python -m maf.cli run --project PROJECT_ID "Xây dựng ứng dụng kho ExpressJS và SQLite"
PYTHONPATH=src .venv/bin/python -m maf.cli status RUN_ID
PYTHONPATH=src .venv/bin/python -m maf.cli approve RUN_ID --gate-id GATE_ID
```

Studio dùng chung department qua Gateway. Nhập `project_id` và `request` để tạo run, hoặc `project_id` và `run_id` để đọc trạng thái run có sẵn. Gantt và timeline công cụ trực tiếp nằm ở dashboard.

## Kiểm thử

```bash
LANGSMITH_TRACING=false PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```
