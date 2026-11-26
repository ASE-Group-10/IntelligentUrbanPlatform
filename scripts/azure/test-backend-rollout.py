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
import os, sys, pathlib, json

args = sys.argv[1:]
log = pathlib.Path(os.environ["TEST_LOG"])
log.open("a").write(" ".join(args) + "\\n")
state = pathlib.Path(os.environ["TEST_STATE"])
old = os.environ["OLD_IMAGE"]
target = os.environ["TARGET_IMAGE"]
names = ["api", "c04-publisher", "c05-publisher", "c06-publisher",
         "c07-worker", "c08-worker", "c11-worker", "c12-worker"]
images = json.loads(state.read_text()) if state.exists() else dict.fromkeys(names, old)
if args[0] == "inspect":
    fmt = args[args.index("--format") + 1]
    cid = args[-1]
    if "Config.Image" in fmt:
        print(images[cid])
    elif "State.Status" in fmt:
        print("running")
    elif "State.Restarting" in fmt:
        print("false")
    elif "RestartCount" in fmt:
        print("0")
elif "ps" in args:
    if "-q" in args:
        print(args[-1])
elif "config" in args:
    resolved = dict.fromkeys(names, os.environ["BACKEND_IMAGE"])
    if os.environ.get("TEST_STALE_OVERRIDE"):
        resolved["c12-worker"] = old
    print(json.dumps({"services": {n: {"image": v} for n, v in resolved.items()}}))
elif "up" in args:
    for name in args[args.index("up") + 1:]:
        if name in names:
            image = os.environ["BACKEND_IMAGE"]
            images[name] = old if name == "c12-worker" and image == target else image
    state.write_text(json.dumps(images))
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

    # A legacy override must abort before any service is recreated.
    (p / "log").write_text("")
    stale = subprocess.run(
        r.args, env=dict(env, TEST_STALE_OVERRIDE="1"), capture_output=True, text=True
    )
    assert stale.returncode != 0
    assert "Resolved image for c12-worker" in stale.stderr
    assert " up " not in (p / "log").read_text()

    # Mutation: API-only rollback must leave workers at target and fail verification.
    script = (repo / "scripts/azure/deploy-backend.sh").read_text()
    rollback = 'if ! BACKEND_IMAGE="${PREVIOUS_IMAGE}" docker compose'
    before, after = script.split(rollback, 1)
    after = after.replace('up -d "${SERVICES[@]}"', "up -d api", 1)
    mutated = p / "api-only-rollback.sh"
    mutated.write_text(before + rollback + after)
    (p / "state").unlink()
    args = list(r.args)
    args[1] = str(mutated)
    broken = subprocess.run(args, env=env, capture_output=True, text=True)
    assert broken.returncode != 0
    assert "ROLLBACK_STATUS=FAILED" in broken.stderr
    assert "ROLLBACK_STATUS=RECOVERED" not in broken.stderr
    print("PASS: legacy override aborts; API-only rollback mutation is detected")
