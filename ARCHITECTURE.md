# L3A Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Luồng từ `inputs/<case_id>.json` đến output và trace:

```text
inputs/<case_id>.json
   │
   ▼
cli._run ──► _run_one_case (MCP session riêng, list_tools, tối đa 3 lần thử, trace buffer theo case)
                 │
                 ▼
            workflow.solve_case
                 coordinator ── case_received
                   ├─ task_assigned ─► order_agent    ── get_order, get_order_items      ─ handoff ─► coordinator
                   ├─ task_assigned ─► seller_agent   ── get_sellers                     ─ handoff ─► coordinator
                   ├─ task_assigned ─► payment_agent  ── get_order_payments, get_payment_timeline,
                   │                                     (get_refund_timeline khi cần)   ─ handoff ─► coordinator
                   ├─ task_assigned ─► shipment_agent ── get_shipment_summary            ─ handoff ─► coordinator
                   ├─ task_assigned ─► policy_agent   ── get_policy ── policy_decided    ─ handoff ─► verifier
                   └─ verifier ── verification_completed ─ handoff ─► coordinator
                 coordinator ── case_finalized
                 │
                 ▼
contracts.validate_output ──► outputs/<case_id>.json (ghi .tmp rồi replace)
TraceWriter (buffer) ───────► traces/trace.jsonl (chỉ ghi khi case thành công)
```

Mọi specialist gọi MCP qua `EvidenceGateway.call`. Gateway luôn gửi kèm `case_id` và validate evidence theo `day09-mcp-evidence-v1` trước khi trả về cho agent. Evidence của một case được giữ trong `CaseEvidence` (theo tên tool) và chỉ sống trong một lần gọi `solve_case`.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Coordinator | Case JSON | Lấy `claimed_order_id`, `claims`, `policy_version`; giao việc tuần tự; dựng output cuối | `case_received`, `task_assigned`, `case_finalized` |
| order_agent | `case_id`, `order_id` | Trạng thái đơn, mốc thời gian, item, giá, phí vận chuyển, `shipping_limit_date` | handoff kèm ref + `order_status` |
| seller_agent | `order_id` | Hồ sơ seller của các item | handoff kèm ref |
| payment_agent | `order_id`, claim topics | Payment gốc, lifecycle event (authorized/captured/…); gọi `get_refund_timeline` chỉ khi timeline có refund hoặc claim về refund (đơn không có refund làm tool này trả lỗi) | handoff kèm ref |
| shipment_agent | `order_id` | Ngày giao carrier, ngày giao khách, ngày dự kiến, shipment id | handoff kèm ref |
| policy_agent | `policy_version` + toàn bộ evidence | Tra policy công khai, chạy detector, chọn `primary_issue`, bên chịu trách nhiệm, refund, actions, confidence | `policy_decided`; handoff tới verifier |
| verifier | Finding + evidence | Kiểm tra invariant (mục 6), chỉnh nếu vi phạm | `verification_completed` với mã điều chỉnh hoặc `INVARIANTS_OK` |

Quyền gọi tool:

| Actor | Tool |
| --- | --- |
| order_agent | `get_order`, `get_order_items` |
| seller_agent | `get_sellers` |
| payment_agent | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` |
| shipment_agent | `get_shipment_summary` |
| policy_agent | `get_policy` |
| coordinator, verifier | Không gọi MCP |

Không dùng `get_customer_history` và `get_product_context`: mọi case đều có `claimed_order_id` và không có kết luận nào cần dữ liệu sản phẩm, nên gọi thêm chỉ làm loãng evidence.

## 3. A2A protocol

- **Envelope:** mỗi message là một trace event `day09-trace-event-v1` (`event_id` ngẫu nhiên, `case_id`, `event_type`, `actor`, `occurred_at`, và tùy chọn `target`, `tool_name`, `decision_code`, `evidence_refs`, `attributes`).
- **Giao việc / trả kết quả:** coordinator phát `task_assigned` (target = agent); agent làm xong phát `handoff` về coordinator kèm các ref nó đã lấy. Policy handoff sang verifier kèm `decision_code`; verifier handoff về coordinator.
- **Correlation:** mọi event và MCP call mang `case_id` của case đang xử lý; mỗi case một MCP session.
- **Tránh vòng lặp:** pipeline tuyến tính cố định, không agent nào gọi ngược agent trước.
- **Timeout:** HTTP đọc 300s, connect/write/pool 30s (`mcp_gateway.connect_gateway`).
- Trace chỉ ghi sự kiện và decision code quan sát được (vd `attributes.domain`, `case_status`, `refund_brl`), không ghi prompt hay nội dung suy luận.

## 4. Evidence lifecycle

1. Specialist gọi `gateway.call(tool, case_id=..., order_id=...)`.
2. Gateway đọc `structuredContent` (hoặc một text block JSON) và chạy `contracts.validate_evidence`.
3. Ngay sau call, specialist emit `tool_result_consumed` với `tool_name`, đúng ref vừa nhận và `domain`.
4. Policy agent chọn các tool **thật sự hỗ trợ** kết luận. `order` và `order_items` được cite cho mọi issue về đơn hàng vì là thực thể gốc; cộng thêm nhóm theo issue: payment (payments + payment timeline) cho các issue về tiền và cho trễ giao (số tiền hoàn lấy từ capture), shipment + item cho trễ giao, seller khi seller chịu trách nhiệm, refund cho refund pending/failed, và policy khi policy quyết định action/số tiền. Chỉ ref của các tool đó được cite trong `evidence_refs` và `claim_assessments`; nếu các tool đó không có ref thì dùng domain liên quan (`ISSUE_DOMAINS`).
5. Hệ thống không tự tạo, sửa hay dùng lại ref giữa các case.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace |
| --- | --- | --- | --- |
| `get_order` lỗi / timeout | Có, tối đa 3 lần/case, chờ 2s × lần thử | Sau 3 lần: bỏ case, in `[FAILED]`, chạy tiếp | Event của lần thử lỗi bị bỏ (buffer) |
| Tool phụ lỗi (vd `get_refund_timeline` khi đơn không có refund) | Không | Bỏ qua tool đó, detector làm việc với evidence còn lại | Không có `tool_result_consumed` cho tool đó |
| Evidence sai contract | Có (exception → retry case) | Như dòng 1 | Như dòng 1 |
| Không detector nào khớp | Không | `unsupported_claim`, `no_action`, confidence 0.75 | `policy_decided` |
| Không có `get_order` | Không | `insufficient_evidence`, confidence 0.30 | `policy_decided` |
| Source conflict (dòng nhiễu, số tiền evidence ≠ policy) | Không | Lọc theo cửa sổ thời gian; số tiền theo policy; mismatch ghi `data_conflicts` | `policy_decided` |

Mọi MCP call là thao tác đọc nên retry idempotent. `day09 run` bỏ qua case đã có output hợp lệ; sau khi sửa logic phải xóa `outputs/*.json` và `traces/trace.jsonl` rồi chạy lại toàn bộ.

## 6. Policy và verification invariants

**Lọc nhiễu.** Evidence của gateway trộn các dòng thuộc đơn với các dòng mang mốc thời gian không liên quan (capture tháng 5 cho đơn tháng 12, `shipping_limit` hoặc refund lệch hàng tuần). Agent chỉ dùng:

- payment event (capture, `reconciliation_mismatch`) trong ±1 ngày quanh `order_approved_at`, bỏ dòng trùng y hệt;
- item, shipping limit, refund và shipment event trong cửa sổ [ngày mua − 1 ngày, max(ngày giao, ngày dự kiến) + 3 ngày].

**Detector** (chạy theo thứ tự, claim của khách không quyết định kết quả):

| Issue | Điều kiện trên evidence đã lọc |
| --- | --- |
| canceled / unavailable_order_paid | `order_status` = canceled / unavailable và có capture |
| late_delivery_seller | giao khách > ngày dự kiến và giao carrier > `shipping_limit` của item; đối chiếu `actor` của event `delivered_late` |
| late_delivery_logistics | giao khách > ngày dự kiến, seller giao carrier đúng hạn |
| refund_failed / refund_pending | refund event trong cửa sổ có status failed / pending |
| payment_mismatch | có `reconciliation_mismatch` đang mở |
| valid_split_payment | ≥ 2 capture có tổng = tổng (price + freight) của item |
| duplicate_charge | ≥ 2 capture cùng số tiền, tổng vượt tổng đơn |
| unsupported_claim | không detector nào khớp |

**Policy.** `get_policy(EC_POLICY_V1)` quy định cho từng issue: `case_status`, `recommended_action` (dùng làm `resolution_actions` và `reason_code`), `responsible_parties` và `refund_brl`. Số tiền tính từ evidence được đối chiếu với policy; nếu lệch thì dùng policy và hạ confidence. `party_id` của seller lấy từ evidence.

**Claim `requested_full_refund`:** `supported` khi issue hoàn toàn bộ (hủy đơn, hết hàng, refund lỗi); `partially_supported` khi chỉ hoàn một phần hoặc refund đang xử lý; `unsupported` khi không hoàn.

Verifier kiểm tra trước khi ghi output:

- **Schema:** output theo `day09-l3a-output-v2`, từng trace event theo `day09-trace-event-v1`.
- **Status/refund/action:** `no_action` ⇒ refund 0 và action `no_action_required`; `recommended_refund_brl` = tổng `refund_lines`.
- **Seller responsibility:** `party_type = seller` ⇒ `party_id` là seller id thật (seller giao trễ hoặc lấy từ evidence); nếu không có thì hạ confidence ≤ 0.6.
- **Evidence linkage:** mọi ref cite đều có trong `tool_result_consumed` của cùng case; không có ref hỗ trợ ⇒ confidence ≤ 0.3.
- **Claim linkage:** mỗi claim có một `claim_assessments`; `requested_full_refund` được đối chiếu với số tiền hoàn đề xuất.
- **Confidence:** kẹp [0, 1]; khi kết luận khác claim của khách, confidence ≤ 0.75.

`scripts/self_check.py` kiểm tra lại các invariant trên toàn bộ outputs và trace (và provenance/nhãn khi chạy với server local). `submission.validate_artifacts` kiểm tra khi đóng gói.

## 7. Reproducibility

- **Model/config:** không dùng LLM; policy là luật xác định. Cấu hình qua `.env`: `COMPETITION_API_URL`, `COMPETITION_TEAM_API_KEY`, `MCP_ENDPOINT` (biến môi trường cùng tên ghi đè `.env`).
- **Dependencies:** Python ≥ 3.11; `httpx2`, `jsonschema[format]`, `mcp` 2.x, `python-dotenv`. Chỉ cần CPU.
- **Concurrency:** tuần tự từng case; không có random seed ngoài `event_id`.
- **Server local để phát triển:** `scripts/local_mcp_server.py` cung cấp đủ 10 tool cùng contract, dữ liệu mô phỏng theo schema Olist, audit log và nhãn kỳ vọng trong `.local_mcp/`. Ref của server local **không hợp lệ** với server thi.
- **Lệnh chạy với server thi:**

```bash
source .venv/bin/activate
day09 validate-inputs
rm -f outputs/*.json traces/trace.jsonl
day09 run
day09 validate
python scripts/self_check.py
day09 package --output dist/submission.zip
```

- **Giới hạn:** mỗi file trong bài nộp ≤ 1 MB, tổng ≤ 12 MB chưa nén.
