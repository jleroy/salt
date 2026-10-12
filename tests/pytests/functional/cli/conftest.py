import base64
import hashlib
import zipfile

import pytest


@pytest.fixture(scope="package")
def salt_cloud_cli(salt_master_factory):
    """
    The ``salt-cloud`` CLI as a fixture against the running master
    """
    return salt_master_factory.salt_cloud_cli()


@pytest.fixture
def build_wheel(tmp_path):
    """Build disposable wheels without a build backend or network access."""

    def _build_wheel(name, version, requires=(), entry_points=None):
        """
        Hand-build a minimal, valid, pure-Python wheel using only the stdlib
        (no setuptools/build backend, no network access) so tests can install
        a disposable fake package via salt-pip.
        """
        module_name = name.replace("-", "_")
        dist_info = f"{module_name}-{version}.dist-info"
        wheel_path = tmp_path / f"{module_name}-{version}-py3-none-any.whl"

        metadata_lines = [
            "Metadata-Version: 2.1",
            f"Name: {name}",
            f"Version: {version}",
        ]
        for req in requires:
            metadata_lines.append(f"Requires-Dist: {req}")
        metadata = "\n".join(metadata_lines) + "\n"

        wheel_metadata = (
            "Wheel-Version: 1.0\n"
            "Generator: salt-test-suite\n"
            "Root-Is-Purelib: true\n"
            "Tag: py3-none-any\n"
        )

        files = {
            f"{module_name}/__init__.py": "# test fixture package\n",
            f"{dist_info}/METADATA": metadata,
            f"{dist_info}/WHEEL": wheel_metadata,
        }

        if entry_points:
            files[f"{dist_info}/entry_points.txt"] = entry_points

        record_lines = []
        for path, content in files.items():
            data = content.encode("utf-8")
            digest = "sha256=" + base64.urlsafe_b64encode(
                hashlib.sha256(data).digest()
            ).rstrip(b"=").decode("ascii")
            record_lines.append(f"{path},{digest},{len(data)}")
        record_lines.append(f"{dist_info}/RECORD,,")

        with zipfile.ZipFile(wheel_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for path, content in files.items():
                zf.writestr(path, content)
            zf.writestr(f"{dist_info}/RECORD", "\n".join(record_lines) + "\n")

        return wheel_path

    return _build_wheel
