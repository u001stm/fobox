// Card events and project availability. The SQLite catalog remains authoritative.
const OFF={last:0,started:false,busy:false,state:null,pending:null};
function offlineBadge(p){
 const host=document.querySelector('.shoot-actions,.unit-heading');if(!host)return;
 let b=document.getElementById('offline_badge');if(!b){b=document.createElement('button');b.id='offline_badge';b.className='btn sm';b.style.cssText='font-size:11px;color:var(--muted);background:none;border-color:transparent';b.onclick=offlineDialog;host.append(b)}
 if(typeof labActive==='function'&&labActive()&&typeof L!=='undefined'&&L.view==='multilayer'&&p.ml_sources){
  const s=p.ml_sources,online=s.total>0&&s.online===s.total;
  b.classList.toggle('source-online',online);b.classList.toggle('source-offline',!online);
  b.textContent='Папки источников · '+s.online+'/'+s.total;
  b.title='Доступность папок проверяется сейчас. Целостность отдельных файлов проверяется при Analyze и экспорте.';
  return;
 }
 b.classList.toggle('source-online',p.online);b.classList.toggle('source-offline',!p.online);
 if(typeof labActive==='function'&&labActive()){
  b.textContent=p.online?'Локальные оригиналы':'Локальные оригиналы недоступны';
  b.title='Исходники этой среды скопированы на компьютер; карта для проявки не нужна';
  return;
 }
 b.textContent=p.online?(p.ready?'Карта подключена':'Сохраняю превью · '+p.cached+'/'+p.total):'Без карты · превью '+p.cached+'/'+p.total;
 b.title=p.ready?'Съёмка доступна без карты. Оригиналы нужны для экспорта.':'Дождитесь сохранения всех превью перед извлечением карты';
}
async function offlinePoll(){
 if(!S.cfg)return;
 try{const d=await api('/api/media-state?ui=1'+(S.sid?'&session='+encodeURIComponent(S.sid):''));OFF.state=d;
 if(d.project&&S.page==='shoot')offlineBadge(d.project);
 const fresh=d.events.filter(e=>e.id>OFF.last);
 if(!OFF.started){OFF.started=true;OFF.last=d.events.at(-1)?.id||0;const q=new URLSearchParams(location.search);if(q.has('card')){const ev=d.events.find(x=>String(x.id)===q.get('event'))||d.events.at(-1);if(ev)offlineArrival(ev);window.history.replaceState(null,'',location.pathname)}else if(!(typeof linkedSession!=='undefined'&&linkedSession)){const ev=d.events.at(-1);if(ev&&ev.sessions.length===1&&ev.sessions[0].id!==S.sid)offlineArrival(ev)}}
 else if(fresh.length&&!OFF.busy){OFF.last=fresh.at(-1).id;offlineArrival(fresh.at(-1))}
 }catch(e){/* reconnect on next tick; catalog writes are handled by existing API */}
}
async function offlineOpen(sid,company){
 if(typeof U!=='undefined'&&U.busy){OFF.pending={sid,company};return}
 await flush();if(S.sid===sid&&S.cfg.settings.current_company===company&&S.page==='shoot'){if(typeof unitActive==='function'&&unitActive())await unitRender();else{await reloadSess(curP()?.key);drawShoot()}toast('Карта снова подключена · отбор сохранён');return}
 if(S.cfg.settings.current_company!==company)await switchEnv(company);
 await openSess(sid);toast('Съёмка восстановлена · отбор сохранён');
}
function offlineArrival(ev){
 // Never close a pending import/export/edit dialog to switch shoots.
 if(document.querySelector('#layer .modal')||S.exportBusy||(typeof U!=='undefined'&&U.busy)){OFF.pending=ev;toast('Карта распознана. Откройте статус карты после завершения текущего действия.');return}
 if(ev.sessions.length===1)return offlineOpen(ev.sessions[0].id,ev.sessions[0].company);
 $('#layer').innerHTML=`<div class="modal"><div class="dlg"><h2>${ev.sessions.length?'Какая съёмка?':'Новая карта'}</h2><p class="mut">${esc(ev.source)}</p>${ev.sessions.map((s,i)=>`<p><button class="btn" data-i="${i}">${esc(s.title)} · ${esc(s.company)}</button></p>`).join('')}${!ev.sessions.length?'<p class="mut">Сохранённой съёмки для этой карты нет. Импорт создаст отдельную запись.</p><button class="btn" id="off_new">Импортировать новую съёмку</button>':''}<button class="btn" onclick="closeL()">Позже</button></div></div>`;
 document.querySelectorAll('#layer [data-i]').forEach(b=>b.onclick=()=>{closeL();const s=ev.sessions[+b.dataset.i];offlineOpen(s.id,s.company)});
 const n=$('#off_new');if(n)n.onclick=()=>{closeL();importDlg();const input=$('#i_src,#u_source');if(input)input.value=ev.source};
}
async function offlineDialog(){
 if(OFF.pending){const p=OFF.pending;OFF.pending=null;if(p.sid)return offlineOpen(p.sid,p.company);return offlineArrival(p)}
 const d=await api('/api/media-state?ui=1&session='+encodeURIComponent(S.sid)),p=d.project;
 $('#layer').innerHTML=`<div class="modal"><div class="dlg"><h2>Съёмка на этом компьютере</h2><p>${p.online?'Оригиналы подключены.':'Карта не подключена или вставлена другая съёмка.'}</p><p>Превью сохранены: <b>${p.cached} / ${p.total}</b>. ${p.ready?'Можно извлечь карту и продолжить отбор.':'Для остальных превью пока нужна исходная карта.'}</p><p class="mut">Отбор, роли, подтверждения, порядок и лица остаются в локальной базе. JSON-снимок проекта сохраняется рядом с превью. Это не резервная копия RAW.</p><p class="mut" style="overflow-wrap:anywhere">${esc(p.snapshot)}</p><div class="row"><button class="btn" onclick="offlinePrepare()">Сохранить все превью</button><button class="btn" onclick="offlinePassport()">Паспорт JSON на карту</button></div><div class="f" style="margin-top:18px"><label>Карта получила другую букву или RAW скопированы в другую папку?</label><div class="row"><input id="off_source" style="flex:1" placeholder="Папка с теми же файлами"><button class="btn" onclick="offlinePick()">Обзор…</button></div></div><button class="btn" onclick="offlineRelink()">Восстановить связь</button><p class="mut">Сначала проверяются отпечатки файлов. Другую съёмку привязать нельзя. Паспорт на карте необязателен и сам по себе не заменяет проверку.</p><p class="mut">Автооткрытие при подключении карты: ${d.autostart?'включено':'выключено'}</p><button class="btn" onclick="offlineAutostart(${!d.autostart})">${d.autostart?'Выключить':'Включить'} автооткрытие</button><p id="off_info" class="mut"></p><button class="btn" onclick="closeL()">Закрыть</button></div></div>`;
}
async function offlinePick(){try{const r=await api('/api/pick',{kind:'dir'});if(r.path)$('#off_source').value=r.path}catch(e){$('#off_info').textContent=e.message}}
async function offlinePrepare(){try{await api('/api/offline-prepare',{session:S.sid});$('#off_info').textContent='Сохраняю превью в фоне. Готовность отображается рядом с шапкой.'}catch(e){$('#off_info').textContent=e.message}}
async function offlinePassport(){try{const r=await api('/api/card-passport',{session:S.sid});$('#off_info').textContent='Паспорт сохранён: '+r.path}catch(e){$('#off_info').textContent=e.message}}
async function offlineRelink(){try{const r=await api('/api/relink',{session:S.sid,source:$('#off_source').value});await flush();S.sess=null;closeL();await openSess(r.session)}catch(e){$('#off_info').textContent=e.message}}
async function offlineAutostart(enabled){try{await api('/api/card-autostart',{enabled});await offlineDialog()}catch(e){$('#off_info').textContent=e.message}}
function offlineSavePlace(){try{if(S.page!=='shoot'||!S.sid)return;const place={sid:S.sid,tab:S.tab,pk:S.people?.[S.pi]?.key,list:$('#secs')?.scrollTop||0,grid:$('#grid,#u_content')?.scrollTop||0,unitView:typeof U!=='undefined'?U.view:null};localStorage.setItem('lumio.project-place.'+S.sid,JSON.stringify(place));localStorage.setItem('lumio.last-project',S.sid)}catch(e){}}
const offlineOriginalOpen=openSess;
openSess=async function(id){offlineSavePlace();let place=null;try{place=JSON.parse(localStorage.getItem('lumio.project-place.'+id)||'null')}catch(e){}if(place){S.tab=place.tab||S.tab;S._restoreKey=place.pk;OFF.restore=place}await offlineOriginalOpen(id);if(place&&typeof unitActive==='function'&&unitActive()&&typeof U!=='undefined'){U.view=place.unitView||'materials';OFF.restore=place;if(U.data)unitDraw()}};
try{const q=new URLSearchParams(location.search);const sid=S.sid||localStorage.getItem('lumio.last-project');const place=JSON.parse(localStorage.getItem('lumio.project-place.'+sid)||'null');if(place&&!q.has('card')&&(!q.has('session')||q.get('session')===sid)){S.sid=sid;S.page='shoot';S.tab=place.tab||S.tab;S._restoreKey=place.pk;OFF.restore=place;if(typeof U!=='undefined'&&place.unitView)U.view=place.unitView}}
catch(e){}
function offlineRestoreScroll(){if(!OFF.restore||OFF.restore.sid!==S.sid)return;const place=OFF.restore;const grid=$('#grid,#u_content');if(!grid)return;OFF.restore=null;if($('#secs'))$('#secs').scrollTop=place.list;grid.scrollTop=place.grid}
const offlineOriginalDraw=drawShoot;
drawShoot=function(){offlineOriginalDraw();offlineRestoreScroll()};
if(typeof unitDraw==='function'){const unitOriginalDraw=unitDraw;unitDraw=function(){unitOriginalDraw();offlineRestoreScroll()}}
window.addEventListener('beforeunload',offlineSavePlace);
setInterval(offlineSavePlace,1000);setInterval(offlinePoll,3000);setTimeout(offlinePoll,1800);
