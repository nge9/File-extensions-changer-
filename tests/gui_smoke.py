import os
import sys
from pathlib import Path
import tempfile
from unittest.mock import patch
import zipfile

if sys.platform.startswith('linux') and not os.environ.get('DISPLAY'):
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    os.environ.setdefault('QT_QUICK_BACKEND', 'software')
    os.environ.setdefault('QT_OPENGL', 'software')
    os.environ.setdefault('QTWEBENGINE_CHROMIUM_FLAGS', '--disable-gpu --disable-dev-shm-usage')

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QTimer, QMimeData, QUrl, QPointF, Qt
from PySide6.QtGui import QDropEvent
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox
from main import create_window
from service import Session


def main():
    app = QApplication([])
    temporary = tempfile.TemporaryDirectory(prefix='extension-ui-')
    root = Path(temporary.name)
    files = root / 'Project files'
    files.mkdir()
    (files / 'a.txt').write_bytes(b'Alpha contents stay the same.\n' * 100)
    (files / 'b.txt').write_bytes(b'Bravo must not be renamed.\n' * 100)
    (files / 'c.png').write_bytes(b'Generated sample bytes, not an actual PNG')
    (files / 'nested').mkdir()
    (files / 'nested/script.py').write_text('print(1)\n')
    extracted = root / 'extracted'
    extracted.mkdir()
    dropped = root / 'Dropped folder'
    dropped.mkdir()
    (dropped / 'extra.log').write_text('A newly added file')
    session = Session(root / 'history')
    window = create_window(session)
    window.resize(1440, 1050)
    window.show()
    view = window.centralWidget()
    messages = []
    def choose_folder(parent, title, *args):
        return str(extracted if title.startswith('Extract') else files)
    def choose_save(parent, title, *args):
        return (str(root / ('selected.zip' if title.startswith('Create') else 'activity.csv')), '')
    patches = [patch.object(QFileDialog, 'getExistingDirectory', side_effect=choose_folder),
               patch.object(QFileDialog, 'getSaveFileName', side_effect=choose_save),
               patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes)]
    for item in patches:
        item.start()
    stages = [
        ('native HTML connection', "typeof bridge!=='undefined' && bridge!==null && state.files.length===0", None),
        ('folder listing without automatic selection', "state.files.length===3 && checked.size===0 && !canApply", "document.getElementById('add-folder').click();"),
        ('select one file and preview', "changes===1 && checked.size===1 && canApply && !busy", "Array.from(document.querySelectorAll('[data-file]')).find(e=>e.dataset.file.endsWith('a.txt')).click();document.getElementById('target-extension').value='custom';document.getElementById('target-extension').dispatchEvent(new Event('input'));"),
        ('apply only the checked file', "state.files.some(f=>f.name==='a.custom') && state.history_total===1 && !busy", "document.getElementById('apply').click();"),
        ('history per file', "!document.getElementById('history-page').hidden && document.getElementById('history-list').textContent.includes('a.custom')", "document.getElementById('history-tab').click();"),
        ('persistent undo record', "state.files.some(f=>f.name==='a.txt') && state.history_total===2 && !busy", "document.getElementById('undo').click();"),
        ('select two files for ZIP', "selectedFiles().length===2 && !busy && !document.getElementById('create-zip').disabled", "document.getElementById('files-tab').click();checked.clear();state.files.filter(f=>['a.txt','b.txt'].includes(f.name)).forEach(f=>checked.add(f.path));schedulePreview();"),
        ('create a real ZIP', "state.files.some(f=>f.name==='selected.zip') && state.history_total===4 && document.getElementById('report').open && !busy", "document.getElementById('create-zip').click();"),
        ('select the ZIP for extraction', "selectedFiles().length===1 && selectedFiles()[0].name==='selected.zip' && !busy && !document.getElementById('unzip').disabled", "document.getElementById('report-ok').click();checked.clear();checked.add(state.files.find(f=>f.name==='selected.zip').path);schedulePreview();"),
        ('extract ZIP into a new folder', "state.history_total===6 && document.getElementById('report').open && !busy", "document.getElementById('unzip').click();"),
        ('export complete per-file history', "!busy && !document.getElementById('history-page').hidden && state.history.some(r=>r.operation==='Create ZIP') && state.history.some(r=>r.operation==='Unzip')", "document.getElementById('report-ok').click();document.getElementById('history-tab').click();document.getElementById('export-history').click();"),
        ('native folder drop', "state.files.some(f=>f.name==='extra.log') && !busy && state.history_total===6", None),
    ]
    position = {'index': 0, 'sent': False, 'polls': 0, 'dropped': False}
    def finish(code, message):
        print(message, flush=True)
        window.close()
        for item in reversed(patches):
            item.stop()
        app.exit(code)
    def tick():
        if position['index'] >= len(stages):
            try:
                assert (files / 'a.txt').read_bytes() == b'Alpha contents stay the same.\n' * 100
                assert (files / 'b.txt').read_bytes() == b'Bravo must not be renamed.\n' * 100
                assert (files / 'c.png').exists()
                assert (files / 'nested/script.py').exists()
                with zipfile.ZipFile(root / 'selected.zip') as archive:
                    assert set(archive.namelist()) == {'a.txt', 'b.txt'}
                folders = [path for path in extracted.iterdir() if path.is_dir()]
                assert len(folders) == 1
                assert (folders[0] / 'a.txt').read_bytes() == (files / 'a.txt').read_bytes()
                assert (root / 'activity.csv').is_file()
                restarted = Session(session.history.folder)
                assert restarted.snapshot()['history_total'] == 6
                assert not window.centralWidget().page().acceptNavigationRequest(QUrl('https://example.com'), None, True)
                finish(0, 'PASS: native HTML UI, checked-only folder operations, per-file history, undo, ZIP, unzip, CSV export and native drop')
            except Exception as exc:
                finish(1, f'FAIL: final checks: {exc}')
            return
        name, predicate, action = stages[position['index']]
        if name == 'native folder drop' and not position['dropped'] and not window.bridge.running:
            position['dropped'] = True
            mime = QMimeData()
            mime.setUrls([QUrl.fromLocalFile(str(dropped))])
            event = QDropEvent(QPointF(100, 100), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            assert view.eventFilter(view.focusProxy(), event)
        if action and not position['sent']:
            position['sent'] = True
            view.page().runJavaScript(action)
        def result(value):
            if value:
                print('PASS:', name, flush=True)
                screenshot = os.environ.get('EXTENSION_SCREENSHOT')
                if screenshot and name == 'select one file and preview':
                    window.grab().save(screenshot)
                if screenshot and name == 'export complete per-file history':
                    window.grab().save(str(Path(screenshot).with_name('history.png')))
                position.update(index=position['index'] + 1, sent=False, polls=0)
            else:
                position['polls'] += 1
                if position['polls'] > 140:
                    view.page().runJavaScript("JSON.stringify({status:document.getElementById('status').textContent,toast:document.getElementById('toast-text').textContent,busy,files:state.files.length,checked:checked.size,changes,canApply,history:state.history_total})", lambda info: finish(1, f'FAIL: {name}: {info}'))
                    return
            QTimer.singleShot(160, tick)
        view.page().runJavaScript('Boolean(' + predicate + ')', result)
    QTimer.singleShot(800, tick)
    result = app.exec()
    view.setPage(None)
    temporary.cleanup()
    return result


if __name__ == '__main__':
    raise SystemExit(main())
