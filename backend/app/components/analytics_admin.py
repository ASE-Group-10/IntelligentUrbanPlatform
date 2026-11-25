"""Trusted local operator commands; no public write/ownership assignment API."""

import argparse
import json
from pathlib import Path

from pydantic import UUID4, TypeAdapter

from app.components.c11_kpi_analytics.store import ObservationStore
from app.components.c12_simulation_replay.persistence import ReplayRepository
from app.components.c12_simulation_replay.recording import CaptureRecorder
from app.config import get_settings
from app.infrastructure.database import make_engine
from app.shared.contracts import UtcDatetime


def uuid4(value):
    return TypeAdapter(UUID4).validate_python(value)


def utc(value):
    return TypeAdapter(UtcDatetime).validate_python(value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    bind = commands.add_parser("bind-delivery")
    bind.add_argument("--provider-id", type=uuid4, required=True)
    bind.add_argument("--delivery-id", type=uuid4, required=True)
    group = bind.add_mutually_exclusive_group(required=True)
    group.add_argument("--live", action="store_true")
    group.add_argument("--run-id", type=uuid4)
    begin = commands.add_parser("begin-capture")
    begin.add_argument("--provider-id", type=uuid4, required=True)
    begin.add_argument("--run-id", type=uuid4, required=True)
    begin.add_argument("--start", type=utc, required=True)
    begin.add_argument("--configuration", type=Path, required=True)
    begin.add_argument("--synthetic", action="store_true")
    seal = commands.add_parser("seal-capture")
    seal.add_argument("--run-id", type=uuid4, required=True)
    seal.add_argument("--end", type=utc, required=True)
    archive = commands.add_parser("import-archive")
    archive.add_argument("file", type=Path)
    args = parser.parse_args()
    engine = make_engine(get_settings().database_url.get_secret_value())
    try:
        if args.operation == "bind-delivery":
            ObservationStore(engine).bind_delivery(
                args.provider_id,
                args.delivery_id,
                "live" if args.live else "simulation",
                args.run_id,
            )
        elif args.operation == "begin-capture":
            config = json.loads(args.configuration.read_text())
            CaptureRecorder(engine).begin(
                args.run_id, args.provider_id, args.start, config, args.synthetic
            )
        elif args.operation == "seal-capture":
            CaptureRecorder(engine).seal(args.run_id, args.end)
        else:
            if args.file.stat().st_size > 50_000_000:
                raise ValueError("archive exceeds 50 MB prototype limit")
            ReplayRepository(engine).import_archive(json.loads(args.file.read_text()))
        print("Completed. Only C11/C12-owned storage was changed.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
