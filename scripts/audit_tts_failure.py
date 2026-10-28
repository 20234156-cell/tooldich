"""Read-only project/cache inspection and optional isolated real Edge probe."""
import argparse
import hashlib
import importlib.metadata
import io
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import requests
import soundfile as sf

from config.settings import settings
from src.pipeline.orchestrator import PipelineOrchestrator
from src.tts.cache import TTSCache
from src.tts.edge import EdgeTTSProvider
from src.tts.normalizer_vi import normalize_vietnamese_text


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('project_id')
    parser.add_argument('--probe', action='store_true')
    parser.add_argument('--voice', default='vi-VN-HoaiMyNeural')
    parser.add_argument('--rate', default='+0%')
    args = parser.parse_args()
    runner = PipelineOrchestrator()
    cues = runner.get_cues(args.project_id)
    snapshot = hashlib.sha256(json.dumps([c.model_dump() for c in cues], sort_keys=True).encode()).hexdigest()
    directory = Path('storage/audits') / ('tts_failure_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    directory.mkdir(parents=True)
    cache = TTSCache()
    matches = []
    for voice in ('vi-VN-HoaiMyNeural', 'vi-VN-NamMinhNeural'):
        for rate in ('+0%', '+30%'):
            valid, invalid, missing = [], [], []
            for cue in cues:
                payload = cache.get(cache.compute_key('edge', voice, normalize_vietnamese_text(cue.vi_text or ''), cue.tts_rate or rate))
                if payload is None:
                    missing.append(cue.index)
                    continue
                try:
                    samples, sr = sf.read(io.BytesIO(payload))
                    ok = sr > 0 and samples.size > 0 and np.isfinite(samples).all() and np.max(np.abs(samples)) >= 1e-5
                except Exception:
                    ok = False
                (valid if ok else invalid).append(cue.index)
            matches.append(dict(voice=voice, rate=rate, valid=valid, invalid=invalid, missing=missing))
    report = dict(project_id=args.project_id, cue_count=len(cues), cue_snapshot=snapshot,
                  cache=matches, edge_version=importlib.metadata.version('edge-tts'),
                  settings={key: getattr(settings, key) for key in ('TTS_CONCURRENCY', 'TTS_NETWORK_CONCURRENCY',
                            'TTS_MAX_RETRIES', 'TTS_REQUEST_TIMEOUT_SECONDS', 'TTS_TOTAL_DEADLINE_SECONDS')})
    try:
        detail = requests.get(f'http://127.0.0.1:8088/api/projects/{args.project_id}', timeout=10).json()
        report['task'] = detail.get('task')
    except Exception as exc:
        report['local_api_error'] = type(exc).__name__
    if args.probe:
        match = next(row for row in matches if row['voice'] == args.voice and row['rate'] == args.rate)
        missing_ids = set(match['missing'] + match['invalid'])
        cue = next((c for c in cues if c.index in missing_ids), cues[0])
        started = time.monotonic()
        probe = dict(cue_index=cue.index, voice=args.voice, rate=cue.tts_rate or args.rate)
        try:
            payload, _ = EdgeTTSProvider().synthesize(normalize_vietnamese_text(cue.vi_text or ''),
                voice=args.voice, rate=cue.tts_rate or args.rate)
            samples, sr = sf.read(io.BytesIO(payload))
            probe.update(passed=bool(samples.size and np.isfinite(samples).all() and np.max(np.abs(samples)) >= 1e-5),
                         sample_rate=sr, actual_duration=len(samples)/sr)
            (directory/'probe.audio').write_bytes(payload)
        except Exception as exc:
            chain = []
            while exc is not None:
                # Error classes suffice for diagnosis without exposing signed URLs.
                chain.append(type(exc).__name__)
                exc = exc.__cause__
            probe.update(passed=False, error_chain=chain)
        probe['elapsed_seconds'] = time.monotonic() - started
        report['probe'] = probe
    after = runner.get_cues(args.project_id)
    report['project_cues_unchanged'] = snapshot == hashlib.sha256(json.dumps([c.model_dump() for c in after], sort_keys=True).encode()).hexdigest()
    (directory/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(directory/'report.json')
    print(json.dumps({**{k:v for k,v in report.items() if k not in ('cache', 'task')},
                      'cache_counts': [{k:(len(v) if isinstance(v,list) else v) for k,v in row.items()} for row in matches]}))


if __name__ == '__main__':
    main()
