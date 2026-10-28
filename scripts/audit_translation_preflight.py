"""Live AI shortening plus semantic review on synthetic cues, never user DB."""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    from config.settings import settings
    root = Path('storage/audits')/('translation_preflight_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    root = root.resolve()
    root.mkdir(parents=True)
    settings.DATABASE_URL = 'sqlite:///'+(root/'audit.db').as_posix()
    from src.database.connection import init_db
    init_db()
    from src.ocr.base import Cue
    from src.translation.preflight import structure_findings
    from src.translation.timing_shortener import TimingShortener, predicted_candidates
    cues = [Cue(index=11, start=0, end=1, text='请马上过来',
                vi_text='Tôi muốn nhờ anh hãy đến chỗ tôi ngay bây giờ nhé', translation_origin='ai')]
    constraints = predicted_candidates(cues)
    runner = TimingShortener(constraints)
    report = {'synthetic_fixture': True, 'input': [c.model_dump() for c in cues],
              'constraints': constraints, 'structure': structure_findings(cues),
              'audio_measured': False, 'user_projects_modified': False}
    started = time.monotonic()
    try:
        result = runner.shorten(cues, root)
        report.update(accepted=[c.model_dump() for c in result],
                      passed=bool(result), models=runner.used_models)
    except Exception as exc:
        report.update(passed=False, error_type=type(exc).__name__)
    report['seconds'] = time.monotonic()-started
    (root/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'report': str(root/'report.json'), 'passed': report['passed'],
                      'seconds': report['seconds']}, ensure_ascii=True))


if __name__ == '__main__':
    main()
