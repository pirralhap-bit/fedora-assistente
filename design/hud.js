const N=DATA.nodes, L=DATA.links;
const $=id=>document.getElementById(id);
const el=(tag,cls,txt)=>{const e=document.createElement(tag);if(cls)e.className=cls;if(txt!=null)e.textContent=txt;return e};
const css=v=>getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const tz='America/Sao_Paulo';
const fmt=(o,d=new Date())=>new Intl.DateTimeFormat('pt-BR',{timeZone:tz,...o}).format(d);

// ---- graph data
const deg=N.map(()=>0), nb=N.map(()=>new Set());
L.forEach(([a,b])=>{deg[a]++;deg[b]++;nb[a].add(b);nb[b].add(a)});
const groups=[...new Set(N.map(n=>n.g))].sort();
const hues=[160,28,205,340,95,260,45,185,10,300,125,230];
const colOf=g=>`hsl(${hues[groups.indexOf(g)%hues.length]} ${css('--node-s')} ${css('--node-l')})`;
const short=g=>g.replace(/^\d+-[A-Z]+\s*/,'').replace(/^[^\p{L}]+/u,'').trim()||'Raiz';
$('gstats').textContent=`${N.length} notas · ${L.length} ligações · ${deg.filter(d=>!d).length} soltas`;
function legend(){$('legend').replaceChildren(...groups.map(g=>{const s=el('span',null,short(g));const i=el('i');i.style.background=colOf(g);s.prepend(i);return s}))}

// ---- clock + B3
function tick(){
  const now=new Date();
  $('clock').textContent=fmt({hour:'2-digit',minute:'2-digit',hourCycle:'h23'},now);
  $('date').textContent=fmt({weekday:'long',day:'numeric',month:'long'},now);
  const p=Object.fromEntries(new Intl.DateTimeFormat('en-US',{timeZone:tz,weekday:'short',hour:'numeric',minute:'numeric',hourCycle:'h23'}).formatToParts(now).map(x=>[x.type,x.value]));
  // shortcut: ignores B3 holidays and the closing call; the real app should use the b3-* calendar
  const m=+p.hour*60+ +p.minute, open=!['Sat','Sun'].includes(p.weekday)&&m>=600&&m<1020;
  const s=$('pregao');s.textContent=open?'Pregão aberto · fecha 17h':'Pregão fechado · abre 10h';s.className='status '+(open?'open':'closed');
}
tick();setInterval(tick,15000);

// ---- machine meters (simulated; the real app reads /proc and the amdgpu sysfs)
const METERS=[
  {k:'cpu',name:'CPU',sub:'Xeon E5-2680 v4 · 28 threads',max:100,unit:'%',v:9,base:9},
  {k:'ram',name:'RAM',sub:'15 GB',max:15,unit:'GB',v:8.1,base:8.1},
  {k:'gpu',name:'GPU',sub:'RX 580',max:100,unit:'%',v:3,base:3},
  {k:'vram',name:'VRAM',sub:'8 GB',max:8,unit:'GB',v:.6,base:.6},
];
const num=new Intl.NumberFormat('pt-BR',{maximumFractionDigits:1});
let ollamaUntil=0;
METERS.forEach(m=>{m.hist=Array(40).fill(m.v);
  const row=el('div','meter');row.innerHTML=`<div class="mh"><span class="mn"></span><span class="mv"></span></div><canvas class="spark" aria-hidden="true"></canvas><div class="ms"></div>`;
  row.querySelector('.mn').textContent=m.name;row.querySelector('.ms').textContent=m.sub;m.row=row;$('meters').append(row)});
function meterTick(){
  const busy=Date.now()<ollamaUntil;
  const goal={cpu:busy?34:9,ram:busy?9.4:8.1,gpu:busy?88:3,vram:busy?5.2:.6};
  METERS.forEach(m=>{const g=goal[m.k];m.v+=(g-m.v)*.35+(Math.random()-.5)*m.max*.03;m.v=Math.max(0,Math.min(m.max,m.v));
    m.hist.push(m.v);m.hist.shift();
    const pct=m.v/m.max;m.row.querySelector('.mv').textContent=m.unit==='%'?`${Math.round(m.v)}%`:`${num.format(m.v)} / ${m.max} GB`;
    m.row.classList.toggle('hot',pct>.8);spark(m.row.querySelector('.spark'),m.hist,m.max)});
  const o=$('ollama');o.textContent=busy?'Ollama organizando o ditado · llama3.2:3b':'Ollama parado · 0 modelos na VRAM';o.classList.toggle('on',busy);
}
function spark(c,h,max){const w=c.clientWidth,ht=c.clientHeight,d=devicePixelRatio||1;if(!w)return;c.width=w*d;c.height=ht*d;
  const x=c.getContext('2d');x.setTransform(d,0,0,d,0,0);const col=css('--accent');
  x.beginPath();h.forEach((v,i)=>{const px=i/(h.length-1)*w,py=ht-1-(v/max)*(ht-2);i?x.lineTo(px,py):x.moveTo(px,py)});
  x.strokeStyle=col;x.lineWidth=1.5;x.stroke();x.lineTo(w,ht);x.lineTo(0,ht);x.closePath();x.globalAlpha=.15;x.fillStyle=col;x.fill();x.globalAlpha=1;
  const ly=ht-1-(h[h.length-1]/max)*(ht-2);x.fillStyle=col;x.beginPath();x.arc(w-2,ly,2.2,0,7);x.fill()}
meterTick();setInterval(meterTick,1000);

// ---- sidebar lists
function noteBtn(i,label,meta,swatch){
  const li=el('li'),b=el('button','item');b.type='button';
  if(swatch){const s=el('span','sw');s.dataset.g=N[i].g;b.append(s)}
  b.append(el('span','t',label));if(meta)b.append(el('span','m',meta));
  b.title=N[i].t;b.addEventListener('click',()=>focusNode(i,true));li.append(b);return li}
const projects=DATA.projects.map(([name,best,count])=>{
  const hub=N.findIndex(n=>n.f.includes(name)&&/hub/i.test(n.t));return[name.replace(/^PRJ-[A-Z0-9]+-\d+\s*/,''),hub>=0?hub:best,count]});
$('projects').replaceChildren(...projects.map(([n,i,c])=>noteBtn(i,n,c+(c===1?' nota':' notas'),false)));
const rel=new Intl.RelativeTimeFormat('pt-BR',{numeric:'auto'});
const ago=ts=>{const h=Math.round((ts*1000-Date.now())/36e5);return Math.abs(h)<24?rel.format(h,'hour'):rel.format(Math.round(h/24),'day')};
$('recent').replaceChildren(...DATA.recent.map(([i,ts])=>noteBtn(i,N[i].t.replace(/\.excalidraw$/,' (desenho)'),ago(ts),true)));
$('inbox').textContent=DATA.inbox?`${DATA.inbox} notas esperando processamento`:'Nada a processar em _a-processar.';

// ---- layout: folders anchored on a ring, short force pass
const P=N.map(n=>{const k=groups.indexOf(n.g),a=k/groups.length*Math.PI*2;
  return{x:Math.cos(a)*250+(Math.random()-.5)*140,y:Math.sin(a)*250+(Math.random()-.5)*140,vx:0,vy:0,ax:Math.cos(a)*230,ay:Math.sin(a)*230}});
for(let k=0;k<260;k++){const al=1-k/300;
  for(let i=0;i<P.length;i++)for(let j=i+1;j<P.length;j++){const a=P[i],b=P[j],dx=a.x-b.x,dy=a.y-b.y,d2=dx*dx+dy*dy+.01;if(d2>40000)continue;
    const f=800/d2*al;a.vx+=dx*f;a.vy+=dy*f;b.vx-=dx*f;b.vy-=dy*f}
  L.forEach(([i,j])=>{const a=P[i],b=P[j],dx=b.x-a.x,dy=b.y-a.y,d=Math.hypot(dx,dy)||1,f=(d-36)*.02*al;a.vx+=dx/d*f;a.vy+=dy/d*f;b.vx-=dx/d*f;b.vy-=dy/d*f});
  P.forEach(p=>{p.vx+=(p.ax-p.x)*.004*al;p.vy+=(p.ay-p.y)*.004*al;p.vx*=.82;p.vy*=.82;p.x+=p.vx;p.y+=p.vy})}
// ---- orbit layout (film style): folders circle a core on concentric rings, notes circle their folder
const ORBIT=css('--layout')==='orbit';
let orbitT=0;const RINGS=[],GI={};
if(ORBIT){
  const off=N.map(()=>({x:0,y:0,vx:0,vy:0}));
  groups.forEach(g=>{
    const ids=N.map((n,i)=>n.g===g?i:-1).filter(i=>i>=0),set=new Set(ids);
    ids.forEach((i,k)=>{const a=k*2.4,r=6*Math.sqrt(k);off[i].x=Math.cos(a)*r;off[i].y=Math.sin(a)*r});
    const inner=L.filter(([a,b])=>set.has(a)&&set.has(b));
    for(let k=0;k<200;k++){const al=1-k/240;
      for(let x=0;x<ids.length;x++)for(let y=x+1;y<ids.length;y++){const a=off[ids[x]],b=off[ids[y]],dx=a.x-b.x,dy=a.y-b.y,d2=dx*dx+dy*dy+.01;if(d2>10000)continue;
        const f=260/d2*al;a.vx+=dx*f;a.vy+=dy*f;b.vx-=dx*f;b.vy-=dy*f}
      inner.forEach(([i,j])=>{const a=off[i],b=off[j],dx=b.x-a.x,dy=b.y-a.y,d=Math.hypot(dx,dy)||1,f=(d-22)*.03*al;a.vx+=dx/d*f;a.vy+=dy/d*f;b.vx-=dx/d*f;b.vy-=dy/d*f});
      ids.forEach(i=>{const p=off[i];p.vx-=p.x*.01*al;p.vy-=p.y*.01*al;p.vx*=.8;p.vy*=.8;p.x+=p.vx;p.y+=p.vy})}
    const r=Math.max(14,...ids.map(i=>Math.hypot(off[i].x,off[i].y)))+8;
    GI[g]={ids,r,ring:ids.length<=5?0:ids.length<=30?1:2};
  });
  N.forEach((n,i)=>{P[i].ox=off[i].x;P[i].oy=off[i].y});
  let prev=0,prevMax=0;
  for(let k=0;k<3;k++){const gs=groups.filter(g=>GI[g].ring===k);if(!gs.length)continue;
    const maxr=Math.max(...gs.map(g=>GI[g].r)),need=gs.reduce((s,g)=>s+2*GI[g].r+40,0)/(2*Math.PI);
    const R=Math.max(prev?prev+prevMax+maxr+50:60+maxr,need);RINGS.push({R,k,speed:(k%2?-1:1)*.035/(k+1)});
    let a=k*1.1;const tot=gs.reduce((s,g)=>s+2*GI[g].r+40,0);
    gs.forEach(g=>{const span=(2*GI[g].r+40)/tot*Math.PI*2;GI[g].phase=a+span/2;GI[g].R=R;GI[g].speed=RINGS.at(-1).speed;a+=span});
    prev=R;prevMax=maxr}
}
function place(){groups.forEach(g=>{const G=GI[g],a=G.phase+G.speed*orbitT,cx0=Math.cos(a)*G.R,cy0=Math.sin(a)*G.R,s=orbitT*.06,c=Math.cos(s),sn=Math.sin(s);
  G.cx=cx0;G.cy=cy0;G.ids.forEach(i=>{const p=P[i];p.x=cx0+p.ox*c-p.oy*sn;p.y=cy0+p.ox*sn+p.oy*c})})}
if(ORBIT)place();
const bounds=()=>{let a=1e9,b=1e9,c=-1e9,d=-1e9;P.forEach(p=>{a=Math.min(a,p.x);b=Math.min(b,p.y);c=Math.max(c,p.x);d=Math.max(d,p.y)});return{x:(a+c)/2,y:(b+d)/2,w:c-a,h:d-b}};

// ---- render: names appear only around a note you asked about
const cv=$('g'),cx=cv.getContext('2d');let W=1,H=1,dpr=1,view={x:0,y:0,s:1},target=null,focus=-1,colors=[];
const reduce=matchMedia('(prefers-reduced-motion: reduce)');
const toS=p=>[(p.x-view.x)*view.s+W/2,(p.y-view.y)*view.s+H/2];
function fitView(){if(ORBIT){const R=Math.max(...groups.map(g=>GI[g].R+GI[g].r));return{x:0,y:0,s:Math.min(W,H-60)/(2*R+30)}}
  const b=bounds();return{x:b.x,y:b.y-10,s:Math.min(W/(b.w+80),(H-90)/(b.h+80))}}
function resize(){dpr=devicePixelRatio||1;W=cv.clientWidth;H=cv.clientHeight;cv.width=W*dpr;cv.height=H*dpr;if(!resize.done&&W>10){view=fitView();resize.done=1}draw()}
function draw(){
  cx.setTransform(dpr,0,0,dpr,0,0);cx.clearRect(0,0,W,H);
  const lit=focus<0?null:new Set([focus,...nb[focus]]),edge=css('--edge'),on=css('--edge-on'),lab=css('--label'),acc=css('--accent'),glow=+css('--glow')||0;
  cx.lineWidth=1;
  if(ORBIT){const[ox,oy]=toS({x:0,y:0});
    cx.setLineDash([2,6]);cx.strokeStyle=css('--line');RINGS.forEach(r=>{cx.beginPath();cx.arc(ox,oy,r.R*view.s,0,7);cx.stroke()});cx.setLineDash([]);
    groups.forEach(g=>{const G=GI[g],[gx,gy]=toS({x:G.cx,y:G.cy});cx.strokeStyle=colOf(g);cx.globalAlpha=.25;cx.beginPath();cx.arc(gx,gy,G.r*view.s,0,7);cx.stroke()});
    cx.globalAlpha=1;const cr=Math.max(8,26*view.s),gr=cx.createRadialGradient(ox,oy,0,ox,oy,cr*2.2);
    gr.addColorStop(0,acc);gr.addColorStop(.35,css('--accent-soft'));gr.addColorStop(1,'transparent');cx.fillStyle=gr;cx.beginPath();cx.arc(ox,oy,cr*2.2,0,7);cx.fill();
    cx.strokeStyle=acc;cx.lineWidth=2;cx.beginPath();cx.arc(ox,oy,cr,0,7);cx.stroke();cx.lineWidth=1}
  L.forEach(([i,j])=>{const hot=lit&&(i===focus||j===focus);cx.strokeStyle=hot?on:edge;cx.globalAlpha=lit&&!hot?.35:1;
    const a=toS(P[i]),b=toS(P[j]);cx.beginPath();cx.moveTo(a[0],a[1]);cx.lineTo(b[0],b[1]);cx.stroke()});
  cx.globalAlpha=1;
  const labels=[];
  P.forEach((p,i)=>{const[x,y]=toS(p),r=(2+Math.sqrt(deg[i])*1.4)*Math.min(1.6,Math.max(.8,view.s));
    cx.globalAlpha=lit&&!lit.has(i)?.18:1;const c=i===focus?acc:colors[i];cx.fillStyle=c;
    if(glow){cx.shadowColor=c;cx.shadowBlur=glow}cx.beginPath();cx.arc(x,y,r,0,7);cx.fill();cx.shadowBlur=0;
    if(i===focus){cx.strokeStyle=acc;cx.lineWidth=2;cx.beginPath();cx.arc(x,y,r+4,0,7);cx.stroke();cx.lineWidth=1}
    if(lit&&lit.has(i))labels.push([x+r+5,y+4,N[i].t,i===focus])});
  cx.globalAlpha=1;cx.fillStyle=lab;
  labels.forEach(([x,y,t,main])=>{cx.font=`${main?600:400} 12px ${css('--label-font')}`;cx.fillText(t.length>38?t.slice(0,37)+'…':t,x,y)});
}
function recolor(){colors=N.map(n=>colOf(n.g));legend();document.querySelectorAll('.sw').forEach(s=>s.style.background=colOf(s.dataset.g));draw()}
matchMedia('(prefers-color-scheme: dark)').addEventListener('change',recolor);
new MutationObserver(recolor).observe(document.documentElement,{attributes:true,attributeFilter:['data-theme']});
function go(t){if(reduce.matches){view=t;target=null;draw();return}const was=target;target=t;if(!was)requestAnimationFrame(anim)}
function anim(){if(!target)return;['x','y','s'].forEach(k=>view[k]+=(target[k]-view[k])*.15);draw();
  if(Math.abs(target.x-view.x)+Math.abs(target.y-view.y)+Math.abs(target.s-view.s)*100<.4){view={...target};target=null;draw()}else requestAnimationFrame(anim)}
function focusNode(i,announce){focus=i;go({x:P[i].x,y:P[i].y,s:Math.max(view.s,2.4)});
  if(announce){const n=[...nb[i]];sys(`${N[i].t} — ${N[i].f||'raiz'}. ${n.length?`Ligada a ${n.length} ${n.length===1?'nota':'notas'}.`:'Nota solta, sem ligações.'}`)}}
function clearFocus(){focus=-1;draw()}

// pan, pinch, wheel, click
const ptr=new Map();let drag=null,pinch=null;
cv.addEventListener('pointerdown',e=>{cv.setPointerCapture(e.pointerId);ptr.set(e.pointerId,{x:e.clientX,y:e.clientY});target=null;
  if(ptr.size===1)drag={x:e.clientX,y:e.clientY,vx:view.x,vy:view.y,moved:false};
  if(ptr.size===2){const[a,b]=[...ptr.values()];pinch={d:Math.hypot(a.x-b.x,a.y-b.y),s:view.s};drag=null}});
cv.addEventListener('pointermove',e=>{if(!ptr.has(e.pointerId))return;ptr.set(e.pointerId,{x:e.clientX,y:e.clientY});
  if(pinch&&ptr.size===2){const[a,b]=[...ptr.values()];view.s=Math.min(6,Math.max(.2,pinch.s*Math.hypot(a.x-b.x,a.y-b.y)/pinch.d));draw();return}
  if(!drag)return;const dx=e.clientX-drag.x,dy=e.clientY-drag.y;if(Math.abs(dx)+Math.abs(dy)>4){drag.moved=true;cv.classList.add('dragging')}
  view.x=drag.vx-dx/view.s;view.y=drag.vy-dy/view.s;draw()});
function up(e){ptr.delete(e.pointerId);cv.classList.remove('dragging');if(ptr.size<2)pinch=null;
  const d=drag;drag=null;if(!d||d.moved||e.type==='pointercancel')return;
  const r=cv.getBoundingClientRect(),mx=e.clientX-r.left,my=e.clientY-r.top;let best=-1,bd=225;
  P.forEach((p,i)=>{const[x,y]=toS(p),dd=(x-mx)**2+(y-my)**2;if(dd<bd){bd=dd;best=i}});
  best<0?clearFocus():focusNode(best,true)}
cv.addEventListener('pointerup',up);cv.addEventListener('pointercancel',up);
cv.addEventListener('wheel',e=>{e.preventDefault();target=null;view.s=Math.min(6,Math.max(.2,view.s*Math.exp(-e.deltaY*.0015)));draw()},{passive:false});
$('zin').onclick=()=>go({...view,s:Math.min(6,view.s*1.4)});
$('zout').onclick=()=>go({...view,s:Math.max(.2,view.s/1.4)});
$('fit').onclick=()=>{focus=-1;go(fitView())};

// ---- centre of the screen: graph, or the dictation desk (the graph steps aside while you write)
function showMode(dict){$('stage').hidden=dict;$('p-dict').hidden=!dict;
  $('t-chat').setAttribute('aria-pressed',!dict);$('t-dict').setAttribute('aria-pressed',dict);
  document.body.classList.toggle('dictating',dict);if(!dict)resize()}
$('t-chat').onclick=()=>showMode(false);
$('t-dict').onclick=()=>{showMode(true);$('rec').focus()};

// ---- conversation (test mode: finds notes, no AI)
const msgs=$('msgs');
const push=m=>{msgs.append(m);msgs.scrollTop=msgs.scrollHeight};
const sys=t=>push(el('div','msg sys',t));
const norm=s=>s.normalize('NFD').replace(/[̀-ͯ]/g,'').toLowerCase();
function search(q){const w=norm(q).split(/[^a-z0-9]+/).filter(x=>x.length>2);if(!w.length)return[];
  return N.map((n,i)=>{const t=norm(n.t),f=norm(n.f);return[i,w.reduce((s,x)=>s+(t.includes(x)?3:f.includes(x)?1:0),0)+deg[i]*.01]})
    .filter(x=>x[1]>=1).sort((a,b)=>b[1]-a[1]).slice(0,4).map(x=>x[0])}
function ask(q){
  const you=el('div','msg you');you.append(el('p',null,q));push(you);
  const hits=search(q),m=el('div','msg jar');m.append(el('span','who','Jarvis'));
  if(!hits.length){clearFocus();m.append(el('p',null,'Não achei nota sobre isso no vault. Com o Claude ligado, eu responderia mesmo assim, sem citar nota.'));push(m);return}
  m.append(el('p',null,`Com o Claude ligado, eu responderia usando ${hits.length===1?'esta nota':'estas '+hits.length+' notas'}. Clique para ver no grafo:`));
  const refs=el('div','refs');hits.forEach(i=>{const b=el('button','ref',N[i].t);b.type='button';b.onclick=()=>focusNode(i,false);refs.append(b)});
  m.append(refs);push(m);focusNode(hits[0],false)}
$('f').addEventListener('submit',e=>{e.preventDefault();const q=$('q').value.trim();if(!q)return;$('q').value='';ask(q)});
$('mic').onclick=()=>{showMode(true);$('rec').focus()};
['O que mudou no Fatia-Ai?','Resumo do Auto-Trade','Notas de Salesforce'].forEach(t=>{const b=el('button','sg',t);b.type='button';b.onclick=()=>ask(t);$('sugg').append(b)});
sys(`${N.length} notas do Cofre Principal carregadas. Neste protótipo eu só encontro as notas que usaria; o Claude ainda não está ligado.`);

// ---- dictation: whisper.cpp hears, Ollama tidies, you approve, it is appended to the note
const today=fmt({day:'2-digit'})+' - '+fmt({weekday:'long'});
const month=new Intl.DateTimeFormat('pt-BR',{timeZone:tz,month:'2-digit'}).format(new Date())+' - '+fmt({month:'long'});
const DEST=[
  ['daily',`Daily de hoje · ${today}`,`08-DNT 📅 Daily Notes/${fmt({year:'numeric'})}/${month}/${today}.md`],
  ['inbox','Nova nota no Inbox','00-INB 📥 Inbox/_a-processar/Ditado '+fmt({day:'2-digit',month:'2-digit',year:'numeric'}).replaceAll('/','-')+'.md'],
];
DEST.forEach(([v,l])=>{const o=el('option',null,l);o.value=v;$('dest').append(o)});
const SAMPLE='então hoje eu fechei o protótipo do jarvis no estilo filme e o painel limpo também ficou salvo no projeto o que eu achei é que o grafo precisava parecer mais vivo tipo as pastas orbitando em volta de um núcleo e quando eu for ditar o grafo tem que sair da frente pra eu ter espaço pra escrever ah e amanhã eu preciso testar o ollama na rx 580 ver se o vulkan funciona e qual modelo cabe nos oito giga de vram acho que um de sete ou oito bilhões de parâmetros e também lembrar de comprar filamento petg preto pro gabinete da impressora e responder o cliente da vitrine sobre o cupom vitrine cinco';
const SAMPLE_OUT=['#### Jarvis','- Protótipo do HUD fechado nos dois estilos: filme e painel limpo, ambos salvos no projeto.','- O grafo precisa parecer mais vivo: pastas orbitando em volta de um núcleo.','- Ao ditar, o grafo sai da frente e o texto ganha o centro da tela.','','#### Próximos passos','- Testar o Ollama na RX 580 com Vulkan e ver qual modelo cabe nos 8 GB de VRAM (provável: 7 a 8 bilhões de parâmetros). #to-do','- Comprar filamento PETG preto para o gabinete da impressora. #to-do','- Responder o cliente da vitrine sobre o cupom VITRINE5. #to-do'];
let recTimer=null,recStart=0;
function stopRec(){clearInterval(recTimer);recTimer=null;$('rec').setAttribute('aria-pressed','false');$('rec').classList.remove('on');$('rec-l').textContent='Ditar'}
$('rec').onclick=()=>{
  if(recTimer){stopRec();return}
  $('rec').setAttribute('aria-pressed','true');$('rec').classList.add('on');$('rec-l').textContent='Parar';recStart=Date.now();
  $('dhint').textContent='Simulação: no navegador o microfone fica desligado, então escrevo uma fala de exemplo.';
  const words=SAMPLE.split(' ');let k=0;$('raw').value='';
  recTimer=setInterval(()=>{$('raw').value+=(k?' ':'')+words[k++];$('raw').scrollTop=$('raw').scrollHeight;
    const s=Math.round((Date.now()-recStart)/1000);$('rect').textContent=`${Math.floor(s/60)}:${String(s%60).padStart(2,'0')}`;
    if(k>=words.length)stopRec()},reduce.matches?0:90)};
function tidy(raw){
  if(norm(raw)===norm(SAMPLE))return SAMPLE_OUT;
  // shortcut: sentence split stands in for the Ollama prompt; the real app sends raw text to the local model
  return raw.split(/(?<=[.!?])\s+|\n+/).map(s=>s.trim()).filter(Boolean).map(s=>'- '+s[0].toUpperCase()+s.slice(1)+(/[.!?]$/.test(s)?'':'.'))}
$('tidy').onclick=()=>{if(recTimer){stopRec();$('raw').value=SAMPLE}const raw=$('raw').value.trim();if(!raw){$('dhint').textContent='Dite ou escreva algo antes de organizar.';$('raw').focus();return}
  ollamaUntil=Date.now()+4000;$('tidy').disabled=true;$('tidy').textContent='Organizando…';$('dhint').textContent='Ollama rodando na GPU, veja o painel Máquina.';
  setTimeout(()=>{const hora=fmt({hour:'2-digit',minute:'2-digit',hourCycle:'h23'});
    $('out').value=`### ${hora} · ditado\n\n`+tidy(raw).join('\n');$('tidy').disabled=false;$('tidy').textContent='Organizar com Ollama';
    $('dhint').textContent='Revise o texto. Os itens com #to-do aparecem na sua lista de tarefas.'},reduce.matches?300:2200)};
$('read').onclick=()=>{const t=($('out').value||$('raw').value).replace(/^#+\s.*$/m,'').replace(/[-#]|to-do/g,' ').trim();
  if(!t){$('dhint').textContent='Não há texto para ler.';return}
  if(!('speechSynthesis' in window)){$('dhint').textContent='Este navegador não lê em voz alta. No app do PC quem lê é o Piper.';return}
  speechSynthesis.cancel();const u=new SpeechSynthesisUtterance(t);u.lang='pt-BR';speechSynthesis.speak(u);
  $('dhint').textContent='Lendo com a voz do navegador. No app do PC quem lê é o Piper.'};
$('save').onclick=()=>{if(!$('out').value.trim()){$('dhint').textContent='Organize o texto antes de salvar.';return}
  const d=DEST.find(x=>x[0]===$('dest').value);$('dhint').textContent=`No app do PC, isto entra no fim de ${d[2]}. Aqui no protótipo nada foi gravado.`};

// orbit clock: stops while a note is in focus, while dictating, off-screen, or with reduced motion
let last=performance.now();
function orbitLoop(now){const dt=Math.min(.1,(now-last)/1000);last=now;
  if(focus<0&&!drag&&!pinch&&!$('stage').hidden&&!document.hidden&&!reduce.matches){orbitT+=dt;place();if(!target)draw()}
  requestAnimationFrame(orbitLoop)}
if(ORBIT)requestAnimationFrame(orbitLoop);
addEventListener('resize',()=>{resize();meterTick()});recolor();resize();
document.fonts&&document.fonts.ready.then(()=>{resize();recolor()});
