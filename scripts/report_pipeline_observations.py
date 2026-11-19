"""Export the selective evidence index without invoking providers or changing cues."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.database.connection import SessionLocal
from src.pipeline.observations import summarize


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--since', type=float, default=0, help='UTC epoch, inclusive')
    parser.add_argument('--until', type=float, help='UTC epoch, exclusive')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    with SessionLocal() as db:
        report = summarize(db, args.since, args.until)
    # Exclusive creation avoids overwriting evidence or arbitrary existing files.
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
    print(f"{report['observation_count']} observations; {report['affected_projects']} affected projects")


if __name__ == '__main__':
    main()
