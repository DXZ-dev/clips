/* Editor de clips: recorte, diseño (auto / cámara+gameplay / completo), subtítulos, gancho y marca de agua. */
(function () {
  const $e = (s) => document.querySelector(s);
  let E = null;            // estado: {name, src:{duration,width,height}, rect, layout, preset, hl, size, words}
  let presets = {};

  const CSS = `
  #ed{position:fixed;inset:0;background:rgba(5,7,10,.88);z-index:50;overflow:auto;display:none}
  #ed .box{max-width:1280px;margin:16px auto;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px}
  #ed .hd{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:12px}#ed .hd h2{margin:0;flex:1;font-size:15px;text-transform:none;letter-spacing:0;color:var(--tx)}
  #ed .cols{display:grid;grid-template-columns:minmax(220px,300px) minmax(320px,1fr) minmax(300px,380px);gap:14px}
  @media(max-width:1000px){#ed .cols{grid-template-columns:1fr}}
  #ed video{width:100%;background:#000;border-radius:8px;display:block}
  #ed .pv video{aspect-ratio:9/16;max-height:72vh;object-fit:contain}
  #ed .sw{position:relative;line-height:0}#ed .sw video{max-height:56vh}
  #ed .rect{position:absolute;border:2px solid var(--ac);background:rgba(255,230,0,.14);cursor:move;touch-action:none}
  #ed .rect b{position:absolute;right:-7px;bottom:-7px;width:14px;height:14px;background:var(--ac);border-radius:3px;cursor:nwse-resize}
  #ed .rect i{position:absolute;left:3px;top:2px;font:11px/1.2 system-ui;color:#111;background:var(--ac);padding:0 4px;border-radius:3px;font-style:normal}
  #ed .bar2{position:relative;height:10px;background:var(--line);border-radius:5px;margin:8px 0}#ed .bar2 i{position:absolute;top:0;bottom:0;background:var(--ac);opacity:.8;border-radius:5px}
  #ed .tabs{display:flex;gap:4px;margin-bottom:8px;flex-wrap:wrap}#ed .tabs button{padding:5px 10px;font-size:13px}
  #ed .pane{display:none}#ed .pane.on{display:block}
  #ed .seg{display:flex;gap:6px;flex-wrap:wrap;margin:4px 0 8px}
  #ed .chip{display:inline-block;width:11px;height:11px;border-radius:50%;margin-right:5px;vertical-align:-1px;border:1px solid #0006}
  #ed .words{max-height:46vh;overflow:auto}#ed .w{display:flex;gap:6px;align-items:center;padding:2px 0}
  #ed .w.out{opacity:.35}#ed .w input{flex:1;margin:0!important;padding:3px 6px!important}#ed .w small{color:var(--mut);width:42px;text-align:right;cursor:pointer}
  #ed .w button{padding:1px 7px}#ed .row2{display:grid;grid-template-columns:1fr 1fr;gap:8px}
  #ed .tools{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin:6px 0}#ed .tools input[type=number]{width:84px!important;display:inline-block!important}
  `;

  const HTML = `
  <div id="ed"><div class="box">
    <div class="hd"><h2 id="ed_title">Editor</h2>
      <button id="ed_pv" class="sel">▶ Previsualizar</button><button id="ed_ap">Aplicar (sobrescribe)</button>
      <button id="ed_new">Guardar como nuevo</button><button id="ed_x">✕ Cerrar</button></div>
    <div class="cols">
      <div class="pv"><div class="mut" style="font-size:12px;margin-bottom:4px">Resultado (9:16)</div>
        <video id="ed_prev" controls playsinline></video><div id="ed_msg" class="mut" style="margin-top:6px;font-size:13px"></div></div>
      <div><div class="mut" style="font-size:12px;margin-bottom:4px">Fuente · recorte y cámara</div>
        <div class="sw" id="ed_sw"><video id="ed_src" playsinline muted preload="auto"></video><div class="rect" id="ed_rect" style="display:none"><i>cámara</i><b></b></div></div>
        <div class="bar2" id="ed_bar"><i id="ed_sel"></i></div>
        <div class="tools"><button id="ed_play">▶</button><input id="ed_seek" type="range" min="0" max="1" step="0.05" value="0" style="flex:1;margin:0"><span id="ed_t" class="mut">0.0</span></div>
        <div class="tools"><label class="mut">Inicio <input id="ed_ts" type="number" step="0.1" min="0"></label><button id="ed_si" title="Inicio = posición actual">⟦ aquí</button>
          <label class="mut">Fin <input id="ed_te" type="number" step="0.1" min="0"></label><button id="ed_se" title="Fin = posición actual">aquí ⟧</button><span id="ed_dur" class="mut"></span></div></div>
      <div><div class="tabs"><button data-p="lay" class="sel">Diseño</button><button data-p="sub">Subtítulos</button><button data-p="txt">Texto</button><button data-p="wd">Palabras</button></div>
        <div class="pane on" id="p_lay">
          <div class="seg" id="ed_layout"><button data-v="auto">Automático</button><button data-v="split">Cámara + gameplay</button><button data-v="fit">Completo (fondo difuminado)</button></div>
          <div id="ed_splitopts"><div class="row2"><label>Cámara en <select id="ed_cpos"><option value="top">Arriba</option><option value="bottom">Abajo</option></select></label>
            <label>Gameplay <select id="ed_gp"><option value="contain">Completo</option><option value="cover">Recortado (sigue la acción)</option></select></label></div>
            <label>Altura de la cámara: <span id="ed_chv">38</span>% <input id="ed_ch" type="range" min="15" max="60" value="38" style="width:100%;margin:0"></label>
            <div class="tools"><button id="ed_det">🔍 Autodetectar cámara</button><button id="ed_draw">Dibujar a mano</button><button id="ed_clr">Auto</button></div>
            <div class="mut" style="font-size:12px">Arrastra el rectángulo amarillo sobre la fuente para encuadrar la cámara. «Auto» la busca en cada clip.</div></div>
        </div>
        <div class="pane" id="p_sub">
          <label class="chk"><input type="checkbox" id="ed_son"> Subtítulos activados</label>
          <div class="seg" id="ed_presets"></div>
          <div class="row2"><label>Color de la palabra activa <input id="ed_hl" type="color"></label><label>Tamaño <input id="ed_size" type="number" placeholder="del estilo"></label>
            <label>Posición <select id="ed_pos"><option value="">Automática</option><option value="seam">En la unión cámara/gameplay</option><option value="center">Centro</option><option value="lower_third">Tercio inferior</option><option value="bottom">Abajo</option></select></label>
            <label>Mayúsculas <select id="ed_up"><option value="">Del estilo</option><option value="1">Sí</option><option value="0">No</option></select></label></div>
        </div>
        <div class="pane" id="p_txt">
          <label>Gancho (rótulo arriba al empezar)<input id="ed_hk" maxlength="90" placeholder="¡No te lo pierdas hasta el final!"></label>
          <label>Duración del gancho (s) <input id="ed_hks" type="number" step="0.5" min="0.5" max="15" value="3"></label>
          <label>Marca de agua / @usuario <input id="ed_wm" maxlength="40" placeholder="@micanal"></label>
          <label>Esquina <select id="ed_wmc"><option value="br">Abajo derecha</option><option value="bl">Abajo izquierda</option><option value="tr">Arriba derecha</option><option value="tl">Arriba izquierda</option></select></label>
        </div>
        <div class="pane" id="p_wd"><div class="mut" style="font-size:12px;margin-bottom:4px">Corrige el texto de los subtítulos o borra palabras. Pulsa el tiempo para ir a ese punto.</div><div class="words" id="ed_words"></div></div>
      </div>
    </div></div></div>`;

  function init() {
    if ($e('#ed')) return;
    const st = document.createElement('style'); st.textContent = CSS; document.head.appendChild(st);
    document.body.insertAdjacentHTML('beforeend', HTML);
    const T = (s) => $e(s);
    T('#ed_x').onclick = close;
    document.querySelectorAll('#ed .tabs button').forEach((b) => (b.onclick = () => {
      document.querySelectorAll('#ed .tabs button').forEach((x) => x.classList.toggle('sel', x === b));
      document.querySelectorAll('#ed .pane').forEach((p) => p.classList.toggle('on', p.id === 'p_' + b.dataset.p));
    }));
    document.querySelectorAll('#ed_layout button').forEach((b) => (b.onclick = () => { E.layout = b.dataset.v; syncLayout(); if (E.layout === 'split' && !E.rect) detect(); }));
    T('#ed_ch').oninput = () => (T('#ed_chv').textContent = T('#ed_ch').value);
    const v = T('#ed_src');
    T('#ed_play').onclick = () => { if (v.paused) { if (v.currentTime >= teVal() - 0.05 || v.currentTime < tsVal()) v.currentTime = tsVal(); v.play(); } else v.pause(); };
    v.onplay = () => (T('#ed_play').textContent = '⏸'); v.onpause = () => (T('#ed_play').textContent = '▶');
    v.ontimeupdate = () => { T('#ed_seek').value = v.currentTime; T('#ed_t').textContent = v.currentTime.toFixed(1) + ' s'; if (!v.paused && v.currentTime >= teVal()) { v.pause(); v.currentTime = tsVal(); } };
    T('#ed_seek').oninput = () => (v.currentTime = +T('#ed_seek').value);
    T('#ed_si').onclick = () => { T('#ed_ts').value = v.currentTime.toFixed(2); trimChanged(); };
    T('#ed_se').onclick = () => { T('#ed_te').value = v.currentTime.toFixed(2); trimChanged(); };
    T('#ed_ts').onchange = T('#ed_te').onchange = trimChanged;
    T('#ed_pv').onclick = () => run('preview'); T('#ed_ap').onclick = () => run('render', false); T('#ed_new').onclick = () => run('render', true);
    T('#ed_clr').onclick = () => { E.rect = null; drawRect(); };
    T('#ed_draw').onclick = () => { E.rect = E.rect || { x: 0.7, y: 0.62, w: 0.28, h: 0.36 }; drawRect(); };
    T('#ed_det').onclick = detect;
    T('#ed_hl').oninput = () => (E.hl = T('#ed_hl').value);
    T('#ed_size').oninput = () => (E.size = T('#ed_size').value ? +T('#ed_size').value : null);
    rectDrag();
  }

  const tsVal = () => +$e('#ed_ts').value || 0;
  const teVal = () => +$e('#ed_te').value || E.src.duration;
  function trimChanged() {
    const d = E.src.duration; let a = Math.max(0, Math.min(tsVal(), d - 1)), b = Math.min(d, Math.max(teVal(), a + 1));
    $e('#ed_ts').value = a.toFixed(2); $e('#ed_te').value = b.toFixed(2);
    $e('#ed_sel').style.left = (a / d * 100) + '%'; $e('#ed_sel').style.width = ((b - a) / d * 100) + '%';
    $e('#ed_dur').textContent = '= ' + (b - a).toFixed(1) + ' s (fuente ' + d.toFixed(1) + ' s)';
    drawWords();
  }

  function syncLayout() {
    document.querySelectorAll('#ed_layout button').forEach((b) => b.classList.toggle('sel', b.dataset.v === E.layout));
    $e('#ed_splitopts').style.display = E.layout === 'split' ? 'block' : 'none';
    drawRect();
  }

  function drawRect() {
    const r = $e('#ed_rect');
    if (E.layout !== 'split' || !E.rect) { r.style.display = 'none'; return; }
    r.style.display = 'block';
    Object.assign(r.style, { left: E.rect.x * 100 + '%', top: E.rect.y * 100 + '%', width: E.rect.w * 100 + '%', height: E.rect.h * 100 + '%' });
  }

  function rectDrag() {
    const r = $e('#ed_rect'), wrap = $e('#ed_sw'); let m = null;
    r.addEventListener('pointerdown', (e) => {
      const b = wrap.getBoundingClientRect();
      m = { resize: e.target.tagName === 'B', sx: e.clientX, sy: e.clientY, r0: { ...E.rect }, W: b.width, H: b.height };
      r.setPointerCapture(e.pointerId); e.preventDefault();
    });
    r.addEventListener('pointermove', (e) => {
      if (!m) return;
      const dx = (e.clientX - m.sx) / m.W, dy = (e.clientY - m.sy) / m.H, a = m.r0, c = (v, lo, hi) => Math.min(Math.max(v, lo), hi);
      if (m.resize) { E.rect.w = c(a.w + dx, 0.06, 1 - a.x); E.rect.h = c(a.h + dy, 0.06, 1 - a.y); }
      else { E.rect.x = c(a.x + dx, 0, 1 - a.w); E.rect.y = c(a.y + dy, 0, 1 - a.h); }
      drawRect();
    });
    const up = () => { m = null; if (E.rect) for (const k in E.rect) E.rect[k] = +E.rect[k].toFixed(4); };
    r.addEventListener('pointerup', up); r.addEventListener('pointercancel', up);
  }

  function drawWords() {
    const a = tsVal(), b = teVal();
    $e('#ed_words').innerHTML = E.words.map((w, i) => `<div class="w ${w.end < a || w.start > b ? 'out' : ''}"><small data-t="${w.start}">${w.start.toFixed(1)}</small>
      <input data-i="${i}" value="${esc(w.text)}"><button data-d="${i}" title="Quitar">✕</button></div>`).join('') || '<span class="mut">sin transcripción</span>';
    document.querySelectorAll('#ed_words input').forEach((x) => (x.oninput = () => (E.words[+x.dataset.i].text = x.value)));
    document.querySelectorAll('#ed_words [data-d]').forEach((x) => (x.onclick = () => { E.words.splice(+x.dataset.d, 1); drawWords(); }));
    document.querySelectorAll('#ed_words small').forEach((x) => (x.onclick = () => ($e('#ed_src').currentTime = +x.dataset.t)));
  }

  function collect() {
    const num = (id) => +$e(id).value;
    return {
      trim_start: tsVal(), trim_end: teVal(), layout: E.layout, facecam: E.rect,
      facecam_position: $e('#ed_cpos').value, facecam_height_pct: num('#ed_ch'), gameplay: $e('#ed_gp').value,
      subtitles: { enabled: $e('#ed_son').checked, preset: E.preset, highlight_color: E.hl, primary_color: null,
        font_size: E.size, position: $e('#ed_pos').value || null, uppercase: $e('#ed_up').value === '' ? null : $e('#ed_up').value === '1' },
      hook: { text: $e('#ed_hk').value, seconds: num('#ed_hks') || 3 },
      watermark: { text: $e('#ed_wm').value, corner: $e('#ed_wmc').value },
      words: E.words.map((w) => ({ start: w.start, end: w.end, text: w.text })),
    };
  }

  async function post(path, body) {
    return api(`/api/clips/${encodeURIComponent(E.name)}/${path}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  }
  async function detect() {
    msg('Buscando la cámara…');
    try { const r = await post('facecam', collect()); if (r.facecam) { E.rect = r.facecam; drawRect(); msg('Cámara detectada: ajústala arrastrando si hace falta.'); } else msg('No se detectó una cámara superpuesta. Usa «Dibujar a mano».'); }
    catch (e) { msg(e.message, true); }
  }
  function msg(t, err) { const m = $e('#ed_msg'); m.textContent = t; m.className = err ? 'err' : 'mut'; }
  function busy(on) { ['#ed_pv', '#ed_ap', '#ed_new'].forEach((s) => ($e(s).disabled = on)); }

  async function run(kind, asNew) {
    busy(true);
    try {
      const spec = collect();
      if (kind === 'preview') {
        msg('Renderizando previsualización…');
        const r = await post('preview', spec); const p = $e('#ed_prev'); p.src = r.url; p.play().catch(() => {}); msg('Previsualización lista (baja resolución).');
      } else {
        msg('Renderizando versión final…');
        const r = await post('render', { spec, as_new: !!asNew });
        msg('Listo: ' + r.name); window.lastClips = ''; if (window.refreshClips) window.refreshClips();
        setTimeout(close, 700);
      }
    } catch (e) { msg(e.message, true); }
    busy(false);
  }

  async function open(name) {
    init();
    try {
      if (!Object.keys(presets).length) presets = await api('/api/presets');
      const d = await api(`/api/clips/${encodeURIComponent(name)}/edit`); const s = d.spec;
      E = { name, src: d.source, rect: s.facecam, layout: s.layout, preset: s.subtitles.preset, hl: s.subtitles.highlight_color, size: s.subtitles.font_size, words: s.words };
      $e('#ed_title').textContent = '✂️ ' + name;
      const v = $e('#ed_src'); v.src = `/api/clips/${encodeURIComponent(name)}/source`;
      $e('#ed_seek').max = d.source.duration; $e('#ed_prev').removeAttribute('src'); $e('#ed_prev').load(); msg('');
      $e('#ed_ts').value = s.trim_start.toFixed(2); $e('#ed_te').value = (s.trim_end ?? d.source.duration).toFixed(2);
      $e('#ed_cpos').value = s.facecam_position; $e('#ed_gp').value = s.gameplay; $e('#ed_ch').value = s.facecam_height_pct; $e('#ed_chv').textContent = s.facecam_height_pct;
      $e('#ed_son').checked = s.subtitles.enabled; $e('#ed_pos').value = s.subtitles.position || ''; $e('#ed_size').value = s.subtitles.font_size || '';
      $e('#ed_up').value = s.subtitles.uppercase == null ? '' : s.subtitles.uppercase ? '1' : '0';
      $e('#ed_hk').value = s.hook.text; $e('#ed_hks').value = s.hook.seconds; $e('#ed_wm').value = s.watermark.text; $e('#ed_wmc').value = s.watermark.corner;
      const hlBox = $e('#ed_hl');
      const drawPresets = () => {
        $e('#ed_presets').innerHTML = Object.entries(presets).map(([k, p]) => `<button data-k="${k}" class="${k === E.preset ? 'sel' : ''}"><span class="chip" style="background:${p.highlight}"></span>${k}</button>`).join('');
        document.querySelectorAll('#ed_presets button').forEach((b) => (b.onclick = () => { E.preset = b.dataset.k; E.hl = null; E.size = null; $e('#ed_size').value = ''; hlBox.value = presets[E.preset].highlight; drawPresets(); }));
      };
      hlBox.value = E.hl || (presets[E.preset] || {}).highlight || '#ffe600'; drawPresets();
      syncLayout(); $e('#ed').style.display = 'block'; if (E.layout === 'split' && !E.rect) detect(); document.body.style.overflow = 'hidden';
      v.onloadedmetadata = () => { v.currentTime = tsVal(); trimChanged(); };
      if (v.readyState >= 1) { v.currentTime = tsVal(); } trimChanged();
    } catch (e) { alert('No se puede editar este clip: ' + e.message); }
  }
  function close() { const v = $e('#ed_src'); v.pause(); $e('#ed_prev').pause(); $e('#ed').style.display = 'none'; document.body.style.overflow = ''; }

  window.openEditor = open;
})();
