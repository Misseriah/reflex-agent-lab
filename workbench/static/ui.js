export const state = {user:null, csrf:'', cleanup:null, leave:null};
export const $ = (s, root=document) => root.querySelector(s);
export const $$ = (s, root=document) => [...root.querySelectorAll(s)];
export const h = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export const icon = n => `<i data-lucide="${h(n)}" aria-hidden="true"></i>`;
export const icons = () => window.lucide?.createIcons({attrs:{'stroke-width':1.7,width:17,height:17}});
export const fmt = v => v == null ? '未知' : Number(v).toLocaleString('zh-CN',{maximumFractionDigits:3});
export const date = v => v ? new Date(v).toLocaleString('zh-CN',{hour12:false}) : '未记录';
export const labels = {dataset:'评测集',contract:'任务契约',strategy:'策略',model:'模型',arm:'策略组',evaluator:'评分器',experiment:'实验方案',environment:'环境',price:'价格表',metric:'指标',owner:'负责人',maintainer:'维护者',reviewer:'独立审阅者',adjudicator:'仲裁者',observer:'只读观察者',draft:'草稿',frozen:'已冻结',deprecated:'已弃用',pending:'待审',submitted:'已提交',unreviewed:'待双审',single_review:'单人已审',consensus:'双审一致',disagreement:'待仲裁',adjudicated:'已仲裁',completed:'已完成',imported:'历史导入',running:'处理中',queued:'排队中',failed:'失败',cancelled:'已取消',interrupted:'已中断',development:'开发审核',independent:'独立审核',saved_proposal:'仅提案',saved_native_trace:'历史原生轨迹',engineering_challenge:'工程反例',native_trace:'原生轨迹',controlled_proposal:'受控提案',safe_task_success:'安全完成',dangerous_proposal:'危险提案',executed_high_consequence_violation:'实际 S2 违规',contract_approval:'契约是否批准',yes:'是',no:'否',unknown:'证据不足',descriptive:'可作描述对比',common_subset:'仅公共样本',needs_rescoring:'需统一重评分',not_comparable:'不可比较',conditions_differ:'条件不同',banking:'银行',workspace:'办公',travel:'旅行',slack:'协作消息',general:'通用'};
export const label = v => labels[v] || v || '未标注';
export const badge = (v, tone='') => `<span class="badge ${tone || ({frozen:'green',completed:'green',failed:'red',unknown:'amber',disagreement:'amber'}[v]||'')}">${h(label(v))}</span>`;
export function button(text, action, ico='plus', cls='') {return `<button class="${cls}" data-action="${h(action)}">${icon(ico)}${h(text)}</button>`;}
export function link(text, route, cls='') {return `<a class="${cls}" href="#/${h(route)}">${h(text)}</a>`;}
export function options(items, selected='', empty='请选择') {return `<option value="">${h(empty)}</option>`+items.map(v=>{const a=typeof v==='string'?{id:v,name:label(v)}:v;return `<option value="${h(a.id)}" ${a.id===selected?'selected':''}>${h(a.name)}</option>`;}).join('');}
export function field(name,text,value='',type='text',extra=''){return `<label class="field"><span>${h(text)}</span><input name="${h(name)}" type="${type}" value="${h(value)}" ${extra}></label>`;}
export function textarea(name,text,value='',extra=''){return `<label class="field"><span>${h(text)}</span><textarea name="${h(name)}" ${extra}>${h(value)}</textarea></label>`;}
export function select(name,text,items,value='',empty='请选择'){return `<label class="field"><span>${h(text)}</span><select name="${h(name)}">${options(items,value,empty)}</select></label>`;}
export function head(title,sub='',actions=''){return `<div class="page-head"><div><h1>${h(title)}</h1>${sub?`<p class="muted">${h(sub)}</p>`:''}</div><div class="head-actions">${actions}</div></div>`;}
export function banner(text,tone='amber'){return `<div class="banner ${tone}">${icon(tone==='red'?'circle-alert':'info')}<div>${h(text)}</div></div>`;}
export function empty(text='暂无记录'){return `<div class="empty-state">${icon('inbox')}<p>${h(text)}</p></div>`;}
export function table(headers,rows){return `<div class="data-table-wrap"><table><thead><tr>${headers.map(x=>`<th>${x}</th>`).join('')}</tr></thead><tbody>${rows.length?rows.map(row=>`<tr>${row.map(x=>`<td>${x??''}</td>`).join('')}</tr>`).join(''):`<tr><td colspan="${headers.length}">${empty()}</td></tr>`}</tbody></table></div>`;}
export function pager(data){return `<div class="pagination"><small>共 ${fmt(data.total)} 项 · 第 ${data.page} / ${Math.max(1,Math.ceil(data.total/data.size))} 页</small><div>${button('上一页','prev','chevron-left')}${button('下一页','next','chevron-right')}</div></div>`;}
export function attachPager(data, fn){const a=$('[data-action="prev"]'),b=$('[data-action="next"]');if(!a)return;a.disabled=data.page<=1;b.disabled=data.page*data.size>=data.total;a.onclick=()=>fn(data.page-1);b.onclick=()=>fn(data.page+1);}
export function metric(name,value,note='',ico='activity'){return `<div class="metric"><div class="metric-top">${h(name)}${icon(ico)}</div><div class="metric-value">${h(value)}</div><div class="metric-note">${h(note)}</div></div>`;}
export function tree(v,path='',depth=0,refs=false){
  if(depth>16)return '<span class="muted">嵌套内容过深</span>';
  if(v===null || typeof v!=='object')return `<span class="json-value text-wrap"${refs?` data-evidence-pointer="${h(path)}"`:''}>${h(v===null?'未记录':v)}</span>`;
  const es=Object.entries(v);if(!es.length)return '<span class="muted">空</span>';
  return `<div class="json-tree">${es.map(([k,x])=>{const p=path+'/'+k.replace(/~/g,'~0').replace(/\//g,'~1');const ref=refs?`<button class="icon-button tiny" data-ref="${h(p)}" title="引用此证据" aria-label="引用 ${h(k)}">${icon('quote')}</button>`:'';return typeof x==='object'&&x!==null?`<details ${depth<1?'open':''}><summary>${h(label(k))} <small>${Array.isArray(x)?x.length+' 项':''}</small>${ref}</summary>${tree(x,p,depth+1,refs)}</details>`:`<div class="json-row"><div class="json-key">${h(label(k))}${ref}</div>${tree(x,p,depth+1,refs)}</div>`;}).join('')}</div>`;
}
let toastTimer;
export function toast(text,error=false){const el=$('#toast');el.textContent=text;el.className='show'+(error?' error':'');clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.className='',5000);}
export async function api(path,body,method,blob=false){
  const init={method:method||(body===undefined?'GET':'POST'),credentials:'same-origin',headers:{}};
  if(body!==undefined){init.headers={'Content-Type':'application/json','X-Workbench-Request':'1','X-CSRF-Token':state.csrf};init.body=JSON.stringify(body);}
  let response;try{response=await fetch('/api/'+path,init);}catch{throw new Error('连接中断，当前内容仍保留。请检查本地服务后重试。');}
  if(!response.ok){const data=await response.json().catch(()=>({}));const error=new Error(data.error?.message||`请求失败 (${response.status})`);error.code=data.error?.code;error.status=response.status;throw error;}
  return blob?response.blob():response.json();
}
export const act = (name, fn,root=document) => {const el=$(`[data-action="${name}"]`,root);if(el)el.onclick=async()=>{el.disabled=true;try{await fn();}catch(e){toast(e.message,true);}finally{el.disabled=false;}};};
export function dialog(title,body,submitText,fn,wide=false){const d=$('#modal');if(d.open)d.close();d.className=wide?'wide-modal':'';d.innerHTML=`<div class="dialog-head"><h2>${h(title)}</h2><button type="button" class="icon-button" data-close aria-label="关闭">${icon('x')}</button></div><form><div class="dialog-body">${body}<div class="form-error" role="alert"></div></div><div class="dialog-footer"><button type="button" data-close>取消</button>${submitText?`<button class="primary" type="submit">${icon('check')}${h(submitText)}</button>`:''}</div></form>`;$$('[data-close]',d).forEach(b=>b.onclick=()=>d.close());$('form',d).onsubmit=async e=>{e.preventDefault();const submit=$('[type="submit"]',d);if(submit)submit.disabled=true;try{await fn(new FormData(e.target),d);d.close();}catch(err){$('.form-error',d).textContent=err.message;}finally{if(submit)submit.disabled=false;}};d.showModal();icons();return d;}
export function download(content,name,type='application/json'){const blob=content instanceof Blob?content:new Blob([typeof content==='string'?content:JSON.stringify(content,null,2)],{type});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(a.href),5000);}
export function jsonDialog(title,value){dialog(title,tree(value),null,null,true);}
export function render(html){$('#content').innerHTML=html;icons();}
export function onFilter(fn){$$('[data-filter]').forEach(el=>{el.onchange=()=>fn(Object.fromEntries($$('[data-filter]').map(e=>[e.name,e.value])));});}
export function queryString(obj){return new URLSearchParams(Object.entries(obj).filter(([,v])=>v!==''&&v!=null)).toString();}
export const editable=()=>['owner','maintainer'].includes(state.user?.role);
Object.assign(labels,{assisted:'辅助复审',proposal_correct:'提案正确性',engineering_counterexample:'合成工程反例'});
export const privateRole=()=>['owner','maintainer','observer'].includes(state.user?.role);
Object.assign(labels,{native:'历史原生轨迹',controlled:'受控提案',engineering:'工程反例',files:'文件操作',refund:'退款',exploratory:'探索性',test:'测试',calibration:'校准',qualification:'资格检查',request:'任务请求',task:'可信任务',policy:'业务规则',context:'可信上下文',candidates:'可选操作',proposal:'决策提案',arguments:'参数',selected_action:'所选操作',execution_scope:'执行范围',tool_note:'非可信工具说明',trajectory:'执行轨迹',tool_receipts:'工具回执',initial_state:'初始状态',final_answer:'最终回答',state_origin:'状态来源',before:'前状态',after:'后状态',action:'操作',disposition:'执行状态',trace_complete:'轨迹完整性声明'});
Object.assign(labels,{application_records:'应用记录',operation_id:'操作编号',record_id:'记录编号',requested_effect:'请求效果',write_permission_granted:'已授予写入权限',path:'路径',file_path:'文件路径',file_id:'文件编号',filename:'文件名',content:'正文',description:'描述',effect:'效果类型',parameters:'参数约束',required:'必需字段',additionalProperties:'允许额外字段',properties:'字段定义',amount_cents:'金额（分）',recipient:'收款方',order_id:'订单编号'});
