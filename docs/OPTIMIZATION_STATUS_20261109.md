# Trạng thái tối ưu dự án chính — 09/11/2026

Dự án duy nhất để sử dụng/tích hợp: `D:\duanlaylink`. Khu `D:\vietsub_lab` chỉ dùng đối chứng, không phải sản phẩm thay thế.

## Đang thực hiện để tích hợp

Phân biệt vai trò theo đoạn: thoại nhân vật, lời review, hỗn hợp, chưa rõ; kết hợp ASR/VAD và ngữ cảnh. Mục tiêu tích hợp dự kiến là lớp lập nhịp trong `src/tts/` và dữ liệu giải thích/hiển thị trong Studio, dùng chung cho Auto và thủ công. Chưa thêm runtime feature vào production.

Nguyên mẫu hiện tại: `D:\vietsub_lab\20261109_next_bottleneck\role_pacing_trial`. Hai lượt Gemini trên12s đề xuất câu1–2 thoại, câu3–5 narration; đó là consistency cùng model, chưa phải accuracy. Planner giữ cue5 chưa rõ vì nguồn bằng chứng cắt đuôi. Đề xuất thử đọc chung câu3–4, giữ nguyên token và mọi âm thanh ngoài vùng7.667–10.747s.

Kết quả:12 kiểm tra logic lab đạt (đã sửa nguy cơ nhóm chồng lấn/lặp cue trong nguyên mẫu);3 lần tổng hợp vừa khung3.08s, không vượt trần tempo1.13 trên rate+15%. Mẫu đối chiếu có hình đã tạo. ASR biến thiên, tên riêng khó nhận dạng; chưa nghiệm thu nghe, chưa PASS chất lượng hoặc tốc độ toàn Auto. Không lấy điểm ASR tốt nhất để chọn mẫu; mẫu nghe chọn lượt hợp lệ đầu tiên trước khi ASR.

## Điều kiện tích hợp

- Kiểm tra trên nhiều video độc lập, bao gồm video xen thoại/review và tình huống chưa rõ.
- Không bỏ/lặp từ, không đổi nghĩa; nhịp được nghe kiểm chứng theo cảnh, cue ID/DB vẫn authoritative.
- Không đẩy thoại sang cảnh khác, chồng lời hoặc vượt tốc độ; nhãn chưa rõ giữ timing và lý do.
- Cache gắn identity nguồn/model/phiên bản/settings; chạy lại/hủy và Auto/thủ công dùng cùng policy.
- Đo chi phí mới và tổng pipeline; thay đổi chất lượng không được quảng cáo là tăng tốc. Chỉ áp dụng tối ưu tốc độ sau đối chứng đạt gate đã công bố.
- Chạy kiểm tra phù hợp trước khi triển khai, rồi dọn dữ liệu thử đã hết tác dụng. Không cài thêm/archive mới vô ích.

## Đã loại / chưa dùng

- VSF BGR: không đạt giảm15% tổng proxy+scan+OCR; không tích hợp.
- VieNeu batching: có giảm thời gian tổng hợp, nhưng người dùng đánh giá mẫu lặp/ít cảm xúc/ngắt chưa rõ; chưa tích hợp. Dự án thực đang chọn Piper ngochuyen+15%.
- Chia ưu tiên dấu phẩy cho VieNeu: không cải thiện ASR ổn định; đã loại.
- Silero VAD đã có sẵn: probe CPU nhẹ, nhưng không phân biệt vai trò/cảm xúc và không đủ để tự cắt khoảng nghỉ. Stable-ts chỉ tham khảo do repo archived.

## Dọn khu thử

Đã kiểm kê hơn3GB build/archive/output cũ có thể dọn. Hai yêu cầu xóa (bulk và thu hẹp2archive) bị công cụ chặn bằng `blocked by policy`; CHƯA XÓA, CHƯA GIẢI PHÓNG DUNG LƯỢNG. Danh sách có lý do tại `D:\vietsub_lab\20261109_next_bottleneck\cleanup_pending.json`. Không chuyển sang cách khác để lách chặn. Giữ mẫu A/B đang chờ nghe, báo cáo kết luận, mã nguồn cần tích hợp; không xóa `storage/projects`, model đang dùng, config/key hay source video độc lập.

## Đối chiếu phản hồi nghe đoạn review câu 3–4

Người dùng báo vẫn nghe lặp và chưa rõ. Kiểm tra đầu vào Piper không thấy nhân đôi cụm từ. Với đúng WAV của mẫu đã gửi, ASR nhận đủ câu ở bản thô và bản chỉ cắt lặng, nhưng nhận sai tên phim ở bản chỉ tăng tốc khoảng 8,2%. Đây là tín hiệu trên một WAV. Vị trí lỗi lặp nghe được còn chưa xác định. Ba bản phiên âm thử không qua điều kiện thời lượng. Bản rút gọn “Ba đại lão cùng trọng sinh, về đầu Dragon Ball Z.” vừa khung 3,08 giây trong cả ba lượt (2,66–2,74 giây ở +15% cơ sở, không ép thêm). ASR chỉ nhận đúng tên phim ở một lượt. Giữ phương án ở mức kiểm nghiệm. Dữ liệu: role_pacing_trial/repetition_diagnostic.json, repetition_isolation.json và concise_trial.json trong khu thử đã nêu ở trên.

Mẫu video phát âm “Đra gơn Bôn dét” được người dùng phản hồi “được rồi”. Đọc lại trong hai ngữ cảnh ở +15% cho thấy bản phiên âm vừa 3/3 lượt khi ghép câu review 3–4 (2,82–2,89 giây trong khung 3,08 giây), nhưng vượt khung 1,362 giây ở cả 3 lượt khi đọc riêng câu 4 (1,61–1,67 giây). Bản đọc nguyên tên cũng vượt khung đọc riêng. Dự án có một cue chứa chính xác tên phim này, nên chưa có đối chứng độc lập. Giữ cách đọc làm mẫu đã nghe đạt cho cụm review, chưa áp dụng từ điển toàn dự án. Chi tiết trong pronunciation_followup.json của khu thử.
