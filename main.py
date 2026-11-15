import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys

try:
    from PySide6.QtCore import QObject, Signal, Slot, QUrl, QEvent, Qt, QLockFile, QStandardPaths
    from PySide6.QtGui import QDesktopServices, QIcon
    from PySide6.QtWidgets import QApplication, QMainWindow, QFileDialog, QMessageBox
    from PySide6.QtWebChannel import QWebChannel
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
except ImportError:
    raise SystemExit('Install the desktop dependency:\n  python -m pip install -r requirements.txt\nThen run:\n  python main.py')

from service import Session

ROOT = Path(__file__).resolve().parent


class Bridge(QObject):
    response = Signal(str, str)
    complete = Signal(str, str)
    busy = Signal(bool)
    notification = Signal(str)

    def __init__(self, window, session, lock=None):
        super().__init__(window)
        self.window, self.session, self.lock = window, session, lock
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.running = False
        self.recursive = False
        self.output_folder = None
        self.complete.connect(self.deliver)

    @Slot(str, str)
    def deliver(self, identifier, payload):
        self.running = False
        data = json.loads(payload)
        result = data.get('result') or {}
        if data.get('ok') and (result.get('output_folder') or result.get('path')):
            candidate = Path(result['output_folder']) if result.get('output_folder') else Path(result['path']).parent
            if candidate.is_dir():
                self.output_folder = candidate
        self.busy.emit(False)
        self.response.emit(identifier, payload)

    def send(self, identifier, result=None, error=None):
        self.response.emit(identifier, json.dumps({'ok': error is None, 'result': result, 'error': error}, ensure_ascii=False))

    def submit(self, identifier, function):
        if self.running:
            self.send(identifier, error='Please wait for the current operation.')
            return
        self.running = True
        self.busy.emit(True)
        def run():
            try:
                result = {'ok': True, 'result': function()}
            except Exception as exc:
                result = {'ok': False, 'error': str(exc) or type(exc).__name__}
            self.complete.emit(identifier, json.dumps(result, ensure_ascii=False))
        self.pool.submit(run)

    def drop(self, paths):
        self.notification.emit('drag-leave')
        self.notification.emit('drop-start')
        if paths:
            self.submit('native-drop', lambda: self.session.add(paths, self.recursive))

    @Slot(str, str)
    def request(self, identifier, raw):
        try:
            if self.running:
                raise ValueError('Please wait for the current operation.')
            args = json.loads(raw)
            action = args['action']
            if action == 'files':
                paths, _ = QFileDialog.getOpenFileNames(self.window, 'Add files to the extension changer')
                if paths:
                    self.submit(identifier, lambda: self.session.add(paths))
                else:
                    self.send(identifier, {'cancelled': True})
            elif action == 'folder':
                path = QFileDialog.getExistingDirectory(self.window, 'Add a folder and select files inside it')
                if path:
                    self.submit(identifier, lambda: self.session.add([path], self.recursive))
                else:
                    self.send(identifier, {'cancelled': True})
            elif action == 'recursive':
                self.recursive = bool(args['value'])
                self.send(identifier, {})
            elif action == 'apply':
                entries = self.session.checked_plan(args['preview_id'])
                count = sum(e.changed for e in entries)
                answer = QMessageBox.question(self.window, 'Change selected extensions?',
                    f'Change the extensions of {count} checked files exactly as previewed?\n\nFile contents will not be converted. Close these files in other apps first.',
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel)
                if answer == QMessageBox.StandardButton.Yes:
                    self.submit(identifier, lambda: self.session.apply(args['preview_id']))
                else:
                    self.send(identifier, {'cancelled': True})
            elif action in ('undo', 'recover'):
                message = 'Restore original names from the last completed change?' if action == 'undo' else 'Restore original names from interrupted operations?'
                answer = QMessageBox.question(self.window, 'Confirm operation', message + '\nUnrelated files will not be overwritten.',
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel)
                if answer == QMessageBox.StandardButton.Yes:
                    self.submit(identifier, self.session.undo if action == 'undo' else self.session.recover)
                else:
                    self.send(identifier, {'cancelled': True})
            elif action == 'zip':
                paths = args.get('paths', [])
                self.session.checked_files(paths)
                path, _ = QFileDialog.getSaveFileName(self.window, 'Create a new ZIP from checked files', 'selected-files.zip', 'ZIP archives (*.zip)')
                if path:
                    self.submit(identifier, lambda: self.session.zip_files(paths, path))
                else:
                    self.send(identifier, {'cancelled': True})
            elif action == 'unzip':
                paths = args.get('paths', [])
                self.session.checked_files(paths)
                folder = QFileDialog.getExistingDirectory(self.window, 'Extract checked ZIPs into new folders here')
                if folder:
                    self.submit(identifier, lambda: self.session.unzip_files(paths, folder))
                else:
                    self.send(identifier, {'cancelled': True})
            elif action == 'export':
                path, _ = QFileDialog.getSaveFileName(self.window, 'Export file change history to a new CSV', 'extension-change-history.csv', 'CSV files (*.csv)')
                if path:
                    self.submit(identifier, lambda: self.session.export_history(path))
                else:
                    self.send(identifier, {'cancelled': True})
            elif action == 'open_output':
                if self.output_folder is None or not self.output_folder.is_dir():
                    raise ValueError('There is no output folder to open yet.')
                if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.output_folder))):
                    raise ValueError(f'Could not open the file manager. Output: {self.output_folder}')
                self.send(identifier, {})
            elif action == 'open_history':
                if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.session.history.folder))):
                    raise ValueError(f'Could not open the file manager. History: {self.session.history.folder}')
                self.send(identifier, {})
            else:
                actions = {'state': self.session.snapshot,
                           'preview': lambda: self.session.make_preview(args.get('paths', []), args.get('extension', ''), args.get('remove', False)),
                           'remove': lambda: self.session.remove(args.get('paths', [])),
                           'clear': self.session.clear}
                if action not in actions:
                    raise ValueError('Unknown action.')
                self.submit(identifier, actions[action])
        except Exception as exc:
            self.send(identifier, error=str(exc) or type(exc).__name__)


class LocalPage(QWebEnginePage):
    def acceptNavigationRequest(self, url, navigation_type, is_main_frame):
        return url.isLocalFile() and Path(url.toLocalFile()).resolve() == ROOT / 'ui/index.html'


class View(QWebEngineView):
    def __init__(self, parent):
        super().__init__(parent)
        self.bridge = None
        self.setAcceptDrops(True)
        self.loadFinished.connect(self.install_filters)

    def install_filters(self, *_):
        for child in self.findChildren(QObject):
            child.installEventFilter(self)
        if self.focusProxy():
            self.focusProxy().setAcceptDrops(True)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.DragEnter, QEvent.Type.DragMove, QEvent.Type.Drop):
            urls = event.mimeData().urls()
            if urls and all(url.isLocalFile() for url in urls):
                if event.type() == QEvent.Type.Drop:
                    if self.bridge:
                        self.bridge.drop([url.toLocalFile() for url in urls])
                    event.setDropAction(Qt.DropAction.CopyAction)
                    event.accept()
                else:
                    event.acceptProposedAction()
                    if self.bridge:
                        self.bridge.notification.emit('drag-enter')
            else:
                event.ignore()
            return True
        if event.type() == QEvent.Type.DragLeave and self.bridge:
            self.bridge.notification.emit('drag-leave')
        return super().eventFilter(watched, event)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and all(u.isLocalFile() for u in event.mimeData().urls()):
            event.acceptProposedAction()
            if self.bridge:
                self.bridge.notification.emit('drag-enter')
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        if self.bridge:
            self.bridge.drop([u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()])
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()

    def dragLeaveEvent(self, event):
        if self.bridge:
            self.bridge.notification.emit('drag-leave')
        event.accept()


class Window(QMainWindow):
    def closeEvent(self, event):
        if self.bridge.running:
            QMessageBox.information(self, 'Please wait', 'A file operation is running. Close the window after it finishes.')
            event.ignore()
            return
        self.bridge.pool.shutdown(wait=True)
        if self.bridge.lock:
            self.bridge.lock.unlock()
        event.accept()


def create_window(session, lock=None):
    window = Window()
    window.setWindowTitle('File Extension Changer')
    window.resize(1380, 900)
    window.setMinimumSize(1000, 680)
    window.setWindowIcon(QIcon(str(ROOT / 'ui/icon.svg')))
    view = View(window)
    view.setPage(LocalPage(view))
    window.setCentralWidget(view)
    bridge = Bridge(window, session, lock)
    window.bridge = view.bridge = bridge
    channel = QWebChannel(view.page())
    channel.registerObject('files', bridge)
    view.page().setWebChannel(channel)
    view.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
    view.settings().setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, False)
    view.setUrl(QUrl.fromLocalFile(str(ROOT / 'ui/index.html')))
    return window


def main():
    app = QApplication(sys.argv[:1])
    app.setOrganizationName('UsefulTools')
    app.setApplicationName('FileExtensionChanger')
    folder = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)) / 'history'
    try:
        session = Session(folder)
        lock = QLockFile(str(folder / '.app.lock'))
        lock.setStaleLockTime(0)
        if not lock.tryLock(100):
            raise ValueError('This application is already open. Close the other window first.')
        window = create_window(session, lock)
    except Exception as exc:
        QMessageBox.critical(None, 'Could not open File Extension Changer', str(exc))
        return 1
    window.show()
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
