(() => {
  if (!document.body) return null;
  const cache = window.__jevFast ||= {ids:new WeakMap(), nodes:new Map(), next:1};
  cache.documentId ||= globalThis.crypto?.randomUUID?.() ||
    `${performance.timeOrigin}-${Math.random().toString(36).slice(2)}`;
  const identity = e => {
    if (!cache.ids.has(e)) cache.ids.set(e,cache.next++);
    const id=cache.ids.get(e); cache.nodes.set(id,e); return id;
  };
  for (const [id,e] of cache.nodes) if (!e.isConnected) cache.nodes.delete(id);
  const safe = e => !['password','hidden'].includes(e.type);
  const visible = e => !e.closest('[aria-hidden="true"],[inert]') &&
    e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
  const cleanURL = value => {
    if (!value) return '';
    try {
      const u=new URL(value,location.href);
      return ['http:','https:'].includes(u.protocol) ? u.origin+u.pathname : u.protocol;
    }
    catch { return ''; }
  };
  const guardURL = value => {
    if (!value) return '';
    try { return new URL(value,location.href).href; }
    catch { return String(value); }
  };
  const submissionURL = e => {
    const type=String(e?.type||'').toLowerCase();
    return ['submit','image'].includes(type) ? e.formAction : '';
  };
  const name = (e,seen=new Set()) => {
    if (!e || seen.has(e)) return '';
    seen.add(e);
    const root=e.getRootNode();
    const referenced=(e.getAttribute('aria-labelledby')||'').split(/\s+/)
      .map(id=>name((root.getElementById?root:document).getElementById(id),seen))
      .filter(Boolean).join(' ');
    return referenced || e.getAttribute('aria-label') ||
      [...(e.labels||[])].map(l=>name(l,seen)).filter(Boolean).join(' ') ||
      (['button','submit','reset'].includes(e.type) ? e.value : '') || e.getAttribute('alt') ||
      (e.tagName==='INPUT' ? '' : [...e.childNodes].map(n=>n.nodeType===3 ? n.textContent :
        n.nodeType===1 && n.getAttribute('aria-hidden')!=='true' ? name(n,seen) : '').join(' ').trim()) ||
      e.getAttribute('title') || e.getAttribute('placeholder') || '';
  };
  const roles=['button','link','checkbox','radio','switch','tab','menuitem','menuitemradio',
    'option','gridcell','combobox','textbox','searchbox','spinbutton','treeitem','listitem','row'];
  const selector='a[href],button,input,textarea,select,summary,[contenteditable="true"],'+
    roles.map(role=>'[role="'+role+'"]').join(',');
  const role = e => {
    const explicit=e.getAttribute('role');
    if (roles.includes(explicit)) return explicit;
    if (e.tagName==='BUTTON' || e.tagName==='SUMMARY') return 'button';
    if (e.tagName==='A') return 'link';
    if (e.tagName==='SELECT') return 'combobox';
    if (e.tagName==='TEXTAREA' || e.isContentEditable) return 'textbox';
    if (e.tagName==='INPUT') {
      if (['checkbox','radio'].includes(e.type)) return e.type;
      if (['button','submit','reset','image'].includes(e.type)) return 'button';
      if (e.type==='search') return 'searchbox';
      if (e.type==='number') return 'spinbutton';
      if (e.type==='file') return 'button';
      if (['text','email','url','tel'].includes(e.type)) return 'textbox';
    }
    return null;
  };
  cache.pageKey=()=>[performance.timeOrigin,location.href,scrollX,scrollY,innerWidth,innerHeight,
    docs.flatMap(d=>[...d.querySelectorAll('input,textarea,select')]).filter(safe)
      .map(e=>[identity(e),e.value,e.checked,e.selectedIndex,e.disabled,e.readOnly])];
  cache.abs=e=>{
    let r=e.getBoundingClientRect(), x=r.x, y=r.y, d=e.ownerDocument;
    while (d?.defaultView?.frameElement) {
      const fr=d.defaultView.frameElement.getBoundingClientRect();
      x+=fr.x; y+=fr.y; d=d.defaultView.frameElement.ownerDocument;
    }
    return {x,y,w:r.width,h:r.height};
  };
  cache.guard=e=>{
    if (!e?.isConnected || !visible(e)) return null;
    const scope=e.closest('form,dialog,[role="dialog"],article,li,tr,[role="row"]') || e.parentElement;
    return [identity(e),role(e),name(e),e.value??null,e.checked??null,e.selectedIndex??null,
      e.readOnly??null,e.matches(':disabled'),e.getAttribute('aria-disabled'),
      e.getAttribute('aria-expanded'),e.getAttribute('aria-checked'),e.getAttribute('aria-selected'),
      guardURL(e.getAttribute('href')),guardURL(submissionURL(e)),e.required??null,
      scope?.innerText?.slice(0,6000)||''];
  };
  // Elements live in the top document, shadow roots, and same-origin iframe documents.
  const docs=[document], found=[];
  let cross_origin_frames=0;
  const walk=root=>{
    for (const e of root.querySelectorAll('*')) {
      if (e.matches(selector)) found.push(e);
      if (e.shadowRoot) { docs.push(e.shadowRoot); walk(e.shadowRoot); }
      if (e.tagName==='IFRAME') {
        try {
          if (e.contentDocument) { docs.push(e.contentDocument); walk(e.contentDocument); }
          else cross_origin_frames++;
        }
        catch { cross_origin_frames++; }
      }
    }
  };
  walk(document);
  const actions=[], offscreen={above:[],below:[]};
  // Controls outside the viewport get named (not indexed) so scroll choices are informed.
  const noteOffscreen=(e,rname,cy)=>{
    const direction=cy<0?'above':'below', label=(name(e)||rname).slice(0,120);
    if (label && offscreen[direction].length<40 && !offscreen[direction].includes(label))
      offscreen[direction].push(label);
  };
  for (const e of found) {
    if (!safe(e) || !visible(e) || e.matches(':disabled') || e.closest('[aria-disabled="true"]')) continue;
    const rname=role(e);
    if (!rname) continue;
    let r=e.getBoundingClientRect(), g=e;
    if (r.width<=0 || r.height<=0) {
      // Monaco's edit-context textbox is 0-wide; click its visible ancestor instead.
      if (!['textbox','searchbox','combobox'].includes(rname) &&
          e.tagName!=='TEXTAREA' && !e.isContentEditable) continue;
      g=null;
      for (let p=e.parentElement,d=0; p && d<3; p=p.parentElement,d++) {
        const pr=p.getBoundingClientRect();
        if (pr.width>0 && pr.height>0) { g=p; r=pr; break; }
      }
      if (!g) continue;
    }
    const x=r.x+r.width/2, y=r.y+r.height/2;
    const root=g.getRootNode(), win=root.nodeType===11 ? root.ownerDocument.defaultView : root.defaultView;
    const abs=cache.abs(g);
    if (x<0 || y<0 || x>=win.innerWidth || y>=win.innerHeight ||
        abs.x+abs.w/2<0 || abs.x>=innerWidth || abs.y+abs.h/2<0 || abs.y+abs.h/2>=innerHeight) {
      noteOffscreen(e,rname,abs.y+abs.h/2);
      continue;
    }
    const hit=root.elementFromPoint(x,y);
    if (!hit || !g.contains(hit)) continue;
    if (['gridcell','listitem','row'].includes(rname) && e.querySelector('button,[role="button"]')) continue;
    const base={node:identity(e),role:rname,label:name(e)||rname,
      rect:{x:r.x,y:r.y,w:r.width,h:r.height}};
    if (e.closest('dialog,[role="dialog"],[aria-modal="true"]')) base.modal=true;
    const href=cleanURL(e.getAttribute('href'));
    if (href) base.href=href;
    const form_action=cleanURL(submissionURL(e));
    if (form_action) base.form_action=form_action;
    if (e.required || e.getAttribute('aria-required')==='true') base.required=true;
    if (g!==e) base.geom=identity(g);
    for (const key of ['checked','selected','expanded']) {
      const value=e.getAttribute('aria-'+key);
      if (value!==null) base[key]=value;
    }
    if (['checkbox','radio'].includes(e.type)) base.checked=String(e.checked);
    if (e.tagName==='SELECT') {
      for (const o of e.options) if (!o.selected && !o.disabled && !o.closest('optgroup[disabled]'))
        actions.push({...base,kind:'select',value:o.value,
          current_value:[...e.selectedOptions].map(o=>o.label).join(', '),label:base.label+' → '+o.label});
    } else {
      const file=e.type==='file';
      const editable=!file && !e.readOnly && e.getAttribute('aria-readonly')!=='true' &&
        (['textbox','searchbox','spinbutton'].includes(rname) ||
          (rname==='combobox' && ['INPUT','TEXTAREA'].includes(e.tagName)));
      const value=file ? (e.files?.length ? [...e.files].map(f=>f.name).join(', ') : '') :
        'value' in e ? String(e.value) :
        e.isContentEditable || rname==='combobox' ? e.innerText.trim() : '';
      actions.push({...base,kind:file?'file':editable?'fill':'click',value});
      if (editable) actions.push({...base,kind:'click',value,label:'Open '+base.label});
    }
  }
  // Preserve satisfied choices even after they scroll out of view. Free-form input
  // values are intentionally excluded; only structured selected state is sent.
  const selected_controls=[];
  let selected_controls_truncated=false;
  const rememberSelected=(e,extra={})=>{
    if (!safe(e) || !visible(e)) return;
    if (extra.omitted_options>0) selected_controls_truncated=true;
    if (selected_controls.length>=50) { selected_controls_truncated=true; return; }
    const label=(name(e)||role(e)||e.tagName.toLowerCase()).slice(0,200);
    if (!label) return;
    selected_controls.push({node:identity(e),role:role(e),label,...extra});
  };
  for (const d of docs) {
    for (const e of d.querySelectorAll('select,input[type="checkbox"],input[type="radio"],'+
      '[role="checkbox"],[role="radio"],[role="switch"],[role="tab"],[role="option"]')) {
      if (e.tagName==='SELECT') {
        const selected=[...e.selectedOptions];
        const options=selected.slice(0,20)
          .map(o=>({label:o.label.slice(0,160),value:String(o.value).slice(0,160)}));
        if (options.length) rememberSelected(e,{options,
          omitted_options:Math.max(0,selected.length-options.length)});
        continue;
      }
      const checked=['checkbox','radio'].includes(e.type) ? e.checked :
        e.getAttribute('aria-checked')==='true' || e.getAttribute('aria-selected')==='true';
      if (checked) rememberSelected(e,{checked:true,value:String(e.value??'').slice(0,160)});
    }
  }
  let focus=null;
  for (const d of docs) {
    const e=d.activeElement;
    if (!e || ['BODY','HTML'].includes(e.tagName) || !safe(e)) continue;
    // Frame elements never become key targets themselves. Same-origin frame
    // documents are visited separately below; cross-origin frames stay outside
    // the action space.
    if (e.tagName==='IFRAME') continue;
    let delegated=false;
    try {
      delegated=Boolean(e.shadowRoot?.activeElement);
    }
    catch { delegated=false; }
    if (delegated) continue;
    focus={node:identity(e),role:role(e),label:(name(e)||role(e)||e.tagName.toLowerCase()).slice(0,200),
      expanded:e.getAttribute('aria-expanded'),checked:e.getAttribute('aria-checked')};
    break;
  }
  if (focus) for (const action of actions) if (action.node===focus.node) action.focused=true;
  const alerts=[];
  for (const d of docs) {
    for (const e of d.querySelectorAll('[role="alert"],[role="status"],output')) {
      if (alerts.length>=10) break;
      if (!visible(e)) continue;
      const r=e.getBoundingClientRect(), view=e.ownerDocument.defaultView;
      if (!r.width || !r.height || r.bottom<=0 || r.top>=view.innerHeight ||
          r.right<=0 || r.left>=view.innerWidth) continue;
      const value=(e.innerText||e.textContent||'').trim().replace(/\s+/g,' ').slice(0,300);
      if (value && !alerts.some(a=>a.text===value))
        alerts.push({role:e.getAttribute('role')||e.tagName.toLowerCase(),text:value});
    }
  }
  const words=[]; let node,length=0;
  for (const d of docs) {
    const owner=d.nodeType===9 ? d : d.ownerDocument;
    const root=d.nodeType===9 ? d.body : d;
    if (!owner || !root) continue;
    const range=owner.createRange(), walker=owner.createTreeWalker(root,4), vw=owner.defaultView;
    while ((node=walker.nextNode()) && length<6000) {
      const value=node.textContent.trim(), parent=node.parentElement;
      if (!value || !parent || parent.closest('script,style,noscript,template') || !visible(parent)) continue;
      range.selectNodeContents(node); const r=range.getBoundingClientRect();
      if (r.width>0 && r.height>0 && r.bottom>0 && r.top<vw.innerHeight && r.right>0 && r.left<vw.innerWidth) {
        words.push(value); length+=value.length;
      }
    }
  }
  const text_truncated=length>=6000;
  const text=words.join('\n').slice(0,6000), height=document.documentElement.scrollHeight,
    width=document.documentElement.scrollWidth;
  const page_key=cache.pageKey(), guards={};
  for (const a of actions) if (!(a.node in guards)) guards[a.node]=cache.guard(cache.nodes.get(a.node));
  // Compare meaning and identity. Geometry is always resolved and hit-tested just before input.
  const semantics=actions.map(({rect,...action})=>action);
  const focus_guard=focus ? [focus.node,focus.role,focus.label,focus.expanded,focus.checked] : null;
  const omitted_actions=Math.max(0,actions.length-600);
  actions.splice(600);
  actions.forEach((a,i)=>a.id='e'+(i+1));
  if (scrollY+innerHeight<height-2) actions.push({id:'scroll_down',kind:'scroll',label:'Scroll down',delta:560});
  if (scrollY>0) actions.push({id:'scroll_up',kind:'scroll',label:'Scroll up',delta:-560});
  if (scrollX+innerWidth<width-2)
    actions.push({id:'scroll_right',kind:'scroll',label:'Scroll right',delta:0,dx:560});
  if (scrollX>0)
    actions.push({id:'scroll_left',kind:'scroll',label:'Scroll left',delta:0,dx:-560});
  if (focus)
    for (const key of ['Enter','Escape','Tab','Backspace','ArrowUp','ArrowDown','ArrowLeft','ArrowRight'])
      actions.push({id:'key_'+key.toLowerCase(),kind:'key',key,label:'Press '+key});
  if (location.href.includes('workbench.html'))
    for (const key of ['Ctrl+P','Ctrl+Shift+P','Ctrl+`','Ctrl+N','Ctrl+S','Ctrl+W','Ctrl+F',
                       'Ctrl+Shift+E','Ctrl+Shift+F','Ctrl+Shift+X','Ctrl+B','Ctrl+Z','Ctrl+Y'])
      actions.push({id:'key_'+key.toLowerCase().replaceAll('+','_'),kind:'key',key,
        label:'Press '+key});
  const containers=[];
  for (const e of docs.flatMap(d=>{
    const root=d.nodeType===9 ? d.body : d;
    return root ? [...root.querySelectorAll('*')] : [];
  })) {
    if (e.scrollHeight<=e.clientHeight+2 && e.scrollWidth<=e.clientWidth+2) continue;
    if (!visible(e)) continue;
    const r=e.getBoundingClientRect(), vw=e.ownerDocument.defaultView;
    const w=Math.min(r.right,vw.innerWidth)-Math.max(r.left,0),
      h=Math.min(r.bottom,vw.innerHeight)-Math.max(r.top,0);
    if (w<120 || h<80) continue;
    const overflow=e.ownerDocument.defaultView.getComputedStyle(e);
    const scrollsX=['auto','scroll'].includes(overflow.overflowX) && e.scrollWidth>e.clientWidth+2;
    const scrollsY=['auto','scroll'].includes(overflow.overflowY) && e.scrollHeight>e.clientHeight+2;
    if (!scrollsX && !scrollsY &&
        !['tree','listbox','list','grid','menu'].includes(e.getAttribute('role'))) continue;
    containers.push({e,area:w*h});
  }
  containers.sort((a,b)=>b.area-a.area);
  for (const {e} of containers.slice(0,3)) {
    const label=name(e)||e.getAttribute('role')||e.tagName.toLowerCase();
    if (e.scrollTop+e.clientHeight<e.scrollHeight-2)
      actions.push({id:'scroll_down_'+identity(e),kind:'scroll',node:identity(e),
        delta:Math.round(e.clientHeight*0.7),label:'Scroll down in '+label});
    if (e.scrollTop>0)
      actions.push({id:'scroll_up_'+identity(e),kind:'scroll',node:identity(e),
        delta:-Math.round(e.clientHeight*0.7),label:'Scroll up in '+label});
    if (e.scrollWidth>e.clientWidth+2 &&
        ['auto','scroll'].includes(e.ownerDocument.defaultView.getComputedStyle(e).overflowX)) {
      if (e.scrollLeft+e.clientWidth<e.scrollWidth-2)
        actions.push({id:'scroll_right_'+identity(e),kind:'scroll',node:identity(e),
          delta:0,dx:Math.round(e.clientWidth*0.7),label:'Scroll right in '+label});
      if (e.scrollLeft>0)
        actions.push({id:'scroll_left_'+identity(e),kind:'scroll',node:identity(e),
          delta:0,dx:-Math.round(e.clientWidth*0.7),label:'Scroll left in '+label});
    }
  }
  const documents_complete=docs.every(d=>{
    if (d.nodeType!==9 || !d.documentElement || !d.defaultView) return true;
    const de=d.documentElement, view=d.defaultView;
    return de.scrollHeight<=view.innerHeight+2 && de.scrollWidth<=view.innerWidth+2;
  });
  const viewport_complete=documents_complete && cross_origin_frames===0 && containers.length===0;
  const text_complete=viewport_complete && !text_truncated;
  const elements_complete=viewport_complete && omitted_actions===0 &&
    !offscreen.above.length && !offscreen.below.length && !selected_controls_truncated;
  const document_id=cache.documentId;
  const marker=[performance.timeOrigin,location.href,scrollX,scrollY,innerWidth,innerHeight,
    document.title,text,semantics,page_key[6],offscreen,selected_controls,
    selected_controls_truncated,focus_guard,alerts,
    text_complete,elements_complete,cross_origin_frames];
  if (!location.href.includes('workbench.html')) {
    if (history.length>1) {
      actions.push({id:'back',kind:'back',label:'Go back in page history'});
      actions.push({id:'forward',kind:'forward',label:'Go forward in page history'});
    }
    actions.push({id:'reload',kind:'reload',label:'Reload the page'});
  }
  actions.push({id:'wait',kind:'wait',label:'Wait for the page to update'});
  return {url:location.href,title:document.title,w:innerWidth,h:innerHeight,text,
    scroll:{y:scrollY,height},actions,marker,page_key,guards,omitted_actions,offscreen,
    selected_controls,selected_controls_truncated,focus,focus_guard,alerts,
    text_complete,elements_complete,document_id,
    cross_origin_frames};
})()
