"""Exercise the real installer in a temporary Venus filesystem layout."""

import os
import re
import shutil
import subprocess
from pathlib import Path


def test_setuphelper_in_place_update_preserves_config_and_boot(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    name = repo.name
    root = tmp_path / "venus"
    install = root / "data" / name
    install.mkdir(parents=True)
    (root / "service").mkdir()
    (root / "proc").mkdir()
    (root / "data" / "rc.local").write_text("#!/bin/sh\nexit 0\n")
    script = (repo / "update.sh").read_text()
    runtime = re.search(r'^RUNTIME_ITEMS="([^"]+)"', script, re.MULTILINE).group(1).split()
    for item in [*runtime, "services"]:
        source = repo / item
        if not source.exists():
            continue
        if source.is_dir():
            shutil.copytree(source, install / item)
        else:
            shutil.copy2(source, install / item)
    local = "config.json" if name == "dbus-emporia-vue" else "local_config.py"
    (install / local).write_text("device-local configuration must survive\n")

    # Redirect only literal device roots. Variables such as INSTALL_DIR/service
    # remain unchanged, so copies, deletes and symlinks exercise the real logic.
    script = script.replace("/data/", str(root / "data") + "/")
    script = re.sub(r"""(?<=[\s"'])/service(?=/|\b)""", str(root / "service"), script)
    script = script.replace("/opt/victronenergy/", str(root / "opt") + "/")
    script = script.replace("/proc/", str(root / "proc") + "/")
    (install / "update.sh").write_text(script)
    commands = tmp_path / "bin"
    commands.mkdir()
    for command in ("svc", "sleep", "python3"):
        stub = commands / command
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
    # Venus uses GNU/BusyBox sed -i; macOS ships BSD sed.
    if shutil.which("gsed"):
        (commands / "sed").symlink_to(shutil.which("gsed"))
    environment = dict(os.environ, PATH=str(commands) + os.pathsep + os.environ["PATH"])

    # Invoke exactly as SetupHelper does, from inside the installed tree.
    for _ in range(2):
        result = subprocess.run(
            ["sh", str(install / "update.sh"), str(install)],
            cwd=install,
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert (install / local).read_text() == "device-local configuration must survive\n"
        for item in runtime:
            if (repo / item).exists():
                assert (install / item).exists(), item
        link = root / "service" / name
        assert link.is_symlink()
        assert link.resolve() == install / "service" / name
        assert (link / "run").exists()
        boot = (root / "data" / "rc.local").read_text()
        marker = f"# === {name} service persistence ==="
        assert boot.count(marker) == 1
        assert boot.index(marker) < boot.index("exit 0")
        # Installed releases keep their unit only under service/, so exercise
        # that layout on the second in-place update as well.
        shutil.rmtree(install / "services", ignore_errors=True)


def test_missing_dependencies_fail_before_install_changes(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    commands = tmp_path / "bin"
    commands.mkdir()
    interpreter = commands / "python3"
    interpreter.write_text("#!/bin/sh\nexit 42\n")
    interpreter.chmod(0o755)
    script = (repo / "update.sh").read_text()
    # Exercise the preflight alone: production filesystem operations start
    # after this marker and must never be reached if an import fails.
    script = script.split("# Stage the release before stopping anything.")[0]
    script += '\nprintf "unexpected continuation"\n'
    result = subprocess.run(
        ["sh", "-c", script],
        cwd=tmp_path,
        env=dict(os.environ, PATH=str(commands) + os.pathsep + os.environ["PATH"]),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 42
    assert "unexpected continuation" not in result.stdout
