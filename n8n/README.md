# Nối n8n và Telegram

n8n tại `http://localhost:5678` dùng hai workflow trong `workflows/`. Gateway tiếp tục chạy trên máy host, còn 9Router giữ cấu hình hiện tại. Container `maf-n8n` đã có `host.docker.internal:host-gateway`.

## 1. Bot và webhook Telegram

1. Mở chat [@BotFather](https://t.me/BotFather), gửi `/newbot`, tạo bot và giữ token. Trong n8n, tạo credential **Telegram API** bằng token này.
2. Dùng reverse proxy hoặc tunnel HTTPS có địa chỉ public trỏ về n8n cổng `5678`. Trên dịch vụ n8n trong Compose hiện có, đặt:

   ```yaml
   environment:
     N8N_WEBHOOK_URL: https://hooks.example.com/
     N8N_PROXY_HOPS: "1"
   ```

   Thay domain bằng URL thật. Nếu có nhiều proxy, đặt số hop tương ứng. Giữ các biến hiện có và recreate riêng dịch vụ n8n. Với n8n 2.47.7, dùng `N8N_WEBHOOK_URL`. Chỉ mở đường dẫn webhook của **Telegram Trigger** qua proxy nếu có thể. Giữ editor, Gateway cổng 8000 và webhook nội bộ `/webhook/maf-events` ở mạng local.

3. Lấy `chat_id` của cuộc chat riêng với bot. Nếu chưa biết, import workflow incoming, gắn credential Telegram, tạm bỏ bộ lọc **Restrict to Chat IDs** và dùng **Listen for test event**. Nhắn bot `/start` rồi đọc `message.chat.id` trong output của Trigger. Điền ID lại vào bộ lọc trước khi publish. Telegram giữ một webhook cho mỗi bot, nên dùng URL production sau khi hoàn tất thử nghiệm.

[Tài liệu Telegram Trigger](https://docs.n8n.io/integrations/builtin/trigger-nodes/n8n-nodes-base.telegramtrigger/common-issues) · [Cấu hình webhook sau reverse proxy](https://docs.n8n.io/deploy/host-n8n/configure-n8n/basic-configuration/configuration-examples/configure-webhook-urls-with-reverse-proxy/)

## 2. Gateway

Thêm vào `.env` của repo:

```dotenv
MAF_TELEGRAM_CHAT_ID=CHAT_ID_CUA_BAN
MAF_N8N_EVENT_URL=http://127.0.0.1:5678/webhook/maf-events
MAF_N8N_WEBHOOK_SECRET=CHUOI_NGAU_NHIEN_RIENG
```

Tạo secret bằng `openssl rand -hex 32`. Gateway dùng secret để ký và xác thực từng event. n8n chuyển payload và chữ ký về Gateway để kiểm tra, nên credential Telegram và secret ký được lưu riêng. Giữ nguyên `MAF_GATEWAY_TOKEN` đã có.

Dừng Gateway cũ rồi mở lại để container n8n truy cập:

```bash
LANGSMITH_TRACING=false PYTHONPATH=src .venv/bin/python -m uvicorn \
  maf.gateway:create_app --factory --host 0.0.0.0 --port 8000
```

Chỉ cho mạng local cần thiết hoặc Docker bridge truy cập cổng 8000. Có thể bind trực tiếp vào IP gateway của bridge nếu bạn muốn giới hạn interface. Từ n8n, các workflow gọi `http://host.docker.internal:8000`.

## 3. Import và publish

1. Trong **Workflows**, chọn **Import from File** cho `wf_in.json` và `wf_out.json`.
2. Với tất cả node **Telegram** và **Telegram Trigger**, chọn cùng credential Telegram API, kể cả **Remove approval buttons**. Trong trigger, thay `REPLACE_WITH_CHAT_ID` bằng ID ở bước 1.
3. Tạo credential **Header Auth** (tên tùy ý) với header `Authorization` và value `Bearer <MAF_GATEWAY_TOKEN>`. Chọn credential này ở cả ba node **Gateway command**, **Verify event**, **Confirm delivery**.
4. Publish workflow **MAF · Telegram notifications** trước rồi publish **MAF · Telegram incoming**.
5. Tạo project trong dashboard. Nhắn bot `/projects` để lấy ID rồi gửi `/run PROJECT_ID Viết hàm Python cộng hai số nguyên`. Chờ kế hoạch task, bấm **Duyệt**, rồi đợi báo cáo cuối. Nếu cần xem lại, dùng `/status RUN_ID` hoặc `/log RUN_ID`.

Gateway chỉ xử lý chat riêng khớp `MAF_TELEGRAM_CHAT_ID`. Sự kiện gửi Telegram nằm trong SQLite outbox, được thử lại với thời gian chờ tăng dần nếu n8n chưa nhận. Tin đã có biên nhận được bỏ qua khi n8n nhận lại event. Nếu Telegram nhận tin nhưng n8n mất kết nối trước khi ghi biên nhận, tin có thể xuất hiện lại. Các nút mang ID của cổng duyệt, nên nút cũ không áp dụng sang cổng mới.

Khi bấm nút duyệt, workflow incoming xác nhận thao tác và chỉnh tin nhắn gốc để gỡ cả hai nút. Gateway ghi ID update và quyết định theo cổng vào SQLite để bỏ qua lần gửi lại. Gửi lại cùng nội dung `/run` khi run tương ứng vẫn đang xử lý hoặc chờ duyệt sẽ trả về ID run đang có. Một yêu cầu mới có thể tạo run khác sau khi run trước kết thúc.

Nút **Yêu cầu sửa** hỏi bạn muốn thay đổi gì và giữ run ở cùng cổng duyệt. Gửi `/reject RUN_ID <góp ý>` để PM lập lại kế hoạch. Bạn có thể gửi `/approve RUN_ID` nếu muốn duyệt kế hoạch hiện tại.

Gửi `/pm` để mở chat với Supervisor hoặc bắt đầu bằng `/pm <tin nhắn>`. Supervisor tự chọn tra danh sách run hoặc đọc checkpoint, timeline và kết quả QA khi câu hỏi cần dữ liệu. Nếu bạn giao rõ một việc triển khai phần mềm, Supervisor có thể tạo run từ câu nhắn tự nhiên. Run đó vẫn chờ bạn duyệt kế hoạch task trước khi đội bắt đầu. Gateway dùng tám lượt hỏi đáp gần nhất làm ngữ cảnh. Gửi `/pm off` để thoát chat. Các lệnh `/run` và `/approve` vẫn dùng được trong phiên.

Supervisor có tối đa bốn bước để chọn công cụ và trả lời, với tổng ngân sách gọi model 120 giây. Khi Supervisor tạo run, Gateway trả ID ngay. Worker lập kế hoạch và gửi cập nhật qua workflow thông báo. Telegram nhận thêm báo cáo mỗi khi một task hoàn tất.
