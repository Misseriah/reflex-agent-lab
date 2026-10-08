import {$,$$,h,icon,icons,api,act,dialog,banner,toast} from './ui.js';

const origins={saved_evidence:'已保存证据',saved_tool_receipt:'已保存工具回执',offline_reconstructed_not_original_capture:'离线重建状态，不是原始采集'};

export function mountReviewAssist(d,isDisposed){
 const root=$('.review-layout');
 const notice=document.createElement('div');notice.className='translation-status';notice.hidden=true;notice.setAttribute('role','status');
 $('.review-mobile-tabs').before(notice);
 let translated=null,visible=false;
 const request=d.evidence.request?'/request':d.evidence.task?.request?'/task/request':d.evidence.task?.description?'/task/description':d.evidence.contract?.request?'/contract/request':d.evidence.contract?.description?'/contract/description':null;
 if(request)$('.review-task',root).dataset.evidencePointer=request;
 const nodes=()=>$$('[data-evidence-pointer]',root);
 act('translate-evidence',async()=>{
  if(!translated){
   translated=await api('reviews/'+d.id+'/translation');
   if(isDisposed())return;
   if(translated.evidence_hash!==d.evidence_hash){translated=null;throw new Error('证据版本已变更，请刷新后再翻译。');}
   const byPointer=new Map(translated.segments.map(s=>[s.pointer,s]));
   const missing=new Set(translated.untranslated);
   for(const node of nodes()){
    const entry=byPointer.get(node.dataset.evidencePointer);
    if(entry){
     if(node.textContent!==entry.source)continue;
     const span=document.createElement('span');span.className='evidence-translation text-wrap';span.lang='zh-CN';span.textContent=entry.text;
     node.after(span);
    }else if(missing.has(node.dataset.evidencePointer)){
     const span=document.createElement('small');span.className='translation-missing';span.textContent='未收录离线译文';node.after(span);
    }
   }
  }
  if(isDisposed())return;
  visible=!visible;
  $$('.evidence-translation,.translation-missing',root).forEach(el=>el.hidden=!visible);
  const coverage=translated.coverage;
  notice.hidden=!visible;
  notice.textContent=`离线辅助译文 · 已覆盖 ${coverage.translated} 段 / 未覆盖 ${coverage.untranslated} 段 · 英文原文为准 · 零外部调用`;
  notice.classList.toggle('partial',coverage.untranslated>0);
  const btn=$('[data-action="translate-evidence"]');btn.innerHTML=icon('languages')+(visible?'隐藏译文':'一键翻译');btn.setAttribute('aria-pressed',String(visible));icons();
 });
 const refs=d.file_references||[];
 async function preview(ref){
  const value=await api('reviews/'+d.id+'/file-preview?'+new URLSearchParams({pointer:ref.pointer}));
  if(isDisposed())return;
  if(value.evidence_hash!==d.evidence_hash)throw new Error('证据版本已变更，请刷新后再预览。');
  const noBody=banner('没有保存文件正文。这是实验记录中的逻辑引用，不表示本机存在该文件；无法据此还原或判断其内容。','neutral');
  const body=`<dl class="preview-meta"><dt>文件引用</dt><dd><code>${h(value.reference)}</code></dd><dt>证据位置</dt><dd><code>${h(value.pointer)}</code></dd><dt>证据版本</dt><dd><code>${h(value.evidence_hash)}</code></dd></dl>`+
   (value.status==='not_recorded'?noBody:'')+
   (value.missing_states.length?banner(`另有 ${value.missing_states.length} 个关联状态快照未保存，预览可能不完整。`):'')+
   value.sources.map((s,i)=>`<section class="file-source"><h3>${i+1}. ${h(origins[s.origin]||s.origin)}</h3><div class="sub text-wrap">${h(s.kind==='receipt'?'工具回执原文，可能包含元数据':'记录中的正文；可信性取决于证据来源')} · ${s.characters} 字符</div><code class="preview-source-pointer">${h(s.pointer)}</code>${s.state_hash?`<div class="sub text-wrap">状态 SHA256：${h(s.state_hash)}</div>`:''}${s.truncated?banner(`正文过长，仅显示前 ${s.content.length} 个字符；未显示部分不代表不存在。`):''}<pre class="code-block file-content">${h(s.content||'（已记录空正文）')}</pre></section>`).join('')+
   (value.sources_truncated?banner(`共有 ${value.source_count} 处记录，当前仅显示前 ${value.sources.length} 处。`):'');
  const modal=dialog('文件证据预览',body,null,null,true);$$('.dialog-footer [data-close]',modal).forEach(b=>b.textContent='关闭');
 }
 for(const node of nodes()){
  const ref=refs.find(r=>r.pointer===node.dataset.evidencePointer);if(!ref)continue;
  const btn=document.createElement('button');btn.type='button';btn.className='file-preview-button';btn.dataset.filePointer=ref.pointer;
  btn.title='预览此审核项中保存的文件内容';btn.setAttribute('aria-label','预览文件 '+ref.reference);btn.innerHTML=icon('file-search')+'预览文件';
  btn.onclick=async()=>{btn.disabled=true;try{await preview(ref);}catch(e){if(!isDisposed())toast(e.message,true);}finally{btn.disabled=false;}};node.after(btn);
 }
 const fileButton=$('[data-action="review-files"]');fileButton.disabled=!refs.length;fileButton.title=refs.length?'查看此审核项的文件引用':'此审核项没有结构化文件引用';
 act('review-files',()=>{
  const unique=[...new Map(refs.map(r=>[r.reference,r])).values()];
  if(unique.length===1)return preview(unique[0]);
  const modal=dialog('文件引用',`<ul class="preview-file-list">${unique.map((r,i)=>`<li><code>${h(r.reference)}</code><button type="button" data-preview-index="${i}">${icon('file-search')}预览</button></li>`).join('')}</ul>`,null,null,true);
  $$('[data-preview-index]',modal).forEach(btn=>btn.onclick=async()=>{btn.disabled=true;try{await preview(unique[Number(btn.dataset.previewIndex)]);}catch(e){if(!isDisposed())toast(e.message,true);}finally{btn.disabled=false;}});
 });
 icons();
}
