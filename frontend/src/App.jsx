import React,{useEffect,useState} from "react";
import PdfImporter from "./PdfImporter.jsx";
import GeometryBuilder from "./GeometryBuilder.jsx";
import MemberSizing from "./MemberSizing.jsx";
import LoadGenerator from "./LoadGenerator.jsx";
import DesignReview from "./DesignReview.jsx";

const modules=[
["01","Project Management","Create and organize project data"],
["02","Plan Import","PDF plan upload, zoom/pan tracing, Master Grid & openings; DXF planned"],
["03","Structural Geometry","3D wireframe generation, 2D floor framing editor & STAAD .std export"],
["04","Member Sizing & Properties","Preliminary NSCP 2015 sections, cracked modifiers, slab thickness & STAAD properties"],
["05","NSCP 2015 Loads & Export","Gravity tributary loads, NSCP 208 seismic, NSCP 207 wind, LRFD/ASD combos & full .std export"],
["06","Analysis & RC Design","3D DSM solver, STAAD .anl bridge, NSCP 2015 drift checks & beam/column/footing schedules"],
["07","Structural Drawings","DXF generation — planned"],
["08","Calculation Reports","DOCX generation — planned"]
];
export default function App(){
 const [health,setHealth]=useState("Checking"),[projects,setProjects]=useState([]),[message,setMessage]=useState(""),[busy,setBusy]=useState(false),[importProject,setImportProject]=useState(null),[geometryProject,setGeometryProject]=useState(null),[sizingProject,setSizingProject]=useState(null),[loadsProject,setLoadsProject]=useState(null),[designProject,setDesignProject]=useState(null);
 async function refresh(){try{let h=await fetch("/api/health").then(r=>r.json());setHealth(h.status==="ok"?"Connected":"Unavailable");setProjects(await fetch("/api/projects").then(r=>r.json()));}catch{setHealth("Backend unavailable");}}
 useEffect(()=>{refresh();},[]);
 async function loadSample(){setBusy(true);setMessage("");try{let sample=await fetch("/sample_project.json").then(r=>r.json());let r=await fetch("/api/projects",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(sample)});let b=await r.json();if(!r.ok)throw Error(b.detail||"Save failed");setMessage("Demonstration project saved.");await refresh();}catch(e){setMessage(e.message);}finally{setBusy(false);}}
 async function action(id,a){setBusy(true);setMessage("");try{let r=await fetch(`/api/projects/${encodeURIComponent(id)}/${a}`,{method:"POST"});let b=await r.json();if(!r.ok)throw Error(b.detail||"Request failed");setMessage(a==="staad-preview"?`STAAD.Pro export created: ${b.path} · ${b.nodes} nodes · ${b.members} members. ${b.notice}`:a==="validate"?`Schema valid. ${b.warnings.join(" ")||"No basic schema warnings."}`:b.message);}catch(e){setMessage(e.message);}finally{setBusy(false);}}
 return <main className="shell">
 <header><div className="logo">SE</div><div><b>STRUCTURAL AI OFFICE</b><small>Engineering workflow workspace</small></div><span className={health==="Connected"?"online":"status"}>{health}</span></header>
 {importProject ? (
   <PdfImporter
     project={importProject}
     onClose={()=>setImportProject(null)}
     onTransferred={async (res)=>{setMessage(res.notice);await refresh();}}
   />
 ) : geometryProject ? (
   <GeometryBuilder
     project={geometryProject}
     onClose={()=>setGeometryProject(null)}
     onUpdated={async ()=>{await refresh();}}
   />
 ) : sizingProject ? (
   <MemberSizing
     project={sizingProject}
     onClose={()=>setSizingProject(null)}
     onUpdated={async ()=>{await refresh();}}
   />
 ) : loadsProject ? (
   <LoadGenerator
     project={loadsProject}
     onClose={()=>setLoadsProject(null)}
     onUpdated={async ()=>{await refresh();}}
   />
 ) : designProject ? (
   <DesignReview
     project={designProject}
     onClose={()=>setDesignProject(null)}
     onUpdated={async ()=>{await refresh();}}
   />
 ) : (
   <>
     <section className="hero"><label>LOCAL-FIRST ENGINEERING PLATFORM</label><h1>Structural projects.<br/><em>One connected workspace.</em></h1><p>Organize project information and prepare structural geometry, preliminary sizing, NSCP 2015 loads, and RC member design for a verified STAAD.Pro workflow.</p><button disabled={busy} onClick={loadSample}>Load demonstration project</button></section>
     {message&&<div className="notice">{message}</div>}
     <label>WORKSPACE</label><h2>Engineering modules</h2><section className="grid">{modules.map(([n,t,d])=><article key={n} className={(n==="02"||n==="03"||n==="04"||n==="05"||n==="06")&&projects.length>0?"module-clickable":""} onClick={()=>{if(projects.length===0)return;if(n==="02")setImportProject(projects[0]);if(n==="03")setGeometryProject(projects[0]);if(n==="04")setSizingProject(projects[0]);if(n==="05")setLoadsProject(projects[0]);if(n==="06")setDesignProject(projects[0]);}}><i>{n}</i><h3>{t}</h3><p>{d}</p></article>)}</section>
     <label>PROJECT DATA</label><h2>Recent projects</h2><section className="projects">{projects.length===0?<p>No projects yet. Load the demonstration project to begin.</p>:projects.map(p=><article key={p.project_id}><div><h3>{p.project_name}</h3><p>{p.project_id} · {p.location}</p><small>{p.design_code}</small></div><nav><button className="secondary pdf-launch-btn" disabled={busy} onClick={()=>setImportProject(p)}>Import PDF</button><button className="secondary geom-launch-btn" disabled={busy} onClick={()=>setGeometryProject(p)}>3D Geometry</button><button className="secondary sizing-launch-btn" disabled={busy} onClick={()=>setSizingProject(p)}>Member Sizing</button><button className="secondary loads-launch-btn" disabled={busy} onClick={()=>setLoadsProject(p)}>NSCP Loads</button><button className="secondary design-launch-btn" disabled={busy} onClick={()=>setDesignProject(p)}>Analysis &amp; Design</button><button className="secondary" disabled={busy} onClick={()=>action(p.project_id,"validate")}>Validate</button><button className="secondary" disabled={busy} onClick={()=>action(p.project_id,"staad-preview")}>STAAD preview</button><button className="secondary" disabled={busy} onClick={()=>action(p.project_id,"simulate")}>Simulation status</button></nav></article>)}</section>
   </>
 )}
 <footer>Starter v0.1 · Local-first structural modeling, NSCP 2015 loads &amp; RC design · Engineer verification required</footer></main>;
}
