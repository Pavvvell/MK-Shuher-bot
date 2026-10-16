/* Шуxер Mini App. All data is fetched from role-checked PostgreSQL API. */
const tg = window.Telegram?.WebApp;
if (tg) { tg.ready(); tg.expand(); }
const state = { me:null,section:"Заявки",items:[],total:0,offset:0,limit:30,
                query:"",loading:false,selected:null,counts:{} };
const labels = {"Заявки":"Заявки","Закупка":"Закупівлі","Склад":"Склад",
  "Доручення":"Доручення","Уточнення відомостей":"Уточнення",
  "Інвентаризація":"Інвентаризація","__migration":"Міграція"};
const symbols = {"Заявки":"▣","Закупка":"◈","Склад":"▤",
  "Доручення":"✓","Уточнення відомостей":"⌁","Інвентаризація":"◫",
  "__migration":"⇄"};
const el = id => document.getElementById(id);
const esc = s => String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",
  '"':"&quot;","'":"&#39;"}[c]));
const attr = s=>esc(s);
const route=(...parts)=>parts.map(x=>encodeURIComponent(x)).join("/");
function toast(message,error=false){
  const t=el("toast");t.textContent=message;t.className="toast show";
  if(error)t.style.background="#60302d";else t.style.background="";
  clearTimeout(toast.timer);toast.timer=setTimeout(()=>t.classList.remove("show"),3500);
}
async function api(path,opts={}){
  const headers={...(opts.headers||{}),"X-Telegram-Init-Data":tg?.initData||""};
  if(opts.json!==undefined){headers["Content-Type"]="application/json";opts.body=JSON.stringify(opts.json);delete opts.json;}
  const response=await fetch("/api/v1"+path,{...opts,headers,cache:"no-store"});
  let body;
  try{body=await response.json();}catch{body={detail:"Сервер повернув некоректну відповідь."};}
  if(!response.ok)throw Error(typeof body.detail==="string"?body.detail:JSON.stringify(body.detail||body));
  return body;
}
function navList(){
  const pages=[...state.me.sheets];
  if(state.me.role==="owner")pages.push("__migration");
  const render = s => `<button class="nav-btn ${state.section===s?"active":""}" data-page="${attr(s)}">
      <span class="nav-icon">${symbols[s]||"▣"}</span><span class="nav-text">${labels[s]||esc(s)}</span></button>`;
  el("sidenav").innerHTML=pages.map(render).join("");
  const mobile=pages.filter(s=>!["Уточнення відомостей","Інвентаризація","__migration"].includes(s));
  el("mobileNav").innerHTML=mobile.map(render).join("");
  document.querySelectorAll("[data-page]").forEach(b=>b.onclick=()=>openPage(b.dataset.page));
}
function setSection(s){
  state.section=s;el("pageTitle").textContent=labels[s]||s;
  navList();el("search").value="";state.query="";
  el("search").closest(".toolbar").classList.toggle("hidden",s==="__migration");
  el("createRequest").hidden=s!=="Заявки";
}
async function openPage(s){
  setSection(s);closeDetail();state.offset=0;
  if(s==="__migration")return renderMigration();
  await Promise.all([loadDashboard(),loadRecords()]);
}
function stat(label,value,note=""){
  return `<div class="metric"><small>${esc(label)}</small><strong>${esc(value)}</strong><em>${esc(note)}</em></div>`;
}
async function loadDashboard(){
  try {
    const stats=await api("/dashboard");state.counts=stats.sections||{};
    let priorities=state.me.sheets.filter(s=>!["Уточнення відомостей","Інвентаризація"].includes(s));
    priorities=priorities.slice(0,3);
    el("overview").innerHTML=priorities.map(s=>stat(labels[s]||s,stats.sections?.[s]||0,"записів")).join("");
  }catch(e){el("message").textContent=e.message;}
}
function badge(status){
  const s=String(status||"Без статусу");
  const lower=s.toLowerCase();
  const kind=/оплачено|виконано|прийнята|отримано|підтверджена/.test(lower)?"good":
    /блок|простроч|борг|заборг/.test(lower)?"bad":
    /чекає|очікує|до оплат|на перевір|нове|не підтвер/.test(lower)?"warn":"";
  return `<span class="badge ${kind}" title="${attr(s)}">${esc(s.length>44?s.slice(0,42)+"…":s)}</span>`;
}
function infoLine(row){
  const c=row.cells||[];
  if(state.section==="Заявки")return `${esc(c[6]||"—")} ${esc(c[7]||"")} · ${esc(c[5]||"")}`;
  if(state.section==="Закупка")return `${esc(c[4]||"—")} ${esc(c[5]||"")} · ${esc(c[6]||"Постачальник не вказаний")}`;
  if(state.section==="Склад")return `${esc(c[8]||"—")} ${esc(c[9]||"")} · ${esc(c[11]||"Без місця")}`;
  if(state.section==="Доручення")return `${esc(c[4]||"—")} · ${esc(c[8]||"")}`;
  return esc((c.slice(2,5).filter(Boolean).join(" · "))||"");
}
async function loadRecords(append=false){
  if(state.loading)return;
  state.loading=true;
  if(!append){state.offset=0;state.items=[];el("items").innerHTML='<div class="empty">Завантажую дані…</div>';}
  el("message").textContent="";
  try{
    let params=new URLSearchParams({limit:String(state.limit),offset:String(state.offset)});
    if(state.query)params.set("q",state.query);
    const result=await api("/records/"+encodeURIComponent(state.section)+"?"+params.toString());
    state.items=append?state.items.concat(result.items):result.items;
    state.total=result.total;state.offset=state.items.length;
    el("items").innerHTML=state.items.length?state.items.map(row=>`
      <button class="item" data-id="${attr(row.id)}">
        <div class="item-head"><div><span class="project">${esc(row.project||"Без проєкту")}</span>
        <h3>${esc(row.title||row.id)}</h3></div>${badge(row.status)}</div>
        <div class="details">${infoLine(row)}</div>
        <div class="item-foot"><span>${esc(row.id)}</span><span>Деталі ↗</span></div>
      </button>`).join(""):'<div class="empty">Поки немає записів за цим запитом.</div>';
    el("pagination").textContent=`Показано ${state.items.length} із ${state.total}`;
    el("more").hidden=state.items.length>=state.total;
    el("items").querySelectorAll("[data-id]").forEach(btn=>btn.onclick=()=>openRecord(btn.dataset.id));
  }catch(e){el("items").innerHTML='<div class="empty error">'+esc(e.message)+'</div>';}
  finally{state.loading=false;}
}
function detailShell(title,subtitle,html){
  el("detailTitle").textContent=title;el("detailLabel").textContent=subtitle;
  el("detailBody").innerHTML=html;el("overlay").hidden=false;
  document.body.style.overflow="hidden";
}
function closeDetail(){el("overlay").hidden=true;document.body.style.overflow="";}
function facts(rec){
  const headers=rec.headers?.length?rec.headers:rec.cells.map((_,i)=>"Поле "+(i+1));
  return rec.cells.map((val,i)=>val===null||val===undefined||val===""?"":`
      <div class="fact"><span class="key">${esc(headers[i]||"Поле "+(i+1))}</span>
      <span class="value">${esc(val)}</span></div>`).join("");
}
async function openRecord(recordId){
  try{
    const sheet=state.section;
    const rec=await api("/records/"+route(sheet,recordId));
    state.selected=rec;
    detailShell(rec.title||rec.id,`${labels[sheet]} · ${rec.id}`,
      `<div class="stack">${facts(rec)}</div><h3 class="section-title">Файли та посилання</h3>
      <div id="attachments" class="stack">Завантажую…</div>
      <div class="stack" style="margin-top:15px">
        <label class="field">Додати фото або документ (до 8 МБ)
        <input id="fileUpload" type="file" accept="image/*,.pdf,.doc,.docx,.xls,.xlsx,.txt"></label>
        <button class="button" id="uploadButton">Зберегти файл</button>
        <label class="field">Додати посилання<input id="linkUrl" type="url" placeholder="https://…"></label>
        <button class="button ghost" id="saveLink">Прикріпити посилання</button>
      </div>`+
      (sheet==="Доручення"?"<div id='taskEditor'></div>":""));
    el("uploadButton").onclick=uploadFile;
    el("saveLink").onclick=saveLink;
    if(sheet==="Доручення")renderTaskEditor(rec);
    await loadAttachments();
  }catch(e){toast(e.message,true);}
}
function renderTaskEditor(rec){
  const current=rec.cells[10]||"Нове";
  const options=["Нове","У роботі","Очікує рішення","Заблоковано","На перевірці","Виконано"];
  el("taskEditor").innerHTML=`<h3 class="section-title">Оновити доручення</h3>
    <div class="stack"><label class="field">Статус<select id="taskStatus">
      ${options.map(x=>`<option ${x===current?"selected":""}>${esc(x)}</option>`).join("")}</select></label>
    <label class="field">Результат / коментар<textarea id="taskComment">${esc(rec.cells[11]||"")}</textarea></label>
    <button class="button primary" id="saveTask">Зберегти статус</button></div>`;
  el("saveTask").onclick=async()=>{
    try{await api("/tasks/"+encodeURIComponent(rec.id)+"/status",{
      method:"POST",json:{status:el("taskStatus").value,comment:el("taskComment").value,version:rec.version}
    });toast("Статус збережено");await loadRecords();await openRecord(rec.id);
    }catch(e){toast(e.message,true);}
  };
}
async function loadAttachments(){
  if(!state.selected)return;
  const rec=state.selected;
  try{
    const rows=await api("/records/"+route(rec.sheet,rec.id)+"/attachments");
    const box=el("attachments");if(!box)return;
    box.innerHTML=rows.length?rows.map(r=>`<div class="file-row">
      <span>${r.url?"🔗":"📎"} ${esc(r.filename)}</span>
      <button class="button ghost" data-file="${attr(r.id)}" data-url="${attr(r.url||"")}">
      ${r.url?"Відкрити":"Завантажити"}</button></div>`).join(""):"<small>Вкладень поки немає.</small>";
    box.querySelectorAll("[data-file]").forEach(b=>b.onclick=()=>downloadAttachment(b.dataset.file,b.dataset.url));
  }catch(e){el("attachments").textContent=e.message;}
}
async function downloadAttachment(id,url){
  if(url){window.Telegram?.WebApp?.openLink?.(url)??window.open(url,"_blank","noopener");return;}
  try{
    const headers={"X-Telegram-Init-Data":tg?.initData||""};
    const response=await fetch("/api/v1/files/"+encodeURIComponent(id),{headers});
    if(!response.ok)throw Error("Файл недоступний.");
    const blob=await response.blob();
    const href=URL.createObjectURL(blob);const a=document.createElement("a");
    a.href=href;a.download="shuher-file";a.click();setTimeout(()=>URL.revokeObjectURL(href),60000);
  }catch(e){toast(e.message,true);}
}
async function uploadFile(){
  const file=el("fileUpload").files?.[0];if(!file)return toast("Вибери файл.",true);
  const rec=state.selected;const form=new FormData();
  form.append("sheet",rec.sheet);form.append("record_id",rec.id);form.append("file",file);
  try{await api("/attachments",{method:"POST",body:form});toast("Файл збережено у PostgreSQL");await loadAttachments();}
  catch(e){toast(e.message,true);}
}
async function saveLink(){
  const url=el("linkUrl").value.trim();if(!url)return;
  const rec=state.selected;
  try{await api("/links",{method:"POST",json:{sheet:rec.sheet,record_id:rec.id,url,title:url,note:""}});
    el("linkUrl").value="";toast("Посилання прикріплено");await loadAttachments();}
  catch(e){toast(e.message,true);}
}
function createForm(){
  detailShell("Нова заявка","Telegram · PostgreSQL",`
    <form id="requestForm" class="stack">
      <label class="field">Проєкт<input name="project" required maxlength="120" placeholder="Наприклад, АР-26"></label>
      <label class="field">Що купити<input name="item" required maxlength="300" placeholder="Матеріал / виріб"></label>
      <label class="field">Кількість<input name="quantity" required inputmode="decimal" placeholder="7"></label>
      <label class="field">Одиниця<input name="unit" required maxlength="30" placeholder="шт"></label>
      <label class="field">Характеристики<textarea name="details" maxlength="2000"></textarea></label>
      <button class="button primary" type="submit">Створити заявку</button>
    </form>`);
  el("requestForm").onsubmit=async e=>{
    e.preventDefault();
    const obj=Object.fromEntries(new FormData(e.target).entries());
    try{const result=await api("/requests",{method:"POST",json:obj});
      closeDetail();toast("Заявку "+result.id+" створено");await loadDashboard();await loadRecords();}
    catch(e){toast(e.message,true);}
  };
}
async function renderMigration(){
  el("overview").innerHTML="";el("items").innerHTML=`<div class="migration">
    <h2>Імпорт Google Sheets → PostgreSQL</h2>
    <p>Одноразове перенесення шести аркушів. Стара таблиця залишається без змін.
       Повторний імпорт не перезаписує існуючі записи.</p>
    <button class="button" id="migrationPreview">Перевірити джерело</button>
    <button class="button primary" id="migrationImport">Імпортувати підтверджені дані</button>
    <pre id="migrationInfo">Очікую перевірку…</pre>
  </div>`;
  el("pagination").textContent="";el("more").hidden=true;
  el("migrationPreview").onclick=async()=>{try{
    const report=await api("/migration/preview");
    el("migrationInfo").textContent=JSON.stringify(report,null,2);
  }catch(e){el("migrationInfo").textContent=e.message;}};
  el("migrationImport").onclick=async()=>{
    if(!confirm("Імпортувати дані з Google Sheets в PostgreSQL? Існуючі записи НЕ буде перезаписано."))return;
    try{const report=await api("/migration/import",{method:"POST",json:{confirm:"IMPORT"}});
      el("migrationInfo").textContent=JSON.stringify(report,null,2);toast("Імпорт завершено");
    }catch(e){toast(e.message,true);}};
  try{const status=await api("/migration/status");
    el("migrationInfo").textContent=JSON.stringify(status,null,2);
  }catch(e){el("migrationInfo").textContent=e.message;}
}
let debounce;
el("search").addEventListener("input",()=>{
  clearTimeout(debounce);debounce=setTimeout(()=>{state.query=el("search").value.trim();loadRecords();},320);
});
el("refresh").onclick=()=>openPage(state.section);
el("more").onclick=()=>loadRecords(true);
el("createRequest").onclick=createForm;
el("closeDetail").onclick=closeDetail;
el("overlay").onclick=e=>{if(e.target===el("overlay"))closeDetail();};
document.addEventListener("keydown",e=>{if(e.key==="Escape")closeDetail();});
async function start(){
  try{
    state.me=await api("/me");
    el("userPill").textContent=state.me.name+" · "+({"owner":"Власник","buyer":"Закупівлі","storekeeper":"Склад"}[state.me.role]||state.me.role);
    el("sideRole").textContent=state.me.role;
    let defaultPage=state.me.role==="storekeeper"?"Склад":state.me.role==="buyer"?"Закупка":"Заявки";
    await openPage(defaultPage);
  }catch(e){
    el("pageTitle").textContent="Доступ обмежений";
    el("overview").innerHTML="";
    el("items").innerHTML='<div class="empty error">'+esc(e.message)+
      '<p>Відкрий Mini App кнопкою в Telegram-боті Шухер. Звичайний браузер не передає підпис Telegram.</p></div>';
    el("search").closest(".toolbar").classList.add("hidden");
    el("pagination").textContent="";
  }
}
start();
