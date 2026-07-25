from __future__ import annotations

import hashlib
import importlib
import builtins
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from PIL import Image, ImageDraw, ImageFilter
import pytest

from quote_app.evidence.geometry import (
    CssRect,
    DisplayBounds,
    GeometryError,
    ViewportGeometry,
)
from quote_app.evidence.models import EvidenceState
from quote_app.evidence.platform import (
    BrowserWindowIdentity,
    CaptureEnvironment,
    CaptureEnvironmentUnavailable,
    CaptureGeometrySnapshot,
    CaptureRequest,
    EvidenceCaptureError,
    EvidenceCapturePipeline,
    NativeScreenPermissionDenied,
    SystemUIProof,
    make_capture_error,
)
from quote_app.evidence.quality import (
    CaptureQualityError,
    assess_capture_quality,
    wait_for_stable_hash,
)
from quote_app.tasks.retry import (
    NonRetryableTechnicalError,
    RetryableTechnicalError,
    classify_attempt_error,
)

NOW = datetime(2026, 7, 25, 10, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("code", "expected_type"),
    [
        ("CAPTURE_PERMISSION", NonRetryableTechnicalError),
        ("CAPTURE_ENVIRONMENT", NonRetryableTechnicalError),
        ("CAPTURE_BLANK", RetryableTechnicalError),
        ("CAPTURE_UNREADABLE", RetryableTechnicalError),
        ("CAPTURE_OBSCURED", RetryableTechnicalError),
        ("CAPTURE_UNSTABLE", RetryableTechnicalError),
        ("CAPTURE_GEOMETRY", RetryableTechnicalError),
        ("CAPTURE_FAILED", RetryableTechnicalError),
    ],
)
def test_capture_codes_have_explicit_retry_semantics(
    code: str,
    expected_type: type[object],
) -> None:
    error = make_capture_error(code, "安全错误说明")

    assert isinstance(error, EvidenceCaptureError)
    assert isinstance(error, expected_type)
    assert classify_attempt_error(error) is error


class _Probe:
    def __init__(self, *values: str) -> None:
        self.values = list(values)
        self.index = 0

    def semantic_hash(self) -> str:
        value = self.values[min(self.index, len(self.values) - 1)]
        self.index += 1
        return value


def _readable_image(width: int = 400, height: int = 300) -> Image.Image:
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, width - 1, 50), fill=(25, 30, 40))
    content_top = min(80, max(55, height // 2))
    content_bottom = max(content_top, height - 10)
    for x in range(0, width, 20):
        color = (30, 90, 180) if (x // 20) % 2 else (220, 120, 30)
        draw.rectangle(
            (x, content_top, min(x + 12, width - 1), content_bottom),
            fill=color,
        )
    draw.text((20, 60), "https://example.test product 3999", fill="black")
    return image


@pytest.mark.parametrize(
    "image",
    [
        Image.new("RGB", (200, 100), (255, 255, 255)),
        Image.new("RGB", (200, 100), (254, 254, 254)),
        Image.new("RGB", (200, 100), (0, 0, 0)),
        Image.new("RGB", (200, 100), (2, 2, 2)),
    ],
)
def test_blank_near_white_and_near_black_images_are_rejected(
    image: Image.Image,
) -> None:
    with pytest.raises(CaptureQualityError) as captured:
        assess_capture_quality(image, expected_size=(200, 100))

    assert captured.value.code == "CAPTURE_BLANK"


def test_blurred_low_contrast_capture_is_unreadable() -> None:
    image = Image.new("L", (200, 100), 128)
    draw = ImageDraw.Draw(image)
    for x in range(0, 200, 20):
        draw.rectangle((x, 0, x + 9, 99), fill=124)
        draw.rectangle((x + 10, 0, x + 19, 99), fill=132)
    image = image.filter(ImageFilter.GaussianBlur(radius=8)).convert("RGB")

    with pytest.raises(CaptureQualityError) as captured:
        assess_capture_quality(image, expected_size=(200, 100))

    assert captured.value.code == "CAPTURE_UNREADABLE"


def test_low_entropy_nonuniform_capture_is_unreadable() -> None:
    image = Image.new("RGB", (200, 100), "white")
    ImageDraw.Draw(image).rectangle((0, 0, 9, 9), fill="black")

    with pytest.raises(CaptureQualityError) as captured:
        assess_capture_quality(
            image,
            expected_size=(200, 100),
            minimum_contrast=1,
            minimum_sharpness=0,
        )

    assert captured.value.code == "CAPTURE_UNREADABLE"


def test_high_contrast_sharp_capture_passes_measurable_quality() -> None:
    metrics = assess_capture_quality(
        _readable_image(200, 100),
        expected_size=(200, 100),
    )

    assert metrics.contrast > 10
    assert metrics.entropy > 1
    assert metrics.sharpness > 1


def test_capture_dimensions_must_match_primary_display() -> None:
    with pytest.raises(CaptureQualityError) as captured:
        assess_capture_quality(
            _readable_image(200, 100),
            expected_size=(300, 100),
        )

    assert captured.value.code == "CAPTURE_GEOMETRY"


def test_page_hash_may_change_once_then_stabilize() -> None:
    probe = _Probe("loading", "ready", "ready")

    assert wait_for_stable_hash(
        probe,
        minimum_interval_seconds=0.001,
        max_checks=3,
        sleeper=lambda _seconds: None,
    ) == "ready"


def test_unstable_page_hash_is_rejected() -> None:
    probe = _Probe("one", "two", "three")

    with pytest.raises(CaptureQualityError) as captured:
        wait_for_stable_hash(
            probe,
            minimum_interval_seconds=0.001,
            max_checks=3,
            sleeper=lambda _seconds: None,
        )

    assert captured.value.code == "CAPTURE_UNSTABLE"


class _Environment:
    def __init__(
        self,
        tmp_path: Path,
        *,
        permission: bool = True,
        system_ui_visible: bool = True,
        foreground: tuple[BrowserWindowIdentity, ...] | None = None,
        image: Image.Image | None = None,
        browser_bounds: DisplayBounds | None = None,
        capture_error: Exception | None = None,
        prepare_error: Exception | None = None,
        snapshot: CaptureGeometrySnapshot | object | None = None,
        snapshot_error: Exception | None = None,
        proof: SystemUIProof | object | None = None,
    ) -> None:
        self.tmp_path = tmp_path
        self.permission = permission
        self.ui_visible = system_ui_visible
        self.expected = BrowserWindowIdentity("test", 42, "window-7")
        self.foreground = list(foreground or (self.expected, self.expected))
        self.image = image or _readable_image()
        self.bounds = browser_bounds or DisplayBounds(0, 0, 400, 260)
        self.capture_error = capture_error
        self.prepare_error = prepare_error
        self.snapshot_error = snapshot_error
        self.captured_paths: list[Path] = []
        self.prepared: list[BrowserWindowIdentity] = []
        self.snapshot = snapshot or CaptureGeometrySnapshot(
            expected_window=self.expected,
            display_physical_bounds=DisplayBounds(0, 0, 400, 300),
            browser_physical_bounds=self.bounds,
            viewport_geometry=ViewportGeometry(0, 60, 1, 1, 0, 0),
            viewport_width_css=400,
            viewport_height_css=200,
            maximized=True,
            fullscreen=False,
            device_pixel_ratio=1,
            sample_id="test-sample",
        )
        self.proof = proof or SystemUIProof(
            expected_window=self.expected,
            system_bar_visible=system_ui_visible,
            date_time_visible=system_ui_visible,
            intersects_primary_display=system_ui_visible,
            authoritative=True,
            source="test-proof",
        )

    def prepare_browser(self, expected: BrowserWindowIdentity) -> None:
        self.prepared.append(expected)
        if self.prepare_error is not None:
            raise self.prepare_error

    def screen_capture_permission(self) -> bool:
        return self.permission

    def foreground_window(self) -> BrowserWindowIdentity:
        if len(self.foreground) > 1:
            return self.foreground.pop(0)
        return self.foreground[0]

    def geometry_snapshot(
        self,
        expected: BrowserWindowIdentity,
    ) -> CaptureGeometrySnapshot:
        assert expected == self.expected
        if self.snapshot_error is not None:
            raise self.snapshot_error
        return self.snapshot  # type: ignore[return-value]

    def system_ui_proof(
        self,
        snapshot: CaptureGeometrySnapshot,
    ) -> SystemUIProof:
        assert snapshot is self.snapshot
        return self.proof  # type: ignore[return-value]

    def capture_primary_display(
        self,
        destination: Path,
        snapshot: CaptureGeometrySnapshot,
    ) -> None:
        assert snapshot is self.snapshot
        self.captured_paths.append(destination)
        if self.capture_error is not None:
            destination.write_bytes(b"partial capture")
            raise self.capture_error
        self.image.save(destination, format="PNG", compress_level=1)


def _request(
    destination: Path,
    environment: _Environment,
    *,
    state: EvidenceState = EvidenceState.NORMAL,
    rectangles: tuple[CssRect, ...] = (),
    roles: tuple[str, ...] = (),
    probe: _Probe | None = None,
) -> CaptureRequest:
    return CaptureRequest(
        destination=destination,
        state=state,
        css_rectangles=rectangles,
        expected_roles=roles,
        expected_window=environment.expected,
        stability_probe=probe or _Probe("ready", "ready"),
        minimum_stability_interval_seconds=0.001,
    )


def _pipeline(environment: _Environment) -> EvidenceCapturePipeline:
    return EvidenceCapturePipeline(
        environment,
        sleeper=lambda _seconds: None,
        now=lambda: NOW,
    )


def test_missing_screen_recording_permission_has_stable_retry_code(
    tmp_path: Path,
) -> None:
    environment = _Environment(tmp_path, permission=False)
    destination = tmp_path / "formal.png"

    with pytest.raises(EvidenceCaptureError) as captured:
        _pipeline(environment).capture(_request(destination, environment))

    assert captured.value.code == "CAPTURE_PERMISSION"
    assert classify_attempt_error(captured.value) is captured.value
    assert isinstance(captured.value, NonRetryableTechnicalError)
    assert environment.prepared == []
    assert environment.captured_paths == []
    assert not destination.exists()


@pytest.mark.parametrize("mismatch_position", ["before", "after"])
def test_foreground_window_mismatch_before_or_after_capture_is_obscured(
    tmp_path: Path,
    mismatch_position: str,
) -> None:
    expected = BrowserWindowIdentity("test", 42, "window-7")
    other = BrowserWindowIdentity("test", 99, "window-9")
    foreground = (
        (other, expected)
        if mismatch_position == "before"
        else (expected, other)
    )
    environment = _Environment(tmp_path, foreground=foreground)
    destination = tmp_path / f"{mismatch_position}.png"

    with pytest.raises(EvidenceCaptureError) as captured:
        _pipeline(environment).capture(_request(destination, environment))

    assert captured.value.code == "CAPTURE_OBSCURED"
    assert isinstance(captured.value, RetryableTechnicalError)
    assert not destination.exists()


def test_hidden_taskbar_or_dock_is_environment_failure(tmp_path: Path) -> None:
    environment = _Environment(tmp_path, system_ui_visible=False)

    with pytest.raises(EvidenceCaptureError) as captured:
        _pipeline(environment).capture(
            _request(tmp_path / "formal.png", environment)
        )

    assert captured.value.code == "CAPTURE_ENVIRONMENT"
    assert isinstance(captured.value, NonRetryableTechnicalError)


def test_nonmaximized_or_outside_browser_bounds_are_environment_failure(
    tmp_path: Path,
) -> None:
    environment = _Environment(
        tmp_path,
        browser_bounds=DisplayBounds(50, 50, 80, 70),
    )

    with pytest.raises(EvidenceCaptureError) as captured:
        _pipeline(environment).capture(
            _request(tmp_path / "formal.png", environment)
        )

    assert captured.value.code == "CAPTURE_ENVIRONMENT"
    assert isinstance(captured.value, NonRetryableTechnicalError)
    assert environment.captured_paths == []


def test_transient_browser_prepare_failure_is_retryable_obscured(
    tmp_path: Path,
) -> None:
    environment = _Environment(
        tmp_path,
        prepare_error=RuntimeError("SetForegroundWindow temporarily failed"),
    )

    with pytest.raises(EvidenceCaptureError) as captured:
        _pipeline(environment).capture(
            _request(tmp_path / "formal.png", environment)
        )

    assert captured.value.code == "CAPTURE_OBSCURED"
    assert isinstance(captured.value, RetryableTechnicalError)
    assert environment.captured_paths == []


def test_missing_native_prepare_capability_is_nonretryable_environment(
    tmp_path: Path,
) -> None:
    environment = _Environment(
        tmp_path,
        prepare_error=CaptureEnvironmentUnavailable("native API missing"),
    )

    with pytest.raises(EvidenceCaptureError) as captured:
        _pipeline(environment).capture(
            _request(tmp_path / "formal.png", environment)
        )

    assert captured.value.code == "CAPTURE_ENVIRONMENT"
    assert isinstance(captured.value, NonRetryableTechnicalError)


def test_small_native_maximized_window_border_outside_display_is_tolerated(
    tmp_path: Path,
) -> None:
    environment = _Environment(
        tmp_path,
        browser_bounds=DisplayBounds(-8, -8, 416, 276),
    )

    record = _pipeline(environment).capture(
        _request(tmp_path / "formal.png", environment)
    )

    assert record.validation_code == "CAPTURE_OK"


def test_success_uses_temporary_png_then_atomically_publishes_original(
    tmp_path: Path,
) -> None:
    environment = _Environment(tmp_path)
    destination = tmp_path / "formal.png"

    record = _pipeline(environment).capture(
        _request(destination, environment)
    )

    assert record.path == destination.resolve()
    assert record.validation_code == "CAPTURE_OK"
    assert record.pixel_width == 400
    assert record.pixel_height == 300
    assert record.sha256 == hashlib.sha256(destination.read_bytes()).hexdigest()
    assert len(environment.captured_paths) == 1
    assert environment.captured_paths[0] != destination
    assert environment.captured_paths[0].parent == destination.parent
    assert not environment.captured_paths[0].exists()


def test_quality_failure_never_publishes_or_replaces_formal_destination(
    tmp_path: Path,
) -> None:
    environment = _Environment(
        tmp_path,
        image=Image.new("RGB", (400, 300), "white"),
    )
    destination = tmp_path / "formal.png"
    destination.write_bytes(b"previous formal evidence")

    with pytest.raises(EvidenceCaptureError) as captured:
        _pipeline(environment).capture(_request(destination, environment))

    assert captured.value.code == "CAPTURE_BLANK"
    assert isinstance(captured.value, RetryableTechnicalError)
    assert destination.read_bytes() == b"previous formal evidence"
    assert all(not path.exists() for path in environment.captured_paths)


def test_capture_backend_partial_write_failure_keeps_old_formal_and_cleans_temp(
    tmp_path: Path,
) -> None:
    environment = _Environment(
        tmp_path,
        capture_error=OSError("screen capture failed"),
    )
    destination = tmp_path / "formal.png"
    destination.write_bytes(b"previous formal evidence")

    with pytest.raises(EvidenceCaptureError) as captured:
        _pipeline(environment).capture(_request(destination, environment))

    assert captured.value.code == "CAPTURE_FAILED"
    assert isinstance(captured.value, RetryableTechnicalError)
    assert destination.read_bytes() == b"previous formal evidence"
    assert all(not path.exists() for path in environment.captured_paths)


def test_atomic_replace_failure_keeps_old_formal_and_cleans_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _Environment(tmp_path)
    destination = tmp_path / "formal.png"
    destination.write_bytes(b"previous formal evidence")

    def fail_replace(_source, _destination) -> None:
        raise OSError("atomic publication failed")

    monkeypatch.setattr("quote_app.evidence.platform.os.replace", fail_replace)

    with pytest.raises(EvidenceCaptureError) as captured:
        _pipeline(environment).capture(_request(destination, environment))

    assert captured.value.code == "CAPTURE_ENVIRONMENT"
    assert destination.read_bytes() == b"previous formal evidence"
    assert all(not path.exists() for path in environment.captured_paths)


def test_pipeline_draws_validated_business_frames_before_hashing(
    tmp_path: Path,
) -> None:
    environment = _Environment(tmp_path)
    destination = tmp_path / "no-model.png"
    rectangles = (
        CssRect(20, 20, 80, 30, "search_keyword"),
        CssRect(20, 80, 200, 100, "result_region"),
    )

    record = _pipeline(environment).capture(
        _request(
            destination,
            environment,
            state=EvidenceState.NO_MODEL,
            rectangles=rectangles,
            roles=("search_keyword", "result_region"),
        )
    )

    assert tuple(annotation.role for annotation in record.annotations) == (
        "search_keyword",
        "result_region",
    )
    with Image.open(destination) as image:
        assert image.convert("RGB").getpixel((20, 80)) == (255, 0, 0)


def test_unstable_probe_and_geometry_failure_leave_no_formal_file(
    tmp_path: Path,
) -> None:
    environment = _Environment(tmp_path)
    unstable_destination = tmp_path / "unstable.png"
    with pytest.raises(EvidenceCaptureError) as unstable:
        _pipeline(environment).capture(
            _request(
                unstable_destination,
                environment,
                probe=_Probe("one", "two", "three"),
            )
        )
    assert unstable.value.code == "CAPTURE_UNSTABLE"
    assert isinstance(unstable.value, RetryableTechnicalError)
    assert not unstable_destination.exists()

    geometry_destination = tmp_path / "geometry.png"
    with pytest.raises(EvidenceCaptureError) as geometry:
        _pipeline(environment).capture(
            _request(
                geometry_destination,
                environment,
                state=EvidenceState.CAPACITY_UNAVAILABLE,
                rectangles=(CssRect(390, 250, 40, 40, "capacity"),),
                roles=("capacity",),
            )
        )
    assert geometry.value.code == "CAPTURE_GEOMETRY"
    assert isinstance(geometry.value, RetryableTechnicalError)
    assert not geometry_destination.exists()


def test_platform_modules_import_without_eager_native_dependencies() -> None:
    assert importlib.import_module("quote_app.evidence.macos")
    assert importlib.import_module("quote_app.evidence.windows")


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (
            lambda snapshot: replace(
                snapshot,
                expected_window=BrowserWindowIdentity("test", 99, "stale"),
            ),
            "CAPTURE_GEOMETRY",
        ),
        (
            lambda snapshot: replace(
                snapshot,
                device_pixel_ratio=2,
            ),
            "CAPTURE_GEOMETRY",
        ),
        (
            lambda snapshot: replace(
                snapshot,
                viewport_height_css=400,
            ),
            "CAPTURE_GEOMETRY",
        ),
        (
            lambda snapshot: replace(snapshot, fullscreen=True),
            "CAPTURE_ENVIRONMENT",
        ),
    ],
)
def test_snapshot_is_a_single_validated_geometry_sample(
    tmp_path: Path,
    mutation,
    expected_code: str,
) -> None:
    environment = _Environment(tmp_path)
    environment.snapshot = mutation(environment.snapshot)

    with pytest.raises(EvidenceCaptureError) as captured:
        _pipeline(environment).capture(
            _request(tmp_path / "formal.png", environment)
        )

    assert captured.value.code == expected_code
    assert environment.captured_paths == []


@pytest.mark.parametrize(
    "bad_snapshot",
    [None, object()],
)
def test_invalid_geometry_provider_data_fails_closed_without_capture(
    tmp_path: Path,
    bad_snapshot: object,
) -> None:
    environment = _Environment(tmp_path)
    environment.snapshot = bad_snapshot

    with pytest.raises(EvidenceCaptureError) as captured:
        _pipeline(environment).capture(
            _request(tmp_path / "formal.png", environment)
        )

    assert captured.value.code == "CAPTURE_ENVIRONMENT"
    assert "secret-provider-detail" not in str(captured.value)
    assert environment.captured_paths == []


def test_geometry_provider_exception_is_normalized_without_secret_text(
    tmp_path: Path,
) -> None:
    environment = _Environment(
        tmp_path,
        snapshot_error=Exception("secret-provider-detail"),
    )

    with pytest.raises(EvidenceCaptureError) as captured:
        _pipeline(environment).capture(
            _request(tmp_path / "formal.png", environment)
        )

    assert captured.value.code == "CAPTURE_ENVIRONMENT"
    assert "secret-provider-detail" not in str(captured.value)


def test_atomic_replace_permission_failure_is_not_capture_permission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _Environment(tmp_path)
    destination = tmp_path / "formal.png"
    destination.write_bytes(b"previous formal evidence")

    def fail_replace(_source, _destination) -> None:
        raise PermissionError("output directory denied")

    monkeypatch.setattr("quote_app.evidence.platform.os.replace", fail_replace)
    with pytest.raises(EvidenceCaptureError) as captured:
        _pipeline(environment).capture(_request(destination, environment))

    assert captured.value.code == "CAPTURE_ENVIRONMENT"
    assert destination.read_bytes() == b"previous formal evidence"


class _FakeMSS:
    def __init__(self, darwin, *, size: tuple[int, int]) -> None:
        self.darwin = darwin
        self.size = size
        self.selected = None
        self.monitors = [
            {"left": -1600, "top": 0, "width": 3040, "height": 1000},
            {"left": -1600, "top": 0, "width": 1600, "height": 900},
            {"left": 0, "top": 0, "width": 1440, "height": 900},
        ]

    def __enter__(self):
        assert self.darwin.IMAGE_OPTIONS == 0
        return self

    def __exit__(self, *_args):
        return None

    def grab(self, monitor):
        assert self.darwin.IMAGE_OPTIONS == 0
        self.selected = monitor
        return SimpleNamespace(size=self.size, rgb=b"pixels")


def test_macos_retina_uses_logical_primary_but_validates_physical_scale(
    tmp_path: Path,
) -> None:
    from quote_app.evidence.macos import capture_macos_primary_display

    darwin = SimpleNamespace(IMAGE_OPTIONS=123)
    fake = _FakeMSS(darwin, size=(2880, 1800))
    writes: list[tuple[tuple[int, int], str]] = []

    capture_macos_primary_display(
        tmp_path / "retina.png",
        expected_physical_size=(2880, 1800),
        expected_scale=(2, 2),
        expected_device_pixel_ratio=2,
        mss_factory=lambda: fake,
        png_writer=lambda _rgb, size, *, output: writes.append(
            (tuple(size), output)
        ),
        darwin_module=darwin,
    )

    assert fake.selected == {
        "left": 0,
        "top": 0,
        "width": 1440,
        "height": 900,
    }
    assert darwin.IMAGE_OPTIONS == 123
    assert writes == [((2880, 1800), str(tmp_path / "retina.png"))]


@pytest.mark.parametrize(
    ("shot_size", "scale"),
    [
        ((1440, 900), (2, 2)),
        ((2880, 1800), (1, 1)),
        ((2880, 1800), (1.5, 1.5)),
    ],
)
def test_macos_retina_rejects_nominal_or_mixed_scale(
    tmp_path: Path,
    shot_size: tuple[int, int],
    scale: tuple[float, float],
) -> None:
    from quote_app.evidence.macos import capture_macos_primary_display

    darwin = SimpleNamespace(IMAGE_OPTIONS=9)
    fake = _FakeMSS(darwin, size=shot_size)

    with pytest.raises(GeometryError):
        capture_macos_primary_display(
            tmp_path / "bad.png",
            expected_physical_size=(2880, 1800),
            expected_scale=scale,
            expected_device_pixel_ratio=scale[0],
            mss_factory=lambda: fake,
            png_writer=lambda *_args, **_kwargs: None,
            darwin_module=darwin,
        )

    assert darwin.IMAGE_OPTIONS == 9


class _FakeWinFunction:
    def __init__(self, result) -> None:
        self.result = result

    def __call__(self, *_args):
        return self.result


class _FakeWinCallback:
    def __init__(self, callback) -> None:
        self.callback = callback

    def __call__(self, *args):
        return self.callback(*args)


def test_windows_dpi_startup_callable_sets_and_verifies_pmv2() -> None:
    from quote_app.evidence.windows import ensure_per_monitor_dpi_awareness

    api = SimpleNamespace(
        SetProcessDpiAwarenessContext=_FakeWinFunction(True),
        GetThreadDpiAwarenessContext=_FakeWinFunction(123),
        GetAwarenessFromDpiAwarenessContext=_FakeWinFunction(2),
        AreDpiAwarenessContextsEqual=_FakeWinFunction(True),
    )

    assert ensure_per_monitor_dpi_awareness(api) is True


@pytest.mark.parametrize(
    ("appbar_state", "taskbar_left", "clock_visible"),
    [
        (1, 0, True),
        (0, 500, True),
        (0, 0, False),
    ],
)
def test_windows_system_ui_proof_fails_closed_for_each_required_signal(
    tmp_path: Path,
    appbar_state: int,
    taskbar_left: int,
    clock_visible: bool,
) -> None:
    import ctypes

    from quote_app.evidence.windows import (
        _Rect,
        _windows_system_ui_proof,
    )

    def find_child(parent, _after, class_name, _title):
        if parent == 100 and class_name == "TrayNotifyWnd":
            return 200
        if parent == 200 and class_name == "TrayClockWClass":
            return 300
        return 0

    def is_visible(handle):
        return bool(handle != 300 or clock_visible)

    def fill_rect(_handle, pointer):
        rectangle = ctypes.cast(pointer, ctypes.POINTER(_Rect)).contents
        rectangle.left = taskbar_left
        rectangle.top = 280
        rectangle.right = taskbar_left + 400
        rectangle.bottom = 300
        return True

    user32 = SimpleNamespace(
        FindWindowW=_FakeWinFunction(100),
        FindWindowExW=_FakeWinCallback(find_child),
        IsWindowVisible=_FakeWinCallback(is_visible),
        GetWindowRect=_FakeWinCallback(fill_rect),
    )
    shell32 = SimpleNamespace(
        SHAppBarMessage=_FakeWinFunction(appbar_state),
    )
    snapshot = cast(CaptureGeometrySnapshot, _Environment(tmp_path).snapshot)

    with pytest.raises(CaptureEnvironmentUnavailable):
        _windows_system_ui_proof(
            snapshot,
            user32=user32,
            shell32=shell32,
        )


def test_fixed_platform_geometry_capability_missing_is_environment_failure() -> None:
    from quote_app.evidence.macos import MacOSCaptureEnvironment
    from quote_app.evidence.windows import WindowsCaptureEnvironment

    expected = BrowserWindowIdentity("test", 42, "0x2a")
    with pytest.raises(CaptureEnvironmentUnavailable):
        MacOSCaptureEnvironment().geometry_snapshot(expected)
    with pytest.raises(CaptureEnvironmentUnavailable):
        WindowsCaptureEnvironment(
            dpi_awareness_verifier=lambda: True,
        ).geometry_snapshot(expected)


@pytest.mark.parametrize("platform_name", ["macos", "windows"])
def test_missing_fixed_geometry_capability_is_nonretryable_in_pipeline(
    tmp_path: Path,
    platform_name: str,
) -> None:
    from quote_app.evidence.macos import MacOSCaptureEnvironment
    from quote_app.evidence.windows import WindowsCaptureEnvironment

    request_environment = _Environment(tmp_path)
    environment: CaptureEnvironment
    if platform_name == "macos":
        environment = MacOSCaptureEnvironment(
            permission_provider=lambda: True,
            prepare_callback=lambda _expected: None,
        )
    else:
        environment = WindowsCaptureEnvironment(
            prepare_callback=lambda _expected: None,
            dpi_awareness_verifier=lambda: True,
        )

    with pytest.raises(EvidenceCaptureError) as captured:
        EvidenceCapturePipeline(
            environment,
            sleeper=lambda _seconds: None,
        ).capture(
            _request(tmp_path / "formal.png", request_environment)
        )

    assert captured.value.code == "CAPTURE_ENVIRONMENT"
    assert isinstance(captured.value, NonRetryableTechnicalError)


def test_unverified_windows_dpi_fails_before_geometry_provider_is_called() -> None:
    from quote_app.evidence.windows import WindowsCaptureEnvironment

    called = False

    def provider(expected):
        nonlocal called
        called = True
        raise AssertionError(expected)

    environment = WindowsCaptureEnvironment(
        geometry_snapshot_provider=provider,
        dpi_awareness_verifier=lambda: False,
    )
    with pytest.raises(CaptureEnvironmentUnavailable):
        environment.geometry_snapshot(
            BrowserWindowIdentity("windows", 42, "0x2a")
        )
    assert called is False


@pytest.mark.parametrize(
    ("scale", "dpi", "native_dpi"),
    [
        (1.25, 120.0, 120),
        (1.5, 144.0, 144),
    ],
)
def test_windows_geometry_accepts_consistent_native_dpi_snapshot_and_dpr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scale: float,
    dpi: float,
    native_dpi: int,
) -> None:
    import quote_app.evidence.windows as windows

    expected = BrowserWindowIdentity("windows", 42, "0x2a")
    base = cast(CaptureGeometrySnapshot, _Environment(tmp_path).snapshot)
    snapshot = replace(
        base,
        expected_window=expected,
        viewport_geometry=ViewportGeometry(0, 40, scale, scale, 0, 0),
        viewport_width_css=400 / scale,
        viewport_height_css=220 / scale,
        device_pixel_ratio=scale,
        dpi_x=dpi,
        dpi_y=dpi,
    )
    api = SimpleNamespace(
        IsZoomed=_FakeWinFunction(True),
        GetDpiForWindow=_FakeWinFunction(native_dpi),
    )
    monkeypatch.setattr(windows, "_configured_user32", lambda *_args: api)
    environment = windows.WindowsCaptureEnvironment(
        geometry_snapshot_provider=lambda _expected: snapshot,
        dpi_awareness_verifier=lambda: True,
    )

    assert environment.geometry_snapshot(expected) is snapshot


@pytest.mark.parametrize(
    ("dpr", "dpi_x", "dpi_y", "expected_error"),
    [
        (None, 120.0, 120.0, CaptureEnvironmentUnavailable),
        (1.25, None, 120.0, CaptureEnvironmentUnavailable),
        (1.25, 120.0, None, CaptureEnvironmentUnavailable),
        (1.5, 144.0, 144.0, GeometryError),
        (1.5, 120.0, 120.0, GeometryError),
    ],
)
def test_windows_geometry_fails_closed_for_missing_or_mismatched_dpi(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dpr: float | None,
    dpi_x: float | None,
    dpi_y: float | None,
    expected_error: type[Exception],
) -> None:
    import quote_app.evidence.windows as windows

    expected = BrowserWindowIdentity("windows", 42, "0x2a")
    base = cast(CaptureGeometrySnapshot, _Environment(tmp_path).snapshot)
    scale = 1.5 if dpr == 1.5 else 1.25
    snapshot = replace(
        base,
        expected_window=expected,
        viewport_geometry=ViewportGeometry(0, 40, scale, scale, 0, 0),
        viewport_width_css=400 / scale,
        viewport_height_css=220 / scale,
        device_pixel_ratio=dpr,
        dpi_x=dpi_x,
        dpi_y=dpi_y,
    )
    api = SimpleNamespace(
        IsZoomed=_FakeWinFunction(True),
        GetDpiForWindow=_FakeWinFunction(120),
    )
    monkeypatch.setattr(windows, "_configured_user32", lambda *_args: api)
    environment = windows.WindowsCaptureEnvironment(
        geometry_snapshot_provider=lambda _expected: snapshot,
        dpi_awareness_verifier=lambda: True,
    )

    with pytest.raises(expected_error):
        environment.geometry_snapshot(expected)


def test_windows_system_ui_rejects_browser_covering_taskbar(
    tmp_path: Path,
) -> None:
    import ctypes

    from quote_app.evidence.windows import (
        _Rect,
        _windows_system_ui_proof,
    )

    def find_child(parent, _after, class_name, _title):
        if parent == 100 and class_name == "TrayNotifyWnd":
            return 200
        if parent == 200 and class_name == "TrayClockWClass":
            return 300
        return 0

    def fill_rect(_handle, pointer):
        rectangle = ctypes.cast(pointer, ctypes.POINTER(_Rect)).contents
        rectangle.left = 0
        rectangle.top = 280
        rectangle.right = 400
        rectangle.bottom = 300
        return True

    user32 = SimpleNamespace(
        FindWindowW=_FakeWinFunction(100),
        FindWindowExW=_FakeWinCallback(find_child),
        IsWindowVisible=_FakeWinFunction(True),
        GetWindowRect=_FakeWinCallback(fill_rect),
    )
    shell32 = SimpleNamespace(SHAppBarMessage=_FakeWinFunction(0))
    base = cast(CaptureGeometrySnapshot, _Environment(tmp_path).snapshot)
    normal = replace(
        base,
        browser_physical_bounds=DisplayBounds(0, 0, 400, 260),
    )
    proof = _windows_system_ui_proof(
        normal,
        user32=user32,
        shell32=shell32,
    )
    assert proof.authoritative is True

    covered = replace(
        normal,
        browser_physical_bounds=DisplayBounds(0, 0, 400, 300),
        fullscreen=False,
        maximized=True,
    )
    with pytest.raises(CaptureEnvironmentUnavailable):
        _windows_system_ui_proof(
            covered,
            user32=user32,
            shell32=shell32,
        )


@pytest.mark.parametrize("platform_name", ["macos", "windows"])
def test_prepare_callback_error_does_not_leak_sensitive_cause(
    tmp_path: Path,
    platform_name: str,
) -> None:
    from quote_app.evidence.macos import MacOSCaptureEnvironment
    from quote_app.evidence.windows import WindowsCaptureEnvironment

    def fail_prepare(_expected):
        raise RuntimeError("token=secret")

    request_environment = _Environment(tmp_path)
    environment: CaptureEnvironment
    if platform_name == "macos":
        environment = MacOSCaptureEnvironment(
            permission_provider=lambda: True,
            prepare_callback=fail_prepare,
        )
    else:
        environment = WindowsCaptureEnvironment(
            prepare_callback=fail_prepare,
            dpi_awareness_verifier=lambda: True,
        )
    with pytest.raises(EvidenceCaptureError) as captured:
        EvidenceCapturePipeline(environment).capture(
            _request(tmp_path / "formal.png", request_environment)
        )

    assert captured.value.code == "CAPTURE_OBSCURED"
    assert "secret" not in str(captured.value)
    assert "secret" not in repr(captured.value.__cause__)


def test_only_explicit_native_screen_denial_maps_to_capture_permission(
    tmp_path: Path,
) -> None:
    native_denied = _Environment(
        tmp_path,
        capture_error=NativeScreenPermissionDenied("native denied"),
    )
    with pytest.raises(EvidenceCaptureError) as captured:
        _pipeline(native_denied).capture(
            _request(tmp_path / "native.png", native_denied)
        )
    assert captured.value.code == "CAPTURE_PERMISSION"

    ordinary_permission = _Environment(
        tmp_path,
        capture_error=PermissionError("png output denied"),
    )
    with pytest.raises(EvidenceCaptureError) as captured:
        _pipeline(ordinary_permission).capture(
            _request(tmp_path / "file.png", ordinary_permission)
        )
    assert captured.value.code == "CAPTURE_FAILED"


def test_mss_png_writer_permission_error_is_capture_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import ModuleType

    from quote_app.evidence.platform import capture_display_with_mss

    class Screenshotter:
        monitors: list[dict[str, int]] = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def grab(self, _monitor):
            return SimpleNamespace(size=(400, 300), rgb=b"pixels")

    def deny_png(*_args, **_kwargs):
        raise PermissionError("temporary png denied")

    fake_mss = ModuleType("mss")
    fake_mss.mss = lambda: Screenshotter()  # type: ignore[attr-defined]
    fake_mss.tools = SimpleNamespace(to_png=deny_png)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mss", fake_mss)
    environment = _Environment(tmp_path)

    def capture_with_mss(
        destination: Path,
        snapshot: CaptureGeometrySnapshot,
    ) -> None:
        capture_display_with_mss(
            destination,
            snapshot.display_physical_bounds,
        )

    environment.capture_primary_display = capture_with_mss  # type: ignore[method-assign]
    with pytest.raises(EvidenceCaptureError) as captured:
        _pipeline(environment).capture(
            _request(tmp_path / "formal.png", environment)
        )

    assert captured.value.code == "CAPTURE_FAILED"


@pytest.mark.parametrize("helper_name", ["macos", "windows"])
def test_missing_lazy_mss_dependency_is_fixed_environment_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    helper_name: str,
) -> None:
    original_import = builtins.__import__

    def missing_mss(name, *args, **kwargs):
        if name == "mss" or name.startswith("mss."):
            raise ModuleNotFoundError("mss unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_mss)
    with pytest.raises(CaptureEnvironmentUnavailable):
        if helper_name == "macos":
            from quote_app.evidence.macos import capture_macos_primary_display

            capture_macos_primary_display(
                tmp_path / "mac.png",
                expected_physical_size=(2880, 1800),
                expected_scale=(2, 2),
                expected_device_pixel_ratio=2,
            )
        else:
            from quote_app.evidence.platform import capture_display_with_mss

            capture_display_with_mss(
                tmp_path / "win.png",
                DisplayBounds(0, 0, 400, 300),
            )


@pytest.mark.parametrize("threshold", [float("nan"), float("inf"), -1.0])
def test_quality_thresholds_must_be_finite_and_nonnegative(
    threshold: float,
) -> None:
    with pytest.raises(ValueError):
        assess_capture_quality(
            _readable_image(),
            expected_size=(400, 300),
            minimum_contrast=threshold,
        )


@pytest.mark.parametrize("interval", [0, -0.01, float("nan")])
def test_production_stability_interval_must_be_strictly_positive(
    interval: float,
) -> None:
    with pytest.raises(ValueError, match="positive"):
        wait_for_stable_hash(
            _Probe("ready", "ready"),
            minimum_interval_seconds=interval,
            sleeper=lambda _seconds: None,
        )
