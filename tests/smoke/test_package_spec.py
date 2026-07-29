from pathlib import Path


def test_package_spec_declares_the_application_entry_and_required_local_resources() -> None:
    """Break caught: a packaged app launches without its template or site catalog."""
    spec = Path("packaging/quotation_app.spec").read_text(encoding="utf-8")

    assert "PROJECT_ROOT = Path(SPECPATH).parent\n" in spec
    assert '"quote_app" / "__main__.py"' in spec
    assert "resources/templates/quote_template.xlsx" in spec
    assert "resources/sites/catalog.json" in spec
    assert "collect_data_files('playwright')" in spec
    assert "console=False" in spec


def test_windows_portable_build_script_needs_no_administrator_elevation() -> None:
    """Break caught: Windows delivery requires an install or an elevated launcher."""
    script = Path("packaging/windows/build.ps1").read_text(encoding="utf-8")
    launcher = Path("packaging/windows/launch.cmd").read_text(encoding="utf-8")

    assert "packaging/quotation_app.spec" in script
    assert "Compress-Archive" in script
    assert "RunAs" not in script
    assert "福建移动铺货报价助手.exe" in launcher


def test_macos_build_script_runs_the_windowed_package_flow() -> None:
    """Break caught: Mac pilot build omits tests, the app bundle, or local signing."""
    script = Path("packaging/macos/build.sh").read_text(encoding="utf-8")

    assert "pytest -q" in script
    assert "PyInstaller" in script
    assert "quotation_app.spec" in script
    assert "codesign" in script
