"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const state = {data:null,index:0,eventIndex:null,citizen:null,tab:"decision",follow:true,playback:null,videoMode:"frames",stream:null,recorder:null,chunks:[],recordBytes:0,recordUrl:null,fetching:false};
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
  function stopReplay(){if(state.playback)clearInterval(state.playback);state.playback=null;text("play","Play replay");$("play").setAttribute("aria-label","Play recorded snapshots");}
  function setFollow(enabled){state.follow=enabled;$("follow").checked=enabled;}
  function selectSnapshot(index,manual=true){
    const total=state.data?.snapshots?.length||0;
    state.index=Math.max(0,Math.min(total-1,index));
    state.eventIndex=wrapper().event_index??null;
    if(manual)setFollow(false);
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
    const limit=state.eventIndex??wrapper().event_index??events.length-1;
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
    $("playhead").max=Math.max(0,total-1);$("playhead").value=state.index;$("playhead").disabled=total<2;
    $("previous").disabled=state.index<=0;$("next").disabled=state.index>=total-1;$("play").disabled=total<2;
    text("snapshot-caption",total?`Snapshot ${state.index+1} of ${total}${selected.turn!=null?` · turn ${selected.turn}`:""} · ${selected.source||"native evidence"}`:"No native snapshots recorded yet");
    text("snapshot-time",selected.at?timeLabel(selected.at):"No recorded wall-clock timestamp");
  }
  function renderInspector(){
    const event=state.data?.events?.[state.eventIndex],decisionEvent=lastDecision(),decision=decisionEvent?.decision;
    const action=decision?.action;
    text("action-name",typeof action==="string"?action:object(action)?JSON.stringify(action):"No decision at this point");
    text("decision-reason",typeof decision?.reason==="string"?decision.reason:"This snapshot has no preceding recorded agent decision.");
    text("decision-note",decisionEvent?`Recorded at ${decisionEvent.turn==null?"an unspecified turn":`turn ${decisionEvent.turn}`}. Queued work is not proof of completion.`:"A queued job is an instruction, not proof of completed work.");
    const value=state.tab==="snapshot"?snapshot():state.tab==="event"?event:decision;
    text("inspector",asJSON(value));
    text("inspector-caption",state.tab==="event"?`Event ${fmt(event?.event)} · ${event?.kind||"none selected"}`:state.tab==="snapshot"?"Complete native snapshot; null means unavailable.":"The agent's recorded decision, reason and notes.");
    document.querySelectorAll("[data-tab]").forEach(button=>button.setAttribute("aria-selected",String(button.dataset.tab===state.tab)));
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
    let indexed=events.map((event,index)=>({event,index})).filter(({event})=>filter==="all"||event.kind===filter||(filter==="error"&&["probe_error","error"].includes(event.kind)));
    const matchingCount=indexed.length;
    if(indexed.length>200){const near=indexed.findIndex(item=>item.index>=(state.eventIndex??events.length-1));const start=Math.max(0,Math.min(indexed.length-200,(near<0?indexed.length:near)-100));indexed=indexed.slice(start,start+200);}
    text("event-count",`${matchingCount} matching events${matchingCount>200?" · showing 200 around the playhead; the full ledger is downloadable":""}`);
    const target=$("event-list");target.replaceChildren();
    for(const {event,index} of indexed){
      const item=node("li"),button=node("button");button.type="button";button.setAttribute("aria-current",String(index===state.eventIndex));
      button.append(node("span",`#${fmt(event.event)}`,"event-number"),node("span",event.kind||"event","event-kind"),node("span",String(eventSummary(event)),"event-text"),node("span",nativeNumber(event.wall_seconds)?`${event.wall_seconds.toFixed(1)} s`:"—","event-time"));
      button.addEventListener("click",()=>{stopReplay();setFollow(false);state.eventIndex=index;state.tab="event";let nearest=-1;list(state.data?.snapshots).forEach((snap,i)=>{if(snap.event_index!=null&&snap.event_index<=index)nearest=i;});if(nearest>=0)state.index=nearest;render();});
      item.append(button);target.append(item);
    }
  }
  function renderVideo(){
    const live=state.videoMode==="live"&&state.stream;
    $("live-video").hidden=!live;$("recorded-frame").hidden=true;
    $("frames-mode").setAttribute("aria-pressed",String(state.videoMode==="frames"));$("live-mode").setAttribute("aria-pressed",String(state.videoMode==="live"));
    let frame=null;
    if(state.videoMode==="frames"){
      const limit=state.eventIndex??wrapper().event_index;
      for(const item of list(state.data?.frames))if(limit==null||item.event_index<=limit)frame=item;
      if(frame&&/^\/frames\/[A-Za-z0-9][A-Za-z0-9_.-]*\.(png|jpg|jpeg|webp)$/.test(frame.url)){$("recorded-frame").src=frame.url;$("recorded-frame").hidden=false;}
      else frame=null;
    }
    $("stage-empty").hidden=Boolean(live||frame);
    text("stage-empty-title",state.videoMode==="live"?"Open a window onto the actual game.":"This point has no recorded game frame.");
    text("stage-empty-copy",state.videoMode==="live"?"Choose the Dwarf Fortress window in your browser's sharing dialog. The stream stays here, on this computer.":"Native telemetry can be replayed without footage. Connect a live window, or inspect the recorded evidence below.");
    text("video-label",live?"LIVE · USER-SELECTED WINDOW":frame?"RECORDED GAME FRAME":"NO VIDEO CONNECTED");
    text("video-note",live?"Live window video is independent of this recording's playhead. Sharing sends no video to this server.":frame?`Actual recorded frame · ${timeLabel(frame.at)}`:"Recorded frames follow the playhead. A shared live window does not rewind.");
    $("stop-capture").hidden=!state.stream;$("record-video").hidden=!state.stream||typeof MediaRecorder==="undefined";
    text("capture-button",state.stream?"Choose another window":"Share game window");
  }
  function renderDownloads(){const target=$("downloads");target.replaceChildren();for(const file of list(state.data?.files)){if(typeof file.url!=="string"||!/^\/evidence\/[A-Za-z0-9_.-]+$/.test(file.url))continue;const link=node("a",file.name);link.href=file.url;link.download=file.name;target.append(link);}}
  function render(){if(!state.data)return;renderMetrics();renderInspector();renderCitizens();renderPerson();renderEvents();renderVideo();}
  async function poll(){
    if(state.fetching)return;state.fetching=true;
    try{
      const response=await fetch("/api/run",{cache:"no-store",credentials:"same-origin"});if(!response.ok)throw new Error(`Local server returned ${response.status}`);
      const data=await response.json();if(!object(data)||!Array.isArray(data.events)||!Array.isArray(data.snapshots))throw new Error("Unrecognized spectator response");
      const changed=!state.data||data.revision!==state.data.revision;state.data=data;
      if(state.follow&&changed){state.index=Math.max(0,data.snapshots.length-1);state.eventIndex=data.events.length?data.events.length-1:null;}else state.index=Math.min(state.index,Math.max(0,data.snapshots.length-1));
      text("connection","Local evidence connected");text("origin",data.origin?.label||"Recorded evidence");text("run-title",data.origin?.run_name||"Fortress recording");
      text("recording-status",data.recording_status==="finished"?"Finished recording":"Open log · no end marker");
      const latest=data.snapshots[data.snapshots.length-1]?.snapshot||{};text("versions",`DF ${fmt(latest.df_version)} · DFHack ${fmt(latest.dfhack_version)}`);
      text("record-count",`${data.snapshots.length} snapshots · ${data.events.length} events`);
      const errors=list(data.errors).map(error=>`${error.source||"Evidence"}${error.line?` line ${error.line}`:""}: ${error.message||"unavailable"}`);
      if(data.tail_incomplete)errors.push("The final event is still being written; complete earlier records remain available.");
      $("errors").hidden=!errors.length;text("errors",errors.join("\n"));
      if(data.origin?.kind==="unknown")announce("This folder has no recognized native experiment or probe markers. Available files are still inspectable.");
      if(data.origin?.kind==="mock")announce("This is mock simulator evidence, not a native Dwarf Fortress experiment. Its raw records remain inspectable.");
      if(changed){renderDownloads();render();}
    }catch(error){text("connection","Local connection unavailable");text("errors",`${error.message}. Existing evidence remains on screen; retrying locally.`);$("errors").hidden=false;}
    finally{state.fetching=false;}
  }
  function finishRecording(){if(state.recorder&&state.recorder.state!=="inactive")state.recorder.stop();}
  function stopCapture(){finishRecording();const old=state.stream;state.stream=null;if(old)old.getTracks().forEach(track=>track.stop());$("live-video").srcObject=null;renderVideo();}
  async function chooseWindow(){
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
    if(!state.stream||typeof MediaRecorder==="undefined")return;
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
  document.querySelectorAll("[data-tab]").forEach(button=>button.addEventListener("click",()=>{state.tab=button.dataset.tab;renderInspector();}));
  $("copy-evidence").addEventListener("click",async()=>{try{await navigator.clipboard.writeText($("inspector").textContent);announce("Selected evidence copied to your clipboard.");}catch{announce("Clipboard access is unavailable. Select and copy text from the evidence inspector.");}});
  $("frames-mode").addEventListener("click",()=>{state.videoMode="frames";renderVideo();});$("live-mode").addEventListener("click",()=>{state.videoMode="live";renderVideo();});
  $("choose-window").addEventListener("click",chooseWindow);$("capture-button").addEventListener("click",chooseWindow);$("stop-capture").addEventListener("click",stopCapture);$("record-video").addEventListener("click",recordVideo);
  $("recorded-frame").addEventListener("error",()=>{$("recorded-frame").hidden=true;$("stage-empty").hidden=false;text("stage-empty-title","This frame could not be displayed.");text("video-label","FRAME UNAVAILABLE");});
  window.addEventListener("pagehide",()=>{stopReplay();stopCapture();if(state.recordUrl)URL.revokeObjectURL(state.recordUrl);});
  poll();setInterval(poll,2000);
})();
