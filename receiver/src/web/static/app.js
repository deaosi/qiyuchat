'use strict';
const $ = (s, root = document) => root.querySelector(s);
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const n = v => Number(v || 0).toLocaleString('zh-CN');
const titles = {overview:'会话概览',questions:'高频问题',clusters:'问题簇',groups:'客户群',customers:'客户',pairs:'问答明细',cleaning:'数据清洗'};
const subtitles = {overview:'从高频问题，回到每一段真实咨询。',questions:'按当前筛选中的出现次数排序，点击查看具体问答。',clusters:'相似说法放在一起，保留原话，方便逐条复核。',groups:'按客户群查看咨询量与共同问题。',customers:'查看同一客户问过什么；匿名客户按会话区分。',pairs:'检索客户问题、客服回复，并追溯会话上下文。',cleaning:'本地文件导入、规则整理和处理记录。'};
const S = {source:'local',tab:'overview',days:0,start:'',end:'',group:'',staff:'',q:'',replied:'',cluster:0,customer:'',offset:0,limit:25,busy:false,revision:0,detailRevision:0};
let controller, file = null, fileDigest = '', toastTimer;
function params(extra = {}, details = false) {
  const p = {source:S.source,days:S.days,start:S.start,end:S.end,group:S.group,staff:S.staff};
  if(details) Object.assign(p,{q:S.q,replied:S.replied,cluster:S.cluster,customer:S.customer});
  return new URLSearchParams(Object.entries({...p,...extra}).filter(([,v]) => v !== '' && v !== null));
}
async function request(name, extra = {}, details = false, signal) {
  const r = await fetch(`/api/${name}?${params(extra,details)}`, {signal});
  const data = await r.json();
  if(!r.ok) throw new Error(data.error || '本地查询失败');
  return data;
}
async function post(name, bytes = new Uint8Array(), headers = {}, source = S.source) {
  const r = await fetch(`/api/${name}?source=${source}`, {method:'POST',headers:{'Content-Type':'application/octet-stream','X-Local-Request':'1',...headers},body:bytes});
  const data = await r.json();
  if(!r.ok) throw new Error(data.error || '本地操作失败');
  return data;
}
function toast(text) { clearTimeout(toastTimer); if($('#import-dialog').open) $('#import-state').textContent = text; $('#toast').textContent = text; $('#toast').hidden = false; toastTimer = setTimeout(() => $('#toast').hidden = true,6000); }
function busy(value) { S.busy = value; $('#source').disabled = value; document.querySelectorAll('[data-action="import"], #export, #preview-file, #confirm-import, #confirm-clean, #file').forEach(el => el.disabled = value); if(!value) $('#confirm-import').disabled = !fileDigest; }
function icon(name) { return `<svg class="icon" aria-hidden="true"><use href="#i-${name}"/></svg>`; }
function empty(title, description, actions = '') { return `<div class="empty"><div class="empty-symbol" aria-hidden="true">${icon('inbox')}</div><h2>${esc(title)}</h2><p>${esc(description)}</p><div class="actions">${actions}</div></div>`; }
function noMatch() { return empty('没有匹配的问答','可以放宽日期、客户群或客服筛选；缺少时间的记录只会在“全部时间”中出现。','<button class="button" data-action="reset">重置全部筛选</button>'); }
function badge(r) { return `<span class="badge ${r.replied ? 'success' : 'warning'}">${icon(r.replied ? 'check' : 'warning')}${r.replied ? '有回复' : '未回复'}</span>`; }
function table(headers, rows) { return `<div class="table-scroll"><table><thead><tr>${headers.map(h => `<th scope="col">${h}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table></div>`; }
function panelHead(title, note = '', button = '') { return `<div class="panel-head"><div><h2>${title}</h2>${note ? `<p>${note}</p>` : ''}</div>${button}</div>`; }
function jumpButton(tab, text) { return `<button class="text-button" data-tab="${tab}">${text}</button>`; }
function pairLink(r) { return `<button class="table-link" data-detail="${r.qa_id}">${esc(r.question)}</button>`; }
function clusterLink(r) { return `<button class="table-link" data-cluster="${r.cluster_id}">${esc(r.question || r.rep)}</button>`; }
function resetDetails() { Object.assign(S,{q:'',replied:'',cluster:0,customer:'',offset:0}); }
function syncFilters() {
  $('#range').value = S.start || S.end ? 'custom' : String(S.days);
  $('#custom-dates').hidden = $('#range').value !== 'custom';
  $('#start').value=S.start; $('#end').value=S.end; $('#group').value=S.group; $('#staff').value=S.staff;
}
function resetFilters() { Object.assign(S,{days:0,start:'',end:'',group:'',staff:''}); resetDetails(); syncFilters(); render(); }
function navigate(tab) {
  if(!titles[tab]) return;
  if(S.tab !== tab) resetDetails();
  S.tab = tab; render();
}
function drill(kind, value) {
  resetDetails();
  if(kind === 'group') { S.group=value; $('#group').value=value; }
  else S[kind]=value;
  S.tab='pairs'; render();
}
async function updateMeta(signal) {
  const m = await request('meta',{},false,signal);
  $('#database').textContent = m.db_name;
  for(const [id,values,label] of [['group',m.groups,'全部客户群'],['staff',m.staff,'全部客服']]) {
    const select=$('#'+id), selected=S[id];
    select.innerHTML=`<option value="">${label}</option>`+values.map(v=>`<option value="${esc(v)}">${esc(v)}</option>`).join('');
    if(selected && !values.includes(selected)) select.add(new Option(selected,selected));
    select.value=selected;
  }
  return m;
}
async function render() {
  const rev=++S.revision;
  controller?.abort(); controller=new AbortController(); const signal=controller.signal;
  $('#chart-tooltip').hidden=true;
  $('#page-title').textContent=titles[S.tab]; $('#page-subtitle').textContent=subtitles[S.tab];
  document.querySelectorAll('#nav button').forEach(b=>{const active=b.dataset.tab===S.tab;b.classList.toggle('active',active);if(active)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current');});
  $('#source').value=S.source; $('#source-notice').hidden=S.source!=='demo';
  $('#filters').hidden=S.tab==='cleaning'; $('#filter-caption').hidden=S.tab==='cleaning';
  $('#filter-caption').textContent=`统计口径：${S.start || S.end ? `${S.start || '不限起始'} 至 ${S.end || '不限结束'}` : S.days ? `最近 ${S.days} 个自然日` : '全部时间（含未知）'}，${S.group || '全部客户群'}，${S.staff || '全部客服'}内的有效问答。`;
  const content=$('#content'); content.setAttribute('aria-busy','true');
  try {
    const meta=await updateMeta(signal);
    let html;
    if(S.tab==='cleaning') html=renderCleaning(await request('status',{},false,signal));
    else if(!meta.total) html=empty(S.source==='demo' ? '演示数据尚未载入' : '从一份本地表格开始','导入会话正文或问答表，自动过滤寒暄、抽取问答并归并相似问题。不需要外部账号或模型凭证。',`<button class="button primary" data-action="${S.source==='demo' ? 'demo' : 'import'}">${S.source==='demo' ? '载入演示数据' : '导入本地数据'}</button>${S.source==='local' ? '<button class="button" data-action="demo">先看演示数据</button>' : ''}<a class="text-link" href="/api/template">下载导入模板</a>`);
    else if(S.tab==='overview') { const [summary,t]=await Promise.all([request('summary',{},false,signal),request('trend',{},false,signal)]); html=renderOverview(summary,t); }
    else if(S.tab==='questions') html=renderQuestions(await request('questions',{top:100},false,signal));
    else if(S.tab==='clusters') html=renderClusters(await request('clusters',{},false,signal));
    else if(S.tab==='groups') html=renderGroups(await request('groups',{},false,signal));
    else if(S.tab==='customers') html=renderCustomers(await request('customers',{},false,signal));
    else html=renderPairs(await request('pairs',{limit:S.limit,offset:S.offset},true,signal));
    if(rev!==S.revision) return;
    content.innerHTML=html;
  } catch(e) {
    if(e.name==='AbortError' || rev!==S.revision) return;
    content.innerHTML=empty('暂时无法读取数据',e.message,'<button class="button" data-action="retry">重试</button>');
  } finally {if(rev===S.revision) content.setAttribute('aria-busy','false');}
}
function ranks(items,total) {
  const max=items[0]?.count || 1;
  return `<ol class="ranking">${items.map((r,i)=>`<li><button class="rank-button" data-cluster="${r.cluster_id}" data-tip="${esc(r.question)}：${n(r.count)} 次，占当前 ${n(total)} 条问答的 ${r.share}%"><span class="rank-index">${i+1}</span><span><span class="rank-question">${esc(r.question)}</span><span class="bar-track" aria-hidden="true"><span class="bar-fill" style="display:block;width:${r.count/max*100}%"></span></span></span><span class="rank-value">${n(r.count)}<small>${r.share}%</small></span></button></li>`).join('')}</ol>`;
}
function rankTable(items) { return `<details class="chart-table"><summary>查看排行数据表</summary>${table(['问题','次数','客户数','占比'],items.map(r=>`<tr><td>${clusterLink(r)}</td><td class="num">${n(r.count)}</td><td class="num">${n(r.customers)}</td><td class="num">${r.share}%</td></tr>`))}</details>`; }
function trendChart(data) {
  const series=data.series;
  if(!series.length) return '<div class="inline-empty">所选范围没有可绘制的日期。</div>';
  const w=510,h=226,p={l:36,r:14,t:15,b:33},pw=w-p.l-p.r,ph=h-p.t-p.b;
  const max=Math.max(1,...series.map(d=>d.questions)); const ymax=Math.max(2,Math.ceil(max/4)*4);
  const x=i=>p.l+(series.length===1 ? pw/2 : i/(series.length-1)*pw);
  const y=v=>p.t+ph-v/ymax*ph;
  const line=series.map((d,i)=>`${i?'L':'M'}${x(i).toFixed(2)},${y(d.questions).toFixed(2)}`).join(' ');
  const ticks=[0,.25,.5,.75,1].map(f=>`<line class="chart-grid" x1="${p.l}" x2="${w-p.r}" y1="${y(ymax*f)}" y2="${y(ymax*f)}"/><text class="chart-label" x="${p.l-9}" y="${y(ymax*f)+4}" text-anchor="end">${ymax*f}</text>`).join('');
  const dates=[...new Set([0,Math.floor((series.length-1)/2),series.length-1])].map(i=>`<text class="chart-label" x="${x(i)}" y="${h-9}" text-anchor="${i===0?'start':i===series.length-1?'end':'middle'}">${series[i].date.slice(5)}</text>`).join('');
  return `<div class="trend"><svg viewBox="0 0 ${w} ${h}" role="group" aria-label="每日有效问答数。悬停或聚焦数据点读数；下方有完整数据表。">${ticks}${dates}<path class="trend-line" d="${line}"/>${series.map((d,i)=>`<circle class="trend-marker" cx="${x(i)}" cy="${y(d.questions)}" r="4"/><circle class="trend-hit" cx="${x(i)}" cy="${y(d.questions)}" r="10" tabindex="0" role="img" aria-label="${d.date}，${d.questions} 条问答" data-point="${x(i)}" data-tip="${d.date}：${n(d.questions)} 条问答"/>`).join('')}<line class="crosshair" hidden x1="0" x2="0" y1="${p.t}" y2="${h-p.b}"/></svg></div><details class="chart-table"><summary>查看每日数据表（${series.length} 天）</summary>${table(['日期','有效问答数'],series.map(d=>`<tr><td>${d.date}</td><td class="num">${n(d.questions)}</td></tr>`))}</details>${data.truncated ? '<p class="small muted">范围超过一年，趋势仅展示最后 366 天；其余指标仍按全部筛选统计。</p>':''}`;
}
function renderOverview(s,t) {
  if(!s.qa_pair) return noMatch();
  const metrics=[['有效问答',s.qa_pair,'过滤寒暄和纯信息后的问答','pairs','message'],['问题类别',s.cluster_count,'相似问句聚类，供人工复核','clusters','clusters'],['咨询客户',s.customer_count,'缺少客户ID时按会话区分','customers','team'],['未回复问答',s.unanswered,'抽取内容中没有客服回复','unanswered','warning']];
  return `<div class="metrics">${metrics.map(([label,val,note,tab,symbol])=>`<button class="metric actionable" ${tab==='unanswered'?'data-action="unanswered"':`data-tab="${tab}"`}><span class="metric-label">${icon(symbol)}${label}</span><span class="metric-value">${n(val)}</span><span class="metric-note${tab==='unanswered'?' warning':''}">${note}</span></button>`).join('')}</div><div class="grid-main"><section class="panel">${panelHead('客户反复问的是什么',`前 ${s.top.length} 类，分母为当前筛选 ${n(s.qa_pair)} 条问答`,jumpButton('questions','全部排行'))}${ranks(s.top,s.qa_pair)}${rankTable(s.top)}</section><section class="panel">${panelHead('每日有效问答','按提问日期统计；无记录的日期补零')}${trendChart(t)}<div class="panel-foot">${s.unknown_time ? `${n(s.unknown_time)} 条问答时间未知，未计入折线。`:'时间范围与上方筛选一致。'}数量是提问条数，不是会话数。</div></section></div><div class="grid-main"><section class="panel">${panelHead('最近的客户问题','点击原话，查看客服回答与上下文',jumpButton('pairs','查看明细'))}<div class="recent-list">${s.recent.map(r=>`<div class="recent-item"><div>${pairLink(r)}<div class="cell-meta">${esc(r.customer_name)}　${esc(r.group_name)}　${esc(r.asked_at_text)}</div></div>${badge(r)}</div>`).join('')}</div></section><section class="panel">${panelHead('本地数据处理状态','此处为当前数据源全库，不受日期或分组筛选影响',jumpButton('cleaning','管理数据'))}${statusList(s.local)}<div class="panel-foot">原始会话 ${n(s.raw_session)} 条，消息 ${n(s.raw_message)} 条。规则仅整理问答，不生成解决率或 AI 质检结论。</div></section></div>`;
}
function statusList(s) {
  return `<dl class="status-list"><div><dt>已处理 / 原始会话</dt><dd>${n(s.processed)} / ${n(s.sessions)}</dd></div><div><dt>有正文的会话</dt><dd>${n(s.with_content)}</dd></div><div><dt>待规则清洗</dt><dd>${n(s.pending)}</dd></div></dl><div class="status-progress" aria-label="已处理 ${s.processed} / ${s.sessions} 条会话"><span style="width:${s.sessions ? s.processed/s.sessions*100 : 0}%"></span></div><dl class="status-list"><div><dt>抽取到问答的会话</dt><dd>${n(s.with_pairs)}</dd></div><div><dt>正文中无有效问题</dt><dd>${n(s.no_question)}</dd></div><div><dt>缺少正文，无法抽取</dt><dd>${n(s.no_content)}</dd></div>${s.archived ? `<div><dt>保留既有问答（正文已归档）</dt><dd>${n(s.archived)}</dd></div>`:''}</dl>`;
}
function renderQuestions(data) {
  if(!data.items.length) return noMatch();
  return `<section class="panel">${panelHead('高频问题排行',`当前筛选 ${n(data.total)} 条问答，展示前 ${data.items.length} 类；相似说法共计为一类。`)}${ranks(data.items,data.total)}${rankTable(data.items)}</section>`;
}
function renderClusters(data) {
  if(!data.items.length) return noMatch();
  return `<p class="small muted" style="margin-bottom:16px">共 ${n(data.items.length)} 个问题簇。规则聚类可能误合或漏合，请结合样本原话复核；重新清洗后簇编号可能变化。</p><div class="cluster-grid">${data.items.map(r=>`<section class="cluster-card">${panelHead(esc(r.question),'',`<span class="badge">簇 ${r.cluster_id}</span>`)}<div class="cluster-stats"><span>${n(r.count)} 条问答</span><span>${n(r.customers)} 位客户</span><span>${n(r.variants)} 种原话</span><span>${n(r.unanswered)} 条未回复</span></div><ul class="cluster-samples">${r.samples.map(q=>`<li>${esc(q)}</li>`).join('')}</ul><button class="text-button" data-cluster="${r.cluster_id}">查看本簇全部问答</button></section>`).join('')}</div>`;
}
function renderGroups(data) {
  if(!data.items.length) return noMatch();
  return `<section class="panel">${panelHead('各客户群的咨询',`共 ${n(data.items.length)} 个客户群，按有效问答量排序。`)}${table(['客户群','有效问答','客户数','未回复','高频问题'],data.items.map(r=>`<tr><td><button class="table-link" data-group="${esc(r.group_name)}">${esc(r.group_name)}</button></td><td class="num">${n(r.total_questions)}</td><td class="num">${n(r.customer_count)}</td><td class="num">${n(r.unanswered)}</td><td class="question-cell">${r.top.slice(0,3).map(q=>`<div><button class="table-link" data-group="${esc(r.group_name)}" data-cluster="${q.cluster_id}">${esc(q.question)}</button> <span class="muted small">${q.count} 次</span></div>`).join('')}</td></tr>`))}</section>`;
}
function renderCustomers(data) {
  if(!data.items.length) return noMatch();
  return `<section class="panel">${panelHead('客户咨询记录',`按问答量展示前 ${n(data.items.length)} 位客户（最多 500 位）；客户ID一致才合并。`)}${table(['客户','客户群','问答数','问题类别','未回复','最近提问'],data.items.map(r=>`<tr><td><button class="table-link" data-customer="${esc(r.customer_id)}">${esc(r.customer_name || '匿名客户')}</button><div class="cell-meta">${esc(r.customer_id)}</div></td><td>${esc(r.group_name)}</td><td class="num">${n(r.total_questions)}</td><td class="num">${n(r.distinct_clusters)}</td><td class="num">${n(r.unanswered)}</td><td>${r.last_seen || '时间未知'}</td></tr>`))}</section>`;
}
function renderPairs(data) {
  const chips=[];
  if(S.cluster) chips.push(`<button class="chip" data-action="clear-cluster" aria-label="清除问题簇 ${S.cluster} 筛选">问题簇 ${S.cluster} ${icon('close')}</button>`);
  if(S.customer) chips.push(`<button class="chip" data-action="clear-customer">指定客户 ${icon('close')}</button>`);
  const page=Math.floor(data.offset/data.limit)+1;
  return `<section class="panel">${panelHead('问答明细',`共 ${n(data.total)} 条匹配记录。原话与回复均脱敏，点击问题展开完整内容。`)}<form id="pair-filters" class="toolbar"><div class="search-field">${icon('search')}<input type="search" id="search" aria-label="搜索问题、回复、客户或会话ID" placeholder="搜索问题、回复、客户或会话ID" maxlength="200" value="${esc(S.q)}"></div><select id="replied" aria-label="回复状态"><option value=""${!S.replied?' selected':''}>全部回复状态</option><option value="unanswered"${S.replied==='unanswered'?' selected':''}>仅未回复</option><option value="answered"${S.replied==='answered'?' selected':''}>有客服回复</option></select><button class="button primary" type="submit">检索</button><button class="text-button" type="button" data-action="clear-detail-filters">清空检索</button></form>${chips.length ? `<div class="chips">${chips.join('')}</div>`:''}${data.items.length ? table(['客户问题 / 提问时间','客服回复','客户 / 客户群','状态'],data.items.map(r=>`<tr><td class="question-cell"><div class="line-clamp">${pairLink(r)}</div><div class="cell-meta">${esc(r.asked_at_text)}　簇 ${r.cluster_id}</div></td><td class="answer-cell"><div class="line-clamp">${esc(r.answer || '抽取内容中未发现客服回复')}</div></td><td><span>${esc(r.customer_name)}</span><div class="cell-meta">${esc(r.group_name)}<br>${esc(r.staff_name)}</div></td><td>${badge(r)}</td></tr>`)) : '<div class="inline-empty">当前检索没有结果，请修改关键词或清空检索条件。</div>'}<div class="pagination"><span>${data.total ? `${data.offset+1}–${Math.min(data.total,data.offset+data.limit)}`:'0'} / ${n(data.total)} 条</span><nav class="actions" aria-label="明细分页"><button class="button" data-page="prev"${data.offset===0?' disabled':''}>${icon('left')}上一页</button><span class="page-number" aria-current="page" aria-label="第 ${page} 页">${page}</span><button class="button" data-page="next"${data.offset+data.limit>=data.total?' disabled':''}>下一页${icon('right')}</button></nav><select id="page-size" aria-label="每页问答条数">${[25,50,100].map(size=>`<option value="${size}"${data.limit===size?' selected':''}>${size} 条 / 页</option>`).join('')}</select></div></section>`;
}
function renderCleaning(s) {
  return `<div class="clean-grid"><div class="panel-stack"><section class="panel">${panelHead('导入与整理',S.source==='demo'?'当前为演示库；导入时会明确切换到本地数据。':'文件留在本机，确认导入后按会话ID更新。')}<div class="flow"><strong>选择表格</strong><span aria-hidden="true">›</span><strong>预览与检查</strong><span aria-hidden="true">›</span><strong>导入并规则清洗</strong></div><div class="actions"><button class="button primary" data-action="import">导入 CSV / XLSX</button><a class="button" href="/api/template">下载导入模板</a><button class="button" data-action="clean"${!s.sessions?' disabled':''}>重新规则清洗</button></div><div class="notice">支持会话正文和问答表；过滤寒暄、纯单号等信息，抽取客户问题与客服回复，再归并相似问句。不调用 AI，不判断问题是否真正解决。<br>同一会话ID按完整快照更新，旧问答尾部会移除；请不要只导入该会话的增量片段。</div></section><section class="panel">${panelHead('最近的本地操作','仅显示当前数据源的导入和规则清洗记录。')}${s.tasks.length ? s.tasks.map(t=>`<div class="task"><time>${esc(t.created_at_text)}</time><strong>${t.kind==='local_import'?'导入并清洗':'重新规则清洗'}</strong><p>${t.kind==='local_import'?`${esc(t.result.filename)}：新增 ${n(t.result.new_sessions)} / 更新 ${n(t.result.updated_sessions)} 个会话；跳过错误行 ${n(t.result.skipped_rows)}，重复行 ${n(t.result.duplicate_rows)}。`:`处理 ${n(t.result.sessions_scanned)} 个会话，得到 ${n(t.result.qa_pairs)} 条问答。`}</p></div>`).join(''):'<div class="inline-empty">还没有本地操作记录。</div>'}</section></div><div class="panel-stack"><section class="panel">${panelHead('处理状态','当前数据源全库统计，不受分析页筛选影响。')}${statusList(s)}<div class="panel-foot">规则版本 ${esc(s.version)}<br>全库保留 ${n(s.total_pairs)} 条问答。未知时间不会补成当前时间。</div></section><section class="panel">${panelHead('数据源与后续接入')}<p class="small muted">本地数据与演示数据分库存放。演示数据仅用于熟悉操作，不计入本地业务。</p><button class="text-button" data-action="demo" style="margin-top:10px">载入 / 查看独立演示数据</button><div class="notice">七鱼接口与字段已预留，当前未连接。后续接入沿用同一套原始会话、消息、问答与看板，不需要重做本地流程。</div></section></div></div>`;
}
async function showDetail(id) {
  const rev=++S.detailRevision; const source=S.source;
  const dialog=$('#detail-dialog'); $('#detail-body').innerHTML='<div class="loading">正在读取会话…</div>'; if(!dialog.open) dialog.showModal();
  try {
    const d=await request('detail',{id}); if(rev!==S.detailRevision || source!==S.source || !dialog.open)return;
    const r=d.pair;
    $('#detail-body').innerHTML=`<div class="detail-meta"><span class="badge">问答 ${r.qa_id}</span>${badge(r)}<span class="badge">问题簇 ${r.cluster_id}</span></div><div class="detail-question">${esc(r.question)}</div><div class="cell-meta">${esc(r.customer_name)}　${esc(r.group_name)}　${esc(r.staff_name)}<br>${esc(r.asked_at_text)}　会话 ${esc(r.session_id)}</div><section class="detail-block"><h3>归一化问法</h3><p>${esc(r.q_norm)}</p></section><section class="detail-block"><h3>客服回复</h3><p>${esc(r.answer || '抽取内容中没有客服回复；这不等于已确认未解决。')}</p></section><section style="margin-top:20px"><h3>会话上下文（脱敏）</h3>${d.archived ? '<div class="notice">原始正文未保留或已归档，仍可查看上方问答资产。</div>':`<div class="messages">${d.messages.map(m=>`<div class="message ${m.role==='staff'?'staff':''}"><div class="message-head"><span>${esc(({user:'客户',staff:'客服',bot:'机器人',system:'系统'})[m.role] || '消息')}</span><time>${esc(m.time)}</time></div>${esc(m.content)}</div>`).join('')}</div>`}</section>`;
  } catch(e) {if(rev===S.detailRevision) $('#detail-body').innerHTML=`<div class="notice error">${esc(e.message)}</div>`;}
}
function setImportStep(step) {
  document.querySelectorAll('[data-import-step]').forEach(el=>{
    const current=Number(el.dataset.importStep);
    el.classList.toggle('current',current===step);
    el.classList.toggle('complete',current<step);
    if(current===step) el.setAttribute('aria-current','step');
    else el.removeAttribute('aria-current');
  });
}
function openImport() {
  if(S.busy)return;
  if(S.source!=='local') { S.source='local'; Object.assign(S,{days:0,start:'',end:'',group:'',staff:''}); resetDetails(); syncFilters(); render(); toast('已切换到本地数据；导入不会写入演示库。'); }
  setImportStep(1); file=null;fileDigest='';$('#file').value='';$('#import-preview').innerHTML='';$('#confirm-import').disabled=true;$('#import-state').textContent='文件只在本机解析，不向外发送。';$('#import-dialog').showModal();
}
async function previewFile() {
  if(S.busy)return;
  setImportStep(1); file=$('#file').files[0]; fileDigest=''; $('#confirm-import').disabled=true;
  if(!file){toast('请先选择 CSV 或 XLSX 文件');return;}
  if(file.size>8*1024*1024){toast('文件超过 8 MB，请拆分后导入');return;}
  busy(true); $('#import-state').textContent='正在本机解析文件…';
  try {
    const p=await post('import/preview',await file.arrayBuffer(),{'X-Filename':encodeURIComponent(file.name)},'local');
    const labels={session_id:'会话ID',content:'会话正文',question:'问题',answer:'回复',customer_id:'客户ID',customer_name:'客户名称',group:'客户群',staff:'客服',time:'时间',end:'结束时间'};
    $('#import-preview').innerHTML=`<div class="preview-summary"><div><strong>${n(p.valid_rows)}</strong>有效行 / ${n(p.total_rows)} 总行</div><div><strong>${n(p.sessions)}</strong>会话</div><div><strong>${n(p.estimated_pairs)}</strong>预计有效问答</div></div><div class="mapping">识别为${esc(p.kind)}：${Object.entries(p.fields).map(([k,v])=>`${esc(v)} → ${labels[k] || esc(k)}`).join('；')}<br>跳过错误行 ${p.skipped_rows}，跳过文件内重复行 ${p.duplicate_rows}；${p.unknown_time} 行未提供会话/问答时间。</div>${p.errors.length ? `<div class="notice error">${p.errors.map(e=>`第 ${e.row} 行：${esc(e.reason)}`).join('<br>')}${p.skipped_rows>p.errors.length?'<br>仅显示前 30 条错误。':''}</div>`:''}<h3 style="margin-top:17px">脱敏预览样本</h3>${p.samples.length ? p.samples.map(r=>`<div class="sample"><strong>问：</strong>${esc(r.question)}<p><strong>答：</strong>${esc(r.answer || '未发现客服回复')}</p></div>`).join(''):'<p class="small muted">可读取会话，但未抽取到有效问题。导入后会记录“正文中无有效问题”。</p>'}<div class="notice">确认后只导入有效行。相同会话ID覆盖该会话快照，不追加旧正文。缺少会话ID时按内容生成稳定标识；内容变更将视为新会话。</div>`;
    fileDigest=p.can_import ? p.digest : ''; setImportStep(p.can_import ? 2 : 1); $('#import-state').textContent=p.can_import ? `已预览 ${p.filename}，确认后写入本地库。`:'没有可以导入的有效行，请修正表格后重试。';
  } catch(e) {$('#import-preview').innerHTML=`<div class="notice error">${esc(e.message)}</div>`;$('#import-state').textContent='预览失败，尚未写入任何数据。';}
  finally {busy(false);}
}
async function importFile() {
  if(S.busy || !fileDigest || !file)return;
  busy(true); setImportStep(3); $('#import-state').textContent='正在导入并清洗，请稍候…';
  try {
    const r=await post('import',await file.arrayBuffer(),{'X-Filename':encodeURIComponent(file.name),'X-File-Digest':fileDigest},'local');
    $('#import-dialog').close(); fileDigest=''; S.tab='cleaning'; toast(`导入并清洗完成：新增 ${r.new_sessions}、更新 ${r.updated_sessions} 个会话，全库 ${r.qa_pairs} 条问答。`); await render();
  } catch(e) {setImportStep(2); $('#import-state').textContent=e.message;}
  finally {busy(false);}
}
async function loadDemo() {
  if(S.busy)return; busy(true);toast('正在准备独立演示数据…');
  try {await post('demo'); S.source='demo'; S.tab='overview';Object.assign(S,{days:0,start:'',end:'',group:'',staff:''}); resetDetails();syncFilters();await render();toast('已载入模拟演示数据；本地数据未改动。');}
  catch(e){toast(e.message);}finally{busy(false);}
}
async function clean() {
  if(S.busy)return;busy(true);$('#confirm-dialog').close();
  try {const r=await post('clean'); resetDetails();await render();toast(`规则清洗完成：处理 ${r.sessions_scanned} 个会话，${r.clusters} 个问题簇。`);}
  catch(e){toast(e.message);}finally{busy(false);}
}
function closeImport() {if(!S.busy) $('#import-dialog').close();else toast('正在处理文件，请等待完成。');}
function showTooltip(el,event) {
  const tip=$('#chart-tooltip');tip.textContent=el.dataset.tip;tip.hidden=false;
  const box=el.getBoundingClientRect();const left=event?.clientX || box.left+box.width/2;const top=event?.clientY || box.top;
  tip.style.left=Math.max(10,Math.min(innerWidth-tip.offsetWidth-10,left+12))+'px';tip.style.top=Math.max(8,Math.min(innerHeight-tip.offsetHeight-10,top-tip.offsetHeight-10))+'px';
  const cross=el.closest('svg')?.querySelector('.crosshair');if(cross){cross.removeAttribute('hidden');cross.setAttribute('x1',el.dataset.point);cross.setAttribute('x2',el.dataset.point);}
}
function hideTooltip(){ $('#chart-tooltip').hidden=true;document.querySelectorAll('.crosshair').forEach(e=>e.setAttribute('hidden','')); }
document.addEventListener('click',e=>{
  const b=e.target.closest('button');if(!b || b.disabled)return;
  if(b.dataset.detail){showDetail(Number(b.dataset.detail));return;}
  if(b.dataset.cluster){if(b.dataset.group){S.group=b.dataset.group;$('#group').value=S.group;}drill('cluster',Number(b.dataset.cluster));return;}
  if(b.dataset.group){drill('group',b.dataset.group);return;}
  if(b.dataset.customer){drill('customer',b.dataset.customer);return;}
  if(b.dataset.tab){navigate(b.dataset.tab);return;}
  if(b.dataset.page){S.offset=Math.max(0,S.offset+(b.dataset.page==='next'?S.limit:-S.limit));render();return;}
  const action=b.dataset.action;
  if(action==='import')openImport();
  else if(action==='close-import')closeImport();
  else if(action==='close-detail'){$('#detail-dialog').close();++S.detailRevision;}
  else if(action==='demo')loadDemo();
  else if(action==='retry')render();
  else if(action==='reset')resetFilters();
  else if(action==='clean')$('#confirm-dialog').showModal();
  else if(action==='cancel-clean')$('#confirm-dialog').close();
  else if(action==='unanswered'){resetDetails();S.replied='unanswered';S.tab='pairs';render();}
  else if(action==='clear-cluster'){S.cluster=0;S.offset=0;render();}
  else if(action==='clear-customer'){S.customer='';S.offset=0;render();}
  else if(action==='clear-detail-filters'){resetDetails();render();}
});
$('#source').addEventListener('change',()=>{S.source=$('#source').value;Object.assign(S,{group:'',staff:'',days:0,start:'',end:''});resetDetails();syncFilters();$('#detail-dialog').close();render();});
$('#range').addEventListener('change',()=>{$('#custom-dates').hidden=$('#range').value!=='custom';});
$('#filters').addEventListener('submit',e=>{e.preventDefault();const custom=$('#range').value==='custom';const start=custom?$('#start').value:'',end=custom?$('#end').value:'';if(custom && !start && !end){toast('自定义日期至少填写一个边界');return;}if(start && end && start>end){toast('开始日期不能晚于结束日期');return;}Object.assign(S,{days:custom?0:Number($('#range').value),start,end,group:$('#group').value,staff:$('#staff').value,offset:0});render();});
document.addEventListener('submit',e=>{if(e.target.id==='pair-filters'){e.preventDefault();S.q=$('#search').value.trim();S.replied=$('#replied').value;S.offset=0;render();}});
$('#reset-filters').addEventListener('click',resetFilters);$('#refresh').addEventListener('click',render);
$('#export').addEventListener('click',()=>{window.location.href='/api/export?'+params({},S.tab==='pairs');toast('正在导出当前筛选的脱敏问答。');});
document.addEventListener('change',e=>{if(e.target.id==='page-size'){const size=Number(e.target.value);if([25,50,100].includes(size)){S.limit=size;S.offset=0;render();}}});
$('#file').addEventListener('change',()=>{setImportStep(1); fileDigest='';$('#confirm-import').disabled=true;$('#import-preview').innerHTML='';$('#import-state').textContent='文件已选择，请先预览。';});
$('#preview-file').addEventListener('click',previewFile);$('#confirm-import').addEventListener('click',importFile);$('#confirm-clean').addEventListener('click',clean);
$('#import-dialog').addEventListener('cancel',e=>{if(S.busy){e.preventDefault();toast('正在处理文件，请等待完成。');}});
$('#detail-dialog').addEventListener('close',()=>++S.detailRevision);
document.addEventListener('pointerover',e=>{const el=e.target.closest('[data-tip]');if(el)showTooltip(el,e);});
document.addEventListener('pointermove',e=>{const el=e.target.closest('[data-tip]');if(el)showTooltip(el,e);});
document.addEventListener('pointerout',e=>{if(e.target.closest('[data-tip]'))hideTooltip();});
document.addEventListener('focusin',e=>{if(e.target.dataset.tip)showTooltip(e.target);});
document.addEventListener('focusout',e=>{if(e.target.dataset.tip)hideTooltip();});
render();
