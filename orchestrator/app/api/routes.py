"""Orchestrator API routes."""
import asyncio, hashlib, json, uuid
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Request, Body
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, update, desc, delete
from app.core.database import get_db
from app.core.redis_client import get_redis, OrchestratorRedis
from app.core.config import get_settings
from app.core.logging import get_logger
from app.models.models import Incident, RemediationOption, Approval, AuditEvent, IncidentEvent, AgentRegistry, IncidentEvidence, ExecutionLog
from app.services.agent_registry import AgentRegistryService
from app.services.teams_notify import send_teams_notification

logger = get_logger(__name__)
router = APIRouter(prefix="/api/v1")
settings = get_settings()
RESOURCE_MAP = {"HostCPUHigh":"CPU","HostLoadHigh":"CPU","HostIOWaitHigh":"CPU","HostStealHigh":"CPU","HostMemoryHigh":"RAM","HostAvailableMemoryLow":"RAM","HostSwapHigh":"RAM","HostOOMRisk":"RAM","HostDiskUsageHigh":"DISK","HostDiskUsageCritical":"DISK","HostDiskInodeHigh":"DISK","HostDiskIOHigh":"DISK","HostDiskLatencyHigh":"DISK"}
def _gid(): return str(uuid.uuid4())
def _ginc():
    n=datetime.now(timezone.utc); return f"INC-{n.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"

@router.get("/health")
async def health(): return {"status":"ok","service":"orchestrator"}

@router.post("/agents/register")
async def register_agent(body:dict=Body(...),db:AsyncSession=Depends(get_db)):
    await AgentRegistryService(db).register(body["agent_id"],body["agent_type"],body["supported_alerts"],body["base_url"],body["queue_name"],body.get("version","1.0.0"))
    await db.commit(); return {"status":"ok"}

@router.post("/agents/heartbeat")
async def agent_hb(body:dict=Body(...),db:AsyncSession=Depends(get_db)):
    await AgentRegistryService(db).heartbeat(body["agent_id"]); await db.commit(); return {"status":"ok"}

@router.get("/agents")
async def list_agents(db:AsyncSession=Depends(get_db)): return await AgentRegistryService(db).list_agents()

@router.post("/alerts/webhook")
async def receive_alert(body:dict=Body(...),db:AsyncSession=Depends(get_db)):
    redis=await get_redis(); orch=OrchestratorRedis(redis); reg=AgentRegistryService(db); created=[]
    for alert in body.get("alerts",[]):
        labels=alert.get("labels",{}); an=labels.get("alertname","Unknown"); inst=labels.get("instance","unknown")
        sev=labels.get("severity","warning"); fp=alert.get("fingerprint") or hashlib.md5(f"{an}:{inst}".encode()).hexdigest()
        logger.info(f"[INTAKE] {an} {inst} {sev}")
        if await orch.check_dedup(fp): logger.info(f"[INTAKE] Dedup {fp}"); continue
        agent=await reg.find_agent_for_alert(an); aid=agent["agent_id"] if agent else "unrouted"; qn=agent["queue_name"] if agent else None
        iid=_gid(); rt=RESOURCE_MAP.get(an,"UNKNOWN")
        db.add(Incident(id=iid,incident_number=_ginc(),alert_name=an,title=f"{an} on {inst}",status="new",severity=sev,instance=inst,resource_type=rt,agent_id=aid,context_json={"labels":labels,"annotations":alert.get("annotations",{})}))
        db.add(AuditEvent(event_type="incident_created",entity_type="incident",entity_id=iid,details_json={"alert_name":an,"instance":inst,"agent":aid}))
        db.add(IncidentEvent(incident_id=iid,event_type="incident_created",event_data_json={"alert_name":an}))
        await orch.set_dedup(fp,iid)
        if qn:
            await orch.push_to_agent(qn,{"incident_id":iid,"alert_name":an,"instance":inst,"severity":sev,"resource_type":rt,"labels":labels,"annotations":alert.get("annotations",{}),"skip_llm":False})
            logger.info(f"[INTAKE] ✅ {iid} → {aid}")
        created.append(iid)
    await db.commit(); return {"status":"ok","incidents_created":len(created),"incident_ids":created}

@router.post("/agents/result")
async def agent_result(body:dict=Body(...),db:AsyncSession=Depends(get_db)):
    redis=await get_redis(); orch=OrchestratorRedis(redis)
    iid=body["incident_id"]; inc=await db.get(Incident,iid)
    if not inc: raise HTTPException(404)
    # Only update fields that agent actually sends (don't overwrite what agent already wrote to shared DB)
    update_vals = {
        "status": body.get("status", "action_proposed"),
        "updated_at": func.now(),
    }
    for field in ("root_cause","root_cause_summary","canonical_root_cause","knowledge_source","summary","ai_analysis_json"):
        v = body.get(field)
        if v is not None:
            update_vals[field] = v
    if body.get("confidence") is not None:
        update_vals["llm_confidence"] = body["confidence"]
    if body.get("operator_message_vi"):
        update_vals["summary"] = body["operator_message_vi"]
    # Only overwrite prompt/response if agent explicitly sends them
    if body.get("llm_prompt_text"):
        update_vals["llm_prompt_text"] = body["llm_prompt_text"]
    if body.get("llm_raw_response"):
        update_vals["llm_raw_response"] = body["llm_raw_response"]
    await db.execute(update(Incident).where(Incident.id==iid).values(**update_vals))
    for i,opt in enumerate(body.get("remediation_options",[])):
        o=dict(opt); oid=o.pop("id",None) or _gid(); [o.pop(k,None) for k in ("incident_id","option_no")]
        db.add(RemediationOption(id=oid,incident_id=iid,option_no=i+1,priority=o.get("priority",i+1),title=o.get("title",f"Opt {i+1}"),description=o.get("description",""),risk_level=o.get("risk_level","medium"),needs_approval=o.get("needs_approval",True),action_type=o.get("action_type",""),commands_json=o.get("commands_json") or o.get("commands",[]),expected_effect=o.get("expected_effect",""),rollback_commands_json=o.get("rollback_commands_json") or o.get("rollback_commands",[]),warnings_json=o.get("warnings_json") or o.get("warnings",[]),source=o.get("source","llm")))
    db.add(IncidentEvent(incident_id=iid,event_type="agent_result",event_data_json={"agent_id":body.get("agent_id"),"status":body.get("status")}))
    await db.commit()
    # Reload from DB to get actual values (agent writes directly to shared DB)
    await db.refresh(inc)
    inc = await db.get(Incident, iid)
    opts=(await db.execute(select(RemediationOption).where(RemediationOption.incident_id==iid))).scalars().all()
    # Send Teams with actual DB data (not from body which may be incomplete)
    rcs = inc.root_cause_summary or inc.summary or body.get("root_cause_summary") or "Đang phân tích..."
    conf = inc.llm_confidence or body.get("confidence") or 0
    asyncio.create_task(send_teams_notification(iid,inc.incident_number,inc.alert_name,inc.instance,inc.severity,rcs,conf,[{"title":o.title,"risk_level":o.risk_level,"commands_json":o.commands_json} for o in opts],settings.ui_base_url))
    await orch.publish_event("incident_analyzed",{"incident_id":iid}); return {"status":"ok"}

@router.get("/incidents/stats")
async def stats(db:AsyncSession=Depends(get_db)):
    t=await db.scalar(select(func.count()).select_from(Incident)) or 0
    a=await db.scalar(select(func.count()).select_from(Incident).where(Incident.status.in_(["new","evidence_collecting","evidence_collected","analyzing","action_proposed","approved","executing","monitoring"]))) or 0
    p=await db.scalar(select(func.count()).select_from(Incident).where(Incident.status=="action_proposed")) or 0
    return {"total":t,"active":a,"pending_approvals":p}

@router.get("/incidents")
async def list_inc(limit:int=50,db:AsyncSession=Depends(get_db)):
    r=(await db.execute(select(Incident).order_by(desc(Incident.created_at)).limit(limit))).scalars().all()
    return [{"id":i.id,"incident_number":i.incident_number,"title":i.title,"status":i.status,"severity":i.severity,"alert_type":i.alert_name,"instance":i.instance,"llm_confidence":i.llm_confidence,"agent_id":i.agent_id,"created_at":str(i.created_at),"updated_at":str(i.updated_at)} for i in r]

@router.get("/incidents/{iid}")
async def get_inc(iid:str,db:AsyncSession=Depends(get_db)):
    inc=await db.get(Incident,iid)
    if not inc: raise HTTPException(404)
    opts=(await db.execute(select(RemediationOption).where(RemediationOption.incident_id==iid).order_by(RemediationOption.priority))).scalars().all()
    apprs=(await db.execute(select(Approval).where(Approval.incident_id==iid))).scalars().all()
    evts=(await db.execute(select(IncidentEvent).where(IncidentEvent.incident_id==iid).order_by(IncidentEvent.created_at))).scalars().all()
    # Evidence from shared DB (agent writes here)
    evid=(await db.execute(select(IncidentEvidence).where(IncidentEvidence.incident_id==iid).order_by(IncidentEvidence.id))).scalars().all()
    # Execution logs
    exlogs=(await db.execute(select(ExecutionLog).where(ExecutionLog.incident_id==iid).order_by(ExecutionLog.step_no))).scalars().all()

    la=None
    if inc.ai_analysis_json: ai=inc.ai_analysis_json; la={"summary":ai.get("summary",""),"confidence":ai.get("confidence",0),"root_causes":ai.get("root_causes",[])}
    return {
        "incident":{"id":inc.id,"incident_number":inc.incident_number,"title":inc.title,"status":inc.status,"severity":inc.severity,"alert_type":inc.alert_name,"instance":inc.instance,"resource_type":inc.resource_type,"component_type":inc.component_type,"agent_id":inc.agent_id,"root_cause_summary":inc.root_cause_summary or inc.summary,"llm_confidence":inc.llm_confidence,"knowledge_source":inc.knowledge_source,"llm_prompt_text":inc.llm_prompt_text,"llm_raw_response":inc.llm_raw_response,"created_at":str(inc.created_at),"updated_at":str(inc.updated_at)},
        "evidence":[{"id":e.id,"command_id":e.command_id,"command_text":e.command_text,"evidence_type":e.evidence_type,"raw_text":e.raw_text,"parsed_json":e.parsed_json,"exit_code":e.exit_code,"duration_ms":e.duration_ms,"is_key_evidence":e.is_key_evidence or False,"source_type":e.source_type,"metric_name":e.metric_name,"metric_value":e.metric_value} for e in evid],
        "action_proposals":[{"id":o.id,"priority":o.priority,"title":o.title,"description":o.description,"risk_level":o.risk_level,"commands":o.commands_json or [],"expected_effect":o.expected_effect,"rollback_commands":o.rollback_commands_json or [],"warnings":o.warnings_json or [],"status":o.status} for o in opts],
        "approvals":[{"id":a.id,"action_proposal_id":a.action_proposal_id,"decision":a.decision,"decided_by":a.decided_by,"decided_at":str(a.decided_at)} for a in apprs],
        "execution_results":[{"id":x.id,"step_no":x.step_no,"step_name":x.step_name,"status":x.status,"command":x.command,"stdout":x.stdout,"stderr":x.stderr,"exit_code":x.exit_code} for x in exlogs],
        "llm_analysis":la,
        "events":[{"event_type":e.event_type,"event_data":e.event_data_json,"created_at":str(e.created_at)} for e in evts],
    }

@router.delete("/incidents/{iid}")
async def del_inc(iid:str,db:AsyncSession=Depends(get_db)):
    for m in (IncidentEvent,Approval,RemediationOption,IncidentEvidence,ExecutionLog): await db.execute(delete(m).where(m.incident_id==iid))
    await db.execute(delete(Incident).where(Incident.id==iid)); await db.commit(); return {"status":"ok"}

@router.post("/approvals")
async def approve(body:dict=Body(...),db:AsyncSession=Depends(get_db)):
    redis=await get_redis(); orch=OrchestratorRedis(redis)
    opt=await db.get(RemediationOption,body["action_proposal_id"])
    if not opt: raise HTTPException(404)
    inc=await db.get(Incident,opt.incident_id)
    if not inc: raise HTTPException(404)
    d=body["decision"]; db.add(Approval(incident_id=inc.id,action_proposal_id=opt.id,decision=d,decided_by=body.get("decided_by","operator")))
    if d=="approved":
        sel=body.get("selected_commands")
        if sel is not None:
            cmds=opt.commands_json or []; filt=[cmds[i] for i in sel if i<len(cmds)]
            await db.execute(update(RemediationOption).where(RemediationOption.id==opt.id).values(commands_json=filt))
        await db.execute(update(Incident).where(Incident.id==inc.id).values(status="approved",selected_option_id=opt.id,updated_at=func.now()))
        await db.execute(update(RemediationOption).where(RemediationOption.id==opt.id).values(status="approved"))
        # Look up agent queue from registry
        agent_reg = (await db.execute(select(AgentRegistry).where(AgentRegistry.agent_id==inc.agent_id))).scalar_one_or_none()
        q = f"{agent_reg.queue_name}:execute" if agent_reg else f"agent:queue:{inc.agent_id}:execute"
        await orch.push_to_agent(q,{"incident_id":inc.id,"option_id":opt.id,"instance":inc.instance,"commands":(opt.commands_json or []) if sel is None else filt})
    else:
        await db.execute(update(Incident).where(Incident.id==inc.id).values(status="canceled",updated_at=func.now()))
        await db.execute(update(RemediationOption).where(RemediationOption.id==opt.id).values(status="canceled"))
    await db.commit(); await orch.publish_event("approval",{"incident_id":inc.id,"decision":d}); return {"status":"ok","decision":d}

@router.post("/incidents/{iid}/suppress")
async def suppress(iid:str,db:AsyncSession=Depends(get_db)):
    await db.execute(update(Incident).where(Incident.id==iid).values(status="suppressed",final_status="suppressed")); await db.commit(); return {"status":"ok"}

@router.post("/incidents/{iid}/unsuppress")
async def unsuppress(iid:str,db:AsyncSession=Depends(get_db)):
    inc=await db.get(Incident,iid); ns="action_proposed" if inc and inc.root_cause else "new"
    await db.execute(update(Incident).where(Incident.id==iid).values(status=ns,final_status=None)); await db.commit(); return {"status":"ok"}

@router.post("/incidents/{iid}/monitor")
async def monitor(iid:str,db:AsyncSession=Depends(get_db),body:dict=Body(default={})):
    dur=body.get("duration_minutes",15); redis=await get_redis(); inc=await db.get(Incident,iid)
    if not inc: raise HTTPException(404)
    fp=hashlib.md5(f"{inc.alert_name}:{inc.instance}".encode()).hexdigest()
    await redis.setex(f"orch:dedup:{fp}",dur*60,iid)
    await db.execute(update(Incident).where(Incident.id==iid).values(status="monitoring")); await db.commit()
    return {"status":"ok","duration_minutes":dur}

@router.post("/incidents/{iid}/skip-llm")
async def skip_llm(iid:str,db:AsyncSession=Depends(get_db)):
    redis=await get_redis(); await OrchestratorRedis(redis).set_skip_llm(iid)
    # Also push skip flag to agent via its queue so it can check mid-processing
    await redis.setex(f"agent:skip_llm:{iid}", 86400, "1")
    return {"status":"ok"}

@router.post("/incidents/{iid}/query-llm")
async def query_llm(iid:str,db:AsyncSession=Depends(get_db)):
    redis=await get_redis(); orch=OrchestratorRedis(redis); await orch.clear_skip_llm(iid)
    inc=await db.get(Incident,iid)
    if not inc: raise HTTPException(404)
    agent_reg = (await db.execute(select(AgentRegistry).where(AgentRegistry.agent_id==inc.agent_id))).scalar_one_or_none()
    q = agent_reg.queue_name if agent_reg else f"agent:queue:{inc.agent_id}"
    await orch.push_to_agent(q,{"incident_id":iid,"alert_name":inc.alert_name,"instance":inc.instance,"severity":inc.severity,"resource_type":inc.resource_type or "","labels":(inc.context_json or {}).get("labels",{}),"skip_llm":False,"rerun_llm_only":True})
    return {"status":"ok"}

@router.get("/audit")
async def audit(limit:int=100,db:AsyncSession=Depends(get_db)):
    r=(await db.execute(select(AuditEvent).order_by(desc(AuditEvent.created_at)).limit(limit))).scalars().all()
    return [{"id":a.id,"event_type":a.event_type,"entity_type":a.entity_type,"entity_id":a.entity_id,"actor":a.actor,"action":a.action,"details":a.details_json,"created_at":str(a.created_at)} for a in r]

@router.get("/events/stream")
async def sse(request:Request):
    async def gen():
        redis=await get_redis(); ps=redis.pubsub(); await ps.subscribe("orch:events")
        try:
            while not await request.is_disconnected():
                msg=await ps.get_message(ignore_subscribe_messages=True,timeout=1.0)
                if msg and msg["type"]=="message": yield f"data: {msg['data']}\n\n"
                yield ": hb\n\n"; await asyncio.sleep(1)
        finally: await ps.unsubscribe("orch:events"); await ps.close()
    return StreamingResponse(gen(),media_type="text/event-stream",headers={"Cache-Control":"no-cache","Connection":"keep-alive"})
