"""Dò các lớp phủ cháy sẵn trên khung hình: watermark góc và dải sub cứng ở đáy.

Cách làm: lấy mẫu vài chục khung hình, tìm **chữ có viền** (điểm sáng nằm cạnh điểm
tối), rồi phân loại bằng **độ bền theo thời gian**:

    xuất hiện gần như mọi khung, cụm gọn ở góc  -> watermark
    xuất hiện ngắt quãng, dồn vào dải hẹp ở đáy -> sub cứng

Vì sao không dùng "phương sai thời gian ~ 0" để tìm vùng tĩnh: watermark là chữ bán
trong suốt, điểm ảnh quan sát được là `p = a*L + (1-a)*B(t)` nên phương sai của nó
vẫn bằng `(1-a)^2` lần phương sai của nền - ở a~0.7 vẫn còn 9%, không tách được khỏi
một vùng tối tĩnh bình thường. Ngưỡng sáng/tối đo đúng thứ cần tìm nên miễn nhiễm với
độ trong suốt.

Lấy mẫu ở GIỮA mỗi cảnh thay vì rải đều theo thời gian: khung hình từ các cảnh khác
nhau phá vỡ mọi nền tĩnh giả, chỉ lớp phủ thật mới sống sót. Đây là thứ xử lý luôn cả
hai ca hỏng khó nhất - video một góc máy cố định, và hoạt hình giữ nguyên cel nhiều giây.
"""

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional, Sequence, Tuple, Union

import numpy as np

from config.settings import settings
from src.jobs.logger import SystemErrorCodes, PipelineException
from src.video.cleanup import DETECTOR_VERSION, CleanPlan, MaskBox, clamp_box

# Ngưỡng nhận diện "chữ có viền", dùng cho dải phụ đề cứng - loại chữ này gần như
# luôn có viền tối để đọc được trên mọi nền.
T_BRIGHT = 200
T_DARK = 90
OUTLINE_RADIUS = 3

# Watermark thì KHÔNG dùng điều kiện viền. Đo thực tế trên cả 4 video mẫu: watermark
# Bilibili là chữ xám trơn phủ alpha, không có viền, nên điều kiện viền loại sạch nó
# (0 điểm sống sót). Chỉ riêng độ bền của độ sáng lại tách rất gọn: lấy mẫu ở giữa
# 15-36 cảnh KHÁC NHAU thì chỉ lớp phủ mới giữ nguyên được cùng một chỗ sáng, cho ra
# hộp bao gần như trùng khít trên cả 4 video (y12-31, x bắt đầu 13-14).
T_STATIC_BRIGHT = 190

# Vùng che phải có CẤU TRÚC mới là chữ. Nền sáng phẳng (bầu trời, bức tường) cũng bền
# theo thời gian nhưng độ lệch chuẩn trong vùng rất thấp, còn chữ thì xen kẽ sáng tối.
MIN_BOX_STRUCTURE_STD = 18.0

# Vùng tìm watermark ở mỗi góc, tính theo tỉ lệ khung hình. Watermark đo được trên
# 4 video mẫu nằm gọn trong x<=28%, y<=13%; để rộng hơn cho chắc.
CORNER_W = 0.45
CORNER_H = 0.22

# Độ bền theo thời gian dùng để phân loại
STATIC_PERSISTENCE = 0.9
BAND_MIN_PERSISTENCE = 0.25
BAND_MAX_PERSISTENCE = 0.85

# Nới vùng che ra vài pixel để phủ hết phần biên chữ bị làm mượt
WATERMARK_PADDING = 3
BAND_PADDING = 4

# Số điểm ảnh chữ tối thiểu để coi là một lớp phủ thật, không phải nhiễu lác đác
MIN_TEXT_PIXELS = 20

# Dò dải sub theo MỐC LỜI THOẠI: so khung hình lúc đang nói với lúc im lặng.
#
# Cách dò theo "độ bền ngắt quãng" (BAND_MIN/MAX_PERSISTENCE) hỏng trên video mà câu
# thoại ngắn và thưa: đo trên proj_ab38ffd2efe3 (720p, 70 câu trong 196s), lấy mẫu
# giữa các cảnh thì chữ Trung chỉ hiện ở 19% khung - dưới ngưỡng 25% - nên bộ dò bỏ
# qua, và phụ đề Việt bị khắc chồng lên chữ Trung. Transcript đã cho biết chính xác
# lúc nào có lời, nên lấy mẫu đúng lúc đó: hàng có chữ lúc nói mà không có lúc im
# lặng chính là dải sub. Đo trên 5 video: dải sub thật cho chênh 0.03-0.13 (tỉ lệ
# điểm chữ trên hàng), còn video không có sub cứng thì đỉnh nằm ở 0.62 chiều cao với
# chênh 0.011 - hai ngưỡng dưới đây tách được cả hai.
TIMED_MIN_ROW_TEXT = 0.02
TIMED_MIN_RATIO = 1.5
# Mốc lời thoại ngắn hơn mức này thì khung lấy mẫu dễ rơi vào lúc chữ vừa tắt
TIMED_MIN_CUE = 0.6
TIMED_MIN_GAP = 0.8


def _axis_slice(arr: np.ndarray, axis: int, start: int, stop: int) -> np.ndarray:
    index = [slice(None)] * arr.ndim
    index[axis] = slice(start, stop)
    return arr[tuple(index)]


def _min_filter(arr: np.ndarray, radius: int) -> np.ndarray:
    """Lọc cực tiểu cửa sổ vuông, thuần numpy và tách theo từng trục.

    Tách trục cho ra cùng kết quả với cửa sổ vuông nhưng chỉ tốn O(radius) phép so
    sánh mỗi trục thay vì O(radius^2). Đệm theo kiểu 'edge' chứ không cuộn vòng, nếu
    không một điểm sáng sát mép trái sẽ "nhìn thấy" điểm tối ở mép phải.
    """
    out = arr
    for axis in (arr.ndim - 2, arr.ndim - 1):
        length = out.shape[axis]
        pad = [(0, 0)] * out.ndim
        pad[axis] = (radius, radius)
        padded = np.pad(out, pad, mode="edge")
        acc = out
        for shift in range(1, radius + 1):
            lo = _axis_slice(padded, axis, radius - shift, radius - shift + length)
            hi = _axis_slice(padded, axis, radius + shift, radius + shift + length)
            acc = np.minimum(acc, np.minimum(lo, hi))
        out = acc
    return out


class OverlayDetector:
    """Tìm vùng cần che trên khung hình. Không bao giờ ném ngoại lệ ra ngoài."""

    def __init__(
        self,
        ffmpeg_path: Optional[str] = None,
        sample_count: Optional[int] = None,
    ):
        self.ffmpeg_path = ffmpeg_path or settings.FFMPEG_PATH
        self.sample_count = sample_count or settings.CLEAN_SAMPLE_FRAMES

    def get_ffmpeg_executable(self) -> Path:
        """Xác định đường dẫn ffmpeg trên hệ thống."""
        if self.ffmpeg_path:
            p = Path(self.ffmpeg_path)
            if p.is_file():
                return p

        found = shutil.which("ffmpeg")
        if found:
            return Path(found)

        raise PipelineException(
            SystemErrorCodes.FFMPEG_NOT_FOUND,
            "Không tìm thấy công cụ ffmpeg. Vui lòng cấu hình FFMPEG_PATH hoặc thêm vào PATH.",
        )

    # ------------------------------------------------------------------ lấy mẫu

    def _sample_timestamps(
        self, duration: float, scenes: Optional[Sequence] = None
    ) -> List[float]:
        """Chọn mốc thời gian để trích khung hình, ưu tiên giữa mỗi cảnh."""
        if duration <= 0:
            return []

        # Bỏ 5% đầu và cuối: hay dính logo mở đầu, thẻ kết thúc, màn hình đen
        lo, hi = duration * 0.05, duration * 0.95
        if hi <= lo:
            lo, hi = 0.0, duration

        def _field(scene, *names) -> float:
            # Danh sách cảnh tới từ hai nơi: scenes.json (dict, khoá 'start') và bảng
            # Scene trong database (object, thuộc tính 'start_time'). Nhận cả hai.
            for name in names:
                value = (
                    scene.get(name) if isinstance(scene, dict)
                    else getattr(scene, name, None)
                )
                if value is not None:
                    return float(value)
            return 0.0

        marks: List[float] = []
        if scenes:
            for scene in scenes:
                start = _field(scene, "start", "start_time")
                dur = _field(scene, "duration")
                mid = start + dur / 2
                if lo <= mid <= hi:
                    marks.append(mid)

        if len(marks) < 4:
            # Chưa tách cảnh (hoặc quá ít cảnh) -> rải đều, kém tin cậy hơn
            count = self.sample_count
            marks = [lo + (hi - lo) * (i + 0.5) / count for i in range(count)]
        elif len(marks) > self.sample_count:
            # Nhiều cảnh hơn cần thiết -> chọn thưa đều trên toàn bộ thời lượng
            step = len(marks) / self.sample_count
            marks = [marks[int(i * step)] for i in range(self.sample_count)]

        return sorted(set(round(m, 3) for m in marks))

    def _extract_gray(
        self, video: Path, timestamps: List[float], work_dir: Path
    ) -> np.ndarray:
        """Trích nhiều khung hình xám trong MỘT lần gọi ffmpeg.

        Giữ NGUYÊN độ phân giải gốc - khác với KeyframeExtractor vốn ép về 640px:
        với video dọc 360x640 thì đó là phóng to, làm nhoè đúng cái biên chữ cần dò.
        Dùng PNG chứ không JPG vì nhiễu nén quanh nét chữ tương phản cao sẽ làm sai
        lệch ngưỡng sáng/tối.
        """
        if not timestamps:
            return np.empty((0, 0, 0), dtype=np.uint8)

        from PIL import Image

        ffmpeg_bin = self.get_ffmpeg_executable()
        select = "+".join(
            f"gte(t\\,{t:.3f})*not(gte(prev_t\\,{t:.3f}))" for t in timestamps
        )
        pattern = work_dir / "_gray_%04d.png"

        try:
            res = subprocess.run(
                [
                    str(ffmpeg_bin), "-hide_banner", "-loglevel", "error", "-y",
                    "-i", str(video), "-an",
                    "-vf", f"select='{select}',format=gray",
                    "-fps_mode", "passthrough", str(pattern),
                ],
                capture_output=True,
                text=True,
                timeout=settings.FFMPEG_TIMEOUT_SECONDS,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError):
            return np.empty((0, 0, 0), dtype=np.uint8)

        made = sorted(work_dir.glob("_gray_*.png"))
        if res.returncode != 0 or len(made) < 4:
            return np.empty((0, 0, 0), dtype=np.uint8)

        frames = []
        for path in made:
            try:
                with Image.open(path) as img:
                    frames.append(np.asarray(img.convert("L"), dtype=np.uint8))
            except Exception:
                continue

        # Khung hình lệch kích thước (video đổi độ phân giải giữa chừng) thì bỏ
        if not frames:
            return np.empty((0, 0, 0), dtype=np.uint8)
        shape = frames[0].shape
        frames = [f for f in frames if f.shape == shape]
        if len(frames) < 4:
            return np.empty((0, 0, 0), dtype=np.uint8)

        return np.stack(frames)

    # ------------------------------------------------------------------ phân tích

    def _text_mask(self, frames: np.ndarray) -> np.ndarray:
        """Mặt nạ 'chữ sáng có viền tối' cho từng khung hình - dùng cho dải phụ đề."""
        bright = frames > T_BRIGHT
        dark_near = _min_filter(frames, OUTLINE_RADIUS) < T_DARK
        return bright & dark_near

    def _bright_mask(self, frames: np.ndarray) -> np.ndarray:
        """Mặt nạ điểm sáng - dùng cho watermark, vốn không có viền (xem T_STATIC_BRIGHT)."""
        return frames > T_STATIC_BRIGHT

    def _box_confidence(
        self, text_mask: np.ndarray, x: int, y: int, w: int, h: int
    ) -> float:
        """Tỉ lệ khung hình mà vùng này vẫn có lượng chữ như thường lệ.

        Lớp phủ thật hiện diện đều đặn nên gần 1.0; vài điểm nhiễu tình cờ trùng nhau
        thì rất thấp. Đây là con số dùng để quyết định che hay không che.
        """
        region = text_mask[:, y:y + h, x:x + w]
        counts = region.sum(axis=(1, 2))
        typical = float(np.median(counts))
        if typical < MIN_TEXT_PIXELS:
            return 0.0
        return float((counts >= typical * 0.5).mean())

    def _pick_method(
        self, frames: np.ndarray, box: Tuple[int, int, int, int]
    ) -> Tuple[str, str]:
        """Chọn bộ lọc che và màu, dựa trên nền ngay quanh vùng che.

        Nếu vùng che nằm trên một dải tối phẳng (viền đen của video) thì tô lại đúng
        màu dải đó là khôi phục chính xác từng điểm ảnh. Ngược lại mới cần delogo nội
        suy, và chỉ khi vùng đủ nhỏ để nội suy không bị nhoè.
        """
        x, y, w, h = box
        height, width = frames.shape[1], frames.shape[2]

        pad = 3
        outer = frames[
            :,
            max(0, y - pad):min(height, y + h + pad),
            max(0, x - pad):min(width, x + w + pad),
        ].astype(np.float32)
        inner_mask = np.ones(outer.shape[1:], dtype=bool)
        top = min(pad, y)
        left = min(pad, x)
        inner_mask[top:top + h, left:left + w] = False
        rings = outer[:, inner_mask]

        # Nền phải phẳng và tối ở GẦN NHƯ MỌI khung, không phải ở khung trung vị.
        # Đo trên proj_ab38ffd2efe3: quanh logo tối ở đa số cảnh nên trung vị ra
        # "đen phẳng", bộ dò tô ô 0x212121 - rồi tới cảnh sáng thì ô đó lộ rõ như một
        # miếng dán. Tô đúng màu chỉ khôi phục chính xác khi nền thật sự là viền đen.
        if rings.size:
            flat = (rings.std(axis=1) < 8) & (rings.mean(axis=1) < 40)
            if float(flat.mean()) >= 0.95:
                level = int(round(float(np.median(rings))))
                return "drawbox", f"0x{level:02x}{level:02x}{level:02x}"

        fits_inset = x >= 1 and y >= 1 and x + w <= width - 1 and y + h <= height - 1
        if fits_inset and (w * h) <= 0.04 * width * height:
            return "delogo", "black"

        # Vùng quá to cho delogo: làm mờ thay vì ô đen đặc. Đo trên proj_ab38ffd2efe3
        # (720p, logo 231x45 góc phải): ô đen hiện rõ như một miếng dán trên nền sáng.
        return "blur", "black"

    def detect_watermark(
        self, bright_mask: np.ndarray, frames: np.ndarray
    ) -> Optional[MaskBox]:
        """Tìm MỘT vùng sáng đứng yên gần như suốt video, nằm ở một góc khung hình."""
        count, height, width = bright_mask.shape
        median_frame = np.median(frames, axis=0)
        persistence = bright_mask.mean(axis=0)
        static = persistence >= STATIC_PERSISTENCE
        if not static.any():
            return None

        zone_h = max(1, int(height * CORNER_H))
        zone_w = max(1, int(width * CORNER_W))
        corners = {
            "top-left": (0, zone_h, 0, zone_w),
            "top-right": (0, zone_h, width - zone_w, width),
            "bottom-left": (height - zone_h, height, 0, zone_w),
            "bottom-right": (height - zone_h, height, width - zone_w, width),
        }

        best: Optional[MaskBox] = None
        for name, (r0, r1, c0, c1) in corners.items():
            zone = static[r0:r1, c0:c1]
            if zone.sum() < MIN_TEXT_PIXELS:
                continue

            rows = np.where(zone.any(axis=1))[0]
            cols = np.where(zone.any(axis=0))[0]
            ty0, ty1 = r0 + int(rows[0]), r0 + int(rows[-1]) + 1
            tx0, tx1 = c0 + int(cols[0]), c0 + int(cols[-1]) + 1
            y0 = ty0 - WATERMARK_PADDING
            y1 = ty1 + WATERMARK_PADDING
            x0 = tx0 - WATERMARK_PADDING
            x1 = tx1 + WATERMARK_PADDING

            x0, y0 = max(0, x0), max(0, y0)
            x1, y1 = min(width, x1), min(height, y1)
            w, h = x1 - x0, y1 - y0

            # Chốt chặn: một vùng che quá lớn là dò SAI, không phải dò trúng đậm.
            # 4 watermark thật đo được đều chỉ chiếm ~3,5% khung hình.
            if w > width * 0.45 or (w * h) > settings.CLEAN_WATERMARK_MAX_AREA * width * height:
                continue

            # Nền sáng phẳng cũng "bền" theo thời gian nhưng không có cấu trúc chữ.
            # Không có chốt này thì một mảng trời sáng ở góc sẽ bị che oan.
            #
            # Đo trên khung SÁT chữ, chưa nới padding: phần nới ra nằm ngoài lớp phủ
            # nên toàn là nội dung có vân, đủ để kéo độ lệch chuẩn của cả một mảng
            # trời phẳng lên trên ngưỡng. Đo đúng vùng bền sáng mới là đo đúng thứ
            # đang xét "cái này có phải chữ không".
            if float(median_frame[ty0:ty1, tx0:tx1].std()) < MIN_BOX_STRUCTURE_STD:
                continue

            confidence = self._box_confidence(bright_mask, x0, y0, w, h)
            if confidence < settings.CLEAN_MIN_CONFIDENCE:
                continue
            if best is not None and confidence <= best.confidence:
                continue

            method, color = self._pick_method(frames, (x0, y0, w, h))
            best = MaskBox(
                x=x0, y=y0, w=w, h=h,
                kind="watermark", method=method, color=color,
                confidence=round(confidence, 3),
            )

        return best

    def detect_subtitle_band(self, text_mask: np.ndarray) -> Optional[MaskBox]:
        """Tìm dải sub cứng: chữ hiện rồi tắt, dồn vào một dải hàng hẹp ở đáy khung.

        Phân tích theo độ bền của từng hàng (row persistence):
        - row_has_text: hàng y có chữ nếu tỉ lệ điểm text > 1% bề rộng.
        - row_persistence: tỉ lệ khung hình mà hàng y có chữ.
        - Dải phụ đề là dải hàng liên tục ở vùng dưới khung hình có tỉ lệ cao (15% - 95%).
        """
        count, height, width = text_mask.shape
        if count < 4 or height < 20 or width < 20:
            return None

        # row_has_text: khung f, hàng y có chữ nếu text_mask[f, y].mean() > 0.01
        row_has_text = text_mask.mean(axis=2) > 0.01  # shape (count, height)
        row_persistence = row_has_text.mean(axis=0)    # shape (height,)

        search_top = int(height * (0.6 if height > width else 0.75))
        region = row_persistence[search_top:]
        if not region.size or region.max() < 0.15:
            return None

        peak_row = search_top + int(np.argmax(region))
        peak = float(row_persistence[peak_row])
        # Bỏ qua nếu là viền/thanh cố định tĩnh (> 95% thời lượng)
        if peak > 0.95:
            return None

        shoulder = peak * 0.25
        top = peak_row
        while top - 1 >= search_top and row_persistence[top - 1] >= shoulder:
            top -= 1
        bottom = peak_row
        while bottom + 1 < height and row_persistence[bottom + 1] >= shoulder:
            bottom += 1

        band_h = bottom - top + 1
        if band_h > settings.CLEAN_SUBTITLE_BAND_MAX_H * height:
            return None

        # Phụ đề lời thoại luôn căn giữa; kiểm tra trọng tâm chữ theo trục hoành
        band_text = text_mask[:, top:bottom + 1, :].sum(axis=(0, 1)).astype(float)
        if band_text.sum() <= 0:
            return None
        centroid = float((band_text * np.arange(width)).sum() / band_text.sum())
        if abs(centroid - width / 2) > 0.15 * width:
            return None

        # Đỉnh phải tương đối cô lập ở nửa dưới khung hình
        lower_profile = row_persistence[int(height * 0.5):]
        lower_total = lower_profile.sum()
        inside = row_persistence[top:bottom + 1].sum()
        confidence = float(inside / lower_total) if lower_total > 0 else 0.0
        if confidence < settings.CLEAN_MIN_CONFIDENCE:
            return None

        return self._band_box(top, bottom, width, height, confidence)

    @staticmethod
    def _band_box(
        top: int, bottom: int, width: int, height: int, confidence: float
    ) -> MaskBox:
        """Vùng làm mờ phủ đúng các hàng có chữ, hết chiều ngang khung.

        Làm mờ chứ không tô đen: khay đen che mất cả dải cảnh phía dưới, còn làm mờ
        xoá được chữ Trung mà vẫn giữ màu và hình khối của cảnh. Chỉ phủ đúng dải chữ
        (không kéo xuống sát đáy) vì chữ Việt sẽ được đặt vào chính giữa dải này.
        """
        y = max(0, top - BAND_PADDING)
        y1 = min(height, bottom + 1 + BAND_PADDING)
        return MaskBox(
            x=0, y=y, w=width, h=y1 - y,
            kind="subtitle_band", method="blur", color="black",
            confidence=round(confidence, 3),
        )

    def detect_subtitle_band_timed(
        self, speech_frames: np.ndarray, silent_frames: np.ndarray
    ) -> Optional[MaskBox]:
        """Tìm dải sub bằng cách so khung lúc đang nói với khung lúc im lặng.

        Xem TIMED_MIN_ROW_TEXT về lý do cần cách này bên cạnh detect_subtitle_band.
        """
        if speech_frames.ndim != 3 or silent_frames.ndim != 3:
            return None
        if speech_frames.shape[1:] != silent_frames.shape[1:]:
            return None
        count, height, width = speech_frames.shape

        on_mask = self._text_mask(speech_frames)
        on = on_mask.mean(axis=(0, 2))
        off = self._text_mask(silent_frames).mean(axis=(0, 2))
        diff = on - off

        search_top = int(height * (0.6 if height > width else 0.75))
        region = diff[search_top:]
        if not region.size:
            return None
        peak_row = search_top + int(np.argmax(region))
        peak = float(diff[peak_row])
        if peak < TIMED_MIN_ROW_TEXT or on[peak_row] < TIMED_MIN_RATIO * off[peak_row]:
            return None

        shoulder = peak * 0.25
        top = peak_row
        while top - 1 >= search_top and diff[top - 1] >= shoulder:
            top -= 1
        bottom = peak_row
        while bottom + 1 < height and diff[bottom + 1] >= shoulder:
            bottom += 1
        if bottom - top + 1 > settings.CLEAN_SUBTITLE_BAND_MAX_H * height:
            return None

        band = on_mask[:, top:bottom + 1]
        col_profile = band.sum(axis=(0, 1)).astype(float)
        if col_profile.sum() <= 0:
            return None
        centroid = float((col_profile * np.arange(width)).sum() / col_profile.sum())
        if abs(centroid - width / 2) > 0.15 * width:
            return None

        # Tỉ lệ khung lúc đang nói mà dải thật sự có chữ - Whisper thỉnh thoảng bắt
        # cả tiếng động không có phụ đề, nên không đòi 100%.
        present = float((band.sum(axis=(1, 2)) >= width * 0.01).mean())
        if present < settings.CLEAN_MIN_CONFIDENCE:
            return None

        return self._band_box(top, bottom, width, height, present)

    def _speech_timestamps(
        self, speech: Sequence, duration: float
    ) -> Tuple[List[float], List[float]]:
        """Mốc giữa các câu thoại và mốc giữa các khoảng lặng, mỗi loại tối đa N mốc."""
        min_cue = 0.3
        min_gap = 0.25
        cues = sorted(
            (float(s), float(e)) for s, e in speech
            if float(e) - float(s) >= min_cue and 0 <= float(s) < duration
        )
        if not cues:
            return [], []

        spoken = [(s + e) / 2 for s, e in cues]
        silent = []

        # Khoảng lặng đầu video trước câu thoại đầu tiên
        if cues[0][0] >= min_gap:
            silent.append(cues[0][0] / 2)

        # Khoảng lặng giữa các câu thoại
        for i in range(len(cues) - 1):
            gap = cues[i + 1][0] - cues[i][1]
            if gap >= min_gap:
                silent.append((cues[i][1] + cues[i + 1][0]) / 2)

        # Khoảng lặng cuối video sau câu thoại cuối cùng
        if duration - cues[-1][1] >= min_gap:
            silent.append((cues[-1][1] + duration) / 2)

        def spread(marks: List[float]) -> List[float]:
            n = min(self.sample_count, len(marks))
            if n <= 0:
                return []
            return [round(marks[int(i * len(marks) / n)], 3) for i in range(n)]

        return spread(spoken), spread(silent)

    def _band_from_speech(
        self, video_path: Path, speech: Sequence, duration: float
    ) -> Optional[MaskBox]:
        """Trích khung theo mốc lời thoại rồi dò dải sub. None nếu không đủ dữ liệu."""
        spoken, silent = self._speech_timestamps(speech, duration)
        # Không có khoảng lặng nào thì không có gì để so - để cách dò cũ lo
        if len(spoken) < 4 or len(silent) < 4:
            return None
        with tempfile.TemporaryDirectory(prefix="overlay_on_") as tmp:
            on_frames = self._extract_gray(video_path, spoken, Path(tmp))
        with tempfile.TemporaryDirectory(prefix="overlay_off_") as tmp:
            off_frames = self._extract_gray(video_path, silent, Path(tmp))
        if on_frames.size == 0 or off_frames.size == 0:
            return None
        return self.detect_subtitle_band_timed(on_frames, off_frames)

    # ------------------------------------------------------------------ đầu vào chính

    def detect(
        self,
        video_path: Union[Path, str],
        width: int,
        height: int,
        duration: float,
        scenes: Optional[Sequence] = None,
        source_key: str = "",
        speech: Optional[Sequence] = None,
    ) -> CleanPlan:
        """Dựng kế hoạch làm sạch cho một video. KHÔNG BAO GIỜ ném ngoại lệ.

        Không dò được thì trả về kế hoạch rỗng với origin='none': để nguyên watermark
        vẫn tốt hơn nhiều so với che nhầm vào nội dung, vì vùng che đã nướng vào tệp
        xuất ra thì không gỡ lại được.
        """
        plan = CleanPlan(
            version=DETECTOR_VERSION,
            source_key=source_key,
            video_width=width,
            video_height=height,
            origin="none",
        )

        try:
            timestamps = self._sample_timestamps(duration, scenes)
            if not timestamps:
                plan.notes.append("Không xác định được thời lượng video để lấy mẫu.")
                return plan
            if not scenes:
                plan.notes.append(
                    "Chưa có dữ liệu phân cảnh, phải lấy mẫu rải đều - độ tin cậy thấp hơn."
                )

            with tempfile.TemporaryDirectory(prefix="overlay_") as tmp:
                frames = self._extract_gray(Path(video_path), timestamps, Path(tmp))

            if frames.size == 0:
                plan.notes.append("Không trích được khung hình để phân tích.")
                return plan

            plan.video_height, plan.video_width = frames.shape[1], frames.shape[2]
            text_mask = self._text_mask(frames)

            boxes: List[MaskBox] = []
            watermark = self.detect_watermark(self._bright_mask(frames), frames)
            if watermark is not None:
                boxes.append(watermark)
            else:
                plan.notes.append("Không thấy watermark tĩnh nào đủ chắc chắn.")

            # Ưu tiên dò theo mốc lời thoại (chính xác hơn hẳn khi đã có transcript),
            # không ra thì mới dùng cách dò theo độ bền ngắt quãng.
            band = (
                self._band_from_speech(Path(video_path), speech, duration)
                if speech else None
            )
            if band is None:
                band = self.detect_subtitle_band(text_mask)
            if band is not None:
                boxes.append(band)
            else:
                plan.notes.append("Không thấy dải phụ đề cứng nào đủ chắc chắn.")

            safe = [
                b for b in (
                    clamp_box(box, plan.video_width, plan.video_height) for box in boxes
                ) if b is not None
            ]
            plan.boxes = safe
            plan.origin = "auto" if safe else "none"

            band_box = next((b for b in safe if b.kind == "subtitle_band"), None)
            if band_box is not None:
                from src.media.subtitles import ass_margin_for_band

                # Chữ Việt nằm giữa dải vừa làm mờ, tức đúng chỗ chữ Trung cũ
                plan.subtitle_margin_v = ass_margin_for_band(
                    band_box.y, band_box.y + band_box.h, plan.video_height
                )
        except Exception as e:
            plan.boxes = []
            plan.origin = "none"
            plan.notes.append(f"Dò lớp phủ gặp lỗi, bỏ qua bước làm sạch: {e}")

        return plan
