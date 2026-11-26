"""Run from the repository root: python3 scripts/azure/test-backend-rollout.py."""

import os
import pathlib
import shutil
import subprocess
import tempfile

repo = pathlib.Path.cwd()
with tempfile.TemporaryDirectory() as t:
    p = pathlib.Path(t)
    b = p / "bin"
    b.mkdir()
    d = p / "deploy"
    d.mkdir()
    for n in ["compose.yaml"]:
        (d / n).write_text("services: {}\n")
    shutil.copy(
        repo / "scripts/azure/backend-image.override.yml",
        d / "backend-image.override.yml",
    )
    old = "ghcr.io/ase-group-10/intelligenturbanplatform-backend:sha-" + "a" * 40
    target = "ghcr.io/ase-group-10/intelligenturbanplatform-backend:sha-" + "b" * 40
    (b / "docker").write_text("""#!/usr/bin/env python3
import os, sys, pathlib

args = sys.argv[1:]
log = pathlib.Path(os.environ["TEST_LOG"])
log.open("a").write(" ".join(args) + "\\n")
state = pathlib.Path(os.environ["TEST_STATE"])
old = os.environ["OLD_IMAGE"]
target = os.environ["TARGET_IMAGE"]
if args[0] == "inspect":
    fmt = args[args.index("--format") + 1]
    cid = args[-1]
    if "Config.Image" in fmt:
        print(
            old
            if not state.exists()
            else (
                old
                if cid == "c12-worker" and state.read_text() == target
                else state.read_text()
            )
        )
    elif "State.Status" in fmt:
        print("running")
    elif "State.Restarting" in fmt:
        print("false")
    elif "RestartCount" in fmt:
        print("0")
elif "ps" in args:
    if "-q" in args:
        print(args[-1])
elif "up" in args:
    state.write_text(os.environ["BACKEND_IMAGE"])
elif "exec" in args:
    for queue in [
        "c07.city-state-updated",
        "c08.plan-ready",
        "c11.delivery-observations",
        "c12.delivery-recordings",
    ]:
        print(queue, 1)
""")
    (b / "curl").write_text(
        '#!/bin/sh\necho \'{"status":"ok","database":"connected"}\'\n'
    )
    for f in b.iterdir():
        f.chmod(0o755)
    env = dict(
        os.environ,
        PATH=str(b) + ":" + os.environ["PATH"],
        TEST_LOG=str(p / "log"),
        TEST_STATE=str(p / "state"),
        OLD_IMAGE=old,
        TARGET_IMAGE=target,
    )
    r = subprocess.run(
        [
            "bash",
            str(repo / "scripts/azure/deploy-backend.sh"),
            "sha-" + "b" * 40,
            "none",
            str(d),
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode != 0, (r.stdout, r.stderr)
    assert "ROLLBACK_STATUS=RECOVERED" in r.stderr, (r.stdout, r.stderr)
    assert "c12-worker" in r.stderr
    assert (d / ".last-deployed-image").read_text().strip() == old
    for n in ["c04-publisher", "c05-publisher", "c11-worker", "c12-worker"]:
        assert n in (d / "compose.override.yml").read_text()
    print("PASS: stale image rejected; rollback restores all eight services")
