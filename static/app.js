const $ = (selector) => document.querySelector(selector);
const toast = $('#toast');
let activeAsset = null;
let editorTab = 'current';

function notify(message, error = false) {
  toast.textContent = message;
  toast.className = `show${error ? ' error' : ''}`;
  clearTimeout(notify.timer);
  notify.timer = setTimeout(() => toast.className = '', 4500);
}

async function request(url, options = {}) {
  const response = await fetch(url, options);
  let body = {};
  try { body = await response.json(); } catch (_) { /* empty response */ }
  if (!response.ok) throw new Error(body.detail || `Request failed (${response.status})`);
  return body;
}

const fileInput = $('#media-file');
const dropZone = $('#drop-zone');
fileInput.addEventListener('change', () => {
  if (!fileInput.files[0]) return;
  const file = fileInput.files[0];
  const url = URL.createObjectURL(file);
  dropZone.classList.remove('has-image', 'has-video');
  if (file.type.startsWith('video/')) {
    $('#video-preview').src = url;
    dropZone.classList.add('has-video');
  } else {
    $('#preview').src = url;
    dropZone.classList.add('has-image');
  }
  dropZone.classList.add('has-preview');
});
['dragenter', 'dragover'].forEach(type => dropZone.addEventListener(type, event => {
  event.preventDefault(); dropZone.classList.add('drag');
}));
['dragleave', 'drop'].forEach(type => dropZone.addEventListener(type, event => {
  event.preventDefault(); dropZone.classList.remove('drag');
}));
dropZone.addEventListener('drop', event => {
  fileInput.files = event.dataTransfer.files;
  fileInput.dispatchEvent(new Event('change'));
});

$('#upload-form').addEventListener('submit', async event => {
  event.preventDefault();
  const button = event.currentTarget.querySelector('button');
  const original = button.innerHTML;
  button.disabled = true; button.textContent = 'PROCESSING...';
  try {
    const result = await request('/api/media', { method: 'POST', body: new FormData(event.currentTarget) });
    notify(`${result.name} is now on the Kraken`);
    activeAsset = result;
    await loadAssets();
    refreshScreen();
    scheduleEditorPreview();
  } catch (error) { notify(error.message, true); }
  finally { button.disabled = false; button.innerHTML = original; }
});

$('#stream-form').addEventListener('submit', async event => {
  event.preventDefault();
  const data = new FormData(event.currentTarget);
  const editor = overlaySettings();
  try {
    await request('/api/stream', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({...Object.fromEntries(data), ...editor}) });
    notify('URL stream started');
  } catch (error) { notify(error.message, true); }
});
$('#stop-stream').addEventListener('click', async () => {
  try { await request('/api/stream', { method: 'DELETE' }); notify('Stream stopped'); }
  catch (error) { notify(error.message, true); }
});

document.querySelectorAll('input[type="range"]').forEach(input => {
  const output = $(`#${input.name}-out`);
  input.addEventListener('input', () => output.textContent = `${input.value}%`);
});

const uploadFit = $('#upload-form select[name="fit"]');
const previewMedia = [$('#preview'), $('#video-preview')];
function updateCropPreview() {
  const fit = uploadFit.value === 'cover' ? 'cover' : (uploadFit.value === 'stretch' ? 'fill' : 'contain');
  const position = `${$('#upload-form [name="focus_x"]').value}% ${$('#upload-form [name="focus_y"]').value}%`;
  previewMedia.forEach(media => { media.style.objectFit = fit; media.style.objectPosition = position; });
}
uploadFit.addEventListener('change', updateCropPreview);
$('#upload-form [name="focus_x"]').addEventListener('input', updateCropPreview);
$('#upload-form [name="focus_y"]').addEventListener('input', updateCropPreview);

let telemetryValues = {liquid: 0, cpu: 0, gpu: 0};
function overlaySettings() {
  return {
    overlay: $('#overlay-mode').value,
    overlay_size: Number($('#overlay-size').value),
    overlay_x: Number($('#overlay-x').value),
    overlay_y: Number($('#overlay-y').value),
    text_color: $('#text-color').value,
    accent_color: $('#accent-color').value,
    background_color: $('#background-color').value,
    background_opacity: Number($('#background-opacity').value),
  };
}
function editorSettings() {
  return {
    fit: uploadFit.value,
    fps: Number($('#upload-form [name="fps"]').value),
    palette_colors: Number($('#upload-form [name="palette_colors"]').value),
    start: Number($('#upload-form [name="start"]').value),
    duration: Number($('#upload-form [name="duration"]').value),
    focus_x: Number($('#upload-form [name="focus_x"]').value),
    focus_y: Number($('#upload-form [name="focus_y"]').value),
    overlay_refresh: Number($('#overlay-refresh').value),
    ...overlaySettings(),
  };
}
function selectPreviewTab(tab) {
  editorTab = tab;
  const editing = tab === 'editor';
  $('#current-screen').classList.toggle('hidden', editing);
  $('#editor-screen').classList.toggle('hidden', !editing);
  $('#tab-current').classList.toggle('active', !editing);
  $('#tab-editor').classList.toggle('active', editing);
  $('#preview-badge').textContent = editing ? 'EXACT RENDER PREVIEW' : 'LIVE OUTPUT';
  if (editing) scheduleEditorPreview();
}
$('#tab-current').addEventListener('click', () => selectPreviewTab('current'));
$('#tab-editor').addEventListener('click', () => selectPreviewTab('editor'));

async function renderEditorPreview() {
  if (!activeAsset?.id || !activeAsset.source) {
    $('#editor-hint').textContent = 'Upload or choose a retained library asset to edit it.';
    return;
  }
  if (renderEditorPreview.controller) renderEditorPreview.controller.abort();
  const controller = new AbortController();
  renderEditorPreview.controller = controller;
  try {
    const response = await fetch(`/api/assets/${activeAsset.id}/preview`, {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify(editorSettings()), signal: controller.signal,
    });
    if (!response.ok) { const body = await response.json(); throw new Error(body.detail || 'Preview failed'); }
    const url = URL.createObjectURL(await response.blob());
    const image = $('#editor-screen');
    const old = image.dataset.objectUrl;
    image.src = url; image.dataset.objectUrl = url;
    if (old) URL.revokeObjectURL(old);
    $('#editor-hint').textContent = `Editing ${activeAsset.name}. This frame uses the exact final renderer.`;
  } catch (error) {
    if (error.name !== 'AbortError') $('#editor-hint').textContent = error.message;
  }
}
function scheduleEditorPreview() {
  clearTimeout(scheduleEditorPreview.timer);
  scheduleEditorPreview.timer = setTimeout(renderEditorPreview, 250);
}
['overlay-mode','overlay-size','overlay-x','overlay-y','text-color','accent-color','background-color','background-opacity','overlay-refresh'].forEach(id => {
  $(`#${id}`).addEventListener('input', scheduleEditorPreview);
});
document.querySelectorAll('#upload-form select, #upload-form input:not([type="file"])').forEach(input => input.addEventListener('input', scheduleEditorPreview));

$('#apply-editor').addEventListener('click', async () => {
  if (!activeAsset?.id || !activeAsset.source) { notify('Upload an asset before applying edits', true); return; }
  const button = $('#apply-editor'); button.disabled = true;
  try {
    activeAsset = await request(`/api/assets/${activeAsset.id}/process`, {
      method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify(editorSettings()),
    });
    notify('Edited media rendered and applied');
    await loadAssets(); refreshScreen(); scheduleEditorPreview();
  } catch (error) { notify(error.message, true); }
  finally { button.disabled = false; }
});
$('#control-form').addEventListener('submit', async event => {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(event.currentTarget));
  Object.keys(values).forEach(key => values[key] = Number(values[key]));
  try {
    await request('/api/control', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(values) });
    notify('Hardware settings applied');
  } catch (error) { notify(error.message, true); }
});

async function showAsset(id) {
  try { await request(`/api/assets/${id}/show`, {method:'POST'}); notify('Media displayed'); refreshScreen(); await loadEditorState(); }
  catch (error) { notify(error.message, true); }
}
async function deleteAsset(id) {
  try { await request(`/api/assets/${id}`, {method:'DELETE'}); await loadAssets(); }
  catch (error) { notify(error.message, true); }
}

async function loadAssets() {
  const assets = await request('/api/assets');
  const grid = $('#asset-grid');
  if (!assets.length) { grid.innerHTML = '<p class="empty">No prepared media yet.</p>'; return; }
  grid.innerHTML = assets.map(asset => `<div class="asset"><strong title="${escapeHtml(asset.name)}">${escapeHtml(asset.name)}</strong><small>${asset.mode.toUpperCase()} / ${(asset.size / 1048576).toFixed(1)} MB${asset.overlay && asset.overlay !== 'none' ? ` / ${asset.overlay.toUpperCase().replace('_', '+')}` : ''}</small><div class="asset-actions"><button data-show="${asset.id}">DISPLAY</button><button data-delete="${asset.id}">DELETE</button></div></div>`).join('');
  grid.querySelectorAll('[data-show]').forEach(button => button.onclick = () => showAsset(button.dataset.show));
  grid.querySelectorAll('[data-delete]').forEach(button => button.onclick = () => deleteAsset(button.dataset.delete));
}

async function loadStreamSettings() {
  const {settings} = await request('/api/stream-settings');
  if (!settings) return;
  const form = $('#stream-form');
  ['url', 'fps', 'fit', 'source_type', 'focus_x', 'focus_y'].forEach(name => {
    if (settings[name] !== undefined && form.elements[name]) form.elements[name].value = settings[name];
  });
}
function escapeHtml(value) { const node = document.createElement('div'); node.textContent = value; return node.innerHTML; }

async function refreshStatus() {
  try {
    const status = await request('/api/status');
    const hardware = status.hardware;
    const pill = $('#device-pill');
    pill.classList.toggle('online', Boolean(hardware.connected));
    pill.classList.toggle('offline', !hardware.connected);
    pill.querySelector('span').textContent = hardware.connected ? hardware.description : (hardware.error || 'Device offline');
    if (hardware.sensors) {
      const liquid = hardware.sensors.find(item => item.name.toLowerCase().includes('liquid temperature'));
      if (liquid) telemetryValues.liquid = Number(liquid.value);
    }
    const stream = status.stream;
    $('#stream-state').textContent = stream.active ? 'LIVE' : (stream.error ? 'ERROR' : 'IDLE');
    $('#stream-fps').textContent = Number(stream.actual_fps || 0).toFixed(1);
    $('#stream-frames').textContent = stream.frames_sent;
    $('#stream-drops').textContent = stream.frames_dropped;
    if (stream.active) refreshScreen();
    if (status.overlay?.revision !== refreshStatus.overlayRevision) {
      if (refreshStatus.overlayRevision !== undefined) refreshScreen();
      refreshStatus.overlayRevision = status.overlay?.revision;
    }
    if (stream.error && refreshStatus.lastError !== stream.error) { notify(stream.error, true); refreshStatus.lastError = stream.error; }
  } catch (error) { $('#device-pill').querySelector('span').textContent = 'Server unavailable'; }
}

async function refreshTelemetryValues() {
  try {
    const values = await request('/api/telemetry');
    telemetryValues = {...telemetryValues, ...values};
    if (editorTab === 'editor') scheduleEditorPreview();
  } catch (_) { /* device status pill reports connection errors */ }
}

function refreshScreen() {
  $('#current-screen').src = `/api/screen?t=${Date.now()}`;
}

function setValue(selector, value) {
  const input = $(selector);
  if (!input || value === undefined || value === null) return;
  input.value = value;
  const output = input.name ? $(`#${input.name}-out`) : null;
  if (output) output.textContent = `${value}%`;
}

async function loadEditorState() {
  const state = await request('/api/editor-state');
  if (!state.asset) {
    activeAsset = null;
    if (renderEditorPreview.controller) renderEditorPreview.controller.abort();
    $('#editor-screen').removeAttribute('src');
    $('#editor-hint').textContent = 'Upload or choose a retained library asset to edit it.';
    return;
  }
  activeAsset = state.asset;
  const processing = activeAsset.processing || {};
  const style = processing.overlay_style || {};
  setValue('#upload-form [name="fit"]', processing.fit);
  setValue('#upload-form [name="fps"]', processing.fps);
  setValue('#upload-form [name="palette_colors"]', processing.palette_colors);
  setValue('#upload-form [name="start"]', processing.start);
  setValue('#upload-form [name="duration"]', processing.duration);
  setValue('#upload-form [name="focus_x"]', Math.round((processing.focus_x ?? .5) * 100));
  setValue('#upload-form [name="focus_y"]', Math.round((processing.focus_y ?? .5) * 100));
  setValue('#overlay-mode', activeAsset.overlay || 'none');
  setValue('#overlay-size', Math.round((style.size ?? 1) * 100));
  setValue('#overlay-x', Math.round((style.x ?? .5) * 100));
  setValue('#overlay-y', Math.round((style.y ?? .76) * 100));
  setValue('#text-color', style.text_color);
  setValue('#accent-color', style.accent_color);
  setValue('#background-color', style.background_color);
  setValue('#background-opacity', Math.round((style.background_opacity ?? .78) * 100));
  setValue('#overlay-refresh', processing.overlay_refresh);
  updateCropPreview();

  if (activeAsset.source) {
    const sourceUrl = `/api/assets/${activeAsset.id}/source?t=${Date.now()}`;
    dropZone.classList.remove('has-image', 'has-video');
    if (/\.(mp4|webm|mkv|mov)$/i.test(activeAsset.name)) {
      $('#video-preview').src = sourceUrl; dropZone.classList.add('has-video');
    } else {
      $('#preview').src = sourceUrl; dropZone.classList.add('has-image');
    }
    dropZone.classList.add('has-preview');
  }
  scheduleEditorPreview();
}

loadAssets().catch(error => notify(error.message, true));
loadStreamSettings().catch(error => notify(error.message, true));
refreshScreen();
refreshTelemetryValues();
loadEditorState().catch(error => notify(error.message, true));
refreshStatus();
setInterval(refreshStatus, 2000);
setInterval(refreshTelemetryValues, 5000);
