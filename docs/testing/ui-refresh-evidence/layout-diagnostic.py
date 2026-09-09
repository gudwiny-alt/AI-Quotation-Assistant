import json
import sys
from pathlib import Path
import tempfile
import tkinter as tk
from quote_app.app import QuoteApp
from quote_app.paths import build_app_paths
from quote_app.services.readiness import ReadinessCheck

out = Path('/private/tmp/quote-ui-layout-diagnostic.json')
records = []
def save():
    out.write_text(json.dumps(records, ensure_ascii=False, indent=2))
records.append({'phase': 'before Tk', 'executable': sys.executable})
save()
root = tk.Tk()
records.append({'phase': 'Tk initialized', 'scaling': root.tk.call('tk', 'scaling')})
save()
def forbidden(*args, **kwargs):
    raise AssertionError('Business action invoked during geometry diagnostic')
app = QuoteApp(root, app_paths=build_app_paths(home=Path(tempfile.mkdtemp(prefix='quotation-layout-'))),
               pipeline=forbidden, website_service=forbidden, login_browser_launcher=forbidden,
               readiness_checker=lambda _: ReadinessCheck(()))

def visit(widget, issues):
    if not widget.winfo_ismapped():
        return
    for child in widget.winfo_children():
        if not child.winfo_ismapped():
            continue
        x, y, w, h = child.winfo_x(), child.winfo_y(), child.winfo_width(), child.winfo_height()
        pw, ph = widget.winfo_width(), widget.winfo_height()
        if widget.winfo_class() != 'Canvas' and (x < -1 or y < -1 or x + w > pw + 1 or y + h > ph + 1):
            issues.append({'type': 'outside-parent', 'widget': str(child), 'class': child.winfo_class(),
                           'text': str(child.cget('text')) if 'text' in child.keys() else '',
                           'box': [x,y,w,h], 'parent_size': [pw,ph]})
        if child.winfo_class() in ('Label', 'Button', 'TButton') and 'text' in child.keys() and child.cget('text'):
            if child.winfo_reqwidth() > w + 2 or child.winfo_reqheight() > h + 2:
                issues.append({'type': 'undersized-text-widget', 'widget': str(child), 'text': str(child.cget('text')),
                              'actual': [w,h], 'requested': [child.winfo_reqwidth(),child.winfo_reqheight()]})
        visit(child, issues)
    children = [child for child in widget.winfo_children() if child.winfo_ismapped()]
    if widget.winfo_class() != 'Canvas':
        for index, first in enumerate(children):
            for second in children[index+1:]:
                overlap_x = min(first.winfo_x()+first.winfo_width(), second.winfo_x()+second.winfo_width()) - max(first.winfo_x(), second.winfo_x())
                overlap_y = min(first.winfo_y()+first.winfo_height(), second.winfo_y()+second.winfo_height()) - max(first.winfo_y(), second.winfo_y())
                if overlap_x > 1 and overlap_y > 1:
                    issues.append({'type':'sibling-overlap', 'widgets':[str(first),str(second)], 'overlap':[overlap_x,overlap_y]})

for size in ('1280x850', '1000x720'):
    root.geometry(size)
    root.update()
    for page in ('overview', 'intelligence', 'decision', 'audit', 'history', 'settings'):
        app.workbench.show_page(page)
        root.update_idletasks()
        root.update()
        issues=[]
        visit(root, issues)
        scroll_checks=[]
        def check_scroll(widget):
            if widget.winfo_class() == 'Canvas':
                widget.yview_moveto(1.0)
                root.update_idletasks()
                children=widget.winfo_children()
                if children:
                    child=children[0]
                    bottom=child.winfo_y()+child.winfo_height()
                    scroll_checks.append({'viewport_height':widget.winfo_height(), 'content_height':child.winfo_height(),
                                          'bottom_after_scroll':bottom, 'fraction':widget.yview()})
                    if bottom > widget.winfo_height()+1:
                        issues.append({'type':'scroll-bottom-unreachable','bottom':bottom,'height':widget.winfo_height()})
                widget.yview_moveto(0)
            for child in widget.winfo_children():
                check_scroll(child)
        check_scroll(app.workbench.body)
        records.append({'phase':'page', 'size_requested':size, 'size_actual':[root.winfo_width(),root.winfo_height()],
                        'page':page, 'issues':issues, 'scroll_checks':scroll_checks})
        save()
records.append({'phase':'complete', 'result': 'PASS' if all(not item.get('issues') for item in records) else 'FAIL'})
save()
root.destroy()
print(out.read_text(), flush=True)
