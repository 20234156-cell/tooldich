# Thiết kế nâng cấp Auto cho video dài

## Triển khai bước7 đang thực hiện — 10/11

Theo lệnh bắt đầu của user, đường Auto/API mới trên đĩa dùng `cluster-budget-v1`:
WAV native đo mẫu→giải lịch→tempo từng bậc→kiểm nguồn→rút lời cuối. Source window
cùng một người nói được hai model độc lập kiểm thì có thể mượn<=0.8s, không
được coi là khoảng im lặng/reset. Thiếu bằng chứng không mượn. Nguồn trùng đã
chứng minh được phục hồi với lịch sử DB độc lập với lỗi timing ở cụm khác.

Giữ40ms/max1.5/mean0.3/tổngspeed1.30; manifest ràng buộc strategy, report nguồn
và file bảo vệ âm thanh. Có fallback source-model tối đa4call/deadline60s,
không cấp quyền khi API lỗi. Giao diện kiểm backend đã nạp; HTTP hiện vẫnv2,
chưa triển khai runtimev1. Các số đo/test/giới hạn đang mở:
[PLAN](../storage/maintenance/step7_replacement_20261110/PLAN.md).

## Cập nhật triển khai 10/11 — lịch QA và sản phẩm khác nhau

Đã nối bộ xếp lịch WAV đo thật vào `run_voiceover`, trước khi tiếp tục rút lời,
cho Auto fit với cấu hình lưu sẵn mix tiếng gốc<=15%. Không đổi hình/tốc độ/lời,
không hạ max1.5/mean0.3. Journal ghi các khoảng ngoài cửa sổ chưa kiểm VAD theo
chính sách mix đã cho phép; render từ chối nếu cấu hình âm thanh đổi không còn
đủ điều kiện. Bộ chọn các mức dịch toàn cục vẫn chưa tích hợp Auto; không nhầm
hai phần này. Source/mix khác tiếp tục dùng chính sách cửa sổ hiện tại.

Replay thật508cue ra MP4 kỹ thuật,max1.499/mean0.135,decode0; cảnh báo nội dung
vẫn còn. Ba link mới là ba sản phẩm main-project riêng, không phải mẫu audit
thay sản phẩm. Đã tải đủ và lưu hash/khung hình từng nguồn; chưa chạy Auto vì
tool chặn reload backend. Trạng thái từng phần và đường dẫn kết quả:
[REPORT](../storage/maintenance/timing_508_20261110/REPORT.md).


## Quyết định mới nhất — vận hành 10–20 sản phẩm/ngày và cải tiến tích lũy

### Cập nhật sau Hybrid: Log và video1246cue

TabLog sửa metadata/hợp đồng dữ liệu, tách JobLog/jobstatus/checkpoint/cảnh báo;29focusedPASS/22,39s. Backend200dòng chưa nạp vì toolpolicy chặn reload; frontend4.6.12 dùng GETjob tương thích đã đọc5log thật. Không báo triển khai backend hoàn tất. [Báo cáo](../storage/maintenance/log_panel_20261110/REPORT.md).

Auto thật proj_67444704942f/2001,72s/1246cue kết thúc FAILED/TIMING_REVIEW_REQUIRED,1110cue chưa đạt,max46,21/mean11,08s. Đây là quan sát trước khi nạp patchLog, chưa xác minh nguyên nhân hoặc sửa. **Ưu tiên kế tiếp là điều tra cue/WAV/timeline và cơ chế phục hồi timing của lượt này**, giữ QA, không kết luận máy yếu/AI sai khi chưa có bằng chứng. DB chính đã có6quan sát dự án mới; kết quả0trước đó đã cũ. Eventcoverage, hợp nhất báo cáo và cursor/export là các bước sau, chưa làm xong.

### Hybrid / đối chiếu lời nhạc — đã chốt 10/11/2026

**Kết quả cuối mốc:**fullsuite mới1.324PASS/1.022,68s,không failure/error/skip;đã nạp bảnpersonal idle,HTTP200,schemahybrid/prooffield/asset4.6.11khớp. Hash4rawtranscriptrows vàMP4business trước/sau không đổi. Báo cáo Hybrid đã cập nhật kết quả đầy đủ. Phần nghĩa/multi-video/recoveryqueue vẫn mở,không tự bật mặc định.

Ưu tiên đọc [báo cáo Hybrid](../storage/maintenance/hybrid_recovery_20261110/REPORT.md) và ACTIVE_WORK trước các mốc lịch sử dưới đây. Đã có nhánh opt-in `hybrid_dub`, không đổi mặc định cũ. Nút thắt #82 là thiếu bước phân vai caption confidence cao; không phải tăng tốc OCR hoặc nâng ngưỡng là sửa được. VAD chỉ chọn ứng viên, vì45/55là thoại dù coverage0. Sáu ảnh đọc khớp và hai actual model đồng thuận hát/không thoại mới cho caption lời nhạc chỉ hiện sub. Bằng chứng gắn nguồn/cue/audio/crop, shared predicate trong giọng/render/API/QA; stale/conflict/unknown giữ thoại, ghi cần xem lại.

Lượt production-entry7ứng viên/9API/81,87s:6thoại được giữ,82đạt đồng thuận3.5+3.6,3.8timeout được lưu. Canonical Auto riêng từ scan+rolecheckpoint thật:87bản dịch,85giọng,82/88subtitle-only;MP4decode0,228.28s,max/mean1.171/.220,-13.67LUFS/-2.50dBTP. Technicaltrue/fullyfalse;46/87vẫn omitted,25chưa đủ bằng chứng phân vai trong lượt kiểm lại; còn provenance scan/bản dịch kế thừa. Master82/88peak0,encoded âmnguồn audible,DB/JSON/WAV/manifest/timeline khớp. Không phải freshURL/speedbenchmark/nghiệm thu toàn bộ nghĩa.

Longselector865cue:15,83slạnh/1,14sấm;8selected/5deferred;0API và rawDBcue không đổi. Cache chỉ hoàn tất đúng nguồn/code/runtime/model/settings; chunk60s+overlap, boundedmemory. Giới hạn8caption/12API/120sadmission giữ chi phí nhưng chưa bảo đảm phân vai hết video dài. Phần deferred phải ra trạng thái chất lượng cần xem lại, không âm thầm coi đã xong.

Đã kiểm nhóm183PASS và contract cuối31PASS; full suite mới1.324PASS/1.022,68s, XML không failure/error/skip. Integrity và triển khai personal đã qua; dữ liệu business giữ nguyên. Chưa làm xong recoveryqueue tự điều phối mọi loại lỗi, bounded omission theo ý nghĩa, dịch chú thích ngoài ROI, hiệu chỉnh mọi lỗi dịch, train tự động hay chứng minh10–20video/ngày. Không coi phiếu AI thành nhãn người xác nhận. Ngưỡng QA số và dữ liệu business giữ nguyên. Bước46localprobe vẫn dưới90;87chưa đủ điều kiện xóa, không thêm ngoại lệ ID/link.

### Lịch sử mốc trước Hybrid — source-role ngày10/11/2026

Đã chốt mốc phục hồi caption ngắn và chú thích cười. Full core1.291PASS trong1.485,96s,không failure/error/skip. Chỉnh GET chỉ đọc/bảng giọng cuối được kiểm riêng bằng nhóm61PASS;2test mới không nằm trong collectionfull. Không cộng các nhóm trùng hoặc tuyên bố fullfresh trên mọi dòng UIcuối.

Audit có87 bản dịch,86 giọng cần nói và #88 chỉ hiện phụ đề;MP4full decode0,technicalQA=true,fully_passed=false. #46/#87và phân vai #82 vẫn mở. [Báo cáo đầy đủ](../storage/maintenance/source_role_recovery_20261109/REPORT.md) là nguồn trạng thái cuối, thay các dòng pending ở ghi nhận trung gian phía dưới.

Ứng dụng cá nhân đã nạp mã tại127.0.0.1:9000,HTTP200,asset4.6.10. Payload cue và MP4business cũ giữ hash trước/sau. Bước tiếp theo là rolecontract/hybrid có đối chứng/ngân sách, tinyglyph/falsepositive46/87 và omissionbounded; không tạo ngoại lệ theo link/cue hoặc train phiếuAI chưa xác minh.

Việc xóa9filetrial bị chính sách công cụ chặn; các file còn trên đĩa,không được ghi là đã dọn xong.

### Mốc mới nhất: phục hồi caption ngắn và chú thích chỉ hiện phụ đề

Theo phép tiếp tục được người dùng cho phép, đã đối chiếu46/87/88từ DBrevisiontrước bỏ, ảnh đúngframe/audio và82/controls. Sửa white-glyphfallback có unmaskedguards/sixreads>=90và giới hạn16MP trước3xallocation, sourceidentityv9. Realreplay89cue phục hồi88,giữ89ID,0discard,46/87pending. Cue88đượcgắnrole nonverbal_laughter từsourceconsensus; chỉ bảnViệthoàntoànâmcười lặp mới khôngTTS, giữsubtitle/timing/âmnguồn trongmix. QAkhai báo alltranslation87/87 vàeligiblevoice86/86,excluded88; khôngcountsilentplaceholderlàvoice. Mọi câu thoại/rolekhôngđủđiềukiện giữ100%voicegates; numericalthresholdkhôngđổi.

Auditproj_source_roles_20261109 đãrender87cue/full decode0,228.28s,technicaltrue/fullyfalse,max/mean1.284/.217,-13.70LUFS/-2.18dBTP. Hash/WAV/DB/timeline/thựcencoded kiểmchứng,source88masterpeak0nhưngencodedâmnguồncònaudible. Giữ sửa35vàprovenance26shortened/1dịchlại. Khôngphải freshURL,speedbenchmarkhoặcnghiệmthunghĩatoànvideo. Mainserverchưa mởlại.

Còn:46mộtchữcảmthánvisual chưađạt90;87likelybackgroundhit nhưngstrictdiscardgatechưađủ;82whitecaption/yellowexplanationvàaudiomusicchưacóclassifier đángtin. Custom/auto→hardsubkhôngphânvai mọi dòngconfidencecao; không tự chuyểnmụctiêusangSTTcho82. MMroleaudit12calls5success/7fail; córesponseđọc lẫnvùng/vaitrò nênkhônglàmtrainingtruth. Cầnhybrid/rolecontract vàcontrols trước khiloạilờicónghĩa. Cácnhánhngoàilaughtervẫnchưahoànchỉnh.

Focusedcuối169passed/112.41s;fullsuitecuối đangchạy,khôngdùng1242cũlàm kếtquảmới. Hai lượtfulltrunggiandừngtrước khicode đóngbăng,khôngtínhpass. Bằngchứngvàbáocáo: storage/maintenance/source_role_recovery_20261109/REPORT.md; bước tiếptheođượcghiở ACTIVE_WORK.

Cập nhật theo trao đổi ngày 09/11/2026. Mục này quyết định hướng triển khai hiện hành; các số đo và điều tra phía dưới được giữ làm lịch sử bằng chứng, không phải danh sách việc đang chạy.

### Những yêu cầu phải giữ qua mọi phiên làm việc

- Sản lượng dự kiến 10–20 sản phẩm mỗi ngày; người dùng có thể gọi trợ lý mỗi ngày để đọc bằng chứng và cải tiến, nhưng vận hành thường ngày không phụ thuộc trợ lý có mặt.
- Ưu tiên cao nhất là chất lượng thành phẩm và ít lỗi ngay lần đầu. Kho vấn đề chỉ hỗ trợ điều tra, phải chọn lọc và tránh làm chậm/chặn việc tốt vô ích; không lấy số bản ghi lỗi làm thành tích.
- Người dùng nhập link, nhận sản phẩm cuối; không biết tiếng Trung và không phải tìm/sửa/duyệt từng câu. Góp ý tiếng Việt là tùy chọn.
- Tool tự kiểm tra và phục hồi trong các nhánh đã xây dựng. Lỗi chưa xử lý được phải được ghi nhận có bằng chứng, không che bằng trạng thái hoàn thành.
- Có thể bỏ lượng nội dung rất nhỏ sau khi phục hồi thất bại; không lạm dụng. Phải xét giá trị nghĩa, từng đoạn, tổng thiếu hụt và chuỗi đoạn liên tiếp. Tên, số, phủ định hoặc chi tiết quyết định không thành nội dung phụ chỉ vì ít chữ. Chưa chốt ngưỡng số trước kiểm chứng.
- Phân biệt lời nói với caption thoại, chữ giải thích, biển hiệu và watermark. Không mặc định chữ trên hình trùng lời nói; nguồn chính theo mục tiêu đã lưu.
- Sửa đúng phần sai và phần phụ thuộc, giữ kết quả hợp lệ. Không yêu cầu một lỗi thuật toán mới được giải quyết bằng sửa riêng link/cue mẫu.
- Mọi tối ưu phục vụ D:/duanlaylink, video ngắn và dài, nhiều bố cục/âm thanh. Đo nút thắt thuật toán, máy, dịch vụ và chính sách trước khi kết luận.
- Mục tiêu mới được ưu tiên là lưu kho vấn đề dài hạn để trợ lý đọc và nâng cấp theo đợt. Không cần tự huấn luyện hoặc tự sửa mã production sau mỗi video.

### Hai vòng hoạt động tách biệt

**Vòng sản xuất tự động:** link → kiểm tra nguồn → nhận dạng/phân loại → dịch/kiểm nghĩa → giọng/timing → phục hồi cục bộ → render/kiểm MP4 → sản phẩm và báo cáo. Ghi nhận vấn đề trong quá trình chạy, kể cả trường hợp tự phục hồi thành công. Các phần độc lập tiếp tục; giới hạn thử và checkpoint ngăn lặp vô ích. Thiếu nguồn/DB lỗi/không có MP4 hợp lệ không được báo đạt.

**Vòng cải tiến theo đợt:** người dùng gọi trợ lý → đọc tài liệu và kho phát sinh kể từ mốc đã xử lý → gom nhóm/ưu tiên → xác minh nguyên nhân → sửa code, glossary, cách đưa ngữ cảnh hoặc đánh giá/train model tùy nguyên nhân → hồi quy/đối chứng video chưa dùng thiết kế → tích hợp và ghi version → theo dõi tái phạm. Mỗi ngày không bắt buộc train hoặc thay đổi mã nếu bằng chứng chưa đủ.

### Hồ sơ vấn đề phải lưu

Thiết kế schema tối thiểu (chưa tuyên bố đã triển khai):

| Nhóm | Nội dung |
| --- | --- |
| Định danh | incident ID, project/run ID, stable cue IDs, thời gian nguồn, bước và ngày phát sinh |
| Identity | source hash, transcript revision, code/model/runtime/settings version; không lưu khóa/cookie |
| Quan sát | dữ kiện đo được: thiếu ID/WAV, độ dài audio, lỗi provider, bất đồng OCR/STT hoặc dấu nghi vấn nghĩa |
| Bằng chứng | đoạn nguồn có ngữ cảnh, khung đầy đủ/crop, OCR/STT/translation trước-sau, WAV/timeline và file identity; liên kết bằng hash |
| Điều tra | giả thuyết, độ chắc chắn, phép kiểm đã làm, kết quả ủng hộ/trái giả thuyết; chưa biết thì ghi chưa xác định |
| Phục hồi | hành động đã thử, input identity, số lượt, thời gian/chi phí, kết quả kiểm tra và phương án cuối |
| Trạng thái | mới/nghi vấn/đã xác minh/đã phục hồi/chờ điều tra/đã sửa hệ thống/theo dõi tái phạm; không đồng nhất phục hồi video với sửa nguyên nhân |
| Vòng đời | nhóm lỗi, số lần/video gặp, mẫu đại diện, bản sửa/regression liên quan, version áp dụng và lần tái phạm |

DB cue vẫn có thẩm quyền; kho vấn đề không thay transcript. Ưu tiên mở rộng lịch sử DB/learning hiện có sau kiểm kê, tránh tạo kho trùng lặp. Việc ghi hồ sơ phải idempotent, chịu restart, không mất dấu khi export lỗi. Quan sát và giả thuyết phải là trường riêng; lời nhận xét của AI không tự thành kết luận nguyên nhân.

### Tích lũy đủ sâu mà không phình vô hạn

10–20 sản phẩm/ngày tương đương khoảng 300–600 sản phẩm/30 ngày nếu chạy hằng ngày; chưa suy ra dung lượng khi chưa có số đo độ dài và số sự kiện. Gom nhóm theo cơ chế/đặc điểm có bằng chứng, không gộp chỉ do cùng thông báo chung. Giữ tần suất trên từng video và các mẫu khác biệt; chống log lặp từ polling/retry.

Giữ metadata/kết luận/lịch sử lâu dài. Bằng chứng media dùng hash/dedup và đoạn ngắn đủ ngữ cảnh, không sao toàn video cho từng cue. Trước khi nguồn bị xóa phải giữ bằng chứng cần thiết cho ca đang mở. Dung lượng quota/retention cần đo rồi thiết kế; chưa tự xóa bằng chứng hay thay đổi chính sách xóa đã được người dùng chốt. Báo rõ evidence missing thay vì giả vờ có thể tái hiện.

### Phát hiện cả lỗi mà detector bỏ sót

Kho chỉ chứa lỗi tự phát hiện sẽ có điểm mù. Lấy mẫu một phần ca được coi là đạt, phân tầng theo loại nguồn, thời lượng, độ chắc chắn và các điểm nhạy cảm tên/số/phủ định/chuyển cảnh/chữ khác tiếng. Giữ ca đúng làm đối chứng chống sửa nhầm. Tỷ lệ lấy mẫu và chi phí phải đo, không quét lại toàn bộ 20 video bằng mọi model một cách vô điều kiện.

Nhãn nghi vấn do AI sinh không phải ground truth. Hai model đồng ý vẫn có thể cùng sai. Chỉ đưa vào regression nội dung hoặc tập train khi có nguồn/nhãn đủ căn cứ; phần chưa xác minh không được lén nâng thành dữ liệu đúng. Người dùng không phải xác nhận tiếng Trung; trường hợp chưa xác minh được phải giữ trạng thái trung thực hoặc cần năng lực đánh giá bổ sung.

### Phiên cải tiến hằng ngày của trợ lý

1. Đọc README, BUG_REGRESSIONS, thiết kế này và ACTIVE_WORK; xác định version hiện hành, mốc đọc gần nhất, việc đang mở, dữ liệu mới và evidence còn tồn tại.
2. Xuất bản tổng hợp: sản phẩm/minutes/cues, lỗi kỹ thuật và nghi vấn riêng, phục hồi thành công, omission, pending, chi phí/retry, top nhóm theo mức nghiêm trọng × độ phổ biến. Không so số lỗi thô giữa các ngày có lượng video khác nhau.
3. Chọn nhóm ảnh hưởng lớn, tái hiện và phân biệt nguyên nhân; không sửa hàng loạt trước bằng chứng. Ghi cả nhóm hoãn và lý do.
4. Chọn cách sửa: code cho thuật toán/DB/cache/timing; glossary cho kiến thức có phạm vi; ngữ cảnh/prompt cho dịch; model/config hoặc train chỉ khi đúng nút thắt và có tập nhãn.
5. Focused regression cho lỗi xác nhận; kiểm tra ca đúng và video độc lập. Full suite offline tại mốc tích hợp; audit provider/video thật ngoài pytest. Không hạ ngưỡng QA.
6. Ghi bản sửa, acceptance trước/sau, version, giới hạn còn lại; cập nhật BUG_REGRESSIONS/README khi phù hợp và ACTIVE_WORK với bước tiếp theo/mốc đã xử lý. Incident chỉ đóng khi điều kiện đã chứng minh; giữ liên kết để phát hiện tái phạm.

Mỗi phiên kết thúc cần có bàn giao ngắn: đã làm gì, bằng chứng, chưa biết gì, việc tiếp theo. Tài liệu và kho bền vững là trí nhớ dự án; không dựa vào trợ lý tự nhớ toàn bộ chat hoặc tự quan sát khi không được gọi.

### Train là phương án có điều kiện

Không nạp toàn bộ log/đề xuất AI vào train. Trước hết làm sạch, xác minh nhãn, loại trùng, tách train/eval theo video và kiểm tra quyền sử dụng dữ liệu/model. So sánh với sửa thuật toán/glossary/ngữ cảnh rẻ hơn. Candidate model chạy đối chứng trước khi thay production; lưu model/version và phương án rollback. Đo lỗi trên video chưa dùng để train, tỷ lệ sửa nhầm và chi phí, không chỉ điểm trên mẫu cũ.

### Kế hoạch hiện hành thay cho thứ tự A–E cũ

0. Hoàn tất đặc tả và inventory điểm ghi lỗi/lịch sử hiện có; map 3 omitted và 26 shortened của mẫu thành quan sát/nghi vấn, không gắn nhãn lỗi dịch hàng loạt.
1. Xây nền ghi nhận vấn đề bền vững và evidence lifecycle trên main project. Gate: không mất/chồng sự kiện khi retry/restart; tra được source→cue→WAV→output; xuất lỗi không mất bản ghi; không đổi cue/QA để thu thập.
2. Xây báo cáo theo ngày và gom nhóm, mẫu đạt đối chứng, mốc đã đọc/đã xử lý. Gate: tổng hợp đúng denominator và loại trùng; đo storage/overhead trên workload đại diện. Chưa tuyên bố chịu tải 20 video/ngày khi chưa có số đo.
3. Tích hợp hàng phục hồi cục bộ và các nhánh nguồn/nghĩa/timing/provider trong thiết kế bên dưới. Gate: thay đổi đúng phụ thuộc, resume/cancel, giới hạn thử, không lặp phương án vô ích và không âm thầm bỏ ý quan trọng.
4. Chạy video mẫu/ngắn/dài/nguồn khác nhau, kiểm DB/WAV/timing/MP4; dùng kho để thực hiện một vòng cải tiến thật và chứng minh giảm tái phạm mà không phá ca đúng.
5. Mở rộng vận hành 10–20 sản phẩm/ngày theo số đo máy, provider, thời lượng và quota; chỉ đặt ngân sách/retention/sampling sau đo. Chạy nhiều sản phẩm không tự đảm bảo chất lượng tăng.

**Trạng thái:** chưa hoàn chỉnh hệ thống tự phục hồi/cải tiến. Đã xây chỉ mục quan sát chọn lọc và sửa lỗi uncertainty theo mốc bên dưới; full1242tests đạt. Bước tiếp: hoàn thiện liên kết cue/run/bằng chứng và phục hồi theo nguyên nhân. 1227tests bên dưới là mốc cũ; không coi là nghiệm thu các khả năng còn thiếu.

### Mốc triển khai nền và lỗi nghĩa cụ thể — 09/11/2026

Đã thêm bảng pipeline_observations và capture tại save_state cùng giao dịch Project.status; chỉ stage có findings/FAILED, fingerprint chống lưu trùng, mask_secrets, CLI báo cáo khoảng UTC. Đây là chỉ mục tham chiếu, chưa phải hồ sơ cue/run/media-retention hoàn chỉnh; chưa bao phủ mọi exception hoặc backfill. Đã kiểm thử offline và replay state thật trong DB audit riêng, không đổi cue production.

Điều tra nội dung mẫu phát hiện cue35 rút gọn mất `hình như` dù MeaningVerifier chấp nhận. Khung nguồn60.100s có `好像`. Đã thêm guard bảo vệ bốn cụm phỏng đoán trước AI approval, prompt/cache v4; regression tái hiện trước sửa, nhóm60tests đạt sau sửa. Live API tạo phương án giữ `hình như`; WAV thật hữu hạn/không im lặng, timing đơn cue không có bad rows. Chưa coi là xác nhận nghĩa/timing toàn bộ video. Báo cáo chi tiết và kết quả cuối: storage/maintenance/auto_quality_foundation_20261109/REPORT.md.

Hai lượt full suite giữa chừng dừng chủ động để bổ sung redaction/boundary regression rồi sửa lỗi nghĩa mới phát hiện; không tính là đã đạt. Lượt cuối đã hoàn tất1242passed/958.18s,0fail/error/skip. Audit mới từ checkpoint dịch chỉ đổi35,86WAV và MP4decode0,QAtechnicaltrue/fullyfalse; còn3omitted và nội dung chưa xác minh. Main server chưa mở lại. Các bước hiện hành còn lại: bổ sung cue/run/source linkage và evidence retention, coordinator phục hồi, cổng omission và đánh giá độc lập trước nghiệm thu 10–20 sản phẩm/ngày.

## Mục tiêu và phạm vi

Giảm thời gian từ khi bấm Auto đến khi có MP4 đạt QA trong dự án chính `D:\duanlaylink`. Giữ đủ nội dung có thể xác minh và hiển thị rõ phần cần duyệt. Mọi phép thử video thật dùng dự án audit riêng. DB là nguồn chính của cue, kể cả danh sách trống. Giữ dữ liệu dự án người dùng, ngưỡng QA và cấu hình mặc định trong khi điều tra.

Tài liệu này giữ thứ tự thực hiện và tiêu chí quyết định. `README.md` giữ hướng dẫn vận hành. `storage/maintenance/BUG_REGRESSIONS.md` ghi lỗi đã xác nhận, nguyên nhân và kiểm thử. Báo cáo trong thư mục audit giữ số đo gốc.

## Trạng thái đã xác minh

Ca mẫu đang xử lý theo yêu cầu ngày 09/11 là Douyin `7702752978171596068`, dự án audit `proj_b3e689c2296a`. Người dùng đã cho phép sửa pipeline dựa trên nguyên nhân đo được. Lượt nền đã kết thúc và được giữ làm bằng chứng đối chiếu. Bộ đối chứng gồm video 17 phút `7700940523627875619`, nguồn 36 phút, đoạn 60 giây và các mẫu bố cục độc lập có bằng chứng. Nghiệm thu bao gồm độ đầy đủ nguồn, nghĩa dịch, giọng và timing, vùng che/màu phụ đề, MP4 cuối và thời gian từng bước. Mỗi khác biệt nội dung cần lý do theo nguồn.

Ưu tiên ở mốc audit trước là hoàn tất ca mẫu qua đường Auto và dùng lỗi thực tế để kiểm tra quy tắc chung. Điều tra Edge và bố cục OCR có thể đọc bằng chứng song song. Các phép đo tải CPU/GPU và video thực chạy tuần tự để kiểm soát tranh tài nguyên.

| Hạng mục | Bằng chứng và trạng thái |
| --- | --- |
| Cache Whisper cho lượt OCR lại | Đã có mã và 67 kiểm thử tập trung. Trên nguồn 36 phút, OCR chạy lại giảm từ 269,387 xuống 43,030 giây, giữ kết quả 887 cue và quyết định duyệt. Full suite mới: 1227 passed; server chưa mở lại. [Đối chứng](../storage/maintenance/long_video_profile_20261109/speech_cache_comparison.json) |
| Kiểm tra trên đoạn 60 giây | 40,63 xuống 20,73 giây khi cùng có cache ảnh. 29 cue giữ kết quả. Đoạn cắt lấy từ nguồn 36 phút. [Số đo](../storage/maintenance/short_video_speech_cache_20261109/speech_cache_summary.json) |
| Link Douyin độc lập `7700940523627875619` | Một lượt Auto với mặc định backend Edge, khác mặc định giao diện Piper, 0 sửa tay. Nguồn dài 1057,51 giây. Auto dừng sau 2517,605 giây ở Edge TTS, báo 230/351 giọng hợp lệ. Chưa có MP4. [Lượt chạy](../storage/maintenance/douyin_7700940523627875619_20261109/summary.json) |
| Độ đầy đủ nguồn của link Douyin | Lịch sử DB ghi bỏ 282 cue, trong đó 268 có cờ `dominant_text_size`. Đối chiếu ảnh báo thiếu dải phụ đề một dòng ổn định. Chưa có nhãn đối chiếu người xem/người nghe cho các cue bị bỏ. [Báo cáo nguồn](../storage/maintenance/douyin_7700940523627875619_20261109/projects/proj_10b6d8830a60/transcript/source_recheck.json) |

## Quy trình chẩn đoán trước khi tối ưu

1. Ghi đầu vào, cấu hình, cache, phiên bản và kết quả cuối. Dùng thời gian thực từ lúc bắt đầu và tách thời gian xử lý hữu ích, thử lại, chờ dịch vụ và chờ duyệt. Lượt dừng lỗi chỉ cho biết thời gian đến lỗi. Lượt xuất thành công cần thêm kết quả QA và độ đầy đủ nội dung.
2. Tách bước chậm thành các thao tác có thể đo. Với OCR, tách tạo proxy, quét, nhận dạng, cắt WAV, nhận dạng tiếng và đối chiếu. Với TTS, tách chờ khe gọi, thời gian phản hồi nhà cung cấp, retry, tổng hợp và ghép timeline. Không cộng hai span cha/con thành hai phần thời gian độc lập.
3. Gắn giả thuyết vào phép thử phân biệt. Giới hạn máy cần số đo tài nguyên đúng lúc bước chậm, như mức dùng từng lõi CPU, GPU compute/encode, RAM/VRAM, I/O đĩa và thời gian chờ. Đối chứng trên cùng đầu vào với mức song song khác hoặc cùng phép tính ở chế độ khác. Chi phí thuật toán cần số lần gọi, đọc hoặc tính lại và đối chứng đầu ra. Lỗi dịch vụ cần nhóm đầu vào thật gọi lặp lại có đối chứng và ghi phản hồi. Chậm do chính sách cần chỉ ra điều kiện khiến bước bị chặn hoặc thử lại. Mức sử dụng tài nguyên cao riêng lẻ chưa chứng minh đã chạm giới hạn phần cứng.
4. Chốt nguyên nhân ở mức bằng chứng cho phép. Phân biệt vị trí chậm, cơ chế kéo dài và nguyên nhân gốc. Chọn sửa đổi nhỏ nhất xử lý cơ chế đã chứng minh, ghi mốc trước/sau và tiêu chí chất lượng, rồi kiểm thử lại. Trường hợp chưa có phép đo phân biệt giữ trạng thái giả thuyết.

Áp dụng cho link Douyin hiện tại: gần 32 phút ở TTS là nút thắt của lượt thất bại. Lỗi Edge xen kẽ với thành công giải thích việc bộ đếm lỗi liên tiếp dừng muộn. Chưa xác định vì sao Edge không gửi audio, và chưa có số đo để kết luận máy đã đạt giới hạn. Với OCR, 282 cue bị bỏ là kết quả của chính sách khi bằng chứng thiếu. Cần đối chiếu mẫu nguồn để biết cờ bố cục sai ở đâu và đo riêng các thao tác để xác định phần còn có thể tăng tốc.

## Thứ tự thực hiện

### 1. Tái hiện lỗi Edge bằng các câu thật

Đọc cue từ DB audit và chọn nhóm ID đã lỗi lặp lại cùng nhóm đã có giọng. Ghi ID, dấu vân tay nội dung, phiên bản client, giọng, tốc độ, cao độ, chế độ boundary, số lần gọi, thời gian và kết quả MP3 giải mã. Bảo vệ khóa và cookie trong mọi log.

Chạy xen kẽ câu lỗi và câu đối chứng, tuần tự, giữ nguyên cấu hình ở vòng đầu. Nếu tái hiện được, đổi từng yếu tố để phân biệt nội dung, cấu hình giọng và hành vi dịch vụ. Ghi cả các kết quả trái giả thuyết. Chỉ chốt nguyên nhân cụ thể khi có đối chứng lặp lại. Nếu dịch vụ chưa cho đủ bằng chứng, giữ trạng thái chưa xác định và ghi yếu tố còn thiếu.

Tách riêng cơ chế dừng muộn. Timeline xóa chuỗi lỗi liên tiếp khi một cue thành công, nên lỗi xen kẽ có thể kéo dài dù nhiều cue chưa có giọng. Một sửa đổi kiểm soát thời gian cần hồi quy lỗi xen kẽ, tác vụ khỏe, giữ cache thành công và tiếp tục đúng phần còn thiếu. Piper chỉ dùng đối chứng hoặc được đánh giá riêng trước khi đổi mặc định.

**Trạng thái:** Đã chạy nhóm câu từ hai video. Các cuộc gọi thất bại ghi WebSocket đóng 1006 và ClientConnectionResetError sau turn.start/response, trước audio, khoảng 2,7 giây với CPU vài phần trăm giây. Cùng câu có thể thành công ở lượt khác. Thêm dấu chấm chưa giải quyết ổn định. Chưa xác định nguyên nhân bên trong dịch vụ. Bằng chứng ở `storage/maintenance/edge_cause_20261109`. Phát hiện script audit dùng mặc định backend Edge +0% trong khi giao diện Auto chọn Piper Ngọc Huyền +15%; các lượt mới ghi cấu hình rõ ràng.

### 2. Xác minh nguyên nhân 282 cue bị bỏ

Lấy cue trước thao tác bỏ từ lịch sử DB. Chọn mẫu theo nhóm cờ và thời điểm đầu, giữa, cuối. Đối chiếu ảnh nguồn tại nhiều mốc trong cue, vùng OCR, các box nhận dạng và đoạn âm thanh gốc. Ghi ID, timestamp và kết luận thoại, phụ đề song ngữ, chữ trong cảnh, hỗn hợp hoặc chưa rõ. Số cue bị bỏ phản ánh quyết định của Auto. Số thoại thật bị mất cần được xác minh bằng mẫu có nhãn.

Nếu cờ bố cục bắt nhầm phụ đề, xác định bố cục gây lỗi và xây hồi quy có ca đúng lẫn ca đối chứng. Nếu giới hạn hoặc thứ tự đối chiếu làm ứng viên phù hợp chưa được xét, ghi bằng chứng để sửa lịch kiểm tra. Giữ những cue chưa đủ bằng chứng ở trạng thái cần duyệt.

**Trạng thái:** Đã xem các khung nguồn của nhóm mẫu. Có lỗi giới hạn 12 ứng viên song ngữ trước khi Auto bỏ câu. Bản sửa hoàn tất danh sách ứng viên, giữ sáu lượt đồng thuận và lưu tiến độ theo nhóm. Replay 633 cue khôi phục thêm 244 trong 115,472 giây, còn 39 pending, giữ mọi ID. Video mẫu mới có lỗi nhận dạng ký tự ngắn và nền bị đọc thành chữ; probe phóng lớn chưa đủ đồng thuận, giữ pending. Lượt mới từ link với Piper +15% hoàn tất 290,915 giây, 86 WAV thực khớp DB/manifest, MP4 decode exit 0. Full suite 1227 passed/888,78 giây. Còn 3 cue bị bỏ và 26 bản rút gọn cần duyệt. Xem báo cáo douyin_7702752978171596068_20261109/REPORT.md.

### 3. Sửa và nghiệm thu theo nguyên nhân

Mỗi lỗi được xác nhận cần có kiểm thử tập trung và mục nguyên nhân trong nhật ký hồi quy. Sau khi sửa, chạy đối chứng trên cùng nguồn và thiết lập trước khi chạy Auto đầy đủ trên dự án riêng. Báo thời gian thực, thời gian thử lại/chờ, số cue giữ/bỏ, đủ WAV trên các cue được dùng, timing, QA kỹ thuật và phần cần duyệt nội dung. Giải mã toàn bộ MP4 nếu có đầu ra. Chạy full suite offline khi chốt mốc tích hợp.

Mốc tốc độ cho OCR mới vẫn là giảm ít nhất 15% trên mốc nguồn 36 phút. Mỗi phép đo cần giữ nguồn và điều kiện tương đương. Khi sửa OCR nhằm cải thiện nội dung, đối chiếu với nhãn nguồn và giải thích mọi thay đổi cue thay vì dùng số cue giống nhau làm tiêu chí duy nhất. Đo lại trên một video dài độc lập trước khi đổi mặc định.

## Cách giữ tiến độ qua các lượt làm việc

Trước mỗi thay đổi pipeline, đọc tài liệu này, README và nhật ký hồi quy. Khi phát hiện bằng chứng mới, cập nhật mục tương ứng với kết quả, đường dẫn và bước kế tiếp. Khi thiết kế thay đổi, ghi lý do và tiêu chí nghiệm thu trước khi tích hợp. Cập nhật tài liệu này qua các lượt thay vì mở một kế hoạch mới cho cùng vấn đề.


## Thiết kế chốt theo trao đổi — tự phục hồi và nghiệm thu nội dung

### Yêu cầu sản phẩm

Người dùng chỉ nhập link và nhận thành phẩm tiếng Việt; không biết tiếng Trung và không phải người duyệt nguồn/dịch. Hệ thống tự kiểm tra và sửa. Áp dụng nhiều thể loại, độ dài, bố cục và âm thanh. Cho phép bỏ phần rất nhỏ sau khi phục hồi thất bại, xét cả ý nghĩa và tổng thiếu hụt; không lấy bỏ cue làm lối tắt. Đây là mục tiêu triển khai, chưa phải khả năng đã hoàn tất.

### Quyền quyết định nội dung

Lưu riêng nguồn lời nói, chữ trên hình và vai trò chữ: caption thoại, giải thích, chữ trong cảnh, watermark hoặc chưa rõ. Chọn nguồn theo mục tiêu video đã lưu; không âm thầm chuyển mục tiêu. Với lồng tiếng theo lời nói, audio là căn cứ cho giọng; chữ bổ sung có thể thành sub riêng. Chế độ dịch chữ phải giữ chữ là nguồn chính. Khi hai nguồn khác nhau, ghi bằng chứng và giữ riêng; không ghép thành một câu giả. Tránh đọc watermark/biển hiệu thành thoại.

### Vòng xử lý

1. Tải và kiểm tra nguồn thực, source/settings/version identity, manifest hoàn tất.
2. Nhận dạng và phân loại nguồn theo đoạn; gắn stable cue ID, timestamp và bằng chứng.
3. Dịch theo ngữ cảnh và thuật ngữ có phạm vi; kiểm tra thiếu ý, tên/số, phủ định và quan hệ nhân vật bằng lượt đối chiếu nguồn riêng. Sự đồng ý của hai mô hình không tự chứng minh đúng.
4. Tạo WAV thật, đo độ dài; giải quyết nguyên nhân trễ ở câu gây trễ trước, thử tốc độ trong giới hạn rồi rút gọn có ràng buộc nghĩa khi cần.
5. Cue lỗi vào hàng phục hồi bền vững. Các cue độc lập tiếp tục. Mỗi lỗi lưu nguyên nhân, bằng chứng, cách đã thử, số lượt/chi phí/thời gian và kết quả. Không lặp cùng yêu cầu khi đầu vào/phương án không đổi.
6. Kiểm tra lại phương án sửa; ghi DB và công bố artifacts nhất quán, vô hiệu hóa đúng phần phụ thuộc. Không dùng JSON thay DB khi DB ghi lỗi. Tính lại timeline bị ảnh hưởng; có thể cần encode lại MP4 dù không dịch/OCR lại toàn bộ.
7. Kiểm tra MP4 cuối: decode toàn bộ, streams/duration, WAV/timeline, subtitle/mask và mức thiếu nội dung. Phân biệt đạt kỹ thuật và đạt nội dung.

### Nhánh phục hồi

- Nguồn mờ: đọc lại nhiều khung/ROI phù hợp; dùng lời nói hỗ trợ khi đúng mục tiêu và có bằng chứng. Không dịch lại từ nguồn đã biết là sai.
- Dịch lỗi: chỉ sửa cue lỗi với ngữ cảnh, lỗi cụ thể và thuật ngữ; kiểm tra lại nguồn và nghĩa.
- Timing: đo WAV, tìm overflow gốc và delayed followers; chỉ tái tạo phần phụ thuộc. Không rút gọn followers vô ích.
- Dịch vụ: retry có backoff và giới hạn; dùng provider/model dự phòng đã cấu hình và kiểm định. Đổi giọng cần kiểm soát tính nhất quán người nói, ghi rõ identity để không dùng cache sai.
- Encode: fallback CPU giữ timing/cover/settings; checkpoint tiếp tục đúng bước.
- Thiếu nguồn, DB lỗi hoặc file hỏng: chờ phục hồi/lỗi kỹ thuật trung thực; không công bố thành phẩm giả. Không bảo đảm hoàn tất trong thời gian hữu hạn khi nguồn/dịch vụ không khả dụng.

### Chính sách bỏ tối thiểu

Chỉ xét sau các phương án phục hồi thích hợp. Số chữ ít không có nghĩa là ít quan trọng: tên, số, phủ định, mệnh lệnh và chi tiết thay đổi cốt truyện không được tự coi là phụ. Theo dõi giới hạn từng đoạn, tổng thời lượng/tỷ lệ nội dung và các đoạn liên tiếp. Ngưỡng số cụ thể chưa được kiểm chứng: chưa tự bật chính sách mới hoặc hạ QA hiện có. Vượt ngân sách thì tiếp tục phục hồi hoặc giữ trạng thái chưa đạt nội dung. Giữ timeline gốc và log omission; chỉ che chữ khi có phương án hiển thị thay thế hợp lệ. Bản dùng âm thanh gốc đáng kể là bản dự phòng, không gọi là lồng tiếng hoàn chỉnh.

### Ghi nhớ qua sản phẩm

Lịch sử sửa riêng từng video; glossary theo phim/chủ đề; quy tắc thuật toán áp dụng chung chỉ sau hồi quy và đối chứng. Đề xuất AI chưa xác minh không tự trở thành nhãn đúng hoặc dữ liệu huấn luyện. Tool không tự sửa mã nguồn production. Lỗi mới được tái hiện, phân tích, thêm regression, sửa và kiểm chứng trước tích hợp; có version và khả năng vô hiệu hóa quy tắc.

### Thứ tự A–E trước cập nhật kho lỗi (tham chiếu; dùng kế hoạch hiện hành phía trên)

A. Kiểm kê các điểm dừng hiện tại và dữ liệu cue lỗi trên mẫu: 3 omitted/26 shortened; ghi sơ đồ source→translation→voice→timeline→render. Gate: mọi lỗi có vị trí, bằng chứng và phần phụ thuộc rõ ràng.
B. Thêm trạng thái/hàng phục hồi theo cue, resume/cancel và ngân sách. Gate: restart không mất việc, không xử lý lại cue đạt, DB/artifacts nhất quán; hồi quy lỗi xen kẽ và hết ngân sách.
C. Phân biệt vai trò chữ/lời nói và tự kiểm tra/sửa nguồn-nghĩa. Gate: tập đối chứng có caption khớp/khác tiếng, watermark, chữ giải thích, tên/số/phủ định; đo cả sửa đúng và sửa nhầm. Không dùng tự chấm AI làm ground truth duy nhất.
D. Tích hợp sửa timing, dịch vụ dự phòng và chính sách bỏ tối thiểu sau khi hiệu chỉnh. Gate: giữ ngưỡng QA, không bỏ nội dung quan trọng; thử lỗi giả lập offline và provider thật ngoài pytest.
E. Chạy lại mẫu từ link và video dài độc lập trong dự án audit; kiểm tra DB/WAV/timing/full decode và nội dung. Full suite offline tại mốc tích hợp. Báo thời gian hữu ích/retry/chờ, tỷ lệ phục hồi, bỏ sót, sửa nhầm, lỗi/1000cue hoặc phút video; không suy rộng từ một mẫu.

Tích hợp duy nhất vào D:/duanlaylink; README giữ hướng dẫn vận hành, BUG_REGRESSIONS giữ nguyên nhân/kiểm thử. Không tạo sản phẩm thử nghiệm thay thế. Mốc 1227 tests và QA mẫu trước đây chỉ xác nhận mã đã chạy lúc đó, không chứng nhận thiết kế mới này đã được thực hiện.


### Bổ sung tương thích và hiển thị — 10/11/2026

Trườngcaption_rolemặcđịnh làmhashsnapshotcũkhácdùkhôngsửa nguồn;đãtái hiện legacyundo fail vàsửabằng transcript_hash_matches:chỉcoithiếucaption_role tươngđươngunclassified,khôngbỏnondefault hoặcfieldkhác.6legacycasesđạt;đổichữ/dịch/timing/rolethựcvẫnbịchặn. Nhómmởrộng195passed/143.74s.

BảngGiọngViệt cònđếmplaceholder làthoại trongkiểmthử;GETcues thêmcomputedsubtitle_only_reason,UIkhôngđếm/khôngchoplayregen vàghi rõchỉhiệnsub. Asset4.6.10.2regressiontrướcsửafail,nhómUI/compat61pass/135.26s sau sửa. SửareadonlyGET/hiểnthịđượcápdụngsaukhi fullcoređãbắtđầu;khôngcộng2testUI mới vào sốfullhoặcclaimfullfreshtrênmọidòngmãcuối. OCR/voice/render/hashlogic đãđóngbăng,fullpending;đợiXML/logchốtrồiupdatebáocáo. Đây làđánhgiáphânphạmvi,tránhbáocáosai.
