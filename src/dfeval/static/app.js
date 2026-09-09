"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const embeddedRecording = $("embedded-recording");
  const standalone = embeddedRecording !== null;
  const state = {data:null,index:-1,eventIndex:null,citizen:null,tab:"decision",follow:!standalone,fullHistory:false,playback:null,videoMode:"frames",stream:null,recorder:null,chunks:[],recordBytes:0,recordUrl:null,fetching:false};
  const fmt = value => typeof value === "number" && Number.isFinite(value) ? value.toLocaleString() : value === true ? "Yes" : value === false ? "No" : value == null ? "Unknown" : String(value);
  const nativeNumber = value => typeof value === "number" && Number.isFinite(value);
  const list = value => Array.isArray(value) ? value : [];
  const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
  const text = (id,value) => { $(id).textContent = value; };
  const snapshot = () => state.data?.snapshots?.[state.index]?.snapshot || {};
  const wrapper = () => state.data?.snapshots?.[state.index] || {};
  const asJSON = value => value == null ? "No record available at this point." : JSON.stringify(value,null,2);
  const node = (tag,content,className) => {const element=document.createElement(tag);if(content!==undefined)element.textContent=content;if(className)element.className=className;return element;};
  const timeLabel = value => {if(!value)return "Timestamp unavailable";const date=new Date(value);return Number.isNaN(date.getTime())?"Timestamp unavailable":date.toLocaleString();};
  const announce = message => {text("notice",message);$("notice").hidden=!message;};
  const eventLimit = () => {
    const selected=state.eventIndex??wrapper().event_index;
    return Number.isInteger(selected)?Math.min(selected,list(state.data?.events).length-1):-1;
  };
  function stopReplay(){if(state.playback)clearInterval(state.playback);state.playback=null;text("play","Play replay");$("play").setAttribute("aria-label","Play recorded snapshots");}
  function setFollow(enabled){state.follow=enabled;$("follow").checked=enabled;}
  function selectSnapshot(index,manual=true){
    const total=state.data?.snapshots?.length||0;
    state.index=total?Math.max(0,Math.min(total-1,index)):-1;
    state.eventIndex=wrapper().event_index??null;
    if(manual)setFollow(false);
    render();
  }
  function selectEvent(index){
    if(!Number.isInteger(index)||index<0||index>=list(state.data?.events).length)return;
    stopReplay();setFollow(false);state.eventIndex=index;state.tab="event";state.index=-1;
    list(state.data?.snapshots).forEach((item,i)=>{if(Number.isInteger(item.event_index)&&item.event_index<=index)state.index=i;});
    render();
  }
  function playReplay(){
    if(state.playback){stopReplay();return;}
    if((state.data?.snapshots?.length||0)<2)return;
    setFollow(false);
    if(state.index>=state.data.snapshots.length-1)selectSnapshot(0,false);
    text("play","Pause replay");$("play").setAttribute("aria-label","Pause recorded snapshot replay");
    state.playback=setInterval(()=>{if(state.index>=state.data.snapshots.length-1){stopReplay();return;}selectSnapshot(state.index+1,false);},Number($("speed").value));
  }
  function lastDecision(){
    const events=state.data?.events||[];
    const limit=eventLimit();
    for(let i=Math.min(limit,events.length-1);i>=0;i--)if(events[i].kind==="decision"&&object(events[i].decision))return events[i];
    return null;
  }
  function renderMetrics(){
    const current=snapshot(),citizens=list(current.citizens),known=Array.isArray(current.citizens);
    text("population",known?fmt(citizens.length):"—");
    text("population-note",known?"Native citizen records":"Citizen measurement unavailable");
    const drinks=current.stocks?.by_item_type?.DRINK?.stack_units;
    text("drink",nativeNumber(drinks)?fmt(drinks):"—");
    const stresses=citizens.map(person=>person.stress).filter(nativeNumber).sort((a,b)=>a-b);
    const n=stresses.length,median=n?(n%2?stresses[(n-1)/2]:(stresses[n/2-1]+stresses[n/2])/2):null;
    text("stress",median==null?"—":fmt(median));
    text("paused",current.paused===true?"Paused":current.paused===false?"Running":"Unknown");
    text("game-clock",nativeNumber(current.year)&&nativeNumber(current.year_tick)?`Year ${fmt(current.year)} · tick ${fmt(current.year_tick)}`:"Native clock unavailable");
    const total=state.data?.snapshots?.length||0,selected=wrapper();
    $("playhead").max=Math.max(0,total-1);$("playhead").value=Math.max(0,state.index);$("playhead").disabled=total<2;
    $("previous").disabled=state.index<=0;$("next").disabled=!total||state.index>=total-1;$("play").disabled=total<2;
    text("snapshot-caption",state.index>=0?`Snapshot ${state.index+1} of ${total}${selected.turn!=null?` · turn ${selected.turn}`:""} · ${selected.source||"native evidence"}`:total?"No native snapshot at or before the selected event":"No native snapshots recorded yet");
    text("snapshot-time",selected.at?timeLabel(selected.at):"No recorded wall-clock timestamp");
  }
  function renderInspector(){
    const event=state.data?.events?.[state.eventIndex],decisionEvent=lastDecision(),decision=decisionEvent?.decision;
    const action=decision?.action;
    text("action-name",typeof action==="string"?action:object(action)?JSON.stringify(action):"No decision at this point");
    text("decision-reason",typeof decision?.reason==="string"?decision.reason:"This snapshot has no preceding recorded agent decision.");
    text("decision-note",decisionEvent?`Recorded at ${decisionEvent.turn==null?"an unspecified turn":`turn ${decisionEvent.turn}`}. Queued work is not proof of completion.`:"A queued job is an instruction, not proof of completed work.");
    const value=state.tab==="snapshot"?(state.index>=0?snapshot():null):state.tab==="event"?event:decision;
    text("inspector",asJSON(value));
    text("inspector-caption",state.tab==="event"?`Event ${fmt(event?.event)} · ${event?.kind||"none selected"}`:state.tab==="snapshot"?"Complete native snapshot; null means unavailable.":"The agent's recorded decision, reason and notes.");
    document.querySelectorAll("[data-tab]").forEach(button=>button.setAttribute("aria-selected",String(button.dataset.tab===state.tab)));
  }
  function renderExperiment(){
    const experiment=object(state.data?.experiment)?state.data.experiment:{};
    text("policy-type",experiment.is_model===true?"Model policy":experiment.is_model===false?"Non-model policy":"Policy unknown");
    text("experiment-model",typeof experiment.model==="string"?experiment.model:experiment.is_model===false?"No model · scripted policy":"Unknown");
    text("experiment-policy",typeof experiment.policy_kind==="string"?experiment.policy_kind:"Unknown");
    const prompt=experiment.system_prompt;
    text("briefing-status",typeof prompt==="string"?(prompt.length?"Exact recorded text. This may be an earlier briefing; it has not been replaced with today's default.":"An empty system prompt was recorded."):"The exact system prompt is unavailable in this recording.");
    text("experiment-prompt",typeof prompt==="string"?prompt:"");
    const settings={budgets:experiment.budgets??null,scenario:experiment.scenario??null};
    text("experiment-settings",JSON.stringify(settings,null,2));
  }
  function journalField(decision,key,label){
    const block=node("div",undefined,"journal-field");block.append(node("h4",label));
    const value=decision[key];
    if(typeof value==="string"){
      if(value.length)block.append(node("pre",value,"journal-words"));
      else block.append(node("p",`An empty ${key} was recorded.`,"journal-missing"));
    }else{
      const missing=Object.prototype.hasOwnProperty.call(decision,key)?`${label} is ${value===null?"null":"not text"} in the recorded decision.`:`No ${key} field was recorded.`;
      block.append(node("p",missing,"journal-missing"));
      if(value!==null&&value!==undefined)block.append(node("pre",asJSON(value),"journal-words"));
    }
    return block;
  }
  function renderJournal(){
    const events=list(state.data?.events),limit=state.fullHistory?events.length-1:eventLimit();
    const entries=events.map((event,index)=>({event,index})).filter(({event,index})=>index<=limit&&event.kind==="decision"&&object(event.decision));
    const cutoff=eventLimit(),selected=events[cutoff];
    const point=Number.isInteger(selected?.event)?`event ${fmt(selected.event)}`:cutoff>=0?`recorded event index ${fmt(cutoff)}`:null;
    const count=`${fmt(entries.length)} ${entries.length===1?"entry":"entries"}`;
    text("journal-scope",state.fullHistory?`${count} · full recording, including entries after the playhead (${point||"event unknown"}). The audit trail below also shows the full recording.`:`${count} ${point?`through ${point}`:"at the playhead (event unknown)"}. Later journal and audit entries stay hidden.`);
    const target=$("journal-list");target.replaceChildren();
    for(const {event,index} of entries){
      const item=node("li",undefined,"journal-entry");if(index===state.eventIndex)item.classList.add("selected");
      const heading=node("div",undefined,"journal-entry-head"),button=node("button",`${event.turn==null?"Turn unknown":`Turn ${fmt(event.turn)}`} · event ${fmt(event.event)}`);
      button.type="button";button.setAttribute("aria-current",String(index===state.eventIndex));
      button.addEventListener("click",()=>{selectEvent(index);$("inspector").focus({preventScroll:true});});
      heading.append(button,node("span",timeLabel(event.at),"muted small"));
      const action=event.decision.action,actionText=typeof action==="string"?action:asJSON(action);
      const choice=node("p",`Recorded action: ${actionText}${Object.prototype.hasOwnProperty.call(event.decision,"workshop_id")?` · workshop ${fmt(event.decision.workshop_id)}`:""}${Object.prototype.hasOwnProperty.call(event.decision,"quantity")?` · quantity ${fmt(event.decision.quantity)}`:""}`,"journal-action");
      const words=node("div",undefined,"journal-fields");words.append(journalField(event.decision,"notebook","Notebook"),journalField(event.decision,"reason","Public reason"));
      const links=node("div",undefined,"journal-links");
      let before=-1,after=-1;
      list(state.data?.snapshots).forEach((entry,i)=>{if(!Number.isInteger(entry.event_index))return;if(entry.event_index<=index)before=i;else if(after<0&&entry.event_index<=limit)after=i;});
      for(const [snapshotIndex,label] of [[before,"Preceding observation"],[after,"Following observation"]])if(snapshotIndex>=0){
        const link=node("button",`${label} · snapshot ${snapshotIndex+1}`);link.type="button";
        link.addEventListener("click",()=>{stopReplay();selectSnapshot(snapshotIndex);state.tab="snapshot";renderInspector();$("inspector").focus({preventScroll:true});});links.append(link);
      }
      if(before<0)links.append(node("span","No preceding native snapshot recorded.","muted small"));
      item.append(heading,choice,words,links);target.append(item);
    }
    $("journal-empty").hidden=entries.length>0;
    text("journal-empty",state.fullHistory?"This recording has no decision notebooks or reasons to display.":"No public journal entries at this point. The initial observation can precede the first decision.");
  }
  function citizensAt(current,includeFormer=true){
    const result=list(current.citizens).filter(object).map(person=>({person,former:false}));
    const ids=new Set(result.map(entry=>entry.person.id));
    if(includeFormer)for(const person of list(current.known_former_citizens))if(object(person)&&!ids.has(person.id)){result.push({person,former:true});ids.add(person.id);}
    return result;
  }
  function renderCitizens(){
    const all=citizensAt(snapshot(),$("include-former").checked),query=$("citizen-search").value.trim().toLocaleLowerCase();
    const filtered=all.filter(({person})=>`${person.name||""} ${person.id??""}`.toLocaleLowerCase().includes(query)).sort((a,b)=>String(a.person.name||a.person.id).localeCompare(String(b.person.name||b.person.id)));
    if(state.citizen===null&&all.length)state.citizen=all[0].person.id;
    const body=$("citizen-rows");body.replaceChildren();
    for(const {person,former} of filtered){
      const row=node("tr");if(person.id===state.citizen)row.className="selected";
      const name=node("td"),button=node("button",person.name||`Citizen ${fmt(person.id)}`);button.type="button";button.setAttribute("aria-pressed",String(person.id===state.citizen));
      button.addEventListener("click",()=>{state.citizen=person.id;renderCitizens();renderPerson();});
      name.append(button,node("span",`#${fmt(person.id)}${person.dead===true?" · death reported":former?" · previously seen":""}`,"citizen-sub"));
      row.append(name,node("td",fmt(person.stress)),node("td",fmt(person.wound_count)),node("td",person.current_job?.name||person.current_job?.type||"No job recorded"));body.append(row);
    }
    text("roster-count",`${filtered.length} shown · ${all.length} current or previously observed records`);
    $("roster-empty").hidden=filtered.length>0;text("roster-empty",all.length?"No citizens match this search.":"No citizen measurement is available in this snapshot.");
  }
  function renderPerson(){
    const entry=citizensAt(snapshot()).find(({person})=>person.id===state.citizen),person=entry?.person;
    text("person-title",person?.name||(!person?"Choose someone":`Citizen ${fmt(person.id)}`));text("person-id",person?`#${fmt(person.id)}`:"—");
    text("person-state",!person?"Select a name in the roster to follow their record.":person.dead===true?"The native record reports this citizen dead.":entry.former?"Previously observed; absence alone does not establish a death.":"Present in the native citizen roster at this snapshot.");
    const facts=$("person-facts");facts.replaceChildren();
    if(person){
      const values=[["Current task",person.current_job?.name||person.current_job?.type||"No job recorded"],["Wound records",fmt(person.wound_count)],["Hunger timer",fmt(person.hunger_timer)],["Thirst timer",fmt(person.thirst_timer)],["Sleepiness timer",fmt(person.sleepiness_timer)],["Blood count / maximum",`${fmt(person.blood_count)} / ${fmt(person.blood_max)}`]];
      for(const [label,value] of values){const pair=node("div");pair.append(node("dt",label),node("dd",value));facts.append(pair);}
    }
    const needs=$("needs-rows");needs.replaceChildren();
    for(const need of list(person?.needs)){const row=node("tr");row.append(node("td",need.type||`Need ${fmt(need.id)}`),node("td",fmt(need.focus_level)),node("td",fmt(need.need_level)));needs.append(row);}
    if(!list(person?.needs).length){const row=node("tr"),cell=node("td",person?.needs==null?"Need measurements unavailable":"No need records");cell.colSpan=3;row.append(cell);needs.append(row);}
    text("person-raw",asJSON(person));renderStressChart(person);
  }
  function svgNode(tag,attributes,content){const element=document.createElementNS("http://www.w3.org/2000/svg",tag);for(const [key,value] of Object.entries(attributes))element.setAttribute(key,String(value));if(content!==undefined)element.textContent=content;return element;}
  function renderStressChart(person){
    const chart=$("stress-chart");chart.replaceChildren();
    if(!person){text("chart-note","Select a citizen to inspect their measured stress history.");return;}
    const history=list(state.data?.snapshots).slice(0,state.index+1).map(item=>citizensAt(item.snapshot||{}).find(entry=>entry.person.id===person.id)?.person.stress);
    const known=history.filter(nativeNumber);
    if(known.length<2){text("chart-note","Two snapshots with measured stress are needed to draw a history.");return;}
    let lo=Math.min(...known),hi=Math.max(...known);if(lo===hi){const pad=Math.max(1,Math.abs(lo)*.1);lo-=pad;hi+=pad;}
    const y=value=>105-(value-lo)/(hi-lo)*85,x=index=>58+index/Math.max(1,history.length-1)*360;
    for(const value of [lo,hi]){chart.append(svgNode("line",{x1:55,y1:y(value),x2:425,y2:y(value),class:"chart-grid"}),svgNode("text",{x:48,y:y(value)+3,"text-anchor":"end"},fmt(Math.round(value))));}
    let path="",connected=false;
    history.forEach((value,index)=>{if(!nativeNumber(value)){connected=false;return;}path+=`${connected?"L":"M"}${x(index).toFixed(2)},${y(value).toFixed(2)} `;connected=true;});
    chart.append(svgNode("path",{d:path,class:"chart-line"}),svgNode("text",{x:58,y:125},"First snapshot"),svgNode("text",{x:420,y:125,"text-anchor":"end"},"At playhead"));
    text("chart-note",`${known.length} native measurements up to the playhead. Missing values leave gaps; no wellbeing score is inferred.`);
  }
  function eventSummary(event){
    if(event.kind==="decision")return event.decision?.reason||event.decision?.action||"Recorded decision";
    if(event.kind==="action_result")return `${event.operation||"Action"} · ${JSON.stringify(event.arguments??event.args??{})}`;
    if(event.kind==="snapshot")return `${list(event.snapshot?.citizens).length} citizen records · native observation`;
    if(event.kind==="initial_state_check")return event.matched===true?"Starting state matches the reference run":`Starting state mismatch · ${list(event.differences).map(item=>item.field).join(", ")}`;
    if(event.kind==="error"||event.kind==="probe_error")return event.message||event.error||"Recorded error";
    if(event.kind==="run_end")return event.outcome||"Run end marker";
    if(event.kind==="policy_response")return "Recorded model response and usage";
    if(event.kind==="policy_input")return "The observation and history supplied to the policy";
    return event.operation||event.label||event.kind||"Event";
  }
  function renderEvents(){
    const events=list(state.data?.events),filter=$("event-filter").value;
    const limit=state.fullHistory?events.length-1:eventLimit();
    let indexed=events.map((event,index)=>({event,index})).filter(({event,index})=>index<=limit&&(filter==="all"||event.kind===filter||(filter==="error"&&["probe_error","error"].includes(event.kind))));
    const matchingCount=indexed.length;
    if(indexed.length>200){const near=indexed.findIndex(item=>item.index>=(state.eventIndex??events.length-1));const start=Math.max(0,Math.min(indexed.length-200,(near<0?indexed.length:near)-100));indexed=indexed.slice(start,start+200);}
    text("event-count",`${matchingCount} matching events · ${state.fullHistory?"full recording":"through the playhead"}${matchingCount>200?" · showing 200 around the playhead; the full ledger is downloadable":""}`);
    const target=$("event-list");target.replaceChildren();
    for(const {event,index} of indexed){
      const item=node("li"),button=node("button");button.type="button";button.setAttribute("aria-current",String(index===state.eventIndex));
      button.append(node("span",`#${fmt(event.event)}`,"event-number"),node("span",event.kind||"event","event-kind"),node("span",String(eventSummary(event)),"event-text"),node("span",nativeNumber(event.wall_seconds)?`${event.wall_seconds.toFixed(1)} s`:"—","event-time"));
      button.addEventListener("click",()=>{selectEvent(index);$("inspector").focus({preventScroll:true});});
      item.append(button);target.append(item);
    }
  }
  function renderVideo(){
    const live=!standalone&&state.videoMode==="live"&&state.stream;
    $("live-video").hidden=!live;$("recorded-frame").hidden=true;
    $("frames-mode").setAttribute("aria-pressed",String(state.videoMode==="frames"));$("live-mode").setAttribute("aria-pressed",String(state.videoMode==="live"));
    let frame=null;
    if(!standalone&&state.videoMode==="frames"){
      const limit=eventLimit();
      for(const item of list(state.data?.frames))if(Number.isInteger(item.event_index)&&item.event_index<=limit)frame=item;
      if(frame&&/^\/frames\/[A-Za-z0-9][A-Za-z0-9_.-]*\.(png|jpg|jpeg|webp)$/.test(frame.url)){$("recorded-frame").src=frame.url;$("recorded-frame").hidden=false;}
      else frame=null;
    }
    $("stage-empty").hidden=Boolean(live||frame);
    text("stage-empty-title",standalone?"Game frames are not included in this page.":state.videoMode==="live"?"Open a window onto the actual game.":"This point has no recorded game frame.");
    text("stage-empty-copy",standalone?"Use the replay controls to follow public words, choices, and native observations. This page does not contact a game, model, or server.":state.videoMode==="live"?"Choose the Dwarf Fortress window in your browser's sharing dialog. The stream stays here, on this computer.":"Native telemetry can be replayed without footage. Connect a live window, or inspect the recorded evidence below.");
    text("video-label",standalone?"Standalone recording":live?"LIVE · USER-SELECTED WINDOW":frame?"RECORDED GAME FRAME":"NO VIDEO CONNECTED");
    text("video-note",standalone?"Replay uses only the evidence embedded in this page. No live capture or server polling.":live?"Live window video is independent of this recording's playhead. Sharing sends no video to this server.":frame?`Actual recorded frame · ${timeLabel(frame.at)}`:"Recorded frames follow the playhead. A shared live window does not rewind.");
    $("stop-capture").hidden=!state.stream;$("record-video").hidden=!state.stream||typeof MediaRecorder==="undefined";
    text("capture-button",state.stream?"Choose another window":"Share game window");
  }
  function embeddedDownloadURL(url){
    const prefix="data:application/octet-stream;base64,";
    if(typeof url!=="string"||!url.startsWith(prefix))return false;
    const encoded=url.slice(prefix.length);
    if(encoded.length%4!==0)return false;
    const padding=encoded.endsWith("==")?2:encoded.endsWith("=")?1:0;
    const body=padding?encoded.slice(0,-padding):encoded;
    return !/[^A-Za-z0-9+/]/.test(body)&&(padding===0||body.length>0);
  }
  function renderDownloads(){
    const target=$("downloads");target.replaceChildren();
    for(const file of list(state.data?.files)){
      if(typeof file.url!=="string"||!(standalone?embeddedDownloadURL(file.url):/^\/evidence\/[A-Za-z0-9_.-]+$/.test(file.url)))continue;
      if(typeof file.name!=="string"||!/^[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(file.name))continue;
      const link=node("a",file.name);link.href=file.url;link.download=file.name;
      if(typeof file.sha256==="string")link.title=`Recorded SHA-256: ${file.sha256}`;
      target.append(link);
    }
    if(!target.childElementCount)target.append(node("span","No downloadable evidence files are available.","muted small"));
  }
  function render(){if(!state.data)return;renderMetrics();renderInspector();renderExperiment();renderJournal();renderCitizens();renderPerson();renderEvents();renderVideo();}
  function acceptData(data){
    if(!object(data)||!Array.isArray(data.events)||!Array.isArray(data.snapshots))throw new Error("Unrecognized spectator response");
    const first=!state.data,changed=first||data.revision!==state.data.revision;state.data=data;
    if(standalone&&first){state.index=data.snapshots.length?0:-1;state.eventIndex=wrapper().event_index??(data.snapshots.length?null:data.events.length?0:null);}
    else if(state.follow&&changed){state.index=data.snapshots.length-1;state.eventIndex=data.events.length?data.events.length-1:null;}
    else state.index=Math.min(state.index,data.snapshots.length-1);
    text("connection",standalone?"Standalone recording · offline":"Local evidence connected");
    text("origin",standalone?`Standalone recording · ${data.origin?.label||"recorded evidence"}`:data.origin?.label||"Recorded evidence");
    text("run-title",data.origin?.run_name||"Fortress recording");
    text("recording-status",data.recording_status==="finished"?"Finished recording":"Open log · no end marker");
    const latest=data.snapshots[data.snapshots.length-1]?.snapshot||{};text("versions",`DF ${fmt(latest.df_version)} · DFHack ${fmt(latest.dfhack_version)}`);
    text("record-count",`${data.snapshots.length} snapshots · ${data.events.length} events`);
    const errors=list(data.errors).map(error=>`${error.source||"Evidence"}${error.line?` line ${error.line}`:""}: ${error.message||"unavailable"}`);
    if(data.tail_incomplete)errors.push(standalone?"The recording ends with an incomplete event; complete earlier records remain available.":"The final event is still being written; complete earlier records remain available.");
    $("errors").hidden=!errors.length;text("errors",errors.join("\n"));
    if(data.origin?.kind==="unknown")announce("This folder has no recognized native experiment or probe markers. Available files are still inspectable.");
    if(data.origin?.kind==="mock")announce("This is mock simulator evidence, not a native Dwarf Fortress experiment. Its raw records remain inspectable.");
    if(standalone){
      const notes=typeof data.standalone_note==="string"&&data.standalone_note.length?[data.standalone_note]:[...list(data.standalone?.omissions),data.standalone?.privacy_notice].filter(value=>typeof value==="string"&&value.length);
      text("standalone-note",[...new Set(notes)].join("\n"));$("standalone-note").hidden=!notes.length;
    }
    if(changed){renderDownloads();render();}
  }
  async function poll(){
    if(standalone||state.fetching)return;state.fetching=true;
    try{
      const response=await fetch("/api/run",{cache:"no-store",credentials:"same-origin"});if(!response.ok)throw new Error(`Local server returned ${response.status}`);
      acceptData(await response.json());
    }catch(error){text("connection","Local connection unavailable");text("errors",`${error.message}. Existing evidence remains on screen; retrying locally.`);$("errors").hidden=false;}
    finally{state.fetching=false;}
  }
  function finishRecording(){if(state.recorder&&state.recorder.state!=="inactive")state.recorder.stop();}
  function stopCapture(){finishRecording();const old=state.stream;state.stream=null;if(old)old.getTracks().forEach(track=>track.stop());$("live-video").srcObject=null;renderVideo();}
  async function chooseWindow(){
    if(standalone)return;
    if(!navigator.mediaDevices?.getDisplayMedia){announce("This browser does not offer window sharing here. Native evidence and recorded frames remain available.");return;}
    try{
      const stream=await navigator.mediaDevices.getDisplayMedia({video:{displaySurface:"window"},audio:false,monitorTypeSurfaces:"exclude",selfBrowserSurface:"exclude",surfaceSwitching:"exclude"});
      const track=stream.getVideoTracks()[0];if(!track){stream.getTracks().forEach(item=>item.stop());throw new Error("No video track was selected");}
      if(track.getSettings().displaySurface==="monitor"){stream.getTracks().forEach(item=>item.stop());announce("Entire-screen sharing was stopped. Choose the Dwarf Fortress application window instead.");return;}
      stopCapture();state.stream=stream;state.videoMode="live";$("live-video").srcObject=stream;track.addEventListener("ended",()=>{if(state.stream===stream)stopCapture();});
      announce("Window sharing is local to this browser. Native evidence and the live video may describe different moments.");renderVideo();
    }catch(error){announce(error.name==="NotAllowedError"?"Window sharing was not started.":`Window sharing could not start: ${error.message}`);}
  }
  function recordVideo(){
    if(standalone||!state.stream||typeof MediaRecorder==="undefined")return;
    if(state.recorder&&state.recorder.state!=="inactive"){finishRecording();return;}
    try{
      if(state.recordUrl){URL.revokeObjectURL(state.recordUrl);state.recordUrl=null;}
      $("download-video").hidden=true;state.chunks=[];state.recordBytes=0;
      const preferred="video/webm;codecs=vp8";const options=MediaRecorder.isTypeSupported(preferred)?{mimeType:preferred}:{};
      const recorder=new MediaRecorder(state.stream,options);state.recorder=recorder;
      recorder.addEventListener("dataavailable",event=>{if(event.data.size){state.chunks.push(event.data);state.recordBytes+=event.data.size;}if(state.recordBytes>256*1024*1024&&recorder.state!=="inactive"){recorder.stop();announce("Recording reached the 256 MB local memory limit. Download it before recording again.");}});
      recorder.addEventListener("stop",()=>{const type=recorder.mimeType||"video/webm",blob=new Blob(state.chunks,{type});state.recordUrl=URL.createObjectURL(blob);const link=$("download-video");link.href=state.recordUrl;link.download=`dfeval-window-${new Date().toISOString().replace(/[:.]/g,"-")}.${type.includes("mp4")?"mp4":"webm"}`;link.hidden=false;text("record-video","Record video");state.recorder=null;});
      recorder.start(1000);text("record-video","Stop recording");announce("Recording the shared window in browser memory. Stop recording, then click Download video to save it locally. Nothing is uploaded.");
    }catch(error){announce(`Video recording is unavailable: ${error.message}`);}
  }
  $("play").addEventListener("click",playReplay);$("previous").addEventListener("click",()=>{stopReplay();selectSnapshot(state.index-1);});$("next").addEventListener("click",()=>{stopReplay();selectSnapshot(state.index+1);});
  $("playhead").addEventListener("input",()=>{stopReplay();selectSnapshot(Number($("playhead").value));});$("follow").addEventListener("change",()=>{stopReplay();setFollow($("follow").checked);if(state.follow)selectSnapshot((state.data?.snapshots?.length||1)-1,false);});
  $("speed").addEventListener("change",()=>{if(state.playback){stopReplay();playReplay();}});$("citizen-search").addEventListener("input",renderCitizens);$("include-former").addEventListener("change",renderCitizens);$("event-filter").addEventListener("change",renderEvents);
  $("full-history").addEventListener("change",()=>{state.fullHistory=$("full-history").checked;renderJournal();renderEvents();});
  document.querySelectorAll("[data-tab]").forEach(button=>button.addEventListener("click",()=>{state.tab=button.dataset.tab;renderInspector();}));
  $("copy-evidence").addEventListener("click",async()=>{try{await navigator.clipboard.writeText($("inspector").textContent);announce("Selected evidence copied to your clipboard.");}catch{announce("Clipboard access is unavailable. Select and copy text from the evidence inspector.");}});
  $("frames-mode").addEventListener("click",()=>{state.videoMode="frames";renderVideo();});$("live-mode").addEventListener("click",()=>{state.videoMode="live";renderVideo();});
  $("choose-window").addEventListener("click",chooseWindow);$("capture-button").addEventListener("click",chooseWindow);$("stop-capture").addEventListener("click",stopCapture);$("record-video").addEventListener("click",recordVideo);
  $("recorded-frame").addEventListener("error",()=>{$("recorded-frame").hidden=true;$("stage-empty").hidden=false;text("stage-empty-title","This frame could not be displayed.");text("video-label","FRAME UNAVAILABLE");});
  window.addEventListener("pagehide",()=>{stopReplay();stopCapture();if(state.recordUrl)URL.revokeObjectURL(state.recordUrl);});
  if(standalone){
    document.body.dataset.recordingMode="standalone";
    document.querySelectorAll("[data-live-control],[data-online-only]").forEach(element=>{element.hidden=true;});
    setFollow(false);text("source-badge","OFFLINE · READ ONLY");
    text("connection","Standalone recording · offline");text("origin","Standalone recording");
    text("downloads-note","The available evidence files are embedded in this page. Replay needs no running game, model connection, or local server.");
    renderVideo();
    try{acceptData(JSON.parse(embeddedRecording.textContent));}
    catch(error){text("connection","Standalone recording unavailable");text("errors",`${error.message}. This page remains offline; no server connection will be attempted.`);$("errors").hidden=false;}
  }else{poll();setInterval(poll,2000);}
})();
