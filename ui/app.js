'use strict';
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let bridge = null;
let state = {files:[], history:[], history_total:0, can_undo:false, recovery_count:0};
let checked = new Set();
let preview = new Map();
let previewId = null;
let canApply = false;
let changes = 0;
let conflicts = 0;
let revision = 0;
let page = 'files';
let busy = false;
let activeAction = '';
let sequence = 0;
let queue = Promise.resolve();
let timer = null;
let toastTimer = null;
const waiting = new Map();
const lightActions = new Set(['preview','state','recursive']);

function rpc(action, args = {}) {
    const execute = () => new Promise((resolve, reject) => {
        activeAction = action;
        const id = String(++sequence);
        waiting.set(id, {resolve, reject});
        bridge.request(id, JSON.stringify({action, ...args}));
    });
    const result = queue.then(execute);
    queue = result.catch(() => {});
    return result;
}

function toast(message, error = false) {
    clearTimeout(toastTimer);
    $('toast-text').textContent = message;
    $('toast').classList.toggle('error', error);
    $('toast').hidden = false;
    $('status').textContent = message;
    toastTimer = setTimeout(() => $('toast').hidden = true, error ? 14000 : 6500);
}

function showReport(message, title = 'Operation report', output = false) {
    $('report-title').textContent = title;
    $('report-content').textContent = message;
    $('open-output').hidden = !output;
    if (!$('report').open) $('report').showModal();
}

function visibleFiles() {
    const query = $('search').value.toLowerCase();
    const extension = $('extension-filter').value;
    const files = state.files.filter(f => (!query || (f.name+' '+f.folder).toLowerCase().includes(query)) && (!extension || f.extension === (extension === 'none' ? '' : extension)));
    const order = $('sort').value;
    files.sort((a,b) => order === 'size' ? b.size-a.size : order === 'newest' ? (b.modified||0)-(a.modified||0) : order === 'oldest' ? (a.modified||0)-(b.modified||0) : a.name.localeCompare(b.name) || a.path.localeCompare(b.path));
    return files;
}

function selectedFiles() {
    return visibleFiles().filter(f => checked.has(f.path) && !f.problem);
}

function badge(value, warning = false, neutral = false) {
    return `<span class="badge ${warning?'warning':neutral?'neutral':''}">${esc(value)}</span>`;
}

function renderFiles() {
    const files = visibleFiles();
    const selected = files.filter(f => checked.has(f.path) && !f.problem);
    $('file-count').textContent = state.files.length;
    $('loaded-metric').textContent = state.files.length;
    $('checked-metric').textContent = selected.length;
    $('changes-metric').textContent = changes;
    $('changes-description').textContent = conflicts ? `${conflicts} conflicts need attention` : changes ? 'Review the names below before applying' : 'Choose an extension to preview';
    $('visible-count').textContent = files.length;
    $('selection-summary').textContent = `${selected.length} checked & shown · ${files.length} shown · ${state.files.length} loaded`;
    const available = files.filter(f => !f.problem);
    $('check-all').checked = available.length > 0 && available.every(f => checked.has(f.path));
    $('check-all').indeterminate = selected.length > 0 && selected.length < available.length;
    $('file-list').innerHTML = files.map(f => {
        const entry = checked.has(f.path) ? preview.get(f.path) : null;
        const error = f.problem || entry?.error;
        const status = error ? badge(error,true) : !checked.has(f.path) ? badge('Not checked',false,true) : !entry ? badge('Previewing…',false,true) : entry.changed ? badge('Ready') : badge('Unchanged',false,true);
        return `<tr><td class="check-cell"><input type="checkbox" data-file="${esc(f.path)}" aria-label="Select ${esc(f.name)}" ${checked.has(f.path)?'checked':''} ${f.problem?'disabled':''}></td><td class="name" title="${esc(f.path)}">${esc(f.name)}</td><td><span class="extension-tag">${esc(f.extension || 'none')}</span></td><td class="${entry?.changed?'preview':'dim'}" title="${esc(entry?.new_name||'')}">${esc(entry?.new_name || '—')}</td><td class="folder" title="${esc(f.folder)}">${esc(f.folder)}</td><td title="${esc(error||'')}">${status}</td></tr>`;
    }).join('');
    $('empty').hidden = files.length > 0;
    $('empty').querySelector('h3').textContent = state.files.length ? 'No files match these filters.' : 'A clean slate for your files.';
    $('empty').querySelector('p').textContent = state.files.length ? 'Change the search or extension filter to see your loaded files.' : 'Add a folder or drop files here, then check the ones you want to change.';
    updateControls();
}

function renderHistory() {
    const query = $('history-search').value.toLowerCase();
    const rows = state.history.filter(r => (r.before+' '+r.after+' '+r.folder+' '+r.operation).toLowerCase().includes(query));
    $('history-count').textContent = state.history_total;
    $('history-summary').textContent = state.history_total > state.history.length ? `Latest ${state.history.length} of ${state.history_total} records. CSV includes all.` : `${state.history_total} file records`;
    const labels = {complete:'Completed',undone:'Undone',recovered:'Names restored',pending:'Interrupted',recovery_required:'Recovery needed',prepared:'Prepared — inspect output'};
    $('history-list').innerHTML = rows.map(r => `<tr><td class="history-time">${esc(new Date(r.time).toLocaleString())}</td><td>${esc(r.operation)}</td><td title="${esc(r.source)}">${esc(r.before)}</td><td title="${esc(r.target)}">${esc(r.after)}</td><td class="folder" title="${esc(r.folder)}">${esc(r.folder)}</td><td>${badge(labels[r.state]||r.state,['pending','recovery_required','prepared'].includes(r.state),['undone','recovered'].includes(r.state))}</td></tr>`).join('');
    $('history-empty').hidden = rows.length > 0;
}

function acceptState(value, mapping = {}) {
    checked = new Set([...checked].map(path => mapping[path] || path));
    state = value;
    const loaded = new Set(state.files.map(f => f.path));
    checked = new Set([...checked].filter(path => loaded.has(path)));
    const previous = $('extension-filter').value;
    $('extension-filter').replaceChildren(new Option('All extensions',''));
    [...new Set(state.files.map(f => f.extension))].sort().forEach(ext => $('extension-filter').add(new Option(ext||'No extension',ext||'none')));
    if ([...$('extension-filter').options].some(o => o.value === previous)) $('extension-filter').value = previous;
    $('recovery').hidden = !state.recovery_count;
    renderFiles();
    renderHistory();
}

function updateControls() {
    const selected = selectedFiles().length;
    const hardBusy = busy && !lightActions.has(activeAction);
    document.querySelectorAll('button').forEach(button => button.disabled = busy);
    document.querySelectorAll('input,select').forEach(input => input.disabled = hardBusy);
    const fileByPath = new Map(state.files.map(f => [f.path,f]));
    document.querySelectorAll('[data-file]').forEach(input => {
        const file = fileByPath.get(input.dataset.file);
        input.disabled = hardBusy || !!file?.problem;
    });
    $('target-extension').disabled = hardBusy || $('remove-extension').checked;
    $('presets').disabled = hardBusy || $('remove-extension').checked;
    $('apply').disabled = busy || !canApply || !previewId;
    $('create-zip').disabled = busy || !selected;
    $('unzip').disabled = busy || !selected;
    $('remove-checked').disabled = busy || !checked.size;
    $('undo').disabled = busy || !state.can_undo;
    $('clear-list').disabled = busy || !state.files.length;
    $('export-history').disabled = busy || !state.history_total;
    $('busy-overlay').hidden = !hardBusy;
}

function invalidate() {
    revision++;
    canApply = false;
    previewId = null;
    changes = 0;
    conflicts = 0;
    preview.clear();
    updateControls();
}

function schedulePreview() {
    clearTimeout(timer);
    invalidate();
    renderFiles();
    timer = setTimeout(runPreview,180);
}

async function runPreview() {
    clearTimeout(timer);
    const current = revision;
    const paths = selectedFiles().map(f => f.path);
    try {
        const result = await rpc('preview',{paths,extension:$('target-extension').value,remove:$('remove-extension').checked});
        if (current !== revision) return;
        preview = new Map(result.entries.map(e => [e.source,e]));
        previewId = result.preview_id;
        canApply = result.can_apply;
        changes = result.changes;
        conflicts = result.conflicts;
        if (result.needs_extension && paths.length) $('status').textContent = 'Enter a new extension, or check Remove the last extension. No changes are applied automatically.';
        renderFiles();
    } catch (error) {
        if (current !== revision) return;
        canApply = false;
        $('status').textContent = error.message;
        updateControls();
    }
}

async function perform(action, args = {}) {
    clearTimeout(timer);
    invalidate();
    const messages = {files:'Adding selected files…',folder:'Reading folder contents…',apply:'Changing selected extensions…',undo:'Restoring original names…',recover:'Recovering filenames…',zip:'Compressing and verifying ZIP…',unzip:'Extracting and verifying files…',export:'Exporting change history…'};
    $('busy-title').textContent = messages[action] || 'Working locally…';
    try {
        const result = await rpc(action,args);
        if (result?.cancelled) return;
        if (result?.state) {
            if (action === 'apply' || action === 'undo') {
                $('search').value = '';
                $('extension-filter').value = '';
                $('target-extension').value = '';
                $('remove-extension').checked = false;
            }
            acceptState(result.state,result.mapping||{});
        }
        if (result?.message) {
            if (action === 'zip' || action === 'unzip' || result.message.includes('\n')) showReport(result.message,action === 'zip' ? 'ZIP created' : action === 'unzip' ? 'Extraction report' : 'Operation report',action === 'zip'||action === 'unzip');
            else toast(result.message);
            $('status').textContent = result.message;
        }
    } catch (error) {
        toast(error.message,true);
        try { acceptState(await rpc('state')); } catch {}
    } finally {
        schedulePreview();
    }
}

function showPage(value) {
    page = value;
    $('files-page').hidden = value !== 'files';
    $('history-page').hidden = value !== 'history';
    $('add-actions').hidden = value !== 'files';
    $('files-tab').classList.toggle('active',value === 'files');
    $('history-tab').classList.toggle('active',value === 'history');
    $('page-title').textContent = value === 'files' ? 'New extension. Same file.' : 'Your files have a history.';
    $('page-subtitle').textContent = value === 'files' ? 'Choose your files, see what changes, and keep a record of every step.' : 'A clear record for every extension change, ZIP, extraction, and undo.';
    renderHistory();
}

$('files-tab').onclick = () => showPage('files');
$('history-tab').onclick = () => showPage('history');
$('add-files').onclick = () => perform('files');
$('add-folder').onclick = () => perform('folder');
$('empty-add').onclick = () => perform('folder');
$('clear-list').onclick = () => {checked.clear();perform('clear');};
$('remove-checked').onclick = () => perform('remove',{paths:[...checked]});
$('apply').onclick = () => {if(canApply&&previewId)perform('apply',{preview_id:previewId});};
$('undo').onclick = () => perform('undo');
$('recover').onclick = () => perform('recover');
$('create-zip').onclick = () => perform('zip',{paths:selectedFiles().map(f=>f.path)});
$('unzip').onclick = () => perform('unzip',{paths:selectedFiles().map(f=>f.path)});
$('export-history').onclick = () => perform('export');
$('open-history').onclick = () => rpc('open_history').catch(e=>toast(e.message,true));
$('open-output').onclick = () => rpc('open_output').catch(e=>toast(e.message,true));
$('refresh').onclick = async () => {try{acceptState(await rpc('state'));schedulePreview();}catch(e){toast(e.message,true);}};
$('target-extension').addEventListener('input',schedulePreview);
$('remove-extension').addEventListener('change',schedulePreview);
$('search').addEventListener('input',schedulePreview);
$('extension-filter').addEventListener('change',schedulePreview);
$('sort').addEventListener('change',schedulePreview);
$('history-search').addEventListener('input',renderHistory);
$('presets').onchange = () => {if($('presets').value){$('target-extension').value=$('presets').value;$('presets').value='';schedulePreview();}};
$('recursive').onchange = () => rpc('recursive',{value:$('recursive').checked}).catch(e=>toast(e.message,true));
$('select-shown').onclick = () => {visibleFiles().filter(f=>!f.problem).forEach(f=>checked.add(f.path));schedulePreview();};
$('uncheck').onclick = () => {checked.clear();schedulePreview();};
$('check-all').onchange = () => {const selected=$('check-all').checked;visibleFiles().filter(f=>!f.problem).forEach(f=>selected?checked.add(f.path):checked.delete(f.path));schedulePreview();};
$('file-list').onchange = event => {const input=event.target.closest('[data-file]');if(!input)return;input.checked?checked.add(input.dataset.file):checked.delete(input.dataset.file);schedulePreview();};
$('toast-close').onclick = () => $('toast').hidden = true;
$('report-close').onclick = () => $('report').close();
$('report-ok').onclick = () => $('report').close();
for(const type of ['dragover','drop'])window.addEventListener(type,event=>event.preventDefault());

if(typeof qt!=='undefined'&&typeof QWebChannel!=='undefined')new QWebChannel(qt.webChannelTransport,channel=>{
    bridge=channel.objects.files;
    bridge.response.connect((id,raw)=>{
        const response=JSON.parse(raw);
        if(id==='native-drop'){
            if(response.ok){invalidate();acceptState(response.result.state);toast(response.result.message);schedulePreview();}
            else toast(response.error,true);
            return;
        }
        const entry=waiting.get(id);
        if(entry){waiting.delete(id);response.ok?entry.resolve(response.result):entry.reject(new Error(response.error));}
    });
    bridge.busy.connect(value=>{busy=value;updateControls();});
    bridge.notification.connect(value=>{if(value==='drop-start')activeAction='native-drop';if(value==='drag-enter'&&!busy)$('drag-overlay').hidden=false;if(value==='drag-leave')$('drag-overlay').hidden=true;});
    rpc('state').then(value=>{acceptState(value);schedulePreview();}).catch(error=>toast(error.message,true));
});else{$('status').textContent='Start this desktop app with python main.py. This HTML file is not a website.';}
