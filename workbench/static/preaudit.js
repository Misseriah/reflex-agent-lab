import {state,$,$$,h,icon,icons,fmt,date,label,badge,button,link,options,head,banner,table,pager,attachPager,metric,tree,toast,api,act,dialog,download,render,onFilter,queryString,editable} from './ui.js';

const states={pending:'待 LLM 初审',running:'LLM 初审中',ready:'报告已生成',failed:'初审失败',stale:'报告已过期',not_prepared:'尚未预审'};
const humans={pending:'待复审',approved:'报告已通过',rejected:'报告未通过',manual_submitted:'已人工修订',draft:'人工草稿',stale:'审核已过期',report_changed:'报告版本已变'};
const executions={proposal_only:'仅提案 · 未执行',synthetic_trace:'合成工程轨迹',recorded_execution:'沙箱执行有记录',unverified:'执行未确认'};
const kinds={saved_proposal:'自建受控提案',saved_native_trace:'AgentDojo 原生轨迹',engineering_counterexample:'自建工程反例'};
const correctness={yes:'正确',no:'不正确',unknown:'未决'};
const metrics=['safe_task_success','dangerous_proposal','executed_high_consequence_violation'];
const code=v=>`<pre class="preaudit-code">${h(typeof v==='string'?v:JSON.stringify(v,null,2))}</pre>`;
const compact=v=>`<div class="preaudit-clamp">${h(v||'未记录自然语言请求')}</div>`;
function decision(rec){return rec?badge('提案：'+correctness[rec.proposal_correct],rec.proposal_correct==='yes'?'green':rec.proposal_correct==='no'?'red':'amber')+`<div class="sub preaudit-clamp">${h(rec.rationale)}</div>`:'<small>未生成结论</small>';}

export async function preauditList(q={}){
 const data=await api('preaudits?'+queryString(q));let disposed=false,timer;
 function draw(d){
  const s=d.summary,hc=d.human_counts;
  render(head('LLM 初审',`${s.items} 条审核材料 · 机器预审与人工判断分开留档`,(editable()?button('补齐初审','run-preaudit','play')+button('导出报告表','export-preaudit','download'):'')+button('刷新','refresh-preaudit','refresh-cw'))+
   banner('通过表示认可整份初审报告，不表示业务任务成功。查看机器意见后的复审属于辅助审核，不计独立盲审。','neutral')+
   `<div class="metrics-strip">${metric('审核材料',fmt(s.items),'保留全部材料与来源','files')}${metric('LLM 报告',`${s.states?.ready||0} / ${s.items}`,`待处理 ${(s.states?.pending||0)+(s.states?.running||0)} · 失败 ${s.states?.failed||0}`,'bot')}${metric('我的复审通过',fmt(hc.approved||0),`未通过 ${hc.rejected||0} · 人工修订 ${hc.manual_submitted||0}`,'clipboard-check')}${metric('已结算费用上界',s.settled_upper_cny==null?'—':'¥ '+Number(s.settled_upper_cny).toFixed(4),s.calls==null?'按授权范围展示':`${s.calls} 次调用 · 未决预留 ¥${Number(s.reserved_cny||0).toFixed(2)} · 累计限额 ¥${s.budget_limit_cny}`,'wallet')}</div>`+
   `<div class="toolbar"><input data-filter name="q" aria-label="搜索初审任务" placeholder="搜索任务、编号或操作" value="${h(q.q||'')}"><select data-filter name="kind" aria-label="材料来源">${options(Object.entries(kinds).map(([id,name])=>({id,name})),q.kind,'全部来源')}</select><select data-filter name="state" aria-label="初审状态">${options(Object.entries(states).map(([id,name])=>({id,name})),q.state,'全部初审状态')}</select><select data-filter name="human" aria-label="复审状态">${options(Object.entries(humans).map(([id,name])=>({id,name})),q.human,'全部复审状态')}</select></div>`+
   table(['任务输入','输出选项','实际选择 / 参数','执行情况','LLM 初审意见','人工复审',''],d.items.map(r=>{
    const o=r.observed,rec=r.recommendation;
    return [`${link(rec?.task_summary||o.request||'结构化工程案例','preaudits/'+r.id)}<div class="sub">${h(kinds[r.kind]||r.kind)} · ${h(label(r.domain))}</div><div class="sub mono">${h(r.id)}</div>`,
     o.candidates.length?`${o.candidates.slice(0,4).map(c=>`<div><code>${h(c.id)}</code></div>`).join('')}${o.candidates.length>4?`<small>共 ${o.candidates.length} 个选项</small>`:''}`:'<small>未保存完整候选集</small>',
     o.selected.slice(0,3).map(x=>`<div class="preaudit-clamp"><code>${h((x.proposal||{}).action||x.selected_action||'未记录')}</code><small> ${h(JSON.stringify((x.proposal||{}).arguments||{}))}</small></div>`).join('')+(o.selected.length>3?`<small>共 ${o.selected.length} 步</small>`:''),
     badge(executions[o.execution],o.execution==='proposal_only'?'amber':'blue'),badge(states[r.state]||r.state,r.state==='failed'?'red':r.state==='ready'?'green':'amber')+decision(rec)+(rec?.disagreements?.length?badge('与规则有分歧','amber'):''),
     badge(humans[r.human.state]||r.human.state,r.human.state==='approved'?'green':r.human.state==='rejected'?'red':'')+`<div class="sub">修订 ${r.human.revision}</div>`,
     link('查看 / 复审','preaudits/'+r.id)];
   }))+pager(d)+`<div class="preaudit-live sub" aria-live="polite">更新于 ${h(new Date().toLocaleTimeString('zh-CN'))}</div>`);
  $('.data-table-wrap').classList.add('preaudit-table');
  const disagreementFilter=document.createElement('select');disagreementFilter.name='disagreement';disagreementFilter.dataset.filter='';disagreementFilter.setAttribute('aria-label','规则分歧');disagreementFilter.innerHTML=options([{id:'1',name:'机器 / LLM 有分歧'}],q.disagreement,'全部意见');$('.toolbar').append(disagreementFilter);
  if(s.batches?.[0]?.state==='stopped'){
   const notice=document.createElement('div');notice.innerHTML=banner('初审已停止：'+h(s.batches[0].error||'待核查')+'。有效报告已保留，未决用量不可当作零费用重试。');$('.metrics-strip').before(notice);
  }
  onFilter(f=>location.hash='#/preaudits?'+queryString(f));attachPager(d,n=>location.hash='#/preaudits?'+queryString({...q,page:n}));
  act('refresh-preaudit',refresh);act('export-preaudit',async()=>download(await api('preaudits/export?format=csv',undefined,undefined,true),'初审报告.csv'));
  act('run-preaudit',()=>dialog('启动有费用的 LLM 初审',`<p>将未完成的评测证据发送至 DeepSeek 官方接口，使用 Flash 初审。内容包括基准任务（可能含姓名/邮箱）、工具参数与回执、已保存的虚拟文件和重建状态，不包含人工意见或原裁判分数。已完成报告不重复调用，本项目初审累计上限 5 元。</p><label class="checkbox-line spaced"><input name="retry" type="checkbox">包含失败报告（会产生新的调用费用）</label>`,'确认启动',async fd=>{await api('preaudits/batches',{confirm_external_call:true,limit_cny:5,retry_failed:fd.get('retry')==='on'});toast('初审已启动');await refresh();}));
  if(editable()&&s.batches?.some(b=>['queued','running'].includes(b.state))){const b=s.batches.find(b=>['queued','running'].includes(b.state)),btn=document.createElement('button');btn.innerHTML=icon('square')+'停止初审';$('.head-actions').append(btn);btn.onclick=async()=>{try{await api('preaudits/batches/'+b.id+'/cancel',{});toast('已请求停止；正在进行的单次调用可能仍会完成。');await refresh();}catch(e){toast(e.message,true);}};icons();}
 }
 async function refresh(){const d=await api('preaudits?'+queryString(q));if(!disposed)draw(d);}
 draw(data);
 timer=setInterval(()=>{if(!disposed&&!$('#modal').open&&!['INPUT','SELECT'].includes(document.activeElement?.tagName))refresh().catch(()=>{});},5000);
 const changed=()=>refresh().catch(()=>{});window.addEventListener('focus',changed);window.addEventListener('storage',changed);
 state.cleanup=()=>{disposed=true;clearInterval(timer);window.removeEventListener('focus',changed);window.removeEventListener('storage',changed);};
}

export async function preauditDetail(id){
 let d=await api('preaudits/'+id+'/open',{}),disposed=false,pending=null,navigating=false,timer;
 function draw(){
  const o=d.observed,rec=d.recommendation;
  const approved=d.human.state==='approved',rejected=d.human.state==='rejected';
  const reviewActions=`<button type="button" data-action="${rejected?'manual-review':'reject-report'}" ${d.state!=='ready'&&!rejected?'disabled':''}>${icon(rejected?'clipboard-check':'x')}${rejected?'跳转人工审核':'不通过'}</button><button type="button" data-action="${approved?'next-report':'approve-report'}" class="primary" ${d.state!=='ready'&&!approved?'disabled':''}>${icon(approved?'arrow-right':'check')}${approved?'审核下一项':'通过初审报告'}</button>`;
  const candidateRows=o.candidates.map(c=>[`<code>${h(c.id)}</code>`,h(c.description||'未记录描述'),`<details><summary>参数约束</summary>${tree(c.parameters||{})}</details>`]);
  render(head('初审报告',`${id} · ${kinds[d.kind]||d.kind} · ${label(d.domain)}`,link('原人工审核页','reviews/'+id)+button('刷新报告','refresh-report','refresh-cw'))+
   `<div class="toolbar">${link('返回初审列表','preaudits')}${badge(states[d.state]||d.state)}${badge(humans[d.human.state]||d.human.state)}<small>人工修订 ${d.human.revision}</small></div>`+
   banner('辅助复审，不计独立盲审。通过是认可下面的判断及证据，不是宣告任务成功。','neutral')+
   `<section class="section"><h2>任务输入</h2>${rec?`<p class="preaudit-summary">${h(rec.task_summary)}</p><small>以上为 LLM 摘要，以下为原始证据</small>`:''}${o.request?`<p class="text-wrap spaced">${h(o.request)}</p>`:''}<details class="spaced"><summary>完整任务、规则与可信上下文</summary>${tree(o.input)}</details></section>`+
   `<section class="section"><h2>输出选项</h2>${candidateRows.length?table(['操作','描述','参数'],candidateRows):banner('该匿名材料没有保存完整候选集。已调用的工具不能被当作全部可选操作。','neutral')}</section>`+
   `<section class="section"><div class="section-head"><h2>实际选择与执行情况</h2>${badge(executions[o.execution],'blue')}</div>${o.execution_note?`<p class="sub">${h(o.execution_note)}</p>`:''}${table(['步骤','决策提案','实际执行动作','执行记录'],o.selected.map(s=>[s.step,code(s.proposal||{selected_action:s.selected_action}),code(s.executed_action||'没有对应执行动作'),badge({executed:'已执行',blocked:'已拦截',not_executed:'未执行',unknown:'未确认'}[s.disposition]||s.disposition)]))}${o.final_answer?`<details class="spaced"><summary>最终回答</summary>${code(o.final_answer)}</details>`:''}<details class="spaced"><summary>工具回执及重建状态依据</summary>${tree({tool_receipts:d.package.evidence.tool_receipts||[],supplementary:d.package.supplementary})}</details></section>`+
   `<div class="preaudit-judgments"><section class="section"><h2>机器规则检查</h2><p class="sub">${h(d.machine.scope)}</p>${decision(d.machine)}${table(['维度','机器判断'],metrics.map(m=>[label(m),badge(label(d.machine.labels[m]))]))}<details><summary>规则检查明细</summary>${tree(d.machine.checks)}</details>${d.machine.error?banner(d.machine.error):''}</section><section class="section"><h2>LLM 初审结论</h2>${rec?decision(rec)+table(['维度','初审判断'],metrics.map(m=>[label(m),badge(label(rec.labels[m]))]))+`<p class="text-wrap">${h(rec.rationale)}</p>${rec.missing_evidence?banner('证据缺口：'+rec.missing_evidence):''}${rec.disagreements?.length?banner('与机器规则检查存在分歧：'+rec.disagreements.map(k=>k==='proposal_correct'?'提案正确性':label(k)).join('、')):''}${rec.warnings?.map(w=>banner(w,'neutral')).join('')||''}<h3 class="spaced">依据引用</h3><div class="reference-list">${rec.references.map((ref,i)=>`<button type="button" data-source-index="${i}">${icon('quote')}<code>${h(ref)}</code></button>`).join('')}</div>`:banner(d.error?'初审未完成：'+d.error:'正在等待 LLM 初审；机器结果尚未变成人工结论。','neutral')}</section></div>`+
   `<section class="section"><h2>当前人工审核记录</h2><p>${h(humans[d.human.state]||d.human.state)} · 修订 ${d.human.revision}</p>${d.human.review?`<details class="spaced"><summary>查看当前判断与理由</summary>${tree(d.human.review.payload)}</details>`:'<p class="sub">尚无人工判断</p>'}<p class="sub spaced">报告 ${h(d.id)} · ${h(d.version)} · 更新 ${h(date(d.updated_at))}</p><p class="sub text-wrap">证据 ${h(d.evidence_hash)} · 规则 ${h(d.rule_hash)}</p></section>`+
   `<div class="preaudit-confirm"><div><strong>${approved?'本报告已通过':rejected?'本报告未通过':'是否通过这份初审报告？'}</strong><small>${h(humans[d.human.state]||d.human.state)}${d.human.revision?' · 修订 '+d.human.revision+' · 已保存':''}</small></div><div class="preaudit-confirm-actions">${reviewActions}</div></div><div id="preaudit-error" role="alert" class="form-error"></div>`);
  act('refresh-report',refresh);
  act('approve-report',()=>confirm('approve'));act('reject-report',()=>confirm('reject'));
  act('next-report',nextReport);act('manual-review',()=>{location.hash='#/reviews/'+id;});
  $$('[data-source-index]').forEach(btn=>btn.onclick=()=>{const pointer=rec.references[Number(btn.dataset.sourceIndex)];let value=d.package.evidence;for(const key of pointer.slice(1).split('/'))value=value[key.replace(/~1/g,'/').replace(/~0/g,'~')];dialog('证据引用 '+pointer,tree(value),null,null,true);});
 }
 async function refresh(){if(navigating)return;const updated=await api('preaudits/'+id+'/open',{});if(!disposed){d=updated;draw();}}
 async function nextReport(){
  if(navigating)return;
  navigating=true;
  $$('.preaudit-confirm-actions button').forEach(b=>b.disabled=true);
  try{
   const rows=[];
   for(let page=1;;page++){
    const data=await api('preaudits?'+queryString({state:'ready',size:100,page}));
    if(disposed)return;
    rows.push(...data.items);
    if(page*data.size>=data.total)break;
   }
   const index=rows.findIndex(r=>r.id===id);
   const ordered=[...rows.slice(index+1),...rows.slice(0,index+1)];
   const next=ordered.find(r=>r.id!==id&&r.state==='ready'&&!['approved','rejected','manual_submitted'].includes(r.human.state));
   if(next)location.hash='#/preaudits/'+next.id;
   else{location.hash='#/preaudits';toast('没有待审核的已生成报告');}
  }finally{navigating=false;if(!disposed)$$('.preaudit-confirm-actions button').forEach(b=>b.disabled=d.state!=='ready'&&!['next-report','manual-review'].includes(b.dataset.action));}
 }
 async function confirm(decision){
  if(navigating)return;
  $$('[data-action="approve-report"],[data-action="reject-report"]').forEach(b=>b.disabled=true);
  if(!pending||pending.decision!==decision||pending.report_id!==d.id||pending.revision!==d.human.revision)pending={report_id:d.id,revision:d.human.revision,decision,request_key:crypto.randomUUID()};
  try{const result=await api('preaudits/'+id+'/confirm',pending);pending=null;if(disposed)return;d.human=result.human;localStorage.setItem('wb-review-change',JSON.stringify({id,revision:result.revision,at:Date.now()}));draw();toast(decision==='approve'?'已通过并同步至人工审核':'已标记不通过，人工审核中保留待更正草稿');}
  catch(e){if(!disposed){$('#preaudit-error').textContent=e.message+'。当前页面未覆盖其他修订；刷新报告后再确认。';throw e;}}
  finally{if(!disposed)$$('[data-action="approve-report"],[data-action="reject-report"]').forEach(b=>b.disabled=d.state!=='ready');}
 }
 draw();
 timer=setInterval(()=>{if(!disposed&&['pending','running'].includes(d.state)&&!$('#modal').open)refresh().catch(()=>{});},5000);
 const sync=()=>{if(!pending&&!navigating&&!disposed&&!$('#modal').open)refresh().catch(()=>{});};window.addEventListener('focus',sync);window.addEventListener('storage',sync);
 state.cleanup=()=>{disposed=true;clearInterval(timer);window.removeEventListener('focus',sync);window.removeEventListener('storage',sync);};
}
