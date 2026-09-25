# L3A Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Luồng từ `inputs/<case_id>.json` đến output và trace:

```text
inputs/<case_id>.json
   │
   ▼
cli._run ──► _run_one_case (mở MCP session riêng, list_tools, tối đa 3 lần thử)
                 │
                 ▼
            workflow.solve_case
                 coordinator ── case_received
                   ├─ task_assigned ─► order_agent    ── get_order, get_order_items
                   ├─ handoff ───────► payment_agent  ── get_order_payments, get_payment_timeline
                   ├─ handoff ───────► shipment_agent ── get_shipment_summary
                   ├─ policy_agent   ── policy_decided
                   ├─ handoff ───────► verifier       ── verification_completed
                 coordinator ── case_finalized
                 │
                 ▼
contracts.validate_output ──► outputs/<case_id>.json (ghi .tmp rồi replace)
TraceWriter.emit ───────────► traces/trace.jsonl (validate từng event)
```

Mọi specialist gọi MCP qua `EvidenceGateway.call`. Gateway luôn gửi kèm `case_id` và validate evidence theo contract trước khi trả về cho agent.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Coordinator | Case JSON | Nhận case, lấy `order_id` từ input, giao việc cho specialist theo thứ tự, gộp evidence, dựng output cuối | `task_assigned`/`handoff` tới specialist; `case_finalized` |
| Order/item | `case_id`, `order_id` | Lấy trạng thái đơn và danh sách item | Evidence order + items; `order_ids`, `item_ids` |
| Payment | `case_id`, `order_id` | Lấy các giao dịch thanh toán và timeline thanh toán | Evidence payments + timeline |
| Shipment | `case_id`, `order_id` | Lấy tóm tắt vận chuyển | Evidence shipment |
| Policy | Claims của case + toàn bộ evidence | Xác định `primary_issue`, `responsible_party`, `case_status`, `confidence`, actions | `policy_decided`; handoff tới verifier |
| Verifier | Kết quả policy + `evidence_refs` | Giới hạn confidence khi thiếu evidence và kẹp về [0, 1] | `verification_completed` |

Quyền gọi tool (áp dụng trong `workflow.py`):

| Actor | Tool được gọi |
| --- | --- |
| order_agent | `get_order`, `get_order_items` |
| payment_agent | `get_order_payments`, `get_payment_timeline` |
| shipment_agent | `get_shipment_summary` |
| coordinator, policy_agent, verifier | Không gọi MCP, chỉ dùng evidence đã được specialist lấy |

Các tool còn lại trên gateway (`get_policy`, `get_sellers`, `get_refund_timeline`, `get_customer_history`, `get_product_context`) hiện chưa được agent nào sử dụng.

## 3. A2A protocol

- **Envelope:** mỗi message là một trace event `day09-trace-event-v1`, gồm `event_id` ngẫu nhiên, `case_id`, `event_type`, `actor`, `occurred_at` và các trường tùy chọn `target`, `tool_name`, `decision_code`, `evidence_refs`, `attributes`.
- **Correlation:** mọi event và mọi MCP call đều mang `case_id` của case đang xử lý. Mỗi case dùng một MCP session riêng.
- **Handoff:** coordinator chuyển tuần tự order → payment → shipment. Mỗi handoff kèm `evidence_refs` mà bước trước đã thu thập. Policy chuyển sang verifier kèm `decision_code`.
- **Tránh vòng lặp:** luồng là một pipeline tuyến tính cố định, không có agent nào gọi ngược lại agent trước.
- **Timeout:** HTTP client dùng timeout đọc 300s, kết nối/ghi/pool 30s (`mcp_gateway.connect_gateway`).
- Trace chỉ ghi sự kiện và decision code quan sát được, không ghi prompt hay nội dung suy luận.

## 4. Evidence lifecycle

1. Specialist gọi `gateway.call(tool, case_id=..., order_id=...)`.
2. Gateway đọc `structuredContent` (hoặc một text block JSON duy nhất) và chạy `contracts.validate_evidence`. Response không hợp lệ sẽ gây exception.
3. `_find_refs` lấy `evidence_ref` từ response. Hệ thống không tự tạo hay sửa ref.
4. Ngay sau mỗi call, specialist emit `tool_result_consumed` kèm `tool_name` và ref vừa nhận.
5. Coordinator gộp và khử trùng lặp ref từ tất cả specialist, rồi đưa vào `evidence_refs` của output (tối đa 30) và của từng claim assessment (tối đa 10).
6. Evidence chỉ sống trong phạm vi một lần gọi `solve_case`, không được lưu hay dùng lại giữa các case.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout | Có, tối đa 3 lần/case, chờ 2s × lần thử | Sau 3 lần: bỏ case, in `[FAILED]`, chạy tiếp case sau | Không có event riêng; log `[WARN]`/`[RETRY]` ra stderr |
| Not found | Có (tool lỗi → `RuntimeError`) | Như trên; case không có output | Không có event riêng |
| Source conflict | Không | Chưa xử lý; `data_conflicts` luôn rỗng | — |
| Invalid specialist result | Có (evidence sai contract → exception) | Như trên | Không có event riêng |
| Thiếu `order_id` trong input | Có, nhưng lần nào cũng lỗi giống nhau | Case thất bại | — |
| Không có evidence ref | Không | Policy trả `insufficient_evidence`, confidence ≤ 0.30 | `policy_decided` với `insufficient_evidence` |

Mọi MCP call đều là thao tác đọc nên retry là idempotent. Output hợp lệ đã có sẵn sẽ được bỏ qua khi chạy lại. Hạn chế đã biết: event của lần thử thất bại vẫn nằm trong `trace.jsonl`.

## 6. Verification invariants

Được kiểm tra trước khi ghi output:

- **Schema:** output validate theo `day09-l3a-output-v2` và từng trace event theo `day09-trace-event-v1`.
- **Entity scope:** `output.case_id` phải khớp case đang chạy; `order_ids` gồm order được claim và order trong evidence.
- **Evidence ownership:** ref chỉ lấy từ MCP response của chính case đó.
- **Claim linkage:** mỗi claim trong input có một `claim_assessments` với verdict và ref.
- **Confidence bounds:** kẹp về [0, 1]; không có evidence thì ≤ 0.30; `insufficient_evidence` thì ≤ 0.60.
- **Responsibility/action consistency:** `resolution_actions` suy ra từ policy; `no_action` thì dùng `no_action_required`.
- **Money totals:** chưa kiểm tra; `recommended_refund_brl` hiện cố định là `0.0`.

Khi đóng gói, `submission.validate_artifacts` kiểm tra lại: đủ output cho mọi case trong case-set, không trùng `event_id`, không có event ngoài case-set, và không lộ Team API Key.

## 7. Reproducibility

- **Model/config:** không dùng LLM; policy là luật xác định (deterministic). Cấu hình qua `.env`: `COMPETITION_API_URL`, `COMPETITION_TEAM_API_KEY`, `MCP_ENDPOINT`.
- **Dependencies:** Python ≥ 3.11; `httpx2>=2,<3`, `jsonschema[format]>=4.25,<5`, `mcp>=2,<3`, `python-dotenv>=1.1,<2`. Chạy được trên CPU, không cần GPU.
- **Concurrency:** chạy tuần tự từng case, mỗi case một MCP session.
- **Random seed:** không có; chỉ `event_id` là ngẫu nhiên.
- **Lệnh chạy:**

```bash
python3.11 -m venv .venv && source .venv/bin/activate
python -m pip install -e ".[dev]"
day09 validate-inputs
day09 run
day09 validate
day09 package --output dist/submission.zip
```

- **Giới hạn:** mỗi file trong bài nộp ≤ 1 MB, tổng ≤ 12 MB chưa nén.
