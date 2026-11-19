"""Only verified completed encodes may resume at audio QC."""
import hashlib
import json
import os
from pathlib import Path


def digest(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def identity(video, voice, roi, cues, cfg, source_cues=None, source_platform=None, split_points=None):
    data = dict(version='encoded-qc-resume-v12-audio-first', video=digest(video), source_platform=source_platform,
                voice=digest(voice) if voice and voice.is_file() else None,
                roi=roi.model_dump(), cues=[c.model_dump() for c in cues], settings=cfg.model_dump(),
                source_cues=[c.model_dump() for c in source_cues] if source_cues is not None else None)
    if split_points:
        data['split_points'] = [round(float(t), 3) for t in split_points]
    if cfg.brand_position != "off" or cfg.corner_logo == "npzzsub":
        data["brand_shape"] = "circle-v1"
        data["brand_asset"] = digest(Path(__file__).resolve().parents[2] / "assets/npzzsub-wordmark.png")
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def read(path, encoded, signature):
    try:
        record = json.loads(path.read_text(encoding='utf-8'))
        if record['signature'] == signature and record['encoded_sha256'] == digest(encoded):
            return record['context']
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def write(path, encoded, signature, context, *, encoded_sha256=None):
    digest_value = encoded_sha256 or digest(encoded)
    temporary = path.with_suffix('.writing.json')
    temporary.write_text(json.dumps(dict(signature=signature, encoded_sha256=digest_value, context=context)), encoding='utf-8')
    os.replace(temporary, path)
    return digest_value
