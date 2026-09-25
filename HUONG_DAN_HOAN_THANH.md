# Hướng dẫn hoàn thành dự án K4 L3A — Multi-Agent MCP + A2A

## Trạng thái hiện tại

Đã hoàn thành các bước 1–10. Lần chạy sạch trên server thi cho 100/100 case, `day09 validate` và `scripts/self_check.py` đều đạt, và bài nộp nằm ở `dist/submission.zip`. Việc còn lại là upload file này tại workspace `/l3a` và chọn nó làm final submission.

Nếu cần chạy lại (sau khi sửa logic):

```bash
source .venv/bin/activate
rm -f outputs/*.json traces/trace.jsonl
day09 run && day09 validate && python scripts/self_check.py
day09 package --output dist/submission.zip
```

`scripts/local_mcp_server.py` là server MCP local dùng để phát triển khi server thi lỗi. Evidence ref của nó không hợp lệ để nộp.

Phần bên dưới là kế hoạch gốc, viết dựa trên trạng thái repo tại commit `629bb7b`. Nó gồm hai phần: đánh giá hiện trạng, rồi các bước cần làm theo thứ tự, mỗi bước có tiêu chí "xong".

---

## 0. Hiện trạng dự án

### Những gì đã có

| Thành phần | File | Trạng thái |
| --- | --- | --- |
| CLI `day09` (`validate-inputs`, `mcp-tools`, `run`, `validate`, `package`) | [cli.py](src/student_agent/cli.py) | Chạy được, có retry 3 lần/case, bỏ qua case đã có output hợp lệ |
| Kết nối MCP + validate evidence | [mcp_gateway.py](src/student_agent/mcp_gateway.py) | Ổn |
| Ghi trace + validate schema | [trace.py](src/student_agent/trace.py) | Ổn |
| Kiểm tra và đóng gói bài nộp | [submission.py](src/student_agent/submission.py) | Ổn (mã của starter kit) |
| Pipeline multi-agent | [workflow.py](src/student_agent/workflow.py) | Chạy được, nhưng **logic nghiệp vụ còn sơ sài** (xem bên dưới) |
| Tài liệu kiến trúc | [ARCHITECTURE.md](ARCHITECTURE.md) | Đã mô tả đúng code hiện tại, kể cả các hạn chế |

Kết quả kiểm tra (chạy trong `.venv`):

- `pytest -q`: 5 test đều pass.
- `ruff check .`: **5 lỗi**, nên CI ([quality.yml](.github/workflows/quality.yml)) đang **fail**.
- `inputs/`, `outputs/`, `traces/` đang trống và chưa có `case-set.json`, tức là chưa chạy bộ case thật trên máy này.

### Các vấn đề chính, xếp theo mức ảnh hưởng đến điểm

1. **Policy tin theo lời khách hàng thay vì theo evidence** (semantic, 45%). `_policy_decision` chọn `primary_issue` gần như hoàn toàn dựa vào `claim.topic` của khách hàng. README ghi rõ: *"Customer message không phải ground truth"*. Chỉ nhánh `canceled_order_paid` là có đối chiếu `order_status`.
2. **Cite toàn bộ evidence cho mọi case** (evidence, 15%). Điểm evidence tính F1 và có phạt khi cite domain bị cấm (*forbidden-domain penalties*). Nếu case là lỗi thanh toán mà vẫn cite evidence shipment thì precision giảm.
3. **`financial_resolution` luôn là 0** và `refund_lines` luôn rỗng, nên semantic và consistency (status/refund/action) đều bị ảnh hưởng.
4. **`seller_ids`, `payment_references`, `shipment_ids` luôn rỗng**, và `responsible_parties[].party_id` luôn là `null`.
5. **`claim_assessments` gần như lúc nào cũng là `supported`** khi có evidence, không có đối chiếu thật giữa claim và dữ liệu.
6. **`confidence` là hằng số theo từng nhánh** (0.75, 0.80…), không phản ánh độ chắc chắn thật (calibration, 5%).
7. **Chưa dùng 5 tool**: `get_refund_timeline`, `get_sellers`, `get_policy`, `get_customer_history`, `get_product_context`. Hai case refund (`refund_pending`, `refund_failed`) và case seller/logistics không thể kết luận đúng nếu thiếu các tool này.
8. **Case không có `order_id` sẽ bị crash** (`ValueError`), không có output, và trúng hard gate khiến case đó 0 điểm.
9. **Rác trong repo**: [cli_backup.py](src/student_agent/cli_backup.py) và [workflow_before_fix.py](src/student_agent/workflow_before_fix.py) giống hệt `workflow.py` (cả hai dài 959 dòng, `diff` không ra khác biệt). `cli_backup.py` thực chất không phải bản sao lưu của `cli.py`. [test_get_order.py](test_get_order.py) nằm ở root và import `src.student_agent`.
10. **Trace bị bẩn khi chạy lại**: `day09 run` không xóa trace cũ và bỏ qua case đã có output. Vì vậy sau khi sửa logic, nếu không xóa `outputs/` thì output cũ vẫn được giữ, còn trace thì có sự kiện của nhiều lần chạy.

---

## Bước 1. Chuẩn bị môi trường

```bash
cd "/home/tutusk4/Documents/AI in Action/K4-L3A-MultiAgent-MCP-A2A"
source .venv/bin/activate && python -m pip install -e ".[dev]"
```

Kiểm tra file `.env` (đã có sẵn, không commit file này):

```dotenv
COMPETITION_API_URL=<URL workspace của lớp>
COMPETITION_TEAM_API_KEY=sk-team-<key thật của team>
MCP_ENDPOINT=<URL MCP của lớp>/mcp
```

> README ghi ví dụ `127.0.0.1`, còn `.env.example` ghi URL công khai. Hãy dùng đúng URL giảng viên cung cấp cho đợt thi hiện tại.

**Xong khi:** `source .venv/bin/activate && day09 mcp-tools` in ra danh sách tool, không báo lỗi xác thực.

---

## Bước 2. Tải input L3A

1. Tải `l3a-inputs-<version>.zip` từ GitHub Release của lớp.
2. Giải nén vào root repo:

```bash
unzip l3a-inputs-<version>.zip -d .
source .venv/bin/activate && day09 validate-inputs
```

**Xong khi:** in ra `OK: l3a / <version> / 100 cases`.

> Sau khi có input, test `test_repository_contains_no_competition_payload` sẽ **fail trên máy local**. Điều này là bình thường: test đó bảo vệ việc không commit dữ liệu thi. `.gitignore` đã chặn `case-set.json`, `inputs/*`, `outputs/*`, `traces/*`. Trên CI, test vẫn pass vì CI checkout sạch.

---

## Bước 3. Dọn repo cho CI xanh

1. Xóa hai file trùng lặp:
   ```bash
   git rm src/student_agent/cli_backup.py src/student_agent/workflow_before_fix.py
   ```
2. Chuyển `test_get_order.py` sang `scripts/explore_case.py` (xem Bước 4), hoặc xóa đi.
3. Sửa các lỗi ruff còn lại:
   - [cli.py:222](src/student_agent/cli.py#L222): `f"\n===== RUN COMPLETE ====="` có tiền tố `f` nhưng không có placeholder, bỏ chữ `f`.
   - [workflow.py:73](src/student_agent/workflow.py#L73): gộp 2 câu `if` lồng nhau thành một câu với `and`.
   - [contracts.py:25](src/student_agent/contracts.py#L25): đổi khối if/else thành biểu thức ternary.
   ```bash
   source .venv/bin/activate && ruff check . --fix && ruff check .
   ```

**Xong khi:** `ruff check .` báo `All checks passed!`. `pytest -q` cũng pass, với điều kiện chạy trên checkout sạch hoặc tạm dời `inputs/` ra chỗ khác.

---

## Bước 4. Khảo sát MCP tool và dữ liệu thật (bước quan trọng nhất)

Chưa thể viết policy đúng nếu chưa biết từng tool trả về trường gì. Bước này cần tìm hiểu 3 điều:

### 4.1. Tham số đầu vào của từng tool

Viết `scripts/explore_case.py` dựa trên `test_get_order.py`. Script này in `name`, `description`, `input_schema` của **tất cả** tool, không chỉ `get_order`. Ghi lại vào bảng:

| Tool | Tham số bắt buộc | `domain` trả về | Trường quan trọng trong `data` |
| --- | --- | --- | --- |
| get_order | case_id, order_id | order | order_status, purchase/approved/delivered/estimated dates, … |
| get_order_items | | item | items[].item_id, seller_id, price, freight_value, … |
| get_order_payments | | payment | payment_sequential, payment_type, payment_value, … |
| get_payment_timeline | | payment | các sự kiện capture/refund, … |
| get_shipment_summary | | shipment | shipment_id, carrier dates, delivered vs estimated, … |
| get_refund_timeline | | refund | ? |
| get_sellers | ? (seller_id?) | seller | ? |
| get_policy | ? (policy code?) | policy | ? |
| get_customer_history | ? | customer | ? |
| get_product_context | ? | product | ? |

Điền các ô `?` sau khi chạy script.

### 4.2. Cấu trúc file input

Mở vài file `inputs/L3A_CASE_*.json` và ghi lại:

- vị trí của `order_id` (hiện code đoán là `customer_request.claimed_order_id`);
- cấu trúc `customer_request.claims[]`: `claim_id`, `topic`, và có số tiền khách khai (`claimed_amount`) hay không;
- có case nào **không có** `order_id`, hoặc có **nhiều** order không;
- danh sách giá trị `topic` thực tế (`jq -r '.customer_request.claims[].topic' inputs/*.json | sort | uniq -c`).

### 4.3. Lưu mẫu evidence

Chạy script cho khoảng 10 case có `topic` khác nhau, lưu JSON trả về vào `scratch/` (thêm `scratch/` vào `.gitignore`). Đây là dữ liệu để viết luật ở Bước 5.

> L3A không chấm efficiency, nên việc gọi tool để khảo sát không bị trừ điểm. Tuy vậy mọi call đều được audit. **Bài nộp cuối cùng phải lấy output và trace từ một lần chạy sạch (Bước 8)**, không trộn với evidence từ lần khảo sát.

**Xong khi:** bảng 4.1 được điền đủ và bạn hiểu mỗi `primary_issue` trong schema được nhận biết bằng dấu hiệu gì trong dữ liệu.

---

## Bước 5. Viết lại policy theo evidence (semantic 45%)

Nguyên tắc là **claim của khách chỉ là giả thuyết, evidence mới là căn cứ để kết luận**. Nên tách policy thành các hàm "detector", mỗi hàm trả về `(khớp?, refs đã dùng, chi tiết)`.

### 5.1. Mở rộng các specialist

| Agent | Tool nên gọi thêm | Mục đích |
| --- | --- | --- |
| order_agent | (giữ nguyên) | trạng thái đơn, item, seller_id |
| payment_agent | `get_refund_timeline` | refund pending/failed |
| shipment_agent | (giữ nguyên) | trễ giao, ngày giao cho carrier |
| **seller_agent** (mới) | `get_sellers` | lấy `seller_id` cho `responsible_parties` / `seller_ids` |
| policy_agent | `get_policy` (nếu tool trả về luật hoàn tiền) | ngưỡng trễ, quy tắc refund |

Chỉ gọi tool khi cần. Ví dụ: không gọi `get_refund_timeline` nếu không có dấu hiệu refund nào (có trong claim, hoặc payment timeline có refund).

### 5.2. Bảng luật gợi ý

Đây là **giả thuyết cần kiểm tra lại với dữ liệu ở Bước 4**:

| primary_issue | Dấu hiệu trong evidence | case_status | Bên chịu trách nhiệm | Refund |
| --- | --- | --- | --- | --- |
| canceled_order_paid | `order_status == canceled` và có payment đã capture, chưa refund | action_required | seller (hoặc platform) | tổng đã trả |
| unavailable_order_paid | `order_status == unavailable` và đã thanh toán | action_required | seller | tổng đã trả |
| late_delivery_seller | giao trễ so với `estimated_delivery_date`, **và** seller giao cho carrier sau `shipping_limit_date` | action_required | seller + `seller_id` | theo policy |
| late_delivery_logistics | giao trễ, nhưng seller giao cho carrier đúng hạn | action_required | logistics_provider | theo policy |
| valid_split_payment | nhiều payment (voucher + card…) có tổng = tổng đơn | no_action | (không có) | 0 |
| payment_mismatch | tổng payment ≠ tổng giá trị đơn (items + freight), vượt ngưỡng sai số | needs_investigation / action_required | payment_provider hoặc platform | phần chênh lệch |
| duplicate_charge | timeline có 2 lần capture cùng số tiền cho cùng một payment | action_required | payment_provider | số tiền bị trừ trùng |
| refund_pending | refund đã khởi tạo nhưng chưa hoàn tất | action_required | platform / payment_provider | số tiền chờ refund |
| refund_failed | refund có trạng thái failed | action_required | payment_provider | số tiền refund lỗi |
| unsupported_claim | khách claim X nhưng evidence cho thấy X không xảy ra (vd: nói trễ nhưng giao đúng hạn) | no_action | customer / unknown | 0 |
| insufficient_evidence | thiếu order, tool lỗi not_found, dữ liệu mâu thuẫn không giải quyết được | needs_investigation | unknown | 0 |

Về thứ tự ưu tiên khi nhiều detector cùng khớp: kiểm tra trước detector ứng với topic khách claim; nếu detector đó **không** khớp thì chạy các detector còn lại; nếu không detector nào khớp thì trả `unsupported_claim` (trường hợp có đủ evidence) hoặc `insufficient_evidence` (trường hợp thiếu evidence). Hiện code rơi vào `insufficient_evidence` với confidence 0.45, và đây nhiều khả năng là kết luận sai.

### 5.3. So sánh tiền

- Dùng `round(x, 2)` và sai số ví dụ `abs(a - b) <= 0.01`. Không so sánh float bằng `==`.
- Tổng đơn = `sum(price) + sum(freight_value)` của các item.

### 5.4. Xử lý case thiếu `order_id`

Không `raise`. Hãy thử `get_customer_history` (nếu tool nhận customer id) để tìm order. Nếu vẫn không có, trả output `insufficient_evidence` với confidence thấp. Lưu ý hard gate `missing_required_evidence`: case vẫn cần ít nhất một evidence ref thật liên quan.

**Xong khi:** với khoảng 10 case đã khảo sát, bạn tự gán nhãn "đúng" bằng tay, và policy cho kết quả khớp phần lớn số case đó.

---

## Bước 6. Hoàn thiện các trường output

### 6.1. `affected_entities`

- `order_ids`: order thật sự liên quan, lấy từ evidence chứ không chỉ từ lời khách.
- `item_ids`: chỉ item liên quan (vd: item của seller giao trễ), hoặc toàn bộ item nếu vấn đề ở cấp đơn hàng.
- `seller_ids`: từ `get_order_items` / `get_sellers`.
- `payment_references`: id hoặc `payment_sequential` của payment liên quan. Kiểm tra định dạng thực tế ở Bước 4.
- `shipment_ids`: từ `get_shipment_summary`.

### 6.2. `root_cause_analysis`

- `ranked_causes`: mã dạng `^[A-Z][A-Z0-9_]{2,79}$`, nên cụ thể hơn `primary_issue.upper()`, vd `SELLER_HANDOFF_LATE`, `DUPLICATE_CAPTURE`. Cause chính có rank 1, cause phụ có rank 2 trở đi.
- `responsible_parties`: nếu `party_type == "seller"` thì `party_id` **phải** là `seller_id` thật (consistency có kiểm tra *seller responsibility*).

### 6.3. `financial_resolution`

- `recommended_refund_brl` = tổng `refund_lines[].amount_brl` (làm tròn 2 chữ số).
- Mỗi `refund_line` có `reason_code` (vd `DUPLICATE_CAPTURE`) và `entity_id` (payment/item/order id).

### 6.4. `resolution_actions`: quy tắc nhất quán (consistency 10%)

| case_status | refund | actions |
| --- | --- | --- |
| no_action | phải bằng 0 | `["no_action_required"]` |
| action_required | > 0 nếu issue có hoàn tiền | có action hoàn tiền, vd `issue_refund`, kèm action nghiệp vụ |
| needs_investigation | thường 0 | `investigate_*` / `collect_additional_evidence` |

Không để trùng action (schema yêu cầu `uniqueItems`). Đặt mã action theo một danh sách cố định.

### 6.5. `claim_assessments`

Mỗi claim của khách có một mục riêng:

- `supported`: detector của topic đó khớp.
- `partially_supported`: đúng một phần (vd: đúng là trễ, nhưng lỗi ở logistics chứ không ở seller như khách nói; hoặc số tiền khách khai lệch).
- `unsupported`: evidence phủ định claim.
- `insufficient_evidence`: không đủ dữ liệu để kết luận.
- `evidence_refs`: chỉ những ref dùng cho **claim đó**, không phải toàn bộ.

### 6.6. `data_conflicts`

Khi có hai nguồn mâu thuẫn (vd: số tiền khách khai ≠ payment; `order_status=delivered` nhưng shipment chưa giao), thêm một mục:

```json
{"field": "payment_total", "sources": ["customer_request", "get_order_payments"],
 "selected_source": "get_order_payments", "resolution_code": "PREFER_AUTHORITATIVE_SOURCE"}
```

### 6.7. `evidence_refs` ở cấp output (evidence 15%)

Chỉ cite ref của các domain **thật sự hỗ trợ kết luận**, ví dụ:

| primary_issue | Domain nên cite |
| --- | --- |
| canceled/unavailable_order_paid | order, payment |
| late_delivery_* | order, shipment, item (seller_id), seller |
| valid_split_payment, payment_mismatch, duplicate_charge | order, item (tổng tiền), payment |
| refund_* | payment, refund |
| unsupported_claim | domain dùng để bác bỏ claim |

Mỗi evidence có trường `domain`, nên hãy lưu evidence theo dạng `{tool_name: evidence}` để dễ lọc.

### 6.8. `confidence` (calibration 5%)

Calibration = `1 − (đúng − confidence)²`. Nên đặt confidence theo mức độ chắc chắn thật:

- ~0.9: detector khớp rõ ràng, đủ evidence, không có mâu thuẫn;
- ~0.6–0.7: khớp nhưng có một mâu thuẫn hoặc thiếu một nguồn;
- ~0.3–0.4: `insufficient_evidence` hoặc phải đoán.

**Xong khi:** mọi trường trong output đều được tính từ evidence, không còn giá trị hằng số hay rỗng mặc định.

---

## Bước 7. Chỉnh trace cho điểm workflow (5%)

Scorer kiểm tra 4 điều: đủ các event bắt buộc, thứ tự receive/finalize, có nhiều actor phối hợp, và evidence được liên kết với trace.

- Mỗi case có đúng **một** `case_received` ở đầu và **một** `case_finalized` ở cuối.
- `task_assigned` nên do **coordinator** phát, với `target=<agent>`. Hiện specialist tự emit `task_assigned` với `target=tool_name`, nên sửa lại hoặc đổi sang `attributes`.
- Có `handoff` giữa các agent, và `verification_completed` do `verifier` phát.
- **Mọi ref trong output phải xuất hiện trong một `tool_result_consumed`** của cùng case (hiện đã đúng, cần giữ nguyên khi refactor).
- Verifier nên thật sự kiểm tra các invariant ở Bước 6.4 và 6.7. Nếu phát hiện lỗi thì sửa output và ghi `decision_code` tương ứng (vd `ADJUSTED_CONFIDENCE`, `FIXED_ACTIONS`).

Về vấn đề trace khi retry: nếu một case thất bại ở lần thử 1 rồi thành công ở lần thử 2, trace sẽ có 2 `case_received`. Có 2 cách xử lý:
- **Cách A (đơn giản):** trước khi nộp, luôn chạy lại toàn bộ từ đầu (Bước 8), và kiểm tra không case nào cần retry.
- **Cách B:** buffer các event của một case trong bộ nhớ và chỉ ghi ra file khi case thành công. Cách này cần sửa `TraceWriter` hoặc `_run_one_case`.

---

## Bước 8. Chạy sạch và kiểm tra

Luôn **xóa output và trace cũ** sau khi sửa logic, vì `day09 run` bỏ qua case đã có output:

```bash
rm -f outputs/*.json traces/trace.jsonl
source .venv/bin/activate && day09 run 2>&1 | tee scratch/run.log
source .venv/bin/activate && day09 validate
```

Tự kiểm tra thêm bằng một script nhỏ (vd `scripts/self_check.py`):

- phân bố `primary_issue` trên 100 case (nếu 90% cùng một nhãn thì có vấn đề);
- số case có `refund > 0` nhưng `case_status == no_action` (phải bằng 0);
- số case có `party_type == seller` mà `party_id == null` (phải bằng 0);
- mỗi case có đúng 1 `case_received` / 1 `case_finalized` trong trace;
- mọi ref trong output có trong `tool_result_consumed` của cùng case;
- so sánh với nhãn tay ở Bước 5.

**Xong khi:** `day09 validate` báo `OK: 100 outputs / N trace events`, log không có `[RETRY]` hay `[FAILED]`, và các chỉ số tự kiểm đều hợp lý.

---

## Bước 9. Đóng gói và nộp

```bash
source .venv/bin/activate && day09 package --output dist/submission.zip
unzip -l dist/submission.zip   # chỉ gồm manifest.json, trace.jsonl, outputs/*.json
```

Upload `dist/submission.zip` tại workspace `/l3a`.

- Trước khi finalize, workspace chỉ hiện **điểm tổng và breakdown của phần public**. Dùng breakdown đó để biết thành phần nào yếu (semantic, evidence…) và quay lại Bước 5–7.
- Điểm cuối = **20% public + 80% private**. Đừng tối ưu luật cho khớp riêng các case public: luật phải dựa trên dữ liệu nghiệp vụ tổng quát.
- Nhớ **chọn submission tốt nhất** làm final trên workspace.

---

## Bước 10. Cập nhật tài liệu và commit

1. Cập nhật [ARCHITECTURE.md](ARCHITECTURE.md) theo code mới:
   - mục 2: thêm seller_agent và các tool mới;
   - mục 5: các failure đã xử lý (not found, thiếu order_id, source conflict → `data_conflicts`);
   - mục 6: bỏ ghi chú "Money totals: chưa kiểm tra", mô tả invariant mà verifier kiểm tra;
   - mục 7: cách xử lý trace khi retry.
2. Chạy lại `ruff check .` và `pytest -q`.
3. Commit theo từng bước nhỏ (vd `chore: remove duplicate workflow copies`, `feat: evidence-based policy detectors`, `feat: financial resolution and entities`, `docs: update ARCHITECTURE`). Không commit `.env`, `inputs/`, `outputs/`, `traces/`, `dist/`, `scratch/`.

---

## Checklist trước khi nộp

- [ ] `ruff check .` sạch, CI xanh
- [ ] `day09 validate-inputs` báo 100 cases
- [ ] Đã khảo sát đủ 10 tool và điền bảng 4.1
- [ ] Policy dựa trên evidence, không chỉ dựa trên `claim.topic`
- [ ] Không case nào crash (thiếu order_id → vẫn có output)
- [ ] `seller_ids` / `payment_references` / `shipment_ids` / `party_id` được điền khi có dữ liệu
- [ ] `recommended_refund_brl` = tổng `refund_lines`, nhất quán với `case_status` và actions
- [ ] `evidence_refs` chỉ gồm domain liên quan; mọi ref có trong `tool_result_consumed`
- [ ] Mỗi case có 1 `case_received` và 1 `case_finalized`; coordinator phát `task_assigned`
- [ ] Confidence thay đổi theo mức độ chắc chắn
- [ ] Đã chạy sạch (xóa outputs/trace) rồi mới `day09 validate` và `day09 package`
- [ ] ZIP chỉ có `manifest.json`, `trace.jsonl`, `outputs/*.json`
- [ ] `ARCHITECTURE.md` khớp với code
