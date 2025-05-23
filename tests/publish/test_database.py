# test get package from database
from pathlib import Path

import pytest
from stubber.publish.database import get_database
from stubber.publish.package import get_package_info

pytestmark = [pytest.mark.stubber]


@pytest.mark.parametrize(
    "package_name, version, present",
    [
        ("micropython-esp32-stubs", "1.18", True),
        ("micropython-stm32-stubs", "1.17", True),
        # ("micropython-doc-stubs", "1.18", True),
        ("micropython-doc-stubs", "1.10", False),
        ("pycopy-foo-stubs", "1.18", False),
    ],
)
def test_get_package_info(pytestconfig, package_name, version, present):
    # Cache database in memory?
    # TODO: use test database with known content
    testdata = pytestconfig.rootpath / "tests/publish/data"
    testdata = pytestconfig.rootpath / "repos/micropython-stubs/data"
    db_conn = get_database(testdata, production=True)
    try:
        pkg_info = get_package_info(db_conn, Path("foo"), pkg_name=package_name, mpy_version=version)
        if present:
            assert pkg_info
            assert pkg_info["name"] == package_name
            assert pkg_info["mpy_version"] == version
            assert len(pkg_info["path"]) > 0
            assert len(pkg_info["pkg_version"]) > 0
            assert len(pkg_info["hash"]) > 0
            assert len(pkg_info["description"]) > 0
            assert len(pkg_info["stub_sources"]) > 0
        else:
            assert pkg_info == None
    finally:
        db_conn.close()
            
