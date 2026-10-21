"""Scripts de déploiement et de rollback, testés avec un faux `docker` (pas de démon Docker requis)."""
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from acsi_agent.agent import Runtime
from acsi_agent.api import create_app

ROOT = Path(__file__).resolve().parents[1]

SHIM = r'''#!/usr/bin/env bash
echo "docker $* [IMAGE_TAG=${IMAGE_TAG:-}]" >> "$FAKE_DOCKER_DIR/calls.log"
case " $* " in
  *" up "*) echo "${IMAGE_TAG}" > "$FAKE_DOCKER_DIR/up_tag" ;;
  *" ps -q api "*) echo "cid123" ;;
  " inspect "*)
    tag="$(cat "$FAKE_DOCKER_DIR/up_tag" 2>/dev/null || echo none)"
    if [[ " ${FAKE_BAD_TAGS:-} " == *" $tag "* ]]; then echo unhealthy; else echo healthy; fi ;;
  " image inspect "*)
    t="${3#acsi-agent:}"; [[ " ${FAKE_IMAGES:-} " == *" $t "* ]] || exit 1 ;;
esac
exit 0
'''


class Env:
    def __init__(self, tmp: Path):
        self.repo = tmp / "repo"
        self.fake = tmp / "fake"
        self.fake.mkdir()
        (self.repo / "scripts").mkdir(parents=True)
        for f in ("lib.sh", "deploy.sh", "rollback.sh"):
            shutil.copy(ROOT / "scripts" / f, self.repo / "scripts" / f)
        shutil.copy(ROOT / "compose.yaml", self.repo / "compose.yaml")
        (self.repo / ".gitignore").write_text(".deploy/\n")
        bindir = tmp / "bin"
        bindir.mkdir()
        (bindir / "docker").write_text(SHIM)
        (bindir / "docker").chmod(0o755)
        self.env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "FAKE_DOCKER_DIR": str(self.fake),
                    "DEPLOY_GATE_CMD": "true", "HEALTH_TIMEOUT_S": "0", "HEALTH_POLL_S": "0", "FAKE_BAD_TAGS": "", "FAKE_IMAGES": ""}
        env_git = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        self.env.update(env_git)
        self.git("init", "-q", "-b", "main")
        self.n = 0

    def git(self, *a):
        return subprocess.run(["git", *a], cwd=self.repo, env=self.env, check=True, capture_output=True, text=True).stdout.strip()

    def commit(self) -> str:
        self.n += 1
        (self.repo / "version.txt").write_text(str(self.n))
        self.git("add", "-A")
        self.git("commit", "-q", "-m", f"v{self.n}")
        return self.git("rev-parse", "--short=12", "HEAD")

    def run(self, script, *args, **env):
        return subprocess.run(["bash", str(self.repo / "scripts" / script), *args], cwd=self.repo, env={**self.env, **env},
                              capture_output=True, text=True)

    def state(self, name):
        p = self.repo / ".deploy" / name
        return p.read_text() if p.exists() else None

    def calls(self):
        p = self.fake / "calls.log"
        return p.read_text().splitlines() if p.exists() else []

    def ups(self):
        return [c.split("IMAGE_TAG=")[1].rstrip("]") for c in self.calls() if " up " in c]


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def test_deploy_records_current_and_previous(env):
    v1 = env.commit()
    assert env.run("deploy.sh").returncode == 0 and env.state("current") == v1 and env.state("previous") is None
    v2 = env.commit()
    assert env.run("deploy.sh").returncode == 0
    assert env.state("current") == v2 and env.state("previous") == v1
    assert any("build api" in c and f"IMAGE_TAG={v2}" in c for c in env.calls())


def test_failed_health_triggers_automatic_rollback(env):
    v1 = env.commit()
    env.run("deploy.sh")
    v2 = env.commit()
    r = env.run("deploy.sh", FAKE_BAD_TAGS=v2)
    assert r.returncode == 2
    assert env.ups() == [v1, v2, v1]            # v2 lancé, jugé malsain, v1 relancé
    assert env.state("current") == v1            # l'état ne bascule jamais sur une version malsaine
    assert "retour arrière automatique" in r.stderr


def test_failed_health_without_previous_stops_the_service(env):
    v1 = env.commit()
    r = env.run("deploy.sh", FAKE_BAD_TAGS=v1)
    assert r.returncode == 2 and env.state("current") is None
    assert any(" stop api" in c for c in env.calls())


def test_rollback_that_also_fails_is_reported(env):
    v1 = env.commit()
    env.run("deploy.sh")
    v2 = env.commit()
    assert env.run("deploy.sh", FAKE_BAD_TAGS=f"{v1} {v2}").returncode == 3


def test_failed_quality_gate_blocks_before_any_docker_call(env):
    env.commit()
    r = env.run("deploy.sh", DEPLOY_GATE_CMD="false")
    assert r.returncode == 1 and env.calls() == [] and env.state("current") is None


def test_dirty_tree_is_refused(env):
    env.commit()
    (env.repo / "version.txt").write_text("modifié")
    r = env.run("deploy.sh")
    assert r.returncode == 1 and "arbre git sale" in r.stderr and env.calls() == []


def test_manual_rollback_to_previous(env):
    v1 = env.commit()
    env.run("deploy.sh")
    v2 = env.commit()
    env.run("deploy.sh")
    r = env.run("rollback.sh", FAKE_IMAGES=f"{v1} {v2}")
    assert r.returncode == 0 and env.state("current") == v1 and env.state("previous") == v2
    assert env.ups()[-1] == v1


def test_rollback_refuses_unknown_image_and_changes_nothing(env):
    v1 = env.commit()
    env.run("deploy.sh")
    v2 = env.commit()
    env.run("deploy.sh")
    before = env.ups()
    r = env.run("rollback.sh", FAKE_IMAGES="")
    assert r.returncode == 1 and env.ups() == before and env.state("current") == v2


@pytest.mark.parametrize("tag", ["x; rm -rf /", "a b", "../x", "$(id)"])
def test_rollback_rejects_malformed_tags(env, tag):
    env.commit()
    assert env.run("rollback.sh", tag).returncode == 1 and env.ups() == []


def test_rollback_with_explicit_tag(env):
    v1 = env.commit()
    env.run("deploy.sh")
    v2 = env.commit()
    env.run("deploy.sh")
    assert env.run("rollback.sh", v1, FAKE_IMAGES=v1).returncode == 0 and env.state("current") == v1


# ----------------------------------------------------------------- smoke
def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_smoke_script_against_a_live_server(tmp_path):
    keys = {"k-smoke": {"id": "smoke", "role": "viewer"}}
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(Runtime(tmp_path / "j"), keys), host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    try:
        ok = subprocess.run([sys.executable, str(ROOT / "scripts" / "smoke.py"), f"http://127.0.0.1:{port}"],
                            env={**os.environ, "ACSI_SMOKE_KEY": "k-smoke"}, capture_output=True, text=True)
        assert ok.returncode == 0, ok.stdout + ok.stderr
        assert ok.stdout.count("✓") == 4
        bad = subprocess.run([sys.executable, str(ROOT / "scripts" / "smoke.py"), f"http://127.0.0.1:{port}"],
                             env={**os.environ, "ACSI_SMOKE_KEY": "mauvaise-cle"}, capture_output=True, text=True)
        assert bad.returncode == 1  # une instance qui n'authentifie pas correctement fait échouer le déploiement
    finally:
        server.should_exit = True
        th.join(timeout=5)
