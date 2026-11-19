# Luật duyệt nguồn OCR

Chưa có lịch sử học. Không có luật được người dùng phê duyệt.
Phiên bản đầu chỉ đề xuất; không tự sửa/loại/cắt/gộp câu.
Mỗi luật trong khối JSON có id, condition (tín hiệu có sẵn trong code), scope,
exceptions, action, correct_count, wrong_count, status, approved_by, validation.
Luật mới: "thử nghiệm". "đang dùng" cần người dùng xác nhận và kiểm thử riêng.
Điều kiện dùng đặc trưng, không dùng mã câu hoặc tên video. Không thực thi mã từ file.

```json
[
  {
    "id": "R001",
    "condition": "ocr_disagreement",
    "action": "CẦN NGƯỜI",
    "scope": {
      "source_kind": "ocr"
    },
    "exceptions": [],
    "correct_count": 0,
    "wrong_count": 0,
    "status": "thử nghiệm",
    "created_at": 1794027618.269057,
    "origin": "agent đề xuất; chưa phải luật được xác nhận"
  },
  {
    "id": "R002",
    "condition": "scene_layout",
    "action": "CẦN NGƯỜI",
    "scope": {
      "source_kind": "ocr"
    },
    "exceptions": [],
    "correct_count": 0,
    "wrong_count": 0,
    "status": "thử nghiệm",
    "created_at": 1794027619.8146763,
    "origin": "agent đề xuất; chưa phải luật được xác nhận"
  }
]
```
