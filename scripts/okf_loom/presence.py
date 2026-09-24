"""Bounded, fail-closed v2 presence authority."""
from __future__ import annotations

import json
import math
import os
import stat
import time
from pathlib import Path
from typing import Any, Callable
from .paths import ConceptIdError, concept_id_from_str, concept_id_to_str

def valid_focus(value: Any) -> bool:
    if not isinstance(value,str) or not value or len(value)>1024: return False
    try: return concept_id_to_str(concept_id_from_str(value)) == value
    except ConceptIdError: return False

def valid_label(value: Any) -> bool:
    return (isinstance(value,str) and bool(value.strip()) and len(value)<=80
            and not any(ord(c)<32 or 0x7f<=ord(c)<=0x9f or 0x202a<=ord(c)<=0x202e
                        or 0x2066<=ord(c)<=0x2069 for c in value))

class PresenceUnavailable(RuntimeError): pass
class PresenceCapacity(RuntimeError): pass


class PresenceStore:
    AGENT_TTL = 300.0
    BROWSER_TTL = 15.0
    MAX_ACTIVE = 128
    MAX_TOMBSTONES = 1024
    MAX_ISSUED = 512
    MAX_AUTHORITY_BYTES = 512 * 1024
    MAX_LABEL = 80

    def __init__(self, path: Path, transaction: Callable[[Callable[[], Any]], Any],
                 *, clock: Callable[[], float] = time.time, agent_ttl: float = AGENT_TTL) -> None:
        self.path, self._transaction, self._clock, self.agent_ttl = path, transaction, clock, agent_ttl

    @staticmethod
    def _finite(value: Any) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

    def _read(self, dir_fd: int) -> dict[str, Any] | None:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try: fd = os.open(self.path.name, flags, dir_fd=dir_fd)
        except FileNotFoundError: return None
        except OSError as exc: raise PresenceUnavailable from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > self.MAX_AUTHORITY_BYTES:
                raise PresenceUnavailable
            raw = os.read(fd, self.MAX_AUTHORITY_BYTES + 1)
            if len(raw) > self.MAX_AUTHORITY_BYTES: raise PresenceUnavailable
            value = json.loads(raw.decode("utf-8"), parse_constant=lambda _v: (_ for _ in ()).throw(ValueError()))
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise PresenceUnavailable from exc
        finally: os.close(fd)
        if not isinstance(value, dict): raise PresenceUnavailable
        return value

    def _validate_row(self, sid: str, row: Any) -> None:
        common = {"session_id","kind","actor","state","client_seq","updated_at","expires_at"}
        if not isinstance(sid, str) or not isinstance(row, dict): raise PresenceUnavailable
        if row.get("session_id") != sid or row.get("kind") not in {"agent","browser"}: raise PresenceUnavailable
        if not isinstance(row.get("client_seq"), int) or isinstance(row.get("client_seq"), bool) or row["client_seq"] < 0: raise PresenceUnavailable
        if not self._finite(row.get("updated_at")) or not self._finite(row.get("expires_at")): raise PresenceUnavailable
        if row["kind"] == "agent":
            allowed = common | {"focus","message"}
            if row.get("actor") not in {"agent","user"} or row.get("state") not in {"idle","watching","editing","thinking"}: raise PresenceUnavailable
            for key in ("focus","message"):
                if key in row and not isinstance(row[key], str): raise PresenceUnavailable
            if "focus" in row and not valid_focus(row["focus"]): raise PresenceUnavailable
            if "message" in row and len(row["message"])>201: raise PresenceUnavailable
        else:
            allowed = common | {"focus","label","viewport","capability_expires_at"}
            vp = row.get("viewport")
            if row.get("actor") != "browser" or row.get("state") not in {"watching","editing","thinking"}: raise PresenceUnavailable
            if not valid_label(row.get("label")): raise PresenceUnavailable
            if "focus" in row and not valid_focus(row["focus"]): raise PresenceUnavailable
            if not isinstance(vp, dict) or set(vp) != {"schema","x","y"} or vp.get("schema") != "graph-viewport-v1": raise PresenceUnavailable
            if not all(self._finite(vp.get(k)) and 0 <= vp[k] <= 1 for k in ("x","y")): raise PresenceUnavailable
            if not self._finite(row.get("capability_expires_at")): raise PresenceUnavailable
        if not set(row).issubset(allowed): raise PresenceUnavailable

    def _validate(self, data: Any) -> dict[str, Any]:
        if not isinstance(data, dict) or not set(data).issubset({"version","presence_rev","records","tombstones","issued","actor","state","focus","message"}): raise PresenceUnavailable
        if data.get("version") != 2 or not isinstance(data.get("presence_rev"), int) or isinstance(data.get("presence_rev"), bool) or data["presence_rev"] < 0: raise PresenceUnavailable
        records, tombstones, issued = data.get("records"), data.get("tombstones"), data.get("issued")
        if not isinstance(records, dict) or not isinstance(tombstones, dict) or not isinstance(issued, dict): raise PresenceUnavailable
        if len(records) > self.MAX_ACTIVE or len(tombstones) > self.MAX_TOMBSTONES or len(issued) > self.MAX_ISSUED: raise PresenceUnavailable
        for sid, row in records.items(): self._validate_row(sid, row)
        for sid, tomb in tombstones.items():
            if (not isinstance(sid,str) or not isinstance(tomb,dict) or set(tomb)!={"client_seq","left_at","retain_until"}
                    or not isinstance(tomb["client_seq"],int) or isinstance(tomb["client_seq"],bool) or tomb["client_seq"]<0
                    or not self._finite(tomb["left_at"]) or not self._finite(tomb["retain_until"])): raise PresenceUnavailable
        for sid, item in issued.items():
            if (not isinstance(sid,str) or not isinstance(item,dict) or set(item)!={"generation","issued_at","expires_at"}
                    or not isinstance(item["generation"],str) or not self._finite(item["issued_at"]) or not self._finite(item["expires_at"])): raise PresenceUnavailable
        agent=records.get("agent")
        for key in ("actor","state","focus","message"):
            if key in data and (agent is None or data[key] != agent.get(key)): raise PresenceUnavailable
        return data

    def _load(self, dir_fd: int) -> dict[str, Any]:
        raw = self._read(dir_fd)
        if raw is None: return {"version":2,"presence_rev":0,"records":{},"tombstones":{},"issued":{}}
        if raw.get("version") == 2: return self._validate(raw)
        if set(raw).issubset({"actor","state","focus","message"}) and isinstance(raw.get("actor"),str) and isinstance(raw.get("state"),str):
            now=self._clock(); row={**raw,"session_id":"agent","kind":"agent","client_seq":0,"updated_at":now,"expires_at":now+self.agent_ttl}
            self._validate_row("agent", row)
            return {"version":2,"presence_rev":0,"records":{"agent":row},"tombstones":{},"issued":{}}
        raise PresenceUnavailable

    def _expired_deltas(self, data: dict[str, Any], now: float) -> list[dict[str, Any]]:
        deltas=[]
        for sid,row in list(data["records"].items()):
            if row["expires_at"] <= now:
                if sid == "agent":
                    idle={"session_id":"agent","kind":"agent","actor":"agent","state":"idle",
                          "client_seq":row["client_seq"]+1,"updated_at":now,
                          "expires_at":now+self.agent_ttl}
                    data["records"][sid]=idle
                    deltas.append({"action":"upsert","record":dict(idle),"reason":"expired"})
                    continue
                if sid not in data["tombstones"] and len(data["tombstones"]) >= self.MAX_TOMBSTONES:
                    raise PresenceCapacity
                data["records"].pop(sid); retain=max(now, row.get("capability_expires_at",now))
                data["tombstones"][sid]={"client_seq":row["client_seq"],"left_at":now,"retain_until":retain}
                deltas.append({"action":"leave","session_id":sid,"reason":"expired"})
        return deltas

    def _sweep_auxiliary(self, data: dict[str, Any], now: float) -> bool:
        changed=False
        for sid,item in list(data["issued"].items()):
            if item["expires_at"] <= now: data["issued"].pop(sid); changed=True
        for sid,item in list(data["tombstones"].items()):
            if item["retain_until"] <= now and sid not in data["issued"]: data["tombstones"].pop(sid); changed=True
        return changed

    def _write(self, data: dict[str, Any], dir_fd: int) -> None:
        agent=data["records"].get("agent")
        for key in ("actor","state","focus","message"): data.pop(key,None)
        if agent:
            for key in ("actor","state","focus","message"):
                if key in agent:data[key]=agent[key]
        self._validate(data)
        payload=json.dumps(data,sort_keys=True,separators=(",",":"),ensure_ascii=False)
        if len(payload.encode()) > self.MAX_AUTHORITY_BYTES: raise PresenceCapacity
        temp=f".presence-{os.getpid()}-{time.time_ns()}.tmp"
        flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,"O_CLOEXEC",0)|getattr(os,"O_NOFOLLOW",0)
        fd=-1
        try:
            fd=os.open(temp,flags,0o600,dir_fd=dir_fd)
            raw=payload.encode("utf-8"); offset=0
            while offset<len(raw): offset += os.write(fd,raw[offset:])
            os.fsync(fd); os.close(fd); fd=-1
            os.replace(temp,self.path.name,src_dir_fd=dir_fd,dst_dir_fd=dir_fd)
            os.fsync(dir_fd)
        except OSError as exc: raise PresenceUnavailable from exc
        finally:
            if fd>=0: os.close(fd)
            try: os.unlink(temp,dir_fd=dir_fd)
            except OSError: pass

    def _public(self,data): return {"version":2,"presence_rev":data["presence_rev"],"records":[dict(v) for _,v in sorted(data["records"].items())]}

    def issue(self, sid: str, generation: str, issued_at: float, expires_at: float) -> None:
        def work(dir_fd):
            data=self._load(dir_fd)
            if len(data["issued"]) >= self.MAX_ISSUED: raise PresenceCapacity
            data["issued"][sid]={"generation":generation,"issued_at":issued_at,"expires_at":expires_at}; self._write(data,dir_fd)
        self._transaction(work)

    def authorized(self,sid,generation,now):
        def work(dir_fd):
            item=self._load(dir_fd)["issued"].get(sid)
            return bool(item and item["generation"]==generation and item["issued_at"]<=now<item["expires_at"])
        return self._transaction(work)

    def snapshot(self):
        def work(dir_fd):
            data=self._load(dir_fd); now=self._clock(); visible=dict(data); visible["records"]={k:v for k,v in data["records"].items() if v["expires_at"]>now}; return self._public(visible)
        return self._transaction(work)

    def mutate(self,sid:str,seq:int,record:dict[str,Any]|None,*,capability_expires_at:float|None=None):
        def work(dir_fd):
            data=self._load(dir_fd); now=self._clock()
            return self._mutate_loaded(data,dir_fd,sid,seq,record,now,capability_expires_at)
        return self._transaction(work)

    def _mutate_loaded(self,data,dir_fd,sid,seq,record,now,capability_expires_at):
            current=data["records"].get(sid); prior=(current or data["tombstones"].get(sid,{})).get("client_seq",-1)
            if seq<=prior:return self._public(data),False,None
            if record is not None and current is None and len(data["records"])>=self.MAX_ACTIVE: raise PresenceCapacity
            if record is None:
                if sid not in data["tombstones"] and len(data["tombstones"])>=self.MAX_TOMBSTONES: raise PresenceCapacity
                data["records"].pop(sid,None); retain=max(now,capability_expires_at if capability_expires_at is not None else now + 60.0); data["tombstones"][sid]={"client_seq":seq,"left_at":now,"retain_until":retain}; delta={"action":"leave","session_id":sid,"reason":"client"}
            else:
                row=dict(record); row.update(session_id=sid,client_seq=seq,updated_at=now,expires_at=now+(self.agent_ttl if row.get("kind")=="agent" else self.BROWSER_TTL))
                if row.get("kind") == "browser": row["capability_expires_at"]=capability_expires_at if capability_expires_at is not None else now + 60.0
                self._validate_row(sid,row); data["records"][sid]=row; data["tombstones"].pop(sid,None); delta={"action":"upsert","record":dict(row)}
            data["presence_rev"]+=1; self._write(data,dir_fd); delta.update(type="presence",presence_version=2,presence_rev=data["presence_rev"]); return self._public(data),True,delta

    def mutate_authorized(self,sid:str,generation:str,issued_at:float,expires_at:float,
                          seq:int,record:dict[str,Any]|None):
        """Authorize capability registry row and mutate under one lock/clock."""
        def work(dir_fd):
            data=self._load(dir_fd); now=self._clock(); item=data["issued"].get(sid)
            if (not item or item["generation"]!=generation or item["issued_at"]!=issued_at
                    or item["expires_at"]!=expires_at or not item["issued_at"]<=now<item["expires_at"]):
                return None
            return self._mutate_loaded(data,dir_fd,sid,seq,record,now,item["expires_at"])
        return self._transaction(work)

    def set_agent(self, record: dict[str, Any]):
        """Allocate the agent sequence and commit under the pinned lock."""
        def work(dir_fd):
            data=self._load(dir_fd); now=self._clock()
            current=data["records"].get("agent")
            prior=(current or data["tombstones"].get("agent",{})).get("client_seq",-1)
            return self._mutate_loaded(data,dir_fd,"agent",prior+1,record,now,None)
        return self._transaction(work)

    def sweep(self, publish: Callable[[dict[str, Any], list[dict[str, Any]]], None] | None = None):
        def work(dir_fd):
            data=self._load(dir_fd); now=self._clock(); deltas=self._expired_deltas(data,now); auxiliary_changed=self._sweep_auxiliary(data,now)
            emitted=[]
            for delta in deltas:
                data["presence_rev"]+=1; delta.update(type="presence",presence_version=2,presence_rev=data["presence_rev"]); emitted.append(delta)
            if deltas or auxiliary_changed:self._write(data,dir_fd)
            snapshot=self._public(data)
            # EventBus publication is bounded and takes only the bus lock. It
            # deliberately occurs while the cross-process presence lock is
            # still pinned so no higher revision can commit before this
            # process publishes the revision it just committed/read.
            if publish is not None: publish(snapshot,emitted)
            return snapshot,emitted
        return self._transaction(work)
