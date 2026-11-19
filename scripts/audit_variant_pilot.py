"""Choose a diagnostic pilot from a completed voice manifest and measured WAVs."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import sys

import soundfile as sf

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.tts.variant_audit import compare_timing_modes
from src.tts.variant_blocks import choose_pilot, independent_blocks
from src.tts.variant_config import VariantTimingConfig
from src.tts.variant_solver import SpeechUnit, SpeechVariant


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def checked_file(root, relative, files):
    path = (root/relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f'Artifact outside project: {relative}')
    with path.open('rb') as stream:
        actual = hashlib.file_digest(stream,'sha256').hexdigest()
    if files.get(relative) != actual:
        raise ValueError(f'Manifest hash mismatch: {relative}')
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('project',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--config',type=Path)
    args = parser.parse_args()
    project = args.project.resolve()
    config = VariantTimingConfig.model_validate(read_json(args.config)) if args.config else VariantTimingConfig()
    manifest = read_json(project/'voiceover/manifest.json')
    files = manifest['files']
    timeline = read_json(checked_file(project,'voiceover/timeline.json',files))
    units = []
    sample_rate = None
    for row in timeline:
        path = checked_file(project,row['clip_path'],files)
        info = sf.info(path)
        if sample_rate is None:
            sample_rate = info.samplerate
        if info.samplerate != sample_rate or abs(info.duration-row['clip_duration']) > .002:
            raise ValueError(f'WAV and timeline mismatch: {row["cue_index"]}')
        start = math.ceil(row['start']*sample_rate)
        end = math.floor(row['end']*sample_rate)
        units.append(SpeechUnit(row['cue_index'],start,start,end,(SpeechVariant('existing',info.frames,0),)))
    if not units:
        raise ValueError('Empty speech timeline')
    pause = math.ceil(config.pause_seconds*sample_rate)
    # A voice manifest establishes WAV identity, not silence in the source.
    blocks = independent_blocks(units,pause,confirmed_gaps=[])
    pilot,score = choose_pilot(blocks,config.pilot_units)
    result = dict(scope='existing measured WAV diagnostic; borrowing not authorized',
                  measured_units=len(units),sample_rate=sample_rate, config=config.model_dump(),
                  block_count=len(blocks),pilot_ids=[unit.cue_id for unit in pilot],
                  own_window_deficit_seconds=score/sample_rate,
                  timing_modes=compare_timing_modes(pilot,pause,sample_rate,config))
    with args.output.open('x',encoding='utf-8') as stream:
        json.dump(result,stream,ensure_ascii=False,indent=2)


if __name__ == '__main__':
    main()
