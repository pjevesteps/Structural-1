import React,{useEffect,useState} from "react";
import PdfImporter from "./PdfImporter.jsx";

const modules=[
["01","Project Management","Create and organize project data"],
["02","Plan Import","PDF plan upload, scale calibration & staged review; DXF planned"],
["03","Structural Geometry","Review grids, columns, beams and levels"],
["04","Loads & Criteria","Engineer-reviewed assignments — planned"],
["05","STAAD.Pro Analysis","Geometry preview; OpenSTAAD integration planned"],
["06","Design Checks","Independent verified checks — planned"],
["07","Structural Drawings","DXF generation — planned"],
["08","Calculation Reports","DOCX generation — planned"]
];
export default function App(){
 const [health,setHealth]=useState("Checking"),[projects,setProjects]=useState([]),[message,setMessage]=useState(""),[busy,setBusy]=useState(false),[importProject,setImportProject]=useState(null);
 async function refresh(){try{let h=await fetch("/api/health").then(r=>r.json());setHealth(h.status==="ok"?"Connected":"Unavailable");setProjects(await fetch("/api/projects").then(r=>r.json()));}catch{setHealth("Backend unavailable");}}
 useEffect(()=>{refresh();},[]);
 async function loadSample(){setBusy(true);setMessage("");try{let sample=await fetch("/sample_project.json").then(r=>r.json());let r=await fetch("/api/projects",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(sample)});let b=await r.json();if(!r.ok)throw Error(b.detail||"Save failed");setMessage("Demonstration project saved.");await refresh();}catch(e){setMessage(e.message);}finally{setBusy(false);}}
 async function action(id,a){setBusy(true);setMessage("");try{let r=await fetch(`/api/projects/${encodeURIComponent(id)}/${a}`,{method:"POST"});let b=await r.json();if(!r.ok)throw Error(b.detail||"Request failed");setMessage(a==="staad-preview"?`Geometry preview created: ${b.path} · ${b.nodes} nodes · ${b.members} members. ${b.notice}`:a==="validate"?`Schema valid. ${b.warnings.join(" ")||"No basic schema warnings."}`:b.message);}catch(e){setMessage(e.message);}finally{setBusy(false);}}
 return <main className="shell">
 <header><div className="logo">SE</div><div><b>STRUCTURAL AI OFFICE</b><small>Engineering workflow workspace</small></div><span className={health==="Connected"?"online":"status"}>{health}</span></header>
 {importProject ? (
   <PdfImporter
     project={importProject}
     onClose={()=>setImportProject(null)}
     onTransferred={async (res)=>{setMessage(res.notice);await refresh();}}
   />
 ) : (
   <>
     <section className="hero"><label>LOCAL-FIRST ENGINEERING PLATFORM</label><h1>Structural projects.<br/><em>One connected workspace.</em></h1><p>Organize project information and prepare structural geometry for a future verified STAAD.Pro workflow.</p><button disabled={busy} onClick={loadSample}>Load demonstration project</button></section>
     {message&&<div className="notice">{message}</div>}
     <label>WORKSPACE</label><h2>Engineering modules</h2><section className="grid">{modules.map(([n,t,d])=><article key={n}><i>{n}</i><h3>{t}</h3><p>{d}</p></article>)}</section>
     <label>PROJECT DATA</label><h2>Recent projects</h2><section className="projects">{projects.length===0?<p>No projects yet. Load the demonstration project to begin.</p>:projects.map(p=><article key={p.project_id}><div><h3>{p.project_name}</h3><p>{p.project_id} · {p.location}</p><small>{p.design_code}</small></div><nav><button className="secondary pdf-launch-btn" disabled={busy} onClick={()=>setImportProject(p)}>Import PDF</button><button className="secondary" disabled={busy} onClick={()=>action(p.project_id,"validate")}>Validate</button><button className="secondary" disabled={busy} onClick={()=>action(p.project_id,"staad-preview")}>STAAD preview</button><button className="secondary" disabled={busy} onClick={()=>action(p.project_id,"simulate")}>Simulation status</button></nav></article>)}</section>
   </>
 )}
 <footer>Starter v0.1 · Geometry preview only · Not structural analysis · Engineer verification required</footer></main>;
}
