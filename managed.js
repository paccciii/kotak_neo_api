/* Explicit arming/resumption only. Strategy execution belongs to the server. */
(()=>{
  const el=id=>document.getElementById(id);
  let online=connected, busy=false, polling=false, resumeReview=null;
  function controls(){el('managedResumeConfirm').disabled=!online||busy||!resumeReview||!el('managedResumeConsent').checked;}
  function invalidate(){resumeReview=null;el('managedResumeConsent').checked=false;el('managedResumeBox').hidden=true;controls();}
  async function action(fn){if(busy)return;busy=true;controls();try{await fn();}catch(e){el('managedMessage').textContent=e.message;}finally{busy=false;controls();await refresh();}}
  async function reviewResume(s){invalidate();const r=await api('managed/resume-preview',{strategy_id:s.id});resumeReview=r.review_id;table('managedResumeTable',{rows:[{Field:'Contract',Value:r.strategy.contract.symbol},{Field:'Entry units filled',Value:r.strategy.entry_filled},{Field:'Exit units filled',Value:r.strategy.exit_filled},{Field:'Current protective trigger',Value:r.strategy.stop},...Object.entries(r.strategy.policy).map(([Field,Value])=>({Field,Value}))]});el('managedResumeNotice').textContent=`Review expires in ${r.expires_in} seconds. ${r.notice}`;el('managedResumeBox').hidden=false;controls();}
  async function refresh(){if(!online||polling)return;polling=true;try{
    const d=await api('managed/status'),box=el('managedStrategies');box.replaceChildren();
    const shown=d.strategies.filter(s=>s.phase!=='review');
    if(!shown.length){box.textContent='No armed or previously submitted strategies.';return;}
    const t=document.createElement('table'),head=t.createTHead().insertRow();
    for(const title of ['Contract','Management','Phase','Entry / exit filled','Avg fill · tick ref · stop · target','Distance / trail','Broker order IDs','Details','Actions']){const th=document.createElement('th');th.textContent=title;head.append(th);}
    const b=t.createTBody();for(const s of shown){const tr=b.insertRow();for(const v of [s.contract.symbol,s.armed?'ARMED':s.phase==='complete'?'COMPLETE':s.phase==='disarmed'?'DISARMED':'SUSPENDED',s.phase,`${s.entry_filled} / ${s.exit_filled}`,`${s.entry_average||'pending'} · ${s.exit_anchor||'pending'} · ${s.stop||'pending'} · ${s.target||'pending'}`,`${s.policy.distance??'fixed'} / ${s.policy.trail}`,`Entry ${s.entry_id||'unverified'}; exit ${s.exit_id||'unverified'}`,s.message])tr.insertCell().textContent=v;
      const td=tr.insertCell();function button(label,fn){const x=document.createElement('button');x.textContent=label;x.type='button';x.className='secondary';x.disabled=busy;x.onclick=()=>action(fn);td.append(x);}
      if(!['complete','disarmed'].includes(s.phase)){button('Disarm',async()=>{invalidate();const r=await api('managed/disarm',{strategy_id:s.id});el('managedMessage').textContent=r.message;});if(!s.armed)button('Review resume',()=>reviewResume(s));}
    }box.append(t);
  }catch(e){el('managedMessage').textContent='Status unavailable; displayed strategy state may be stale. '+e.message;el('managedStrategies').querySelectorAll('button').forEach(b=>b.disabled=true);}finally{polling=false;}}
  el('managedResumeConsent').onchange=controls;el('managedResumeBack').onclick=invalidate;
  el('managedResumeConfirm').onclick=()=>action(async()=>{if(!resumeReview||!el('managedResumeConsent').checked)return;const id=resumeReview;invalidate();const r=await api('managed/resume',{review_id:id,confirmed:true,automation_confirmed:true});el('managedMessage').textContent=r.message;});
  document.addEventListener('neo-connection',e=>{online=e.detail;invalidate();el('managedStrategies').replaceChildren();el('managedMessage').textContent='';if(online)refresh();});
  document.addEventListener('neo-managed-refresh',refresh);
  setInterval(()=>{if(!document.hidden)refresh();},5000);controls();if(online)refresh();
})();
