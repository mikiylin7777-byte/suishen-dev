"""Locate and prepare the reproducible integration SDK cache."""
import argparse
import fcntl
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def test_python():
    return shutil.which(os.environ.get("PORTABLE_TEST_PYTHON") or "python3") or sys.executable


def sdk_cache_dir():
    checkout = Path(__file__).resolve().parents[1]
    override = os.environ.get("PORTABLE_TEST_CACHE_DIR")
    if override:
        cache = Path(override).expanduser()
        if not cache.is_absolute():
            raise SystemExit("PORTABLE_TEST_CACHE_DIR must be absolute")
    else:
        home_cache = Path.home() / ("Library/Caches" if sys.platform == "darwin" else ".cache")
        base = Path(os.environ.get("XDG_CACHE_HOME") or home_cache).expanduser()
        if not base.is_absolute():
            base = home_cache
        cache = base / "suishen-dev" / "test"
    lockfile = checkout / "scripts/mcp-sdk.lock.txt"
    python = test_python()
    runtime = subprocess.check_output(
        [python, "-I", "-B", "-c",
         "import platform, sys, sysconfig; "
         "print(sys.version, sys.implementation.cache_tag, platform.machine(), "
         "sysconfig.get_platform(), sysconfig.get_config_var('SOABI'))"],
        timeout=10,
    )
    identity = hashlib.sha256()
    identity.update(os.fsencode(checkout) + b"\0")
    identity.update(os.fsencode(Path(python).resolve()) + b"\0")
    identity.update(runtime + b"\0")
    identity.update(lockfile.read_bytes())
    path = (cache / identity.hexdigest()[:24]).resolve()
    if path.is_relative_to(checkout):
        raise SystemExit("PORTABLE_TEST_CACHE_DIR must be outside the source checkout")
    return path


def refresh_wheels(python, lockfile, wheelhouse):
    with tempfile.TemporaryDirectory(prefix=".wheelhouse-", dir=wheelhouse.parent) as directory:
        stage = Path(directory) / "current"
        stage.mkdir()
        subprocess.run(
            [python, "-m", "pip", "download", "--disable-pip-version-check",
             "--only-binary=:all:", "-q", "-d", str(stage), "-r", str(lockfile)],
            check=True, stdout=sys.stderr,
        )
        previous = Path(directory) / "previous"
        if os.path.lexists(wheelhouse):
            wheelhouse.rename(previous)
        try:
            stage.rename(wheelhouse)
        except OSError:
            if os.path.lexists(previous):
                previous.rename(wheelhouse)
            raise


def install_sdk(python, lockfile, wheelhouse, sdk):
    if sdk.exists():
        shutil.rmtree(sdk)
    try:
        subprocess.run([python, "-m", "venv", str(sdk)], check=True, stdout=sys.stderr)
        subprocess.run(
            [str(sdk / "bin/python"), "-m", "pip", "install", "--isolated",
             "--disable-pip-version-check", "-q", "--no-index", "--no-cache-dir",
             "--find-links", str(wheelhouse), "-r", str(lockfile)],
            check=True, stdout=sys.stderr,
        )
    except BaseException:
        if sdk.exists():
            shutil.rmtree(sdk)
        raise


def prepare_sdk(cache):
    cache.mkdir(parents=True, exist_ok=True)
    lockfile = Path(__file__).with_name("mcp-sdk.lock.txt")
    sdk = cache / "integration-sdk"
    sdk_python = sdk / "bin/python"
    wheelhouse = cache / "integration-wheelhouse"
    python = test_python()
    with (cache / ".prepare.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        if sdk_python.is_file() and subprocess.run(
            [str(sdk_python), "-B", "-c", "import mcp"], capture_output=True
        ).returncode == 0:
            return sdk
        reused_wheels = any(wheelhouse.glob("*.whl"))
        if not reused_wheels:
            refresh_wheels(python, lockfile, wheelhouse)
        try:
            install_sdk(python, lockfile, wheelhouse, sdk)
        except subprocess.CalledProcessError:
            if not reused_wheels:
                raise
            refresh_wheels(python, lockfile, wheelhouse)
            install_sdk(python, lockfile, wheelhouse, sdk)
        subprocess.run([str(sdk_python), "-B", "-c", "import mcp"], check=True)
    return sdk


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare", action="store_true")
    args = parser.parse_args()
    directory = sdk_cache_dir()
    print(prepare_sdk(directory) if args.prepare else directory)
