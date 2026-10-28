"""Ước lượng thời gian còn lại của pipeline ("còn khoảng 10 phút nữa xong").

Mỗi công đoạn được chia thành các đơn vị nhỏ (tách audio, nhận dạng giọng nói,
dịch...). Thời gian của một đơn vị ước bằng:  cố_định + tốc_độ × độ_dài_video.
Tốc độ khởi điểm lấy từ số đo thật; sau mỗi lần chạy xong, hệ thống tự học lại tốc
độ của chính máy này (lưu ở storage/eta_stats.json), nên càng chạy càng đúng.

Khi đơn vị đang chạy báo được tiến độ bên trong (câu thứ mấy / tổng số câu), ước
lượng chuyển dần từ "theo lịch sử" sang "theo tốc độ đang thấy".
"""

import json
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional

from config.settings import settings

# (giây cố định, giây trên mỗi giây video). Đo trên proj_ab38ffd2efe3 (video 196s)
# và bài đo Whisper CPU/GPU; sẽ được thay bằng số đo thật sau vài lần chạy.
DEFAULT_RATES: Dict[str, tuple] = {
    "source": (2.0, 0.0),
    "download": (3.0, 0.06),
    "extract_audio": (0.5, 0.007),
    "stt:cpu": (3.0, 0.18),
    "stt:cuda": (4.0, 0.06),
    "translate": (5.0, 0.15),
    "scenes": (2.0, 0.25),
    "scene_ai": (10.0, 0.8),
    "emotion": (4.0, 0.1),
    "tts": (3.0, 0.35),
    "mux": (2.0, 0.12),
    "script": (20.0, 0.45),
}

LABELS = {
    "source": "Lấy thông tin video",
    "download": "Tải video",
    "extract_audio": "Tách audio",
    "stt": "Nhận dạng giọng nói",
    "translate": "Dịch tiếng Việt",
    "scenes": "Phân cảnh & khung hình",
    "scene_ai": "AI phân tích cảnh",
    "emotion": "Phân tích cảm xúc",
    "tts": "Đọc lồng tiếng",
    "mux": "Ghép video",
    "script": "Sinh kịch bản",
}

# Đơn vị con của từng công đoạn công khai (tên công đoạn như trong run_full_pipeline)
STEP_UNITS: Dict[str, List[str]] = {
    "source": ["source"],
    "download": ["download"],
    "transcribe": ["extract_audio", "stt", "translate"],
    "analyze": ["scenes", "scene_ai"],
    "voiceover": ["emotion", "tts", "mux"],
    "generate-script": ["script"],
}

# Chưa biết độ dài video (trước khi lấy metadata) thì tạm đoán như video ngắn 3 phút
FALLBACK_DURATION = 180.0
LEARN_WEIGHT = 0.5


@dataclass
class _Unit:
    name: str
    group: int
    state: str = "pending"  # pending | running | done | failed
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    fraction: float = 0.0


@dataclass
class _Plan:
    units: List[_Unit]
    duration: Optional[float]
    started_at: float = field(default_factory=time.time)
    adhoc: bool = False
    finished: bool = False


class ProgressTracker:
    def __init__(self, stats_path=None):
        self._plans: Dict[str, _Plan] = {}
        self._lock = threading.RLock()
        self._stats_path = stats_path
        self._stats: Optional[Dict[str, List[float]]] = None

    # ------------------------------------------------------------ tốc độ đã học
    def _path(self):
        return self._stats_path or (settings.storage_path.parent / "eta_stats.json")

    def _load_stats(self) -> Dict[str, List[float]]:
        if self._stats is None:
            try:
                self._stats = json.loads(self._path().read_text(encoding="utf-8"))
            except Exception:
                self._stats = {}
        return self._stats

    def _rate_key(self, unit: str) -> str:
        if unit == "stt":
            from src.gpu import whisper_runtime

            return f"stt:{whisper_runtime()[0]}"
        return unit

    def estimate(self, unit: str, duration: Optional[float]) -> float:
        key = self._rate_key(unit)
        base, per_sec = DEFAULT_RATES.get(key, (5.0, 0.1))
        learned = self._load_stats().get(key)
        if learned:
            base, per_sec = learned
        return base + per_sec * (duration or FALLBACK_DURATION)

    def _learn(self, unit: str, elapsed: float, duration: Optional[float]) -> None:
        if not duration or duration <= 0 or elapsed <= 0:
            return
        key = self._rate_key(unit)
        base, old = self._load_stats().get(key) or DEFAULT_RATES.get(key, (5.0, 0.1))
        observed = max(0.0, (elapsed - base) / duration)
        # Lần chạy dùng lại kết quả cũ (bộ nhớ đệm giọng đọc, cảm xúc...) xong gần như
        # tức thì; kẹp lại để một lần như vậy không kéo tụt ước lượng của lần sau.
        if old > 0:
            observed = min(max(observed, old / 3), old * 3)
        self._stats[key] = [base, round(old * (1 - LEARN_WEIGHT) + observed * LEARN_WEIGHT, 5)]
        try:
            target = self._path()
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp_target = target.with_suffix(f"{target.suffix}.tmp")
            tmp_target.write_text(json.dumps(self._stats, indent=2), encoding="utf-8")
            os.replace(tmp_target, target)
        except Exception:
            pass

    # ------------------------------------------------------------ kế hoạch chạy
    def plan(
        self,
        project_id: str,
        groups: Iterable[Iterable[str]],
        duration: Optional[float],
        adhoc: bool = False,
    ) -> None:
        """Khai báo các công đoạn sắp chạy. Mỗi nhóm chạy song song, nhóm sau chờ nhóm trước."""
        units = []
        for index, group in enumerate(groups):
            for step in group:
                for name in STEP_UNITS.get(step, [step]):
                    units.append(_Unit(name=name, group=index))
        with self._lock:
            self._plans[project_id] = _Plan(units=units, duration=duration, adhoc=adhoc)

    def set_duration(self, project_id: str, duration: Optional[float]) -> None:
        with self._lock:
            plan = self._plans.get(project_id)
            if plan and duration:
                plan.duration = float(duration)

    def finish(self, project_id: str) -> None:
        with self._lock:
            plan = self._plans.get(project_id)
            if plan:
                plan.finished = True

    def forget(self, project_id: str) -> None:
        with self._lock:
            self._plans.pop(project_id, None)

    def _active(self, project_id: str) -> Optional[_Plan]:
        plan = self._plans.get(project_id)
        return plan if plan and not plan.finished else None

    @contextmanager
    def step(self, project_id: str, step: str, duration_fn: Callable[[], Optional[float]]):
        """Bọc một công đoạn công khai. Chạy lẻ (không qua run-all) thì tự lập kế hoạch riêng."""
        with self._lock:
            plan = self._active(project_id)
            names = STEP_UNITS.get(step, [step])
            own = plan is None or not any(u.name in names for u in plan.units)
        if own:
            try:
                duration = duration_fn()
            except Exception:
                duration = None
            self.plan(project_id, [[step]], duration, adhoc=True)
        try:
            yield
        except BaseException:
            with self._lock:
                plan = self._plans.get(project_id)
                for unit in plan.units if plan else []:
                    if unit.name in names and unit.state == "running":
                        unit.state = "failed"
            raise
        else:
            with self._lock:
                plan = self._active(project_id)
                for unit in plan.units if plan else []:
                    if unit.name not in names:
                        continue
                    if unit.state == "running":
                        self._complete(plan, unit)
                    elif unit.state == "pending":
                        # Không chạy lần này (ví dụ tắt dịch, tắt cảm xúc)
                        unit.state = "done"
        finally:
            if own:
                self.finish(project_id)

    def start(self, project_id: str, name: str) -> None:
        """Đơn vị `name` bắt đầu chạy. Đơn vị trước đó của cùng công đoạn coi như xong."""
        with self._lock:
            plan = self._active(project_id)
            if not plan:
                return
            step = _step_of(name)
            for unit in plan.units:
                if unit.state == "running" and unit.name != name and _step_of(unit.name) == step:
                    self._complete(plan, unit)
            unit = next((u for u in plan.units if u.name == name and u.state != "done"), None)
            if unit:
                unit.state = "running"
                unit.started_at = time.time()
                unit.fraction = 0.0

    def done(self, project_id: str, name: str) -> None:
        with self._lock:
            plan = self._active(project_id)
            unit = next(
                (u for u in plan.units if u.name == name and u.state == "running"), None
            ) if plan else None
            if unit:
                self._complete(plan, unit)

    def skip(self, project_id: str, name: str) -> None:
        """Đơn vị không cần chạy lần này (ví dụ không dịch, không phân tích cảm xúc)."""
        with self._lock:
            plan = self._active(project_id)
            for unit in plan.units if plan else []:
                if unit.name == name and unit.state == "pending":
                    unit.state = "done"

    def _complete(self, plan: _Plan, unit: _Unit) -> None:
        unit.state = "done"
        unit.finished_at = time.time()
        self._learn(unit.name, unit.finished_at - (unit.started_at or unit.finished_at), plan.duration)

    @contextmanager
    def unit(self, project_id: str, name: str):
        self.start(project_id, name)
        yield
        self.done(project_id, name)

    def fraction(self, project_id: str, name: str, value: float) -> None:
        """Tiến độ bên trong một đơn vị đang chạy (0..1)."""
        with self._lock:
            plan = self._active(project_id)
            if not plan:
                return
            for unit in plan.units:
                if unit.name == name and unit.state == "running":
                    unit.fraction = max(unit.fraction, min(1.0, max(0.0, float(value))))

    # ------------------------------------------------------------ kết quả
    def _remaining(self, unit: _Unit, duration: Optional[float], now: float) -> float:
        est = self.estimate(unit.name, duration)
        if unit.state in ("done", "failed"):
            return 0.0
        if unit.state == "pending":
            return est
        elapsed = now - (unit.started_at or now)
        if unit.fraction > 0.03 and elapsed > 2:
            projected = elapsed / unit.fraction
            # Càng chạy lâu càng tin tốc độ thực tế hơn số liệu lịch sử
            est = unit.fraction * projected + (1 - unit.fraction) * est
        left = est - elapsed
        if left <= 0:
            # Chậm hơn dự kiến: đừng báo "còn 0 giây" mãi, báo một khoảng ngắn hợp lý
            left = min(60.0, max(5.0, 0.15 * elapsed))
        return left

    def snapshot(self, project_id: str) -> Optional[Dict]:
        with self._lock:
            plan = self._plans.get(project_id)
            if not plan:
                return None
            now = time.time()
            groups: Dict[int, List[_Unit]] = {}
            for unit in plan.units:
                groups.setdefault(unit.group, []).append(unit)

            # Trong một nhóm các nhánh chạy song song: nhóm xong khi nhánh lâu nhất xong.
            # Một nhánh gồm các đơn vị của cùng một công đoạn, chạy nối tiếp nhau.
            remaining = 0.0
            for units in groups.values():
                branches: Dict[str, float] = {}
                for unit in units:
                    key = _step_of(unit.name)
                    branches[key] = branches.get(key, 0.0) + self._remaining(
                        unit, plan.duration, now
                    )
                remaining += max(branches.values()) if branches else 0.0

            failed = any(u.state == "failed" for u in plan.units)
            done = plan.finished or all(u.state == "done" for u in plan.units)
            if done or failed:
                remaining = 0.0
            elapsed = now - plan.started_at
            running = [u for u in plan.units if u.state == "running"]
            percent = 100 if done else int(100 * elapsed / max(elapsed + remaining, 1))

            return {
                "state": "failed" if failed else ("done" if done else "running"),
                "eta_seconds": int(round(remaining)),
                "elapsed_seconds": int(round(elapsed)),
                "percent": min(percent, 100 if done else 99),
                "finish_at": time.strftime(
                    "%H:%M:%S", time.localtime(now + remaining)
                ),
                "current": [LABELS.get(u.name, u.name) for u in running],
                "text": format_eta(remaining) if not (done or failed) else "",
                # Chưa biết độ dài video thì ước lượng chỉ là phỏng đoán thô
                "confident": plan.duration is not None,
                "units": [
                    {
                        "name": u.name,
                        "label": LABELS.get(u.name, u.name),
                        "state": u.state,
                        "fraction": round(u.fraction, 3),
                    }
                    for u in plan.units
                ],
            }


def _step_of(unit_name: str) -> str:
    return next(
        (step for step, names in STEP_UNITS.items() if unit_name in names), unit_name
    )


def format_eta(seconds: float) -> str:
    """'còn khoảng 10 phút' / 'còn khoảng 45 giây'."""
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"còn khoảng {max(5, (seconds + 4) // 5 * 5)} giây"
    minutes = (seconds + 30) // 60
    if minutes < 60:
        return f"còn khoảng {minutes} phút"
    return f"còn khoảng {minutes // 60} giờ {minutes % 60} phút"


tracker = ProgressTracker()
