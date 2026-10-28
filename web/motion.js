/* Shared presentation only: never infer pipeline completion from animation. */
(() => {
  const reduced=matchMedia('(prefers-reduced-motion: reduce)');
  window.workspaceFlash=node=>{
    if(reduced.matches || !node || document.hidden)return;
    node.classList.remove('value-updated');
    void node.offsetWidth;
    node.classList.add('value-updated');
    node.addEventListener('animationend',()=>node.classList.remove('value-updated'),{once:true});
  };
  window.workspaceToast=(message,error=false)=>{
    let region=document.getElementById('workspace-toasts');
    if(!region){region=document.createElement('div');region.id='workspace-toasts';document.body.append(region);}
    while(region.children.length>=3)region.firstElementChild.remove();
    const toast=document.createElement('div');toast.className='workspace-toast'+(error?' toast-error':'');
    const text=document.createElement('span');text.setAttribute('role',error?'alert':'status');text.textContent=message;
    const close=document.createElement('button');close.type='button';close.textContent='×';close.setAttribute('aria-label','Đóng thông báo');
    if(!error){const mark=document.createElementNS('http://www.w3.org/2000/svg','svg');mark.setAttribute('viewBox','0 0 24 24');mark.setAttribute('aria-hidden','true');mark.innerHTML='<circle cx="12" cy="12" r="10"/><path d="m7 12 3 3 7-7"/>';toast.append(mark);}
    toast.append(text,close);region.append(toast);
    let remaining=error?6000:3000,start,timer;
    const pause=()=>{if(timer){clearTimeout(timer);timer=null;remaining-=performance.now()-start;}};
    const resume=()=>{if(timer || toast.matches(':hover') || toast.contains(document.activeElement))return;start=performance.now();timer=setTimeout(()=>toast.remove(),Math.max(0,remaining));};
    close.addEventListener('click',()=>{pause();toast.remove();});
    toast.addEventListener('pointerenter',pause);toast.addEventListener('pointerleave',resume);
    toast.addEventListener('focusin',pause);toast.addEventListener('focusout',()=>queueMicrotask(resume));resume();
  };
  document.addEventListener('DOMContentLoaded',()=>{
    document.querySelectorAll('#dashboard-stats .stat-card').forEach((card,index)=>{
      card.style.setProperty('--entry-delay',`${index*40}ms`);card.classList.add('stat-enter');
      card.addEventListener('animationend',()=>card.classList.remove('stat-enter'),{once:true});
    });
    let last='dashboard';
    window.addEventListener('workspace-view',event=>{
      if(last===event.detail)return;last=event.detail;
      const node=document.getElementById('view-'+event.detail);
      if(node && !reduced.matches){node.getAnimations().forEach(animation=>animation.cancel());node.animate([{opacity:0,transform:event.detail==='detail'?'translateX(16px)':'translateY(8px)'},{opacity:1,transform:'none'}],{duration:event.detail==='detail'?240:200,easing:'cubic-bezier(.2,.7,.2,1)'});}
    });
    reduced.addEventListener('change',()=>{if(reduced.matches)document.getAnimations().forEach(animation=>animation.cancel());});
    document.addEventListener('visibilitychange',()=>document.documentElement.classList.toggle('motion-paused',document.hidden));
  });
})();
