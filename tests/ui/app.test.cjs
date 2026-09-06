const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {execFileSync} = require('node:child_process');
const {JSDOM} = require('jsdom');
const base = require('./bootstrap.json');
const staticRoot = 'src/secretary_bot/web/static/';
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
async function screen(t, {status=200, theme='dark', handler, data=structuredClone(base)}={}) {
  const dom = new JSDOM(readFileSync(staticRoot+'index.html','utf8'), {url:'https://testserver/app/', runScripts:'outside-only', pretendToBeVisual:true});
  t.after(()=>dom.window.close());
  const w = dom.window;
  w.matchMedia = () => ({matches:theme==='light'});
  w.HTMLElement.prototype.scrollIntoView = ()=>{};
  w.confirm = ()=>true;
  w.fetch = async (path, options) => {
    if (handler) { const value = await handler(path, options); if (value) return value; }
    return {status, ok:status===200, json:async()=>status===200?data:{detail:'test error'}};
  };
  w.eval(readFileSync(staticRoot+'ui-utils.js','utf8'));
  w.eval(readFileSync(staticRoot+'app.js','utf8'));
  await tick(); await tick();
  return w;
}
function response(data,status=200){return {status,ok:status===200,json:async()=>data};}
function contact(id){return {contact_id:id,contact_name:'Контакт '+id,exclusion:'none',windows:[],auto_reply_count:0,preview_count:0,paid_escalation_count:0,off_hours_request_count:0};}

test('server failure offers retry rather than blaming authentication',async t=>{
  const w=await screen(t,{status:503});
  assert.equal(w.document.querySelector('#load-error').classList.contains('hidden'),false);
  assert.equal(w.document.querySelector('#auth-state').classList.contains('hidden'),true);
});
test('expired session shows authentication guidance',async t=>{
  const w=await screen(t,{status:401});
  assert.equal(w.document.querySelector('#auth-state').classList.contains('hidden'),false);
});
test('pause overrides live badge, and bot range matches the actual 60-second cap',async t=>{
  const data=structuredClone(base); data.connection.dry_run=false;
  data.status={...data.status,code:'paused',label:'Тимчасова пауза'};
  data.delivery.delay_max_seconds=120;
  const w=await screen(t,{data});
  assert.equal(w.document.querySelector('#operating-title').textContent,'Тимчасова пауза');
  assert.equal(w.document.querySelector('#bot-delay-range').textContent,'5–60 с');
  assert.equal(w.document.querySelector('#connection-pill').textContent.includes('Активний'),false);
});
test('switching contacts preserves edits when discard is declined',async t=>{
  const w=await screen(t,{handler:path=>path.startsWith('/api/v1/contacts')?response({items:[contact(100),contact(101)],has_more:false}):null});
  w.document.querySelector('[data-view=contacts]').click(); await tick();
  w.document.querySelector('[data-contact-id="100"]').click();
  const field=w.document.querySelector('[name=exclusion_until]'); field.value='2026-12-01T12:30';field.dispatchEvent(new w.Event('input',{bubbles:true}));
  w.confirm=()=>false;
  w.document.querySelector('[data-contact-id="101"]').click();
  assert.equal(w.document.querySelector('#contact-title').textContent,'Контакт 100');
  assert.equal(field.value,'2026-12-01T12:30');
});
test('saving a contact clears dirty state without a discard confirmation',async t=>{
  let saved;
  const w=await screen(t,{handler:(path,options)=>{
    if(path==='/api/v1/contacts/100'){saved=JSON.parse(options.body);return response({...contact(100),...saved});}
    if(path.startsWith('/api/v1/contacts'))return response({items:[contact(100)],has_more:false});
  }});
  w.document.querySelector('[data-view=contacts]').click(); await tick();
  w.document.querySelector('[data-contact-id="100"]').click();
  const form=w.document.querySelector('#contact-form');
  form.elements.exclusion.value='forever';form.dispatchEvent(new w.Event('input',{bubbles:true}));
  w.confirm=()=>{throw new Error('unexpected discard prompt');};
  form.dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true})); await tick();await tick();
  assert.equal(saved.exclusion,'forever');
  assert.equal(form.dataset.dirty,'false');
});
test('adding a schedule window marks the form dirty and enables explicit discard',async t=>{
  const w=await screen(t);
  w.document.querySelector('#add-schedule-window').click();
  const form=w.document.querySelector('#schedule-form');
  assert.equal(form.dataset.dirty,'true');
  form.querySelector('.discard-changes').click();
  assert.equal(form.dataset.dirty,'false');
  assert.equal(form.querySelectorAll('.window-row').length,base.schedule.windows.length);
});
test('validation stays visible and identifies the invalid field',async t=>{
  const w=await screen(t,{handler:path=>path==='/api/v1/delivery'?response({detail:[{loc:['body','delay_min_seconds'],msg:'Завеликий мінімум'}]},422):null});
  const form=w.document.querySelector('#delivery-form');
  form.dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));await tick();await tick();
  assert.equal(form.elements.delay_min_seconds.getAttribute('aria-invalid'),'true');
  assert.match(form.querySelector('.field-error').textContent,/Завеликий мінімум/);
});
test('light theme is explicitly selected',async t=>{
  const w=await screen(t,{theme:'light'});
  assert.equal(w.document.documentElement.dataset.theme,'light');
});
test('date round-trip respects device timezone across summer and winter',()=>{
  for(const tz of ['UTC','Europe/Prague','Europe/Kyiv']) {
    const code=`const ui=require('./${staticRoot}ui-utils.js'); for(const v of ['2026-09-06T18:00:00.000Z','2026-01-06T18:00:00.000Z','2026-03-29T04:30:00.000Z']) if(ui.utcDateTime(ui.localDateTime(v))!==v) throw Error(v);`;
    execFileSync(process.execPath,['-e',code],{env:{...process.env,TZ:tz}});
  }
});

// --- Review of the uncommitted work (docs/uncommitted-review-2026-09-05.md) ----

test('R2: abandoned notifications are shown with a retry action',async t=>{
  const data=structuredClone(base); data.status={...data.status,failed_notifications:2,last_error:'NOTIFICATION_FAILED'};
  let retried=false;
  const w=await screen(t,{data,handler:path=>{ if(path==='/api/v1/notifications/retry'){retried=true;return response({...data,status:{...data.status,failed_notifications:0,pending_notifications:2}});} }});
  const button=w.document.querySelector('#retry-notifications');
  assert.equal(button.classList.contains('hidden'),false);
  assert.match(w.document.querySelector('#operating-history').textContent,/Недоставлені сповіщення: 2/);
  assert.match(w.document.querySelector('#operating-history').textContent,/не доставлено/);
  button.click(); await tick(); await tick();
  assert.equal(retried,true);
  assert.equal(button.classList.contains('hidden'),true);
});
test('R3: rule preview says it ignores unsaved contact edits and names the template',async t=>{
  let previewBody;
  const w=await screen(t,{handler:(path,options)=>{
    if(path==='/api/v1/preview'){previewBody=JSON.parse(options.body);return response({decision:'allowed',category:'general',template_code:'money_priority',forced_template:true,text:'x',dry_run:true,timezone:'Europe/Kyiv',source:'keywords',personal_schedule:false});}
    if(path.startsWith('/api/v1/contacts'))return response({items:[contact(100)],has_more:false});
  }});
  w.document.querySelector('[data-view=contacts]').click(); await tick();
  w.document.querySelector('[data-contact-id="100"]').click();
  const form=w.document.querySelector('#contact-form');
  form.elements.exclusion.value='forever';form.dispatchEvent(new w.Event('input',{bubbles:true}));
  const preview=w.document.querySelector('#preview-form');
  preview.elements.text.value='Привіт';
  preview.dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));await tick();await tick();
  assert.equal(previewBody.contact_id,100);
  const text=w.document.querySelector('#preview-result').textContent;
  assert.match(text,/незбережені зміни контакту не враховано/);
  assert.match(text,/персональний для контакту/);
});
test('R4: discarding contact edits after a search restores the card from its snapshot',async t=>{
  const w=await screen(t,{handler:path=>{
    if(path.startsWith('/api/v1/contacts?search=101'))return response({items:[contact(101)],has_more:false});
    if(path.startsWith('/api/v1/contacts'))return response({items:[contact(100),contact(101)],has_more:false});
  }});
  w.document.querySelector('[data-view=contacts]').click(); await tick();
  w.document.querySelector('[data-contact-id="100"]').click();
  const form=w.document.querySelector('#contact-form');
  form.elements.exclusion.value='forever';form.dispatchEvent(new w.Event('input',{bubbles:true}));
  const search=w.document.querySelector('#contact-search');search.value='101';search.dispatchEvent(new w.Event('input',{bubbles:true}));
  await new Promise(resolve=>setTimeout(resolve,320));await tick();
  assert.equal(w.document.querySelector('[data-contact-id="100"]'),null);
  form.querySelector('.discard-changes').click();
  assert.equal(form.dataset.dirty,'false');
  assert.equal(form.elements.exclusion.value,'none');
  assert.equal(w.document.querySelector('#contact-title').textContent,'Контакт 100');
});
test('R5: every colour outside theme variables comes from a variable',()=>{
  const css=readFileSync(staticRoot+'styles.css','utf8').split('\n');
  const raw=css.filter(line=>{const rest=line.replace(/--[\w-]+:\s*[^;]+;/g,'');return /#[0-9a-fA-F]{6}\b/.test(rest)||rest.includes('rgba(20, 30, 43');});
  assert.deepEqual(raw,[]);
  assert.match(readFileSync(staticRoot+'styles.css','utf8'),/label \{[^}]*color: var\(--label\)/);
});
test('R7: an untouched exclusion keeps its instant across the repeated autumn hour',()=>{
  const code=`const ui=require('./${staticRoot}ui-utils.js');
    const original='2026-10-25T01:30:00.000Z'; const field=ui.localDateTime(original);
    const kept=ui.resolveDateTime(field, original); if(kept!==original) throw Error('unchanged field lost its instant: '+kept);
    const changed=ui.resolveDateTime('2026-10-25T03:30', original); if(changed!=='2026-10-25T02:30:00.000Z') throw Error('changed field: '+changed);
    if(ui.resolveDateTime('', original)!==null) throw Error('empty field must clear the date');`;
  execFileSync(process.execPath,['-e',code],{env:{...process.env,TZ:'Europe/Prague'}});
});

test('saving the global schedule refreshes the open contact without losing its draft', async t => {
  const w = await screen(t, {handler: (path, options) => {
    if (path === '/api/v1/schedule') return response(JSON.parse(options.body));
    if (path.startsWith('/api/v1/contacts')) return response({items:[contact(100)],has_more:false});
  }});
  const d = w.document;
  d.querySelector('[data-view=contacts]').click(); await tick();
  d.querySelector('[data-contact-id="100"]').click();
  const contactForm = d.querySelector('#contact-form');
  contactForm.elements.exclusion.value = 'forever';
  contactForm.dispatchEvent(new w.Event('input', {bubbles:true}));
  const saveSchedule = async start => {
    d.querySelector('#schedule-windows .time-from').value = start;
    d.querySelector('#timezone-select').value = 'Europe/Prague';
    d.querySelector('#schedule-form').dispatchEvent(new w.Event('submit', {bubbles:true,cancelable:true}));
    await tick(); await tick();
  };
  await saveSchedule('21:15');
  assert.match(d.querySelector('#contact-schedule-preview').textContent, /21:15/);
  assert.match(d.querySelector('#contact-meta').textContent, /Розклад — Europe\/Prague/);
  assert.equal(contactForm.elements.exclusion.value, 'forever');
  assert.equal(contactForm.dataset.dirty, 'true');
  d.querySelector('#add-contact-window').click();
  const personalStart = d.querySelector('#contact-windows .time-from');
  personalStart.value = '13:30';
  await saveSchedule('23:00');
  assert.equal(personalStart.isConnected, true);
  assert.equal(personalStart.value, '13:30');
  assert.equal(contactForm.dataset.dirty, 'true');
  assert.equal(d.querySelector('#contact-schedule-preview').classList.contains('hidden'), true);
});
