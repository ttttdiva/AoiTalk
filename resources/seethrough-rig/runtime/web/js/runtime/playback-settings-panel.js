// Canonical DOM controls shared by the standalone editor and Web hosts.
// Native hosts render the same metadata. No framework or global DOM dependency.
import { PLAYBACK_SETTINGS_DEFINITION, playbackSettingsFields, resolvePlaybackSettings, updatePlaybackSetting } from './playback-settings.js';

let panelSequence = 0;
const at = (value, path) => path.split('.').reduce((current, key) => current?.[key], value);
const matches = (settings, condition = {}) => Object.entries(condition).every(([path, value]) => at(settings, path) === value);
const css = `
.strig-playback-panel{color:inherit;font:inherit;width:100%;min-width:0}
.strig-playback-panel details{border:1px solid var(--line,#4b556344);border-radius:8px;margin:8px 0;padding:8px;min-width:0}
.strig-playback-panel summary{cursor:pointer;font-weight:600}
.strig-playback-panel fieldset{border:0;margin:0;padding:0;min-width:0}
.strig-playback-panel .strig-playback-row{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:6px;align-items:center;margin:10px 0}
.strig-playback-panel .strig-playback-row-number{grid-template-columns:minmax(0,1fr) 76px;row-gap:4px}
.strig-playback-panel .strig-playback-row-number label{grid-column:1/-1}
.strig-playback-panel label{font-size:12px;min-width:0;white-space:normal;overflow-wrap:anywhere}
.strig-playback-panel input[type=range]{width:100%;min-width:0;accent-color:var(--accent,#4b8ed9)}
.strig-playback-panel input[type=checkbox]{justify-self:start;accent-color:var(--accent,#4b8ed9)}
.strig-playback-panel select{font:inherit;color:inherit;background:var(--panel,transparent);min-width:0;max-width:100%;border:1px solid var(--line,#9ca3af);border-radius:4px;padding:3px}
.strig-playback-panel output{font-size:11px;text-align:right;font-variant-numeric:tabular-nums}
.strig-playback-panel .strig-playback-value{display:flex;align-items:center;gap:2px;min-width:0}
.strig-playback-panel .strig-playback-value:empty{display:none}
.strig-playback-panel .strig-playback-row:not(.strig-playback-row-number) .strig-playback-value{display:none}
.strig-playback-panel input[type=number]{width:100%;min-width:0;padding:3px;color:inherit;background:var(--panel,transparent);border:1px solid var(--line,#9ca3af);border-radius:4px;font:inherit;font-size:11px;font-variant-numeric:tabular-nums}
.strig-playback-panel input[aria-invalid=true]{border-color:#c53a44}
.strig-playback-panel [data-disabled=true]{opacity:.5}
.strig-playback-panel .strig-playback-error{color:#c53a44;font-size:12px}
`;

/** No persistence: hosts own save/cancel/reset and model identity. */
export function createPlaybackSettingsPanel(container, options = {}) {
  const doc = container?.ownerDocument;
  if (!doc?.createElement) throw new TypeError('Playback panel requires a DOM container');
  let settings = resolvePlaybackSettings(options.settings ?? undefined);
  let capabilities = options.capabilities;
  let selectedGroups = options.groups;
  let disabled = Boolean(options.disabled), disposed = false;
  const prefix = `strig-playback-${++panelSequence}`;
  const root = doc.createElement('div');
  root.className = 'strig-playback-panel';
  root.setAttribute('role', 'group');
  root.setAttribute('aria-label', options.label || '再生設定');
  const style = doc.createElement('style'); style.textContent = css; root.append(style);
  const rows = [];
  const sections = [];
  const error = doc.createElement('div');
  error.className = 'strig-playback-error'; error.setAttribute('role', 'status'); error.setAttribute('aria-live', 'polite');

  function refresh() {
    const groups = playbackSettingsFields(capabilities, options.profile);
    for (const row of rows) {
      const field = groups.find(group => group.id === row.group).fields.find(field => field.id === row.field.id);
      const value = settings[row.group][field.id];
      const unavailable = disabled || !field.supported || !matches(settings, field.enabledWhen);
      row.input.disabled = unavailable;
      if (row.number) {
        row.number.disabled = unavailable;
        row.number.value = String(value);
        row.number.setAttribute('aria-invalid', 'false');
      }
      row.element.dataset.disabled = String(unavailable);
      row.element.title = !field.supported ? 'このモデル / 再生エンジンでは変更できません' : field.description || '';
      if (field.type === 'boolean') row.input.checked = value;
      else row.input.value = String(value);
      row.output.textContent = field.unit || '';
    }
  }

  function change(group, field, value) {
    const previous = settings;
    try {
      settings = updatePlaybackSetting(settings, group, field.id, value);
      options.onChange?.(structuredClone(settings));
      error.textContent = '';
    } catch (cause) {
      settings = previous;
      error.textContent = cause?.message || '設定を変更できませんでした';
    }
    refresh();
  }

  function clearGroups() {
    for (const row of rows) {
      row.input.removeEventListener(row.field.type === 'number' ? 'input' : 'change', row.onInput);
      row.number?.removeEventListener('input', row.onNumber);
      row.number?.removeEventListener('blur', row.onNumberBlur);
    }
    rows.length = 0;
    for (const section of sections) section.remove();
    sections.length = 0;
  }

  function renderGroups() {
    for (const group of PLAYBACK_SETTINGS_DEFINITION.groups) {
      if (selectedGroups !== undefined && !selectedGroups.includes(group.id)) continue;
      const details = doc.createElement('details');
      details.setAttribute('data-playback-group', group.id);
      details.open = selectedGroups !== undefined || group.id === 'wind' || group.id === 'blink';
      const summary = doc.createElement('summary'); summary.textContent = group.label; details.append(summary);
      const section = doc.createElement('fieldset');
      section.setAttribute('aria-label', group.label);
      for (const field of group.fields) {
        const element = doc.createElement('div'); element.className = `strig-playback-row${field.type === 'number' ? ' strig-playback-row-number' : ''}`;
        const label = doc.createElement('label'); label.textContent = field.label;
        const input = doc.createElement(field.type === 'enum' ? 'select' : 'input');
        input.id = `${prefix}-${group.id}-${field.id}`;
        input.setAttribute('data-playback-path', `${group.id}.${field.id}`);
        label.htmlFor = input.id;
        const output = doc.createElement('output'); output.htmlFor = input.id;
        const valueContainer = doc.createElement('div'); valueContainer.className = 'strig-playback-value';
        let number = null, onNumber = null, onNumberBlur = null;
        if (field.type === 'enum') {
          for (const item of field.options) {
            const option = doc.createElement('option'); option.value = item.value; option.textContent = item.label; input.append(option);
          }
        } else if (field.type === 'boolean') input.type = 'checkbox';
        else {
          Object.assign(input, { type: 'range', min: field.min, max: field.max, step: field.step });
          number = doc.createElement('input');
          Object.assign(number, { type: 'number', min: field.min, max: field.max, step: field.step });
          number.id = input.id + '-number';
          number.setAttribute('data-playback-number', `${group.id}.${field.id}`);
          number.setAttribute('aria-label', `${group.label} ${field.label}${field.unit ? ` (${field.unit})` : ''}`);
          onNumber = () => {
            if (disposed || number.disabled) return;
            const value = Number(number.value);
            const valid = number.value.trim() !== '' && Number.isFinite(value) && value >= field.min && value <= field.max;
            number.setAttribute('aria-invalid', String(!valid));
            // Empty / partial / out-of-range typing remains local until corrected.
            if (valid) change(group.id, field, value);
          };
          onNumberBlur = () => { if (!disposed) refresh(); };
          number.addEventListener('input', onNumber);
          number.addEventListener('blur', onNumberBlur);
          valueContainer.append(number);
        }
        const onInput = () => {
          if (disposed || input.disabled) return;
          change(group.id, field, field.type === 'boolean' ? input.checked : field.type === 'number' ? Number(input.value) : input.value);
        };
        input.addEventListener(field.type === 'number' ? 'input' : 'change', onInput);
        rows.push({ group: group.id, field, element, input, number, output, onInput, onNumber, onNumberBlur });
        valueContainer.append(output);
        element.append(label, input, valueContainer); section.append(element);
      }
      details.append(section); root.append(details); sections.push(details);
    }
  }
  renderGroups();
  root.append(error); container.append(root); refresh();
  return {
    update(next = {}) {
      if (disposed) return;
      if (next.settings !== undefined) settings = resolvePlaybackSettings(next.settings);
      if ('capabilities' in next) capabilities = next.capabilities;
      if ('disabled' in next) disabled = Boolean(next.disabled);
      if ('groups' in next) {
        selectedGroups = next.groups;
        clearGroups();
        error.remove(); renderGroups(); root.append(error);
      }
      error.textContent = ''; refresh();
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      clearGroups();
      root.remove();
    },
  };
}
