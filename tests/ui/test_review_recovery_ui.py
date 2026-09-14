import os
from PIL import Image
import pytest
from tests.ui.test_desktop_workbench import workbench as base_workbench
from tests.test_review_support import session as base_session
from quote_app.desktop_widgets import SoftButton

pytestmark = pytest.mark.skipif(
    os.environ.get("QUOTE_NATIVE_UI_TESTS") != "1", reason="Needs native desktop"
)


@pytest.fixture
def workbench(tmp_path):
    yield from base_workbench.__wrapped__(tmp_path)


@pytest.fixture
def session(tmp_path):
    return base_session.__wrapped__(tmp_path)


def walk(widget):
    for child in widget.winfo_children():
        yield child
        yield from walk(child)


def test_real_screenshot_import_preview_and_save(workbench, session, tmp_path, monkeypatch):
    from quote_app.desktop_evidence import open_evidence_editor

    image = tmp_path / "screen.png"
    Image.new("RGB", (1200, 800), "white").save(image)
    monkeypatch.setattr(
        "quote_app.desktop_evidence.filedialog.askopenfilename", lambda **kw: str(image)
    )
    messages = []
    monkeypatch.setattr(
        "quote_app.desktop_evidence.messagebox.showinfo", lambda *a, **kw: messages.append(a)
    )
    monkeypatch.setattr(
        "quote_app.desktop_evidence.messagebox.showerror", lambda *a, **kw: pytest.fail(str(a))
    )
    saved = []
    root = workbench.root
    root.deiconify()
    dialog = open_evidence_editor(
        root, session, session.products[0], lambda: saved.append(True), channel="tmall"
    )
    root.update()
    controls = {w.cget("text"): w for w in walk(dialog) if type(w) is SoftButton}
    submit = controls["确认商品与渠道并保存"]
    assert submit.cget("state") == "disabled"
    controls["选择截图"].invoke()
    root.update()
    assert submit.cget("state") == "normal"
    assert dialog.winfo_height() < root.winfo_screenheight() - 50
    from quote_app.desktop_controls import SoftEntry
    fields = [w for w in walk(dialog) if isinstance(w, SoftEntry)]
    fields[0].variable.set('1999')
    fields[1].variable.set('https://detail.tmall.com/item.htm?id=123')
    submit.invoke()
    root.update()
    assert saved == [True]
    assert session._replacements[session.products[0].id]["tmall"]["evidence_path"]
    assert messages
