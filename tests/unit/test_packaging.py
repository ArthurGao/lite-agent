"""What the published wheel must carry (design §15 S5.5): hosts will install the release wheel by
URL, so a missing file or an unresolvable dependency shows up only in a host."""

import subprocess
import tomllib
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("dist")
    # --offline: the build backend comes from uv's cache (`uv sync` put it there); the socket
    # guard in conftest.py cannot see a subprocess.
    subprocess.run(
        ["uv", "build", "--offline", "--quiet", "--wheel", "--out-dir", str(out)],
        cwd=ROOT,
        check=True,
        timeout=180,
    )
    return next(out.glob("lite_agent-*.whl"))


def test_the_wheel_carries_the_license_the_types_and_its_own_lite_model(wheel: Path) -> None:
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    info = f"lite_agent-{version}.dist-info"
    with zipfile.ZipFile(wheel) as zf:
        names = set(zf.namelist())
        metadata = zf.read(f"{info}/METADATA").decode()
        assert "Apache License" in zf.read(f"{info}/licenses/LICENSE").decode()
    assert "License-Expression: Apache-2.0" in metadata
    assert "lite_agent/py.typed" in names  # hosts type-check against it (mypy strict)
    assert not [n for n in names if n.startswith("tests/")]
    # the wheel installs on its own: lite-model is named by URL in the metadata (design §13)
    requires = [
        line for line in metadata.splitlines() if line.startswith("Requires-Dist: lite-model")
    ]
    assert requires == [
        "Requires-Dist: lite-model @ https://github.com/ArthurGao/lite-model/releases/download/"
        "v0.4.0/lite_model-0.4.0-py3-none-any.whl"
    ]
