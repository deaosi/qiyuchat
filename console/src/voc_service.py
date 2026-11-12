"""HTTP-facing jobs for the derived VOC engine, isolated from business tables.

The worker has its own system service so a web restart cannot kill a model task.
Credentials use short-lived private profile files, never job rows or arguments.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import uuid
from contextlib import closing, contextmanager
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import LLMConfig, Settings, load_settings

LAUNCH_LOCK = threading.RLock()
try:
    DISPLAY_ZONE = ZoneInfo('Asia/Shanghai')
except ZoneInfoNotFoundError:
    DISPLAY_ZONE = timezone(timedelta(hours=8), 'Asia/Shanghai')
ACTIVE = ('queued', 'running')
STATES = frozenset((*ACTIVE, 'completed', 'partial', 'failed', 'skipped'))
WORKBENCH_TRENDS = frozenset(('all', 'new', 'rising', 'persistent'))
SCHEMA = """
CREATE TABLE IF NOT EXISTS voc_service_job (
 id TEXT PRIMARY KEY,status TEXT NOT NULL,total INTEGER NOT NULL DEFAULT 0,
 processed INTEGER NOT NULL DEFAULT 0,failed INTEGER NOT NULL DEFAULT 0,
 skipped INTEGER NOT NULL DEFAULT 0,usage_tokens INTEGER NOT NULL DEFAULT 0,
 error TEXT NOT NULL DEFAULT '',owner INTEGER NOT NULL DEFAULT 0,
 request_json TEXT NOT NULL,model TEXT NOT NULL,created_at REAL NOT NULL,updated_at REAL NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS voc_service_one_active
 ON voc_service_job((1)) WHERE status IN ('queued','running');
CREATE TABLE IF NOT EXISTS voc_service_worker (
 id INTEGER PRIMARY KEY CHECK(id=1),pid INTEGER NOT NULL,updated_at REAL NOT NULL);
"""


class ServiceError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _engine():
    from . import voc_engine
    return voc_engine


def _path(cfg: Settings) -> Path:
    return Path(_engine().path(cfg))


@contextmanager
def _db(cfg: Settings):
    path = _path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(str(path), timeout=15)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=15000')
        db.executescript(SCHEMA)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        yield db
        db.commit()


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if os.name=='nt':
            import ctypes
            handle=ctypes.windll.kernel32.OpenProcess(0x1000,False,pid)
            if handle:ctypes.windll.kernel32.CloseHandle(handle)
            return bool(handle)
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (OSError, ValueError):
        return False


def _recover(db, cfg):
    now = time.time()
    for row in db.execute("SELECT id,owner,updated_at FROM voc_service_job WHERE status='running'").fetchall():
        if not _alive(row['owner']) and now - row['updated_at'] > 60:
            db.execute("UPDATE voc_service_job SET status='failed',error='整理进程已中断，可重新整理未完成内容。',updated_at=? WHERE id=?", (now,row['id']))
            _discard_profile(cfg,row['id'])


def _public(row) -> dict:
    keys = ('id','status','total','processed','failed','skipped','usage_tokens','error','model')
    data = {key: row[key] for key in keys}
    for key in ('created_at','updated_at'):
        data[key] = datetime.fromtimestamp(row[key], DISPLAY_ZONE).strftime('%Y-%m-%d %H:%M:%S')
    return data


def jobs(cfg: Settings) -> dict:
    with _db(cfg) as db:
        _recover(db,cfg)
        return {'items': [_public(row) for row in db.execute('SELECT * FROM voc_service_job ORDER BY created_at DESC LIMIT 30')]}


def busy(cfg: Settings) -> bool:
    path = _path(cfg)
    if maintenance(cfg):
        return True
    if not path.exists():
        return False
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='voc_service_job'").fetchone():
            return False
        return any(row[0]=='queued' or _alive(row[1]) for row in db.execute("SELECT status,owner FROM voc_service_job WHERE status IN ('queued','running')"))


def maintenance(cfg: Settings) -> bool:
    return (_path(cfg).parent/'.voc-release-maintenance').is_file()


def _legacy_busy(cfg: Settings) -> bool:
    path = Path(cfg.runtime.db_path).resolve()
    if not path.is_file():
        return False
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
        for table in ('voc_job','knowledge_job'):
            if db.execute("SELECT 1 FROM sqlite_master WHERE name=?",(table,)).fetchone():
                for status,owner in db.execute('SELECT status,owner FROM '+table+" WHERE status IN ('queued','running')"):
                    try: pid = int(str(owner).split(':',1)[0])
                    except ValueError: continue
                    if _alive(pid): return True
    return False


def _integer(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ServiceError(f'{name}须为 {low}–{high} 的整数。')
    return value


def _profile_path(cfg: Settings, job_id: str) -> Path:
    if len(job_id)!=32 or any(c not in '0123456789abcdef' for c in job_id):
        raise ServiceError('任务编号无效。')
    return _path(cfg).parent/'.voc-private-jobs'/(job_id+'.json')


def _save_profile(cfg: Settings, job_id: str):
    path=_profile_path(cfg,job_id)
    path.parent.mkdir(mode=0o700,exist_ok=True)
    path.parent.chmod(0o700)
    descriptor,temp=tempfile.mkstemp(prefix='.profile-',dir=path.parent)
    try:
        with os.fdopen(descriptor,'w',encoding='utf-8') as file:
            json.dump(asdict(cfg.llm),file,ensure_ascii=False)
            file.flush();os.fsync(file.fileno())
        os.chmod(temp,0o600);os.replace(temp,path)
    finally:
        if os.path.exists(temp):os.unlink(temp)


def _discard_profile(cfg: Settings, job_id: str):
    try:_profile_path(cfg,job_id).unlink(missing_ok=True)
    except (OSError,ServiceError):pass


def _prune_profiles(cfg: Settings):
    folder=_path(cfg).parent/'.voc-private-jobs'
    if not folder.is_dir():return
    with _db(cfg) as db:
        active={row[0] for row in db.execute("SELECT id FROM voc_service_job WHERE status IN ('queued','running')")}
    for file in folder.glob('*.json'):
        try:
            if file.stem not in active and time.time()-file.stat().st_mtime>3600:
                _discard_profile(cfg,file.stem)
        except OSError:continue


def _worker_ready(cfg: Settings) -> bool:
    with _db(cfg) as db:
        row=db.execute('SELECT pid,updated_at FROM voc_service_worker WHERE id=1').fetchone()
        return bool(row and time.time()-row['updated_at']<20 and _alive(row['pid']))


def start(cfg: Settings, filters: dict, body: object, *, launch=True) -> dict:
    if cfg.runtime.mock or cfg.llm.dry_run or not cfg.llm.api_key:
        raise ServiceError('请先启用业务模型网关。')
    if not isinstance(body,dict) or set(body)-{'limit','max_calls','max_tokens'}:
        raise ServiceError('整理参数不符合约定。')
    limits = {'limit':_integer(body.get('limit',20),'整理条数',1,100),
              'max_calls':_integer(body.get('max_calls',20),'模型调用上限',1,100),
              'max_tokens':_integer(body.get('max_tokens',400000),'本轮token预算',4000,2000000)}
    if len(json.dumps(filters,ensure_ascii=False))>4000:
        raise ServiceError('筛选内容过长。')
    with LAUNCH_LOCK:
        if maintenance(cfg):
            raise ServiceError('整理服务正在更新，请稍后重试。',503)
        if _legacy_busy(cfg):
            raise ServiceError('已有整理任务正在运行，请完成后再启动新整理。',409)
        if busy(cfg):
            raise ServiceError('已有新整理任务正在运行。',409)
        if launch and not _worker_ready(cfg):
            raise ServiceError('独立整理服务尚未就绪，请稍后重试。',503)
        check = _engine().check(cfg,filters)
        semantics = check.get('semantics',check)
        if isinstance(semantics,dict) and semantics.get('ready') is False:
            raise ServiceError('中文语义模型尚未就绪，请联系管理员完成模型准备。',503)
        if check.get('alignment_ready') is False:
            raise ServiceError('原文定位组件尚未就绪，请联系管理员完成依赖安装。',503)
        with _db(cfg) as db:
            db.execute('BEGIN IMMEDIATE')
            _recover(db,cfg)
            if db.execute("SELECT 1 FROM voc_service_job WHERE status IN ('queued','running')").fetchone():
                raise ServiceError('已有新整理任务正在运行。',409)
            job_id,now = uuid.uuid4().hex,time.time()
            request = json.dumps({'filters':filters,'domain':cfg.runtime.voc_domain,
                                  'episode_linking':cfg.runtime.voc_domain=='zhihuige',**limits},ensure_ascii=False)
            _save_profile(cfg,job_id)
            try:
                db.execute('INSERT INTO voc_service_job(id,status,owner,request_json,model,created_at,updated_at) VALUES (?,?,?,?,?,?,?)',
                           (job_id,'queued',0,request,cfg.llm.model_main,now,now))
                db.commit()
            except BaseException:
                _discard_profile(cfg,job_id)
                raise
        return {'job':next(item for item in jobs(cfg)['items'] if item['id']==job_id)}


def _update(cfg,job_id,**values):
    allowed = {'status','total','processed','failed','skipped','usage_tokens','error','owner'}
    values = {key:value for key,value in values.items() if key in allowed}
    values['updated_at'] = time.time()
    with _db(cfg) as db:
        db.execute('UPDATE voc_service_job SET '+','.join(key+'=?' for key in values)+' WHERE id=?',(*values.values(),job_id))


def execute(cfg: Settings, job_id: str) -> dict:
    with _db(cfg) as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM voc_service_job WHERE id=?',(job_id,)).fetchone()
        if row is None or row['status']!='queued':
            raise ServiceError('待执行任务不存在或已经执行。',409)
        db.execute("UPDATE voc_service_job SET status='running',owner=?,updated_at=? WHERE id=?",(os.getpid(),time.time(),job_id))
    auxiliary_usage=0
    def progress(data):
        values={key:data[key] for key in ('total','processed','failed','skipped','usage_tokens')
                if type(data.get(key)) is int and data[key]>=0}
        if auxiliary_usage:
            values['usage_tokens']=values.get('usage_tokens',0)+auxiliary_usage
        _update(cfg,job_id,**values)
    try:
        request=json.loads(row['request_json'])
        with _profile_path(cfg,job_id).open(encoding='utf-8') as file:
            profile=json.load(file)
        profile['extra_headers']=tuple(tuple(pair) for pair in profile.get('extra_headers',()))
        cfg=replace(cfg,llm=LLMConfig(**profile))
        cfg=replace(cfg,runtime=replace(cfg.runtime,voc_domain=request.get('domain','')))
        _discard_profile(cfg,job_id)
        calls,tokens=request['max_calls'],request['max_tokens']
        domain_analysis=request.get('episode_linking') is True and cfg.runtime.voc_domain=='zhihuige'
        notices=[]
        auxiliary_processed=0
        solution_calls=min(4,2*(calls//16))
        solution_tokens=min(160000,tokens//6)
        if domain_analysis and solution_calls and solution_tokens>=32768:
            from . import voc_solution_store
            try:
                solutions=voc_solution_store.run(cfg,request['filters'],max_calls=solution_calls,max_tokens=solution_tokens)
                calls-=solutions['calls']
                tokens-=max(solutions['reserved_tokens'],solutions['usage_tokens'])
                auxiliary_usage+=solutions['usage_tokens']
                auxiliary_processed+=solutions['generated']
                if solutions['errors']:
                    notices.append('部分处理方案暂未生成，系统会在后续任务中重试。')
            except (OSError,sqlite3.Error,RuntimeError,ValueError):
                calls-=solution_calls
                tokens-=solution_tokens
                notices.append('处理方案生成暂未完成，系统会在后续任务中重试。')
            if auxiliary_usage:
                _update(cfg,job_id,usage_tokens=auxiliary_usage)
        episode_calls=min(4,calls//4)
        episode_tokens=min(200000,tokens//4)
        if domain_analysis and episode_calls and episode_tokens>=32768:
            from . import voc_episode_live
            from .voc_episode_topics import LocalReranker
            from .voc_semantics import SemanticError
            try:
                model_path=os.environ.get('VOC_EPISODE_RERANKER_PATH','').strip()
                rerank=LocalReranker(model_path) if model_path else None
                episode=voc_episode_live.run(cfg,filters=request['filters'],max_calls=episode_calls,
                                             max_tokens=episode_tokens,embedder=_engine()._semantic(cfg),rerank=rerank)
                calls-=episode['calls']
                tokens-=max(episode['reserved_tokens'],episode['usage_tokens'])
                auxiliary_usage+=episode['usage_tokens']
                auxiliary_processed+=episode['segments_processed']
                if any(key.startswith('gateway_') or key=='semantic_unavailable' for key in episode['errors']):
                    notices.append('部分多轮会话暂未整理，系统会在后续任务中重试。')
            except (OSError,sqlite3.Error,SemanticError,RuntimeError,ValueError):
                # A stopped episode pass may already have reached the gateway.
                calls-=episode_calls
                tokens-=episode_tokens
                notices.append('多轮会话整理暂未完成，系统会在后续任务中重试。')
            if auxiliary_usage:
                _update(cfg,job_id,usage_tokens=auxiliary_usage)
        if tokens>=4000:
            result=_engine().run(cfg,request['filters'],limit=request['limit'],max_calls=calls,
                                 max_tokens=tokens,shadow=True,progress=progress)
            job=result.get('job',result)
        else:
            job={'status':'partial','usage_tokens':0,'error':'本轮模型预算已用完，其余问答将在后续任务中整理。'}
        progress(job)
        status=job.get('status','completed')
        if status not in STATES-set(ACTIVE):
            raise ServiceError('整理任务没有进入终态。',502)
        # Engine errors have a designated public boundary; never expose unknown exceptions.
        error=job.get('error','') if isinstance(job.get('error',''),str) else ''
        if notices:
            error=' '.join(filter(None,(error,*notices)))
            if status in ('completed','skipped'):
                status='partial' if job.get('processed',0) or auxiliary_processed else 'failed'
        if cfg.llm.api_key and cfg.llm.api_key in error:
            error='模型服务返回了无法安全展示的错误，请检查配置后重试。'
        _update(cfg,job_id,status=status,error=error[:240])
    except Exception:
        _update(cfg,job_id,status='failed',error='新整理未完成，请查看异常记录或缩小范围重试。')
    finally:
        _discard_profile(cfg,job_id)
    return next(item for item in jobs(cfg)['items'] if item['id']==job_id)


def get(cfg: Settings, name: str, filters: dict, identity: str = '', *,
        workbench: bool = False, trend: str = 'all') -> dict:
    engine=_engine()
    if workbench and name=='topics':return engine.topics(cfg,filters,workbench=True,trend=trend)
    if workbench and name=='topic':return engine.topic_detail(cfg,filters,identity,workbench=True)
    if name=='summary':
        result=engine.summary(cfg,filters)
        all_jobs=jobs(cfg)['items']
        return {**result,'latest_job':all_jobs[0] if all_jobs else None,'worker_ready':_worker_ready(cfg)}
    if name=='jobs':return jobs(cfg)
    functions={'topics':lambda:engine.topics(cfg,filters),
               'knowledge':lambda:engine.knowledge(cfg,filters,identity),
               'sops':lambda:engine.sops(cfg,filters,identity),
               'issue-pilot':lambda:engine.issue_pilot(cfg,filters),
               'playbooks':lambda:engine.playbooks(cfg,filters),
               'exceptions':lambda:engine.exceptions(cfg,filters),
               'defects':lambda:engine.defects(cfg,filters),
               'topic':lambda:engine.topic_detail(cfg,filters,identity),
               'event':lambda:engine.event_detail(cfg,filters,identity)}
    if name not in functions:raise ServiceError('新整理接口不存在。',404)
    result=functions[name]()
    if name=='knowledge' and identity:
        return {'item':result['knowledge'],'evidence':result.get('evidence',[])}
    return result


def _heartbeat_once(cfg: Settings) -> bool:
    try:
        with _db(cfg) as db:
            db.execute('INSERT OR REPLACE INTO voc_service_worker VALUES (1,?,?)',(os.getpid(),time.time()))
        return True
    except (sqlite3.Error,OSError):
        return False


def worker(cfg: Settings, stop: threading.Event | None = None):
    stop=stop or threading.Event()
    def heartbeat():
        while not stop.is_set():
            _heartbeat_once(cfg)
            stop.wait(3)
    thread=threading.Thread(target=heartbeat,name='voc-worker-heartbeat',daemon=True)
    thread.start()
    try:
        _prune_profiles(cfg)
        while not stop.is_set():
            with _db(cfg) as db:
                _recover(db,cfg)
                row=db.execute("SELECT id FROM voc_service_job WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
            if row:
                try:execute(cfg,row['id'])
                except ServiceError:pass
            else:stop.wait(1)
    finally:
        stop.set();thread.join(timeout=5)


def main():
    parser=argparse.ArgumentParser()
    group=parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--job')
    group.add_argument('--worker',action='store_true')
    group.add_argument('--hourly',action='store_true')
    parser.add_argument('--limit',type=int,default=100)
    args=parser.parse_args()
    if args.worker:
        worker(load_settings());return 0
    if args.hourly:
        cfg=load_settings()
        if busy(cfg) or _legacy_busy(cfg):
            print(json.dumps({'status':'skipped','reason':'model_busy'}));return 0
        check=_engine().check(cfg,{})
        if check.get('coverage',{}).get('pending_pairs',1)==0 and not check.get('needs_compile'):
            analysis_pending=False
            if cfg.runtime.voc_domain=='zhihuige':
                from . import voc_episode_live, voc_solution_store
                analysis_pending=voc_episode_live.pending(cfg) or voc_solution_store.pending(cfg)
            if not analysis_pending:
                print(json.dumps({'status':'skipped','reason':'no_changes'}));return 0
        result=start(cfg,{}, {'limit':min(100,max(1,args.limit)),'max_calls':min(100,max(1,args.limit)),'max_tokens':2000000})
        print(json.dumps(result,ensure_ascii=False));return 0
    job=execute(load_settings(),args.job)
    print(json.dumps(job,ensure_ascii=False))
    return int(job['status']=='failed')


if __name__=='__main__':
    raise SystemExit(main())
