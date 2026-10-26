/* Explicit, reversible reportage kits. Source files remain read-only. */
const RE={showHidden:false,target:null};
const reportKind=x=>x&&['group','reportage'].includes(x.kind);
const reportEdit=x=>x.report_edit||{parents:{},hidden:[],order:[]};
function reportMains(x){return [...S.sels[x.id]].filter(p=>!S.dsel[x.id].has(p))}
function reportChange(si,path,mode,parent=null){
 const x=S.sess.sections[si];if(!reportKind(x)||!x.files.includes(path))return false;
 if(x.mandatory[path]){toast('Это обязательная общая фотография. Замените её через меню общей.');return false}
 const sel=new Set(S.sels[x.id]),ds=new Set(S.dsel[x.id]),edit=structuredClone(reportEdit(x)),mains=reportMains(x);
 edit.parents=Object.fromEntries([...ds].map(p=>[p,dupParent(x,p,mains)]).filter(([,p])=>p));
 const oldParent=edit.parents[path];
 if(mode==='extra'){
  if(!parent||parent===path||!mains.includes(parent)){toast('Выберите основной кадр в списке рядом с кнопкой.',1);return false}
  for(const [child,owner] of Object.entries(edit.parents))if(owner===path)edit.parents[child]=parent;
  sel.add(path);ds.add(path);edit.parents[path]=parent;
 }else if(mode==='main'||mode==='new'){
  const previous=mode==='new'?null:oldParent||mains.find(p=>p!==path&&sceneFrames(x,path).includes(p));
  if(previous){for(const [child,owner] of Object.entries(edit.parents))if(owner===previous)edit.parents[child]=path;ds.add(previous);edit.parents[previous]=path}
  sel.add(path);ds.delete(path);delete edit.parents[path];
 }else if(mode==='remove'||mode==='hide'){
  const children=Object.keys(edit.parents).filter(p=>edit.parents[p]===path);
  if(children.length){const promoted=children[0];ds.delete(promoted);delete edit.parents[promoted];children.slice(1).forEach(p=>edit.parents[p]=promoted)}
  sel.delete(path);ds.delete(path);delete edit.parents[path];
 }
 edit.hidden=mode==='hide'?[...new Set([...edit.hidden,path])]:edit.hidden.filter(p=>p!==path);
 const nextMains=[...sel].filter(p=>!ds.has(p));
 edit.parents=Object.fromEntries(Object.entries(edit.parents).filter(([p,q])=>ds.has(p)&&nextMains.includes(q)));
 const previousEdit=x.report_edit;x.report_edit=edit;const error=dupError(x,sel,ds);x.report_edit=previousEdit;
 if(error){toast(error,1);return false}
 pushUndo(si);x.report_edit=edit;S.sels[x.id]=sel;S.dsel[x.id]=ds;RE.target=parent||nextMains.find(p=>sceneFrames(x,path).includes(p))||null;scheduleSave(si);
 if(S.viewer){refreshSide();renderViewer()}else drawShoot();return true;
}
function reportDrop(e,si,parent){e.preventDefault();e.stopPropagation();e.currentTarget.classList.remove('dzon');let item;try{item=JSON.parse(e.dataTransfer.getData('application/lumio-report'))}catch{return}if(item.session!==S.sid||item.si!==si)return toast('Перенос доступен внутри этого репортажа',1);reportChange(si,item.path,'extra',parent)}
const reportBaseTile=tile;
tile=function(f,si){let html=reportBaseTile(f,si);const x=S.sess.sections[si];if(!reportKind(x))return html;return html.replace('class="tile ',`draggable="true" ondragstart="event.dataTransfer.setData('application/lumio-report',JSON.stringify({session:S.sid,si:${si},path:this.dataset.p}))" class="tile `)};
const reportBasePersonView=personView;
personView=function(){let groups=reportBasePersonView();if(!['group','reportage'].includes(curP()?.kind))return groups;
 const hidden=new Set(curP().secs.flatMap(si=>reportEdit(S.sess.sections[si]).hidden));
 if(!RE.showHidden)groups=groups.map(g=>({...g,items:g.items.filter(i=>!hidden.has(i.f.path))})).filter(g=>g.items.length||g.keepEmpty);
 S.flat=groups.flatMap(g=>g.items);return groups;
};
const reportBaseSceneFrames=sceneFrames;
sceneFrames=function(x,path){const paths=reportBaseSceneFrames(x,path);return RE.showHidden?paths:paths.filter(p=>!reportEdit(x).hidden.includes(p))};
const reportBaseDraw=drawShoot;
drawShoot=function(){reportBaseDraw();if(!['group','reportage'].includes(curP()?.kind))return;
 const si=curP().secs[0],x=S.sess.sections[si],hidden=reportEdit(x).hidden;
 const help=document.querySelector('.scene-help');if(help)help.textContent='Откройте фото → «Основной» или «Дополнительный». Перетащите фото вправо к нужному основному кадру. «Не использовать» скрывает кадр; оригинал сохраняется.';
 const grid=document.querySelector('#grid');if(grid){const bar=document.createElement('div');bar.className='scene-filter';bar.style.gridColumn='1 / -1';bar.innerHTML=`<span>Основной слева · дополнительные справа · до ${x.dup_per_main??5}</span><button class="lnk">${RE.showHidden?'Скрыть исключённые':'Не используются · '+hidden.length}</button>`;bar.querySelector('button').onclick=()=>{RE.showHidden=!RE.showHidden;drawShoot()};grid.prepend(bar)}
 const mains=[...document.querySelectorAll('.main-photo-group')];mains.forEach(group=>{const tile=group.querySelector('.tile');if(!tile)return;const parent=tile.dataset.p,extras=group.nextElementSibling;if(!extras?.classList.contains('extra-photo-group'))return;
  extras.ondragover=e=>{e.preventDefault();extras.classList.add('dzon')};extras.ondragleave=()=>extras.classList.remove('dzon');extras.ondrop=e=>reportDrop(e,si,parent);
  const head=extras.querySelector('.gh2');if(head){const button=document.createElement('button');button.className='lnk';button.textContent='Добавить из серии';button.onclick=()=>{RE.target=parent;jumpTo(parent)};head.replaceChildren(document.createTextNode(`Дополнительные · ${extras.querySelectorAll('.tile').length} из ${x.dup_per_main??5} `),button)}
  const label=group.querySelector('.gh2 b');if(label){const index=(x.scenes||[]).findIndex(sc=>sc.includes(parent));label.textContent=`Основной · сцена ${index+1}`}
 });
};
const reportBaseViewer=renderViewer;
renderViewer=function(){reportBaseViewer();const v=S.viewer;if(!v)return;const si=v.sis[v.i],x=S.sess.sections[si],path=v.list[v.i];if(!reportKind(x))return;
 const row=document.querySelector('.vbar>.row');if(!row)return;row.replaceChildren();
 const button=(text,mode,parent)=>{const b=document.createElement('button');b.className='btn sm';b.textContent=text;b.onclick=()=>reportChange(si,path,mode,typeof parent==='function'?parent():parent);row.append(b);return b};
 const mains=reportMains(x),isMain=mains.includes(path),selected=S.sels[x.id].has(path);
 button(isMain?'✓ Основной':'Сделать основным','main').disabled=isMain;
 button('Новый отдельный комплект','new').disabled=isMain;
 const select=document.createElement('select');select.title='К какому основному добавить';select.style.maxWidth='230px';select.onchange=()=>RE.target=select.value;
 const candidates=mains.filter(p=>p!==path),owner=dupParent(x,path,candidates),chosen=candidates.includes(RE.target)?RE.target:owner;
 candidates.forEach(p=>{const option=document.createElement('option');option.value=p;option.textContent=S.fmap[p].name;option.selected=p===chosen;select.append(option)});row.append(select);
 button('Дополнительный · D','extra',()=>select.value).disabled=!candidates.length;
 if(!candidates.length&&!isMain){const hint=document.createElement('span');hint.className='mut';hint.textContent='Начните с «Сделать основным»';row.append(hint)}
 if(selected)button('Убрать из комплекта','remove');button(reportEdit(x).hidden.includes(path)?'Вернуть в серию':'Не использовать',reportEdit(x).hidden.includes(path)?'restore':'hide');
};
const reportBaseTogDup=togDup;
togDup=function(path,si){const x=S.sess.sections[si];if(!reportKind(x))return reportBaseTogDup(path,si);const mains=reportMains(x),parent=mains.includes(RE.target)&&RE.target!==path?RE.target:dupParent(x,path,mains.filter(p=>p!==path));return reportChange(si,path,S.dsel[x.id].has(path)?'main':parent?'extra':'main',parent)};
const reportBaseTogPath=togPath;
togPath=function(path,si){const x=S.sess.sections[si];if(!reportKind(x))return reportBaseTogPath(path,si);return reportChange(si,path,S.sels[x.id].has(path)?'remove':'new')};
const reportBaseMenu=reportSceneMenu;
reportSceneMenu=function(btn){reportBaseMenu(btn);const tile=btn.closest('.tile'),si=+tile.dataset.s,path=tile.dataset.p,x=S.sess.sections[si],menu=document.querySelector('#rmenu');if(!menu)return;
 for(const [label,mode] of [['Сделать основным этой серии','main'],['Новый отдельный комплект','new'],[reportEdit(x).hidden.includes(path)?'Вернуть в серию':'Не использовать',reportEdit(x).hidden.includes(path)?'restore':'hide']]){const b=document.createElement('button');b.textContent=label;b.onclick=()=>{closeMenu();reportChange(si,path,mode)};menu.prepend(b)}
};
document.addEventListener('keydown',e=>{if(!S.viewer||/INPUT|TEXTAREA|SELECT/.test(e.target.tagName)||e.ctrlKey||e.metaKey)return;const v=S.viewer,si=v.sis[v.i],path=v.list[v.i];if(!reportKind(S.sess.sections[si]))return;if(['d','D','в','В'].includes(e.key)){e.preventDefault();e.stopImmediatePropagation();togDup(path,si)}else if(e.key==='Delete'||e.key==='Backspace'){e.preventDefault();e.stopImmediatePropagation();reportChange(si,path,'hide')}},true);
