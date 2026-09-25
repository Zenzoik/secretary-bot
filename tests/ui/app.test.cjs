const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {execFileSync} = require('node:child_process');
const {JSDOM} = require('jsdom');
const base = require('./bootstrap.json');
const staticRoot = 'src/secretary_bot/web/static/';
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
async function screen(t, {status=200, theme='dark', handler, data=structuredClone(base), url='https://testserver/app/'}={}) {
  const dom = new JSDOM(readFileSync(staticRoot+'index.html','utf8'), {url, runScripts:'outside-only', pretendToBeVisual:true});
  t.after(()=>dom.window.close());
  const w = dom.window;
  w.matchMedia = () => ({matches:theme==='light'});
  w.HTMLElement.prototype.scrollIntoView = ()=>{};
  w.scrollTo = ()=>{};
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
function contact(id){return {contact_id:id,contact_name:'Контакт '+id,configured:true,exclusion:'none',windows:[],auto_reply_count:0,preview_count:0,paid_escalation_count:0,off_hours_request_count:0};}

test('Telegram SDK cannot block rendering and launch data still authenticates',async t=>{
  const html=readFileSync(staticRoot+'index.html','utf8');
  assert.match(html, /id="telegram-web-app-sdk"[^>]+async/);
  const initData='query_id=test&user=%7B%22id%22%3A42%7D&hash=signed';
  let auth;
  const w=await screen(t,{url:`https://testserver/app/#tgWebAppData=${encodeURIComponent(initData)}&tgWebAppVersion=9.1`,handler:(path,options)=>{
    if(path==='/api/v1/bootstrap') auth=options.headers['X-Telegram-Init-Data'];
  }});
  assert.equal(auth,initData);
  assert.equal(w.document.querySelector('#views').classList.contains('hidden'),false);
});

test('server failure offers retry rather than blaming authentication',async t=>{
  const w=await screen(t,{status:503});
  assert.equal(w.document.querySelector('#load-error').classList.contains('hidden'),false);
  assert.equal(w.document.querySelector('#auth-state').classList.contains('hidden'),true);
});
test('expired session shows authentication guidance',async t=>{
  const w=await screen(t,{status:401});
  assert.equal(w.document.querySelector('#auth-state').classList.contains('hidden'),false);
});
test('approved user sees the connection step before settings unlock',async t=>{
  const w=await screen(t,{status:409});
  assert.equal(w.document.querySelector('#load-error h2').textContent,'Завершіть підключення');
  assert.match(w.document.querySelector('#load-error-message').textContent,/Chat Automation/);
});
test('master can create an invite and approve a pending user in the mini app',async t=>{
  const calls=[];
  const w=await screen(t,{handler:(path,options)=>{
    if(path==='/api/v1/access/users') return response({users:[
      {user_id:42,role:'master',status:'active',display_name:'Owner'},
      {user_id:99,role:'user',status:'pending',display_name:'Candidate'}
    ]});
    if(path==='/api/v1/access/invites') return response({url:'https://t.me/test_bot?start=invite_token'});
    if(path==='/api/v1/access/users/99/approve'){
      calls.push(options.method);return response({approved:true,notified:true});
    }
  }});
  const nav=w.document.querySelector('#users-nav');
  assert.equal(nav.classList.contains('hidden'),false);
  nav.click(); await tick();
  assert.match(w.document.querySelector('#access-users').textContent,/Candidate/);
  w.document.querySelector('#create-invite').click(); await tick();
  assert.equal(w.document.querySelector('#invite-url').value,'https://t.me/test_bot?start=invite_token');
  w.document.querySelector('[data-access-action=approve]').click(); await tick(); await tick();
  assert.deepEqual(calls,['POST']);
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
test('a new contact is marked until its rules are saved',async t=>{
  const w=await screen(t,{handler:(path,options)=>{
    if(path==='/api/v1/contacts/101')return response({...contact(101),...JSON.parse(options.body)});
    if(path.startsWith('/api/v1/contacts'))return response({items:[{...contact(101),configured:false},contact(100)],has_more:false});
  }});
  w.document.querySelector('[data-view=contacts]').click(); await tick();
  const item=w.document.querySelector('[data-contact-id="101"]');
  assert.ok(item.classList.contains('needs-setup'));
  assert.match(item.textContent,/Не налаштовано/);
  assert.ok(!w.document.querySelector('[data-contact-id="100"]').classList.contains('needs-setup'));
  item.click();
  assert.ok(!w.document.querySelector('#contact-setup-note').classList.contains('hidden'));
  w.document.querySelector('#contact-form').requestSubmit(); await tick(); await tick();
  assert.ok(w.document.querySelector('#contact-setup-note').classList.contains('hidden'));
  assert.ok(!w.document.querySelector('[data-contact-id="101"]').classList.contains('needs-setup'));
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
  form.elements.delay_max_seconds.value='90'; form.elements.delay_max_seconds.dispatchEvent(new w.Event('input',{bubbles:true}));
  form.dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));await tick();await tick();
  assert.equal(form.elements.delay_min_seconds.getAttribute('aria-invalid'),'true');
  assert.match(form.querySelector('.field-error').textContent,/Завеликий мінімум/);
});
test('light theme is explicitly selected',async t=>{
  const w=await screen(t,{theme:'light'});
  assert.equal(w.document.documentElement.dataset.theme,'light');
});
test('the action log is timestamped in the bot timezone and says so',async t=>{
  const data=structuredClone(base); data.schedule.timezone='Pacific/Auckland';
  const occurred='2026-09-06T19:16:09.000Z';
  const w=await screen(t,{data,handler:path=>{
    if(path.startsWith('/api/v1/contacts')) return response({items:[contact(100)],has_more:false,next_offset:1});
    if(path.startsWith('/api/v1/logs')) return response({items:[{occurred_at:occurred,contact_label:'@poldotk',
      action:'skipped_window_limit',category:null,error_code:null,template_code:null}],has_more:false,next_offset:1});
  }});
  w.document.querySelector('[data-view=logs]').click(); await tick(); await tick();
  const expected=new Intl.DateTimeFormat('uk-UA',{dateStyle:'short',timeStyle:'short',timeZone:'Pacific/Auckland'}).format(new Date(occurred));
  assert.equal(w.document.querySelector('#log-rows td').textContent,expected);
  assert.match(w.document.querySelector('#log-timezone').textContent,/Pacific\/Auckland/);
});
test('date round-trip uses the bot timezone whatever the device is set to',()=>{
  for(const device of ['UTC','Europe/Prague','Pacific/Auckland']) {
    const code=`const ui=require('./${staticRoot}ui-utils.js');
      for(const v of ['2026-09-06T18:00:00.000Z','2026-01-06T18:00:00.000Z','2026-03-29T04:30:00.000Z'])
        if(ui.utcDateTime(ui.zonedDateTime(v,'Europe/Kyiv'),'Europe/Kyiv')!==v) throw Error(v);
      // Kyiv is three hours ahead of UTC in September, and the field must say so
      // no matter where the owner's device is.
      if(ui.zonedDateTime('2026-09-06T18:00:00.000Z','Europe/Kyiv')!=='2026-09-06T21:00') throw Error('wrong wall clock');`;
    execFileSync(process.execPath,['-e',code],{env:{...process.env,TZ:device}});
  }
});

// --- Review of the uncommitted work (docs/uncommitted-review-2026-09-05.md) ----

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
  const code=`const ui=require('./${staticRoot}ui-utils.js'); const zone='Europe/Prague';
    const original='2026-10-25T01:30:00.000Z'; const field=ui.zonedDateTime(original, zone);
    const kept=ui.resolveDateTime(field, original, zone); if(kept!==original) throw Error('unchanged field lost its instant: '+kept);
    const changed=ui.resolveDateTime('2026-10-25T03:30', original, zone); if(changed!=='2026-10-25T02:30:00.000Z') throw Error('changed field: '+changed);
    if(ui.resolveDateTime('', original, zone)!==null) throw Error('empty field must clear the date');`;
  execFileSync(process.execPath,['-e',code],{env:{...process.env,TZ:'UTC'}});
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
    d.querySelector('#schedule-form').dispatchEvent(new w.Event('input', {bubbles:true}));
    d.querySelector('#schedule-form').dispatchEvent(new w.Event('submit', {bubbles:true,cancelable:true}));
    await tick(); await tick();
  };
  await saveSchedule('21:15');
  assert.match(d.querySelector('#contact-schedule-preview').textContent, /21:15/);
  assert.match(d.querySelector('#exclusion-zone').textContent, /Europe\/Prague/);
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

test('templates tab is gone: built-in types show their reply, old links land on the types', async t => {
  const w = await screen(t, {url:'https://testserver/app/#templates'});
  const doc = w.document;
  assert.equal(doc.querySelector('[data-view="templates"]'), null);
  assert.equal(doc.querySelector('#templates-form'), null);
  assert.equal(doc.querySelector('[data-view-panel="classifier"]').classList.contains('active'), true);
  assert.equal(doc.querySelector('[data-code="general"] .direction-template').value, base.templates.off_hours_default);
  assert.equal(doc.querySelector('[data-code="money"] .direction-template').value, base.templates.money_priority);
});

test('pause shows on the home screen, and the bot range matches the actual 60-second cap',async t=>{
  const data=structuredClone(base); data.connection.dry_run=false;
  data.status={...data.status,code:'paused',label:'Тимчасова пауза',muted_until:'2026-09-05T20:30:00Z'};
  data.delivery.delay_max_seconds=120;
  const w=await screen(t,{data});
  const d=w.document;
  assert.equal(d.querySelector('#operating-title').textContent,'Пауза до 23:30');
  assert.equal(d.querySelector('#status-dot').dataset.state,'paused');
  assert.equal(d.querySelector('[data-mode=live]').getAttribute('aria-pressed'),'true');
  assert.equal(d.querySelector('#pause-toggle').textContent,'Зняти паузу');
  assert.equal(d.querySelector('#bot-delay-range').textContent,'5–60 с');
});

test('the three modes map onto the existing control actions',async t=>{
  const calls=[];
  const data=structuredClone(base);
  const w=await screen(t,{data,handler:(path,options)=>{
    if(path!=='/api/v1/control')return null;
    const {action}=JSON.parse(options.body); calls.push(action);
    const c=data.connection;
    if(action==='stop')c.kill_switch=true; if(action==='resume')c.kill_switch=false;
    if(action==='dry_run')c.dry_run=true; if(action==='live')c.dry_run=false;
    data.status={...data.status,code:c.kill_switch?'stopped':c.dry_run?'dry_run':'live'};
    return response(structuredClone(data));
  }});
  const d=w.document; const click=async mode=>{d.querySelector(`[data-mode=${mode}]`).click();await tick();await tick();await tick();};
  assert.equal(d.querySelector('[data-mode=test]').getAttribute('aria-pressed'),'true');
  await click('off');
  assert.deepEqual(calls,['stop']);
  assert.equal(d.querySelector('#operating-title').textContent,'Вимкнено');
  assert.equal(d.querySelector('#pause-toggle').classList.contains('hidden'),true);
  await click('test');
  assert.deepEqual(calls,['stop','dry_run','resume']);
  w.confirm=()=>false; await click('live');
  assert.equal(calls.length,3,'live needs an explicit confirmation');
  w.confirm=()=>true; await click('live');
  assert.deepEqual(calls.slice(3),['live']);
  assert.equal(d.querySelector('[data-mode=live]').getAttribute('aria-pressed'),'true');
  await click('live');
  assert.equal(calls.length,4,'choosing the current mode sends nothing');
});

test('R2: abandoned notifications are shown with a retry action',async t=>{
  const data=structuredClone(base); data.status={...data.status,failed_notifications:2,last_error:'NOTIFICATION_FAILED'};
  let retried=false;
  const w=await screen(t,{data,handler:path=>{ if(path==='/api/v1/notifications/retry'){retried=true;return response({...data,status:{...data.status,failed_notifications:0,pending_notifications:2}});} }});
  const attention=w.document.querySelector('#attention');
  assert.equal(attention.classList.contains('hidden'),false);
  assert.match(attention.textContent,/Сповіщення не доставлено\s*2/);
  w.document.querySelector('#retry-notifications').click(); await tick(); await tick();
  assert.equal(retried,true);
  assert.equal(w.document.querySelector('#retry-notifications'),null);
});

test('R3: rule preview says it ignores unsaved contact edits and names the template',async t=>{
  let previewBody;
  const w=await screen(t,{handler:(path,options)=>{
    if(path==='/api/v1/preview'){previewBody=JSON.parse(options.body);return response({decision:'allowed',category:'general',template_code:'money_priority',forced_template:true,text:'x',dry_run:true,timezone:'Europe/Kyiv',source:'keywords',personal_schedule:false});}
    if(path.startsWith('/api/v1/contacts'))return response({items:[contact(100)],has_more:false});
  }});
  const d=w.document;
  d.querySelector('#navigation [data-view=contacts]').click(); await tick();
  d.querySelector('[data-contact-id="100"]').click();
  const form=d.querySelector('#contact-form');
  form.elements.exclusion.value='forever';form.dispatchEvent(new w.Event('input',{bubbles:true}));
  d.querySelector('#navigation [data-view=more]').click();
  d.querySelector('[data-view=check]').click();
  assert.match(d.querySelector('#preview-scope').textContent,/Контакт 100/);
  const preview=d.querySelector('#preview-form');
  preview.elements.text.value='Привіт';
  preview.dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));await tick();await tick();
  assert.equal(previewBody.contact_id,100);
  const text=d.querySelector('#preview-result').textContent;
  assert.match(text,/Незбережені зміни контакту не враховано/);
  assert.match(text,/персональний для контакту/);
});

test('pages open from "Ще" and the back button returns there',async t=>{
  const w=await screen(t);
  const d=w.document;
  assert.equal(d.querySelector('#back-button').classList.contains('hidden'),true);
  d.querySelector('#navigation [data-view=more]').click();
  d.querySelector('.list-row[data-view=escalation]').click();
  assert.equal(d.querySelector('[data-view-panel=escalation]').classList.contains('active'),true);
  assert.equal(d.querySelector('#page-title').textContent,'Платні звернення');
  assert.equal(d.querySelector('#navigation [data-view=more]').classList.contains('active'),true);
  assert.equal(d.querySelector('#back-button').classList.contains('hidden'),false);
  d.querySelector('#back-button').click();
  assert.equal(d.querySelector('[data-view-panel=more]').classList.contains('active'),true);
  assert.equal(d.querySelector('#escalation-badge').textContent,'Вимкнено');
  d.querySelector('#navigation [data-view=overview]').click();
  d.querySelector('.list-row[data-view=schedule]').click();
  d.querySelector('#back-button').click();
  assert.equal(d.querySelector('[data-view-panel=overview]').classList.contains('active'),true,'back returns to the tab it came from');
});

test('an open contact replaces the list on a phone and back closes it',async t=>{
  const w=await screen(t,{handler:path=>path.startsWith('/api/v1/contacts')?response({items:[contact(100)],has_more:false}):null});
  const d=w.document;
  d.querySelector('#navigation [data-view=contacts]').click(); await tick();
  d.querySelector('[data-contact-id="100"]').click();
  assert.equal(d.querySelector('#contact-layout').classList.contains('editing'),true);
  assert.equal(d.querySelector('#page-title').textContent,'Контакт 100');
  assert.equal(d.querySelector('#contact-form .exclusion-until').classList.contains('hidden'),true);
  const form=d.querySelector('#contact-form');
  form.elements.exclusion.value='until'; form.elements.exclusion[1].dispatchEvent(new w.Event('change',{bubbles:true}));
  assert.equal(d.querySelector('#contact-form .exclusion-until').classList.contains('hidden'),false);
  w.confirm=()=>true;
  d.querySelector('#back-button').click();
  assert.equal(d.querySelector('#contact-layout').classList.contains('editing'),false);
  assert.equal(d.querySelector('#page-title').textContent,'Контакти');
});

test('delivery shows only the minimum of the chosen sender',async t=>{
  const w=await screen(t);
  const form=w.document.querySelector('#delivery-form');
  const visible=name=>!form.elements[name].closest('label').classList.contains('hidden');
  assert.equal(visible('bot_delay_seconds'),true);
  assert.equal(visible('delay_min_seconds'),false);
  form.elements.sender_identity.value='owner';
  form.querySelector('[value=owner]').dispatchEvent(new w.Event('change',{bubbles:true}));
  assert.equal(visible('bot_delay_seconds'),false);
  assert.equal(visible('delay_min_seconds'),true);
});

test('save bar appears only with changes, except for a new contact',async t=>{
  const w=await screen(t);
  const form=w.document.querySelector('#schedule-form');
  assert.equal(form.dataset.dirty===undefined||form.dataset.dirty==='false',true);
  assert.equal(form.classList.contains('needs-save'),false);
  w.document.querySelector('#add-schedule-window').click();
  assert.equal(form.dataset.dirty,'true');
});

function classifierScreen(t, calls, {expand}={}) {
  return screen(t, {handler: (path, options) => {
    if (path === '/api/v1/classifier/expand') {
      const body = JSON.parse(options.body); calls.push(['expand', body]);
      if (expand) return expand(body);
      return response({system_prompt:'Новий майстер-промпт для всіх типів звернень.', directions: body.directions.map(d => ({code:d.code,keywords:d.code==='general'?[]:d.code==='money'?['оплата']:['увійти','авторизац']}))});
    }
    if (path === '/api/v1/classifier') { const body = JSON.parse(options.body); calls.push(['save', body]); return response(body); }
  }});
}

test('saving changed types refreshes the AI rules first, then saves once', async t => {
  const calls = [];
  const w = await classifierScreen(t, calls);
  const doc = w.document;
  doc.querySelector('#add-direction').click();
  const card = [...doc.querySelectorAll('.direction-card')].at(-1);
  assert.equal(doc.activeElement, card.querySelector('.direction-template'));
  assert.equal(card.querySelector('.direction-more').open, true);
  assert.equal(card.querySelector('.direction-priority'), null);
  card.querySelector('.direction-label').value = 'Підтримка';
  card.querySelector('.direction-description').value = 'Помилки в роботі';
  card.querySelector('.direction-template').value = 'Перевірю';
  const form = doc.querySelector('#classifier-form');
  assert.equal(form.dataset.dirty, 'true');
  assert.equal(form.dataset.promptStale, 'true');
  assert.equal(doc.querySelector('#expand-classifier'), null);
  form.dispatchEvent(new w.Event('submit', {bubbles:true,cancelable:true}));
  await tick(); await tick(); await tick();
  assert.deepEqual(calls.map(([kind]) => kind), ['expand', 'save']);
  const saved = calls[1][1];
  assert.equal(saved.directions.length, 3);
  assert.equal(saved.directions[2].reply_template, 'Перевірю');
  assert.equal('priority' in saved.directions[2], false);
  assert.deepEqual(saved.directions[2].keywords, ['увійти','авторизац']);
  assert.equal(saved.directions[0].reply_template, base.templates.off_hours_default);
  assert.match(saved.system_prompt, /Новий майстер/);
  assert.equal(form.dataset.dirty, 'false');
});

test('editing only a reply saves without touching the AI rules', async t => {
  const calls = [];
  const w = await classifierScreen(t, calls);
  const doc = w.document;
  const reply = doc.querySelector('[data-code="general"] .direction-template');
  reply.value = 'Відповім зранку';
  reply.dispatchEvent(new w.Event('input', {bubbles:true}));
  const form = doc.querySelector('#classifier-form');
  assert.equal(form.dataset.promptStale, 'false');
  form.dispatchEvent(new w.Event('submit', {bubbles:true,cancelable:true}));
  await tick(); await tick();
  assert.deepEqual(calls.map(([kind]) => kind), ['save']);
  assert.equal(calls[0][1].directions[0].reply_template, 'Відповім зранку');
});

test('a type cannot be saved without a name, description and reply', async t => {
  const calls = [];
  const w = await classifierScreen(t, calls);
  const doc = w.document;
  doc.querySelector('#add-direction').click();
  const card = [...doc.querySelectorAll('.direction-card')].at(-1);
  const form = doc.querySelector('#classifier-form');
  form.dispatchEvent(new w.Event('submit', {bubbles:true,cancelable:true})); await tick();
  assert.equal(calls.length, 0);
  assert.match(doc.querySelector('#toast').textContent, /назву, опис і відповідь/);
  card.querySelector('.direction-label').value = 'Підтримка';
  card.querySelector('.direction-description').value = 'Проблеми зі входом';
  const reply = card.querySelector('.direction-template');
  reply.value = '   ';
  card.querySelector('.direction-more').open = false;
  form.dispatchEvent(new w.Event('submit', {bubbles:true,cancelable:true})); await tick();
  assert.equal(calls.length, 0);
  assert.equal(doc.activeElement, reply);
  assert.match(doc.querySelector('#toast').textContent, /Додайте відповідь клієнту/);
  assert.equal(doc.querySelector('[data-code="general"] .direction-template').required, true);
});

test('a failed AI refresh saves only with consent and keeps the manual instruction', async t => {
  const calls = [];
  const w = await classifierScreen(t, calls, {expand: () => response({detail:'ШІ недоступний'}, 503)});
  const doc = w.document;
  const form = doc.querySelector('#classifier-form');
  form.elements.system_prompt.value = 'Моя вручну відредагована інструкція';
  const toggle = doc.querySelector('[data-code="money"] .direction-active');
  toggle.click();
  // First the owner agrees to replace the hand-edited instruction, then declines
  // to save once the AI call has failed.
  const answers = [true, false]; w.confirm = () => answers.shift();
  form.dispatchEvent(new w.Event('submit', {bubbles:true,cancelable:true})); await tick(); await tick(); await tick();
  assert.deepEqual(calls.map(([kind]) => kind), ['expand']);
  assert.equal(form.elements.system_prompt.value, 'Моя вручну відредагована інструкція');
  assert.equal(form.dataset.dirty, 'true');
  assert.equal(doc.querySelector('#save-classifier').textContent, 'Зберегти');
  w.confirm = () => true;
  form.dispatchEvent(new w.Event('submit', {bubbles:true,cancelable:true})); await tick(); await tick(); await tick();
  assert.deepEqual(calls.map(([kind]) => kind), ['expand', 'expand', 'save']);
  assert.equal(calls[2][1].system_prompt, 'Моя вручну відредагована інструкція');
  assert.equal(calls[2][1].directions[1].is_active, false);
});

test('each type has a clear on/off switch and general cannot be switched off', async t => {
  const w = await screen(t);
  const doc = w.document;
  assert.equal(doc.querySelector('[data-code="general"] .direction-active'), null);
  const money = doc.querySelector('[data-code="money"]');
  assert.equal(money.querySelector('.direction-title').textContent, 'Гроші');
  const toggle = money.querySelector('.direction-active');
  assert.match(toggle.getAttribute('aria-label'), /Гроші/);
  toggle.click();
  assert.equal(money.classList.contains('is-off'), true);
  assert.equal(doc.querySelector('#classifier-form').dataset.promptStale, 'true');
  const label = money.querySelector('.direction-label');
  label.value = 'Оплати';
  label.dispatchEvent(new w.Event('input', {bubbles:true}));
  assert.equal(money.querySelector('.direction-title').textContent, 'Оплати');
});

// --- Adversarial review of the minimal redesign -------------------------------

test('after pausing, the button offers to lift the pause, and controls wait for each other',async t=>{
  const data=structuredClone(base); data.connection.dry_run=false; data.status={...data.status,code:'live'};
  const calls=[]; let release;
  const w=await screen(t,{data,handler:async(path,options)=>{
    if(path!=='/api/v1/control')return null;
    calls.push(JSON.parse(options.body).action);
    await new Promise(resolve=>{release=resolve;});
    data.connection.muted_until='2026-09-05T20:30:00Z';
    data.status={...data.status,code:'paused',muted_until:'2026-09-05T20:30:00Z'};
    return response(structuredClone(data));
  }});
  const d=w.document; const pause=d.querySelector('#pause-toggle');
  assert.equal(pause.textContent,'Пауза на 1 год');
  pause.click(); await tick();
  assert.equal(d.querySelector('[data-mode=off]').disabled,true,'other controls wait');
  d.querySelector('[data-mode=off]').click(); pause.click(); await tick();
  assert.deepEqual(calls,['pause']);
  release(); await tick(); await tick(); await tick();
  assert.equal(pause.textContent,'Зняти паузу');
  assert.equal(pause.dataset.control,'resume');
  assert.equal(d.querySelector('[data-mode=off]').disabled,false);
});

test('without the reply right the owner can still switch the bot off or to test',async t=>{
  const data=structuredClone(base); data.connection.rights={can_reply:false};
  data.status={...data.status,code:'inactive'};
  const w=await screen(t,{data});
  const d=w.document;
  assert.equal(d.querySelector('[data-mode=off]').disabled,false);
  assert.equal(d.querySelector('[data-mode=test]').disabled,false);
  assert.equal(d.querySelector('[data-mode=live]').disabled,true);
  assert.match(d.querySelector('#attention').textContent,/Немає права відповідати/);
});

test('the hidden sender minimum never blocks saving and is clamped for the server',async t=>{
  let saved;
  const w=await screen(t,{handler:(path,options)=>{ if(path==='/api/v1/delivery'){saved=JSON.parse(options.body);return response(saved);} }});
  const form=w.document.querySelector('#delivery-form');
  form.elements.bot_delay_seconds.value='75';
  form.elements.sender_identity.value='owner';
  form.querySelector('[value=owner]').dispatchEvent(new w.Event('change',{bubbles:true}));
  assert.equal(form.elements.bot_delay_seconds.disabled,true);
  assert.equal(form.elements.delay_min_seconds.disabled,false);
  form.elements.delay_max_seconds.value='30';
  assert.equal(form.checkValidity(),true);
  form.dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true})); await tick(); await tick();
  assert.equal(saved.sender_identity,'owner');
  assert.equal(saved.bot_delay_seconds,30);
  assert.equal(saved.delay_max_seconds,30);
});

test('the types form is locked while its rules are regenerated and saved',async t=>{
  let release; const calls=[];
  const w=await screen(t,{handler:async(path,options)=>{
    if(path==='/api/v1/classifier/expand'){calls.push('expand'); await new Promise(r=>{release=r;}); const body=JSON.parse(options.body);
      return response({system_prompt:'Нова інструкція для всіх типів звернень.',directions:body.directions.map(d=>({code:d.code,keywords:[]}))});}
    if(path==='/api/v1/classifier'){calls.push('save'); return response(JSON.parse(options.body));}
  }});
  const doc=w.document; const form=doc.querySelector('#classifier-form');
  doc.querySelector('[data-code="money"] .direction-active').click();
  form.dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true})); await tick();
  assert.equal(form.inert,true);
  release(); await tick(); await tick(); await tick(); await tick();
  assert.deepEqual(calls,['expand','save']);
  assert.equal(form.inert,false);
});

test('a hand-edited AI instruction is replaced only after consent',async t=>{
  const calls=[];
  const w=await classifierScreen(t,calls);
  const doc=w.document; const form=doc.querySelector('#classifier-form');
  const prompt=form.elements.system_prompt;
  prompt.value='Моя інструкція, яку я написав сам для класифікатора.'; prompt.dispatchEvent(new w.Event('input',{bubbles:true}));
  doc.querySelector('[data-code="money"] .direction-active').click();
  w.confirm=()=>false;
  form.dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true})); await tick(); await tick(); await tick();
  assert.deepEqual(calls,[],'declining keeps everything unsaved');
  assert.equal(form.dataset.dirty,'true');
  assert.equal(form.dataset.promptStale,'true');
  assert.equal(prompt.value,'Моя інструкція, яку я написав сам для класифікатора.');
  w.confirm=()=>true;
  form.dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true})); await tick(); await tick(); await tick();
  assert.deepEqual(calls.map(([kind])=>kind),['expand','save']);
});

test('types edited during an AI refresh are not saved with rules made for the old ones',async t=>{
  let release; const calls=[];
  const w=await screen(t,{handler:async(path,options)=>{
    if(path==='/api/v1/classifier/expand'){calls.push('expand'); await new Promise(r=>{release=r;}); const body=JSON.parse(options.body);
      return response({system_prompt:'Інструкція для старих типів звернень.',directions:body.directions.map(d=>({code:d.code,keywords:[]}))});}
    if(path==='/api/v1/classifier'){calls.push('save'); return response(JSON.parse(options.body));}
  }});
  const doc=w.document; const form=doc.querySelector('#classifier-form');
  doc.querySelector('[data-code="money"] .direction-active').click();
  form.dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true})); await tick();
  doc.querySelector('[data-code="money"] .direction-description').value='Змінено під час запиту';
  release(); await tick(); await tick(); await tick(); await tick();
  assert.deepEqual(calls,['expand']);
  assert.match(doc.querySelector('#toast').textContent,/Типи змінилися під час оновлення/);
  assert.equal(form.dataset.promptStale,'true');
  assert.equal(form.inert,false);
});

test('checking a reply marks the check button busy, not the scope chip',async t=>{
  let resolve;
  const w=await screen(t,{handler:path=>{
    if(path==='/api/v1/preview')return new Promise(r=>{resolve=()=>r(response({decision:'allowed',category:'general',template_code:'off_hours_default',forced_template:false,text:'x',dry_run:true,timezone:'Europe/Kyiv',source:'keywords',personal_schedule:false}));});
    if(path.startsWith('/api/v1/contacts'))return response({items:[contact(100)],has_more:false});
  }});
  const d=w.document;
  d.querySelector('#navigation [data-view=contacts]').click(); await tick();
  d.querySelector('[data-contact-id="100"]').click();
  d.querySelector('#navigation [data-view=more]').click();
  d.querySelector('[data-view=check]').click();
  const form=d.querySelector('#preview-form'); form.elements.text.value='Привіт';
  form.dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true})); await tick();
  assert.equal(form.querySelector('button[type=submit]').disabled,true);
  assert.equal(d.querySelector('#preview-clear-contact').disabled,false);
  resolve(); await tick(); await tick();
});

test('a deep link to a page returns to "Ще", and the log page shows diagnostics',async t=>{
  const data=structuredClone(base); data.status={...data.status,last_error:'STALE_REPLY',summary_status:'error'};
  const w=await screen(t,{data,url:'https://testserver/app/#logs',handler:path=>{
    if(path.startsWith('/api/v1/contacts'))return response({items:[],has_more:false,next_offset:0});
    if(path.startsWith('/api/v1/logs'))return response({items:[],has_more:false,next_offset:0});
  }});
  const d=w.document;
  assert.equal(d.querySelector('#navigation [data-view=more]').classList.contains('active'),true);
  assert.match(d.querySelector('#operating-history').textContent,/Запізнілу відповідь скасовано/);
  d.querySelector('#back-button').click();
  assert.equal(d.querySelector('[data-view-panel=more]').classList.contains('active'),true);
  assert.match(d.querySelector('#attention').textContent,/Підсумок не надіслано/);
});

test('connecting a channel by link does not leave the summary form unsaved',async t=>{
  const w=await screen(t);
  const form=w.document.querySelector('#summary-form');
  const field=w.document.querySelector('#summary-channel-reference');
  field.value='https://t.me/c/1/2'; field.dispatchEvent(new w.Event('input',{bubbles:true}));
  assert.notEqual(form.dataset.dirty,'true');
});

test('undoing a change leaves nothing to save and the AI rules untouched',async t=>{
  const calls=[];
  const w=await classifierScreen(t,calls);
  const doc=w.document; const form=doc.querySelector('#classifier-form');
  const toggle=doc.querySelector('[data-code="money"] .direction-active');
  toggle.click();
  assert.equal(form.dataset.dirty,'true');
  assert.equal(form.dataset.promptStale,'true');
  toggle.click();
  assert.equal(form.dataset.dirty,'false');
  assert.equal(form.dataset.promptStale,'false');
  const reply=doc.querySelector('[data-code="general"] .direction-template');
  const original=reply.value;
  reply.value=original+'!'; reply.dispatchEvent(new w.Event('input',{bubbles:true}));
  assert.equal(form.dataset.dirty,'true');
  reply.value=original; reply.dispatchEvent(new w.Event('input',{bubbles:true}));
  assert.equal(form.dataset.dirty,'false');
  form.dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true})); await tick(); await tick();
  assert.deepEqual(calls,[],'an unchanged form is never sent');
  doc.querySelector('#add-direction').click();
  assert.equal(form.dataset.dirty,'true');
  [...doc.querySelectorAll('.direction-card')].at(-1).querySelector('.remove-direction').click();
  assert.equal(form.dataset.dirty,'false');
  assert.equal(form.dataset.promptStale,'false');
});

test('every settings form forgets a change that was undone',async t=>{
  const w=await screen(t,{handler:path=>path.startsWith('/api/v1/contacts')?response({items:[contact(100)],has_more:false}):null});
  const d=w.document;
  const flip=(form,field,value)=>{const old=field.type==='checkbox'?field.checked:field.value;
    if(field.type==='checkbox')field.checked=!old;else field.value=value;field.dispatchEvent(new w.Event('input',{bubbles:true}));
    assert.equal(form.dataset.dirty,'true',form.id);
    if(field.type==='checkbox')field.checked=old;else field.value=old;field.dispatchEvent(new w.Event('change',{bubbles:true}));
    assert.equal(form.dataset.dirty,'false',form.id);};
  const delivery=d.querySelector('#delivery-form'); flip(delivery,delivery.elements.mark_read);
  const escalation=d.querySelector('#escalation-form'); flip(escalation,escalation.elements.offer_text,'Інший текст');
  const summary=d.querySelector('#summary-form'); flip(summary,summary.elements.summary_time,'07:15');
  const schedule=d.querySelector('#schedule-form'); flip(schedule,schedule.querySelector('.time-from'),'21:00');
  d.querySelector('#add-schedule-window').click();
  assert.equal(schedule.dataset.dirty,'true');
  schedule.querySelectorAll('.remove-window')[1].click();
  assert.equal(schedule.dataset.dirty,'false','adding and removing an interval is no change');
  d.querySelector('#navigation [data-view=contacts]').click(); await tick();
  d.querySelector('[data-contact-id="100"]').click();
  const contactForm=d.querySelector('#contact-form');
  contactForm.elements.exclusion.value='forever'; contactForm.elements.exclusion[2].dispatchEvent(new w.Event('change',{bubbles:true}));
  assert.equal(contactForm.dataset.dirty,'true');
  contactForm.elements.exclusion.value='none'; contactForm.elements.exclusion[0].dispatchEvent(new w.Event('change',{bubbles:true}));
  assert.equal(contactForm.dataset.dirty,'false');
});

test('declining to store texts leaves the summary form unchanged',async t=>{
  const calls=[];
  const w=await screen(t,{handler:path=>{ if(path==='/api/v1/summary'){calls.push(path);return response({});} }});
  const form=w.document.querySelector('#summary-form');
  const box=form.elements.message_retention_enabled;
  box.checked=true; box.dispatchEvent(new w.Event('change',{bubbles:true}));
  assert.equal(form.dataset.dirty,'true');
  w.confirm=()=>false;
  form.dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true})); await tick(); await tick();
  assert.equal(box.checked,false);
  assert.equal(form.dataset.dirty,'false');
  assert.deepEqual(calls,[]);
});

test('adding a type keeps the other cards as they are',async t=>{
  const w=await screen(t);
  const doc=w.document;
  const money=doc.querySelector('[data-code="money"]');
  money.querySelector('.direction-more').open=true;
  const keywords=money.querySelector('.direction-keywords'); keywords.value='a,b'; keywords.dispatchEvent(new w.Event('input',{bubbles:true}));
  doc.querySelector('#add-direction').click();
  assert.equal(doc.querySelector('[data-code="money"]'),money,'the card is not re-rendered');
  assert.equal(money.querySelector('.direction-more').open,true);
  assert.equal(keywords.value,'a,b');
});

function statsScreen(t,{stats={new:1,active:3,paused:0,never:2},items=[{...contact(1),configured:false},contact(2)],onSave}={}){
  return screen(t,{handler:(path,options)=>{
    if(path==='/api/v1/contacts/stats')return response(typeof stats==='function'?stats():stats);
    if(onSave&&options?.method==='PUT'&&path.startsWith('/api/v1/contacts/'))return onSave(path,options);
    if(path.startsWith('/api/v1/contacts'))return response({items,has_more:false});
  }});
}

test('home shows only the contact groups that have someone in them',async t=>{
  const w=await statsScreen(t); const d=w.document; await tick();
  const block=d.querySelector('#contact-stats');
  assert.equal(block.classList.contains('hidden'),false);
  const cells=[...block.querySelectorAll('.stat')].map(cell=>cell.textContent);
  assert.deepEqual(cells,['1Нові','3Активні','2Без відповіді']);
  assert.equal(block.querySelector('.stat').classList.contains('warn'),true);
});

test('home hides the contact block when there are no contacts',async t=>{
  const w=await statsScreen(t,{stats:{new:0,active:0,paused:0,never:0},items:[]}); await tick();
  assert.equal(w.document.querySelector('#contact-stats').classList.contains('hidden'),true);
});

test('a link from the home screen to a tab offers Back to the home screen',async t=>{
  const w=await statsScreen(t); const d=w.document; await tick();
  d.querySelector('#contact-stats .stat').click(); await tick();
  assert.equal(d.querySelector('[data-view-panel=contacts]').classList.contains('active'),true);
  assert.equal(d.querySelector('#back-button').classList.contains('hidden'),false);
  d.querySelector('[data-contact-id="1"]').click();
  d.querySelector('#back-button').click();
  assert.equal(d.querySelector('[data-view-panel=contacts]').classList.contains('active'),true,'first Back closes the contact');
  d.querySelector('#back-button').click();
  assert.equal(d.querySelector('[data-view-panel=overview]').classList.contains('active'),true);
  assert.equal(d.querySelector('#back-button').classList.contains('hidden'),true);
  d.querySelector('#contact-stats .stat').click(); await tick();
  d.querySelector('#navigation [data-view=contacts]').click(); await tick();
  assert.equal(d.querySelector('#back-button').classList.contains('hidden'),true,'a tab tap starts over');
});

test('the contact block opens the list even if a contact was left open',async t=>{
  const w=await statsScreen(t); const d=w.document; await tick();
  d.querySelector('#navigation [data-view=contacts]').click(); await tick();
  d.querySelector('[data-contact-id="2"]').click();
  d.querySelector('#navigation [data-view=overview]').click(); await tick();
  d.querySelector('#contact-stats .stat').click(); await tick();
  assert.equal(d.querySelector('#contact-layout').classList.contains('editing'),false);
  assert.equal(d.querySelector('#page-title').textContent,'Контакти');
});

test('saving a new contact refreshes the home counts',async t=>{
  let saved=false;
  const w=await statsScreen(t,{stats:()=>saved?{new:0,active:1,paused:0,never:0}:{new:1,active:0,paused:0,never:0},items:[{...contact(101),configured:false}],
    onSave:(path,options)=>{saved=true;return response({...contact(101),...JSON.parse(options.body)});}});
  const d=w.document; await tick();
  assert.match(d.querySelector('#contact-stats').textContent,/1Нові/);
  d.querySelector('#navigation [data-view=contacts]').click(); await tick();
  d.querySelector('[data-contact-id="101"]').click();
  const form=d.querySelector('#contact-form');
  assert.equal(form.classList.contains('needs-save'),true);
  form.requestSubmit(); await tick(); await tick(); await tick();
  assert.equal(form.classList.contains('needs-save'),false);
  assert.equal(d.querySelector('#contact-stats').textContent,'1Активні');
});

test('"Цілодобово" in an interval hides the hours and saves the whole day',async t=>{
  let saved;
  const w=await screen(t,{handler:(path,options)=>{ if(path==='/api/v1/schedule'){saved=JSON.parse(options.body);return response(saved);} }});
  const d=w.document; const form=d.querySelector('#schedule-form');
  const row=form.querySelector('.window-row'); const allDay=row.querySelector('.all-day-toggle');
  assert.equal(allDay.checked,false);
  allDay.checked=true; allDay.dispatchEvent(new w.Event('change',{bubbles:true}));
  assert.equal(row.classList.contains('is-all-day'),true);
  assert.equal(form.dataset.dirty,'true');
  allDay.checked=false; allDay.dispatchEvent(new w.Event('change',{bubbles:true}));
  assert.equal(row.querySelector('.time-from').value,'22:00','unticking brings the hours back');
  assert.equal(form.dataset.dirty,'false');
  allDay.checked=true; allDay.dispatchEvent(new w.Event('change',{bubbles:true}));
  form.dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true})); await tick(); await tick();
  assert.deepEqual([saved.windows[0].time_from,saved.windows[0].time_to],['00:00','00:00']);
  assert.match(d.querySelector('#home-schedule').textContent,/^цілодобово, щодня$/);
});

test('a contact can be answered around the clock in one tap',async t=>{
  let saved;
  const w=await screen(t,{handler:(path,options)=>{
    if(path==='/api/v1/contacts/100'){saved=JSON.parse(options.body);return response({...contact(100),...saved});}
    if(path.startsWith('/api/v1/contacts'))return response({items:[contact(100)],has_more:false});
  }});
  const d=w.document;
  d.querySelector('#navigation [data-view=contacts]').click(); await tick();
  d.querySelector('[data-contact-id="100"]').click();
  d.querySelector('#contact-all-day').click();
  assert.equal(d.querySelector('#contact-all-day').classList.contains('hidden'),true);
  assert.equal(d.querySelector('#contact-windows .all-day-toggle').checked,true);
  d.querySelector('#contact-form').requestSubmit(); await tick(); await tick();
  assert.deepEqual(saved.windows,[{weekday_mask:127,time_from:'00:00',time_to:'00:00',is_active:true}]);
});

test('the replies section is named for templates and hides the model',async t=>{
  const w=await screen(t,{url:'https://testserver/app/#classifier'});
  const d=w.document;
  assert.equal(d.querySelector('#page-title').textContent,'Шаблони відповідей');
  assert.equal(d.querySelector('#navigation [data-view=classifier]').textContent.trim(),'Шаблони');
  assert.equal(d.querySelector('#add-direction').textContent,'+ Шаблон відповіді');
  const model=d.querySelector('#classifier-form').elements.model;
  assert.equal(model.type,'hidden');
  assert.equal(model.value,base.classifier.model);
});

test('a 24/7 schedule does not claim to end at midnight',async t=>{
  const data=structuredClone(base); data.connection.dry_run=false;
  data.schedule.windows=[{id:1,weekday_mask:127,time_from:'00:00',time_to:'00:00',is_active:true}];
  data.status={...data.status,code:'live',window_end:'2026-09-25T21:00:00Z'}; // 00:00 in Kyiv
  const w=await screen(t,{data});
  assert.equal(w.document.querySelector('#operating-note').textContent,'Цілодобово');
  assert.equal(w.document.querySelector('#home-schedule').textContent,'цілодобово, щодня');
});

test('consecutive whole days end at the last of them',async t=>{
  const data=structuredClone(base); data.connection.dry_run=false;
  // Saturday and Sunday are whole days; Saturday 2026-09-26 starts at 21:00 UTC on Friday.
  data.schedule.windows=[{id:1,weekday_mask:96,time_from:'00:00',time_to:'00:00',is_active:true}];
  data.status={...data.status,code:'live',window_end:'2026-09-25T21:00:00Z'};
  const w=await screen(t,{data});
  assert.equal(w.document.querySelector('#operating-note').textContent,'До кінця 27 вересня');
});

test('a contact that is never answered hides its schedule, and 24/7 needs no shortcut',async t=>{
  const data=structuredClone(base);
  data.schedule.windows=[{id:1,weekday_mask:127,time_from:'00:00',time_to:'00:00',is_active:true}];
  const w=await screen(t,{data,handler:path=>path.startsWith('/api/v1/contacts')&&!path.includes('stats')?response({items:[contact(100)],has_more:false}):null});
  const d=w.document;
  d.querySelector('#navigation [data-view=contacts]').click(); await tick();
  d.querySelector('[data-contact-id="100"]').click();
  assert.equal(d.querySelector('#contact-all-day').classList.contains('hidden'),true);
  assert.match(d.querySelector('#contact-schedule-preview').textContent,/Цілодобово/);
  const form=d.querySelector('#contact-form');
  form.elements.exclusion.value='forever'; form.elements.exclusion[2].dispatchEvent(new w.Event('change',{bubbles:true}));
  assert.equal(form.querySelector('.contact-schedule').classList.contains('hidden'),true);
});
