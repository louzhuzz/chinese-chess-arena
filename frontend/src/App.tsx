import {useCallback,useEffect,useMemo,useRef,useState} from 'react';
import {Activity,Archive,ChevronLeft,ChevronRight,CircleStop,Download,Pencil,Play,RefreshCw,RotateCcw,Settings2,Swords,Trophy} from 'lucide-react';
import {Board,PALETTE,PieceIcon,START_FEN,cleanFen,parseFen,setSideToMove,setSquareInFen} from './board';

type Preset={id:string,name:string,connection_id:string,model:string;temperature?:number|null;max_tokens?:number;structured_output?:boolean;reasoning_effort?:'low'|'medium'|'high'|'max'|null;thinking?:'enabled'|'disabled'|null};
type Connection={id:string,name:string,protocol:string;base_url:string;builtin_provider_id?:string|null;api_key_masked?:string|null;api_key_env?:string|null;extra_headers?:Record<string,string>};
type BuiltinProvider={id:string;name:string;protocol:string;base_url:string;api_key_env:string;auth_mode:'api_key';models:string[];model_count:number;supported:boolean;discovery:string;note:string;aliases?:string[]};
type MemoryEntry={ply:number;move:string;notation?:string|null;note:string};
type ContextInfo={count:number;last_ply?:number|null};
type AgentAction={ply:number;side:string;sequence:number;kind:'request'|'tool'|'submit'|'text_submit';name?:string|null;label:string;args?:Record<string,unknown>|null;result?:Record<string,unknown>|null;text?:string|null;duration_ms?:number|null;error?:string|null;input_tokens?:number|null;output_tokens?:number|null;total_input_tokens?:number|null;cache_read_tokens?:number|null;cache_write_tokens?:number|null;connect_ms?:number|null;first_byte_ms?:number|null;provider_request_id?:string|null;finish_reason?:string|null;request?:Record<string,unknown>|null;raw?:Record<string,unknown>|null};
type AgentMove={ply:number;side:string;attempts:number;duration_ms:number;input_tokens:number;output_tokens:number;cache_read_tokens:number;cache_write_tokens:number;requests:number;tools:number;submits:number;tool_errors:number;failed:boolean;cost_estimate:number|null;actions:AgentAction[]};
type AgentTimeline={mode:string;max_rounds:number;moves:AgentMove[];total:{requests:number;tools:number;input_tokens:number;output_tokens:number;cache_read_tokens:number;cache_write_tokens:number;duration_ms:number;cost_estimate:number|null}};
type Game={id:string,status:string;red_preset:string;black_preset:string;winner?:string|null;reason?:string|null;history?:string[];current_fen?:string;initial_fen?:string;moves?:Move[];awaiting?:string|null;legal_moves?:string[];in_check?:boolean;created_at:string;move_timeout?:number;max_plies?:number;mode?:'direct'|'agent';agent_max_rounds?:number;timeline?:AgentTimeline;private_memory?:Record<string,MemoryEntry[]>;context_messages?:Record<string,ContextInfo>};
type Move={ply:number;side:string;move?:string|null;notation?:string|null;fen_before:string;fen_after?:string|null;response_text?:string;duration_ms?:number;connect_ms?:number|null;first_byte_ms?:number|null;provider_request_id?:string|null;total_input_tokens?:number|null;cache_read_tokens?:number|null;cache_write_tokens?:number|null;cache_miss_tokens?:number|null;attempt:number;error?:string|null;note?:string|null};
type UsageRow={preset_id:string;moves:number;attempts:number;input_tokens:number|null;output_tokens:number|null;total_input_tokens:number|null;cache_read_tokens:number|null;cache_write_tokens:number|null;cache_miss_tokens:number|null;cache_hit_rate:number|null;duration_ms:number;cost_estimate:number|null};
type ModelResult={wins:number;losses:number;draws:number;games:number;score_rate:number|null};
type Benchmark={id:string;status:string;preset_a:string;preset_b:string;pairs:number;created_at:string;settings?:Record<string,unknown>;stats?:Record<string,number|null>;usage?:UsageRow[];games?:Game[];model_results?:Record<string,ModelResult>};
type Health={status:string;ruleset_id:string;arbiter:{backend:string;executable?:string|null;last_error?:string|null}};
type Analysis={fen:string;side_to_move:'red'|'black';legal_moves:string[];in_check:boolean;ended:boolean;winner:string|null;reason:string|null};

const api=async<T,>(path:string,init?:RequestInit):Promise<T>=>{const r=await fetch(path,{headers:{'Content-Type':'application/json',...(init?.headers||{})},...init});if(!r.ok)throw new Error(await r.text());return r.json()};

const REASON:Record<string,string>={checkmate:'将死',stalemate:'困毙',repetition_draw:'重复局面和棋',natural_limit_draw:'自然限着和棋',insufficient_material:'子力不足和棋',perpetual_check:'长将判负',perpetual_chase:'长捉判负',invalid_move:'非法着法判负',timeout:'超时判负',api_failure:'接口故障',max_plies:'步数上限截断',user_stopped:'手动停止',process_restart:'进程重启中断'};

// 一手失败时的原因码：网页上要能直接读出「输出预算用尽」还是「轮数用尽」，而不是只看到一个英文码。
const MOVE_ERROR:Record<string,string>={'agent_no_submit':'整手没有正式落子','agent_rounds_exhausted':'每手请求轮数用尽，仍未落子','agent_output_limit':'整手输出预算用尽（被 max_tokens 截断）','request_timeout':'本次请求超时','api_error':'接口故障','invalid_move':'非法着法','timeout':'超时'};
function errorText(code:string){const key=Object.keys(MOVE_ERROR).find(item=>code.startsWith(item));return key?`${MOVE_ERROR[key]}（${code}）`:code}

// 每步总时限的常用档位；后端默认 600 秒，首答可用的时间是「时限 - 最多 30 秒纠错余量」。
const TIMEOUT_PRESETS:[number,string][]=[[120,'2 分钟'],[300,'5 分钟'],[600,'10 分钟'],[1800,'30 分钟']];

export function App(){
 const [tab,setTab]=useState<'arena'|'bench'|'history'|'settings'>('arena'),[presets,setPresets]=useState<Preset[]>([]),[connections,setConnections]=useState<Connection[]>([]),[providerCatalog,setProviderCatalog]=useState<BuiltinProvider[]>([]),[games,setGames]=useState<Game[]>([]),[benchmarks,setBenchmarks]=useState<Benchmark[]>([]),[report,setReport]=useState<Benchmark|null>(null),[health,setHealth]=useState<Health|null>(null),[active,setActive]=useState<Game|null>(null),[red,setRed]=useState(''),[black,setBlack]=useState(''),[redEffort,setRedEffort]=useState('default'),[blackEffort,setBlackEffort]=useState('default'),[replay,setReplay]=useState(0),[pairs,setPairs]=useState(5),[moveTimeout,setMoveTimeout]=useState(600),[maxPlies,setMaxPlies]=useState(400),[notice,setNotice]=useState('');
 const [mode,setMode]=useState<'direct'|'agent'>('direct'),[agentRounds,setAgentRounds]=useState(6);
 const [selected,setSelected]=useState<string|null>(null),[busy,setBusy]=useState(false);
 const [editor,setEditor]=useState(false),[editFen,setEditFen]=useState(cleanFen(START_FEN)),[editPiece,setEditPiece]=useState<string>('R'),[editNote,setEditNote]=useState('');
 const load=useCallback(async()=>{
   const [c,g,b,h]=await Promise.all([api<{presets:Preset[];connections:Connection[];builtin_providers?:BuiltinProvider[]}>('/api/config'),api<Game[]>('/api/games'),api<Benchmark[]>('/api/benchmarks'),api<Health>('/api/health')]);
   setPresets(c.presets);setConnections(c.connections);setProviderCatalog(c.builtin_providers||[]);setGames(g);setBenchmarks(b);setHealth(h);
   setRed(current=>current||c.presets[0]?.id||'');
   setBlack(current=>current||(c.presets[1]||c.presets[0])?.id||'');
 },[]);
 useEffect(()=>{load().catch(e=>setNotice(e.message))},[load]);
 const open=useCallback(async(id:string)=>{const g=await api<Game>('/api/games/'+id);setActive(g);setSelected(null);setEditor(false);setReplay((g.history||[]).length);setTab('arena')},[]);
 const openReport=async(id:string)=>{const r=await api<Benchmark>('/api/benchmarks/'+id);setReport(r);setTab('bench')};
 const bootstrapped=useRef(false);
 useEffect(()=>{if(bootstrapped.current||!games.length)return;bootstrapped.current=true;open(games[0].id).catch(()=>{})},[games,open]);
 useEffect(()=>{if(!active||!['queued','running'].includes(active.status))return;const s=new EventSource(`/api/games/${active.id}/events`);s.onmessage=()=>open(active.id);return()=>s.close()},[active?.id,active?.status,open]);
 useEffect(()=>{if(!report||!['queued','running'].includes(report.status))return;const timer=setInterval(()=>api<Benchmark>('/api/benchmarks/'+report.id).then(setReport).catch(()=>{}),1200);return()=>clearInterval(timer)},[report?.id,report?.status]);

 const protocolOf=useCallback((presetId:string)=>connections.find(c=>c.id===presets.find(p=>p.id===presetId)?.connection_id)?.protocol,[connections,presets]);
 const isHuman=useCallback((presetId:string)=>protocolOf(presetId)==='human',[protocolOf]);
 const humanSide=(game:Game|null,side:string)=>!!game&&isHuman(side==='red'?game.red_preset:game.black_preset);

 const gameSettings={move_timeout_seconds:moveTimeout,max_plies:maxPlies,red_reasoning_effort:redEffort,black_reasoning_effort:blackEffort,mode,agent_max_rounds:agentRounds};
 const start=async()=>{try{const g=await api<{id:string}>('/api/games',{method:'POST',body:JSON.stringify({red_preset_id:red,black_preset_id:black,...gameSettings})});await open(g.id)}catch(e){setNotice(String(e))}};
 const startBench=async()=>{try{const b=await api<{id:string}>('/api/benchmarks',{method:'POST',body:JSON.stringify({preset_a_id:red,preset_b_id:black,pairs,move_timeout_seconds:moveTimeout,max_plies:maxPlies,preset_a_reasoning_effort:redEffort,preset_b_reasoning_effort:blackEffort,mode,agent_max_rounds:agentRounds})});setNotice(`评测 ${b.id.slice(0,8)} 已排队，共 ${pairs*2} 局`);await openReport(b.id)}catch(e){setNotice(String(e))}};
 const startFromEditor=async()=>{try{const g=await api<{id:string}>('/api/games',{method:'POST',body:JSON.stringify({red_preset_id:red,black_preset_id:black,initial_fen:cleanFen(editFen),...gameSettings})});await open(g.id)}catch(e){setNotice(String(e))}};
  // 以当前显示的盘面（含回放位置）另开一局：沿用这局的红黑模型、时限与对弈模式，行棋方由 FEN 决定。
  const continueFromBoard=async()=>{try{const g=await api<{id:string}>('/api/games',{method:'POST',body:JSON.stringify({red_preset_id:active?.red_preset||red,black_preset_id:active?.black_preset||black,initial_fen:cleanFen(fen??START_FEN),move_timeout_seconds:active?.move_timeout??moveTimeout,max_plies:active?.max_plies??maxPlies,mode:active?.mode??mode,agent_max_rounds:active?.agent_max_rounds??agentRounds,red_reasoning_effort:redEffort,black_reasoning_effort:blackEffort})});await open(g.id)}catch(e){setNotice(String(e))}};
 const readFen=async(text:string)=>{try{const r=await api<Analysis>('/api/analyze',{method:'POST',body:JSON.stringify({fen:text})});setEditFen(cleanFen(r.fen));setEditNote(`合法着法 ${r.legal_moves.length} 步`)}catch(e){setEditNote(String(e))}};

 const played=useMemo(()=>(active?.moves||[]).filter(m=>m.move&&!m.error&&m.fen_after),[active]);
 const fen=useMemo(()=>{if(!active)return undefined;if(replay===0)return active.initial_fen;return played[Math.min(replay,played.length)-1]?.fen_after||active.current_fen},[active,replay,played]);
 const lastMoveRecord=useMemo(()=>replay>0?played[Math.min(replay,played.length)-1]??null:null,[replay,played]);
 const lastMove=lastMoveRecord?.move??null;
 const lastMoveNotation=lastMoveRecord?.notation??lastMove;
 const sideToMove=useMemo(()=>{const f=(editor?editFen:fen)||START_FEN;return f.split(' ')[1]==='w'?'red':'black'},[fen,editor,editFen]);
 const boardPieces=useMemo(()=>parseFen(editor?editFen:(fen??START_FEN)),[editor,editFen,fen]);
 const canPlay=!!active&&active.status==='running'&&active.awaiting===sideToMove&&humanSide(active,sideToMove);
 const targets=useMemo(()=>{
   if(!selected||!active?.legal_moves)return [];
   return active.legal_moves.filter(move=>move.startsWith(selected)).map(move=>move.slice(2));
 },[selected,active?.legal_moves]);
 const submit=async(move:string)=>{
   if(!active)return;
   setBusy(true);
   try{await api(`/api/games/${active.id}/move`,{method:'POST',body:JSON.stringify({move,expected_ply:(active.history||[]).length,expected_side:sideToMove})});setSelected(null)}
   catch(e){setNotice(String(e))}
   finally{setBusy(false)}
 };
 const onSquare=(square:string)=>{
   if(editor){setEditFen(current=>setSquareInFen(current,square,editPiece==='erase'?null:editPiece));setEditNote('');return}
   if(!canPlay||busy)return;
   if(selected&&targets.includes(square)){submit(selected+square);return}
   const piece=boardPieces.get(square);
   const side=piece?(piece===piece.toUpperCase()?'red':'black'):null;
   setSelected(side===sideToMove?square:null);
 };
 const statusText=editor?'摆棋模式：点棋盘落子，再点同一格清除':canPlay?`轮到你走（${sideToMove==='red'?'红方':'黑方'}）· 点棋子看可走位置`:(active?.status==='running'?'等待模型行棋…':active?.status==='truncated'?'已到步数上限':'');
 const failureNote=useMemo(()=>{
   if(!active||!active.reason)return '';
   const failed=(active.moves||[]).filter(m=>m.error);
   if(!failed.length)return '';
   const fatal=['api_failure','invalid_move','timeout','arbiter_failure'].some(prefix=>active.reason?.startsWith(prefix));
   return fatal?errorText(failed[failed.length-1].error||''):'';
 },[active]);

 const judge=health?.arbiter?.backend||'未检测';
 return <div className="shell"><header><div className="brand"><span className="seal">弈</span><div><b>中国象棋擂台</b><small>CHINESE CHESS ARENA</small></div></div><nav><button aria-label="对弈台" title="对弈台" className={tab==='arena'?'on':''} onClick={()=>setTab('arena')}><Swords/><span>对弈台</span></button><button aria-label="评测" title="评测" className={tab==='bench'?'on':''} onClick={()=>setTab('bench')}><Trophy/><span>评测</span></button><button aria-label="棋谱库" title="棋谱库" className={tab==='history'?'on':''} onClick={()=>setTab('history')}><Archive/><span>棋谱库</span></button><button aria-label="模型" title="模型" className={tab==='settings'?'on':''} onClick={()=>setTab('settings')}><Settings2/><span>模型</span></button></nav><span className="engine"><i/> {judge.toUpperCase()}</span></header>
 {notice&&<div className="notice" onClick={()=>setNotice('')}>{notice}</div>}
 {tab==='arena'&&<main className="arena">
  {editor?<aside className="match-card">
    <p className="eyebrow">POSITION EDITOR</p><h1>自由摆棋</h1>
    <p className="editor-hint">点棋盘放子，再点同一格清除。双方各需一个将/帅才能开局。</p>
    <div className="palette">{PALETTE.map(piece=><button key={piece} className={editPiece===piece?'on':''} title={`${GLYPH_NAME[piece]}`} onClick={()=>setEditPiece(piece)}><PieceIcon piece={piece}/></button>)}<button className={editPiece==='erase'?'on erase':''} onClick={()=>setEditPiece('erase')}>清</button></div>
    <label>行棋方<select value={editFen.split(' ')[1]} onChange={e=>setEditFen(setSideToMove(editFen,e.target.value as 'w'|'b'))}><option value="w">红先</option><option value="b">黑先</option></select></label>
    <label>FEN<input value={editFen} onChange={e=>setEditFen(e.target.value)} onBlur={()=>readFen(editFen)}/></label>
    {editNote&&<small className="editor-note">{editNote}</small>}
    <div className="editor-actions"><button onClick={()=>{setEditFen(cleanFen(START_FEN));setEditNote('')}}>标准开局</button><button onClick={()=>{setEditFen(cleanFen('9/9/9/9/9/9/9/9/9/9 w - - 0 1'));setEditNote('')}}>清空棋盘</button></div>
    <button className="primary" onClick={startFromEditor}><Play/>以此开局</button>
    <button className="secondary" onClick={()=>setEditor(false)}>返回对局</button>
  </aside>:<aside className="match-card">
    <p className="eyebrow">NEW MATCH</p><h1>两军对垒</h1>
    <label>执红模型<select value={red} onChange={e=>setRed(e.target.value)}>{presets.map(p=><option key={p.id} value={p.id}>{p.name} · {p.model}</option>)}</select></label>
    <EffortSelect label="红方思考强度" value={redEffort} onChange={setRedEffort}/>
    <div className="versus"><span/><b>VS</b><span/></div>
    <label>执黑模型<select value={black} onChange={e=>setBlack(e.target.value)}>{presets.map(p=><option key={p.id} value={p.id}>{p.name} · {p.model}</option>)}</select></label>
    <EffortSelect label="黑方思考强度" value={blackEffort} onChange={setBlackEffort}/>
    <div className="limits"><label>每步时限（秒）<input type="number" min="5" max="3600" value={moveTimeout} onChange={e=>setMoveTimeout(+e.target.value)}/></label><label>最大单方着数<input type="number" min="1" max="2000" value={maxPlies} onChange={e=>setMaxPlies(+e.target.value)}/></label></div>
    <div className="quick">{TIMEOUT_PRESETS.map(([seconds,label])=><button key={seconds} className={moveTimeout===seconds?'on':''} onClick={()=>setMoveTimeout(seconds)}>{label}</button>)}</div>
    <div className="mode-pick"><span>对弈模式</span><div><button className={mode==='direct'?'on':''} onClick={()=>setMode('direct')} title="每次请求直接提交一步，规则事实由提示词给出">一次定一步</button><button className={mode==='agent'?'on':''} onClick={()=>setMode('agent')} title="一手之内可以连续调用规则工具：查询合法着法、试走变化、校验并修正，直到正式提交">规则工具</button></div>{mode==='agent'&&<label className="rounds">每手最多请求<input type="number" min="1" max="12" value={agentRounds} onChange={e=>setAgentRounds(+e.target.value)}/>次</label>}</div>
    <button className="primary" onClick={start}><Play/>开始一局</button>
    <button className="secondary" onClick={()=>{setEditFen(cleanFen(fen??START_FEN));setEditor(true)}}><Pencil/>自由摆棋</button>
    <div className="batch"><input type="number" min="1" max="100" value={pairs} onChange={e=>setPairs(+e.target.value)}/><span>对交换先后</span><button onClick={startBench}><Trophy/>批量评测</button></div>
  </aside>}
  <section className="board-panel"><div className={`player black${!editor&&sideToMove==='black'?' turn':''}`}><span className="disc">黑</span><div><small>BLACK</small><b>{active&&!editor?name(active.black_preset,presets):'等待开局'}</b></div><em>{editor?'摆棋':active?.status||'IDLE'}</em></div><Board fen={editor?editFen:(fen??START_FEN)} lastMove={editor?null:lastMove} selected={editor?null:selected} targets={editor?[]:targets} onSquare={editor||canPlay?onSquare:undefined}/><div className="board-status">{statusText}</div><div className={`player red${!editor&&sideToMove==='red'?' turn':''}`}><span className="disc">红</span><div><small>RED</small><b>{active&&!editor?name(active.red_preset,presets):'等待开局'}</b></div><div className="player-actions">{active&&!editor&&<button className="resume" onClick={continueFromBoard} title="以当前显示的盘面另开一局，沿用这局的红黑模型与时限"><RotateCcw/>从当前盘面继续</button>}{active?.status==='running'&&!editor?<button className="stop" onClick={()=>api(`/api/games/${active.id}/stop`,{method:'POST'}).then(()=>open(active.id))}><CircleStop/>停止</button>:<em>{editor?'摆棋模式':lastMoveNotation?`上一手 ${lastMoveNotation}`:'开局局面'}</em>}</div></div></section>
  <aside className="record"><div className="record-head"><div><p className="eyebrow">LIVE RECORD</p><h2>行棋记录</h2></div><div className="record-actions">{active&&<a className="export" href={`/api/games/${active.id}/export.fen`} download title="导出 FEN 棋谱（标准格式）"><Download/>FEN 棋谱</a>}{active?.timeline&&<a className="export" href={`/api/games/${active.id}/timeline.txt`} download title="导出智能体行动回放（每手的请求、工具调用与费用）"><Download/>回放</a>}<button onClick={()=>active&&open(active.id)}><RefreshCw/></button></div></div>{!active?<Empty/>:<><div className="result"><b>{resultText(active)}</b><small>{reasonText(active)||`第 ${(active.history||[]).length} 手`}</small>{failureNote&&<small className="failure">{failureNote}</small>}</div><div className="moves">{(active.moves||[]).filter(m=>m.move||m.error).map(m=><button key={`${m.ply}-${m.attempt}`} title={m.error||m.response_text||''} className={`${m.error?'bad':''}${replay===m.ply+1&&!m.error?' on':''}`} onClick={()=>setReplay(Math.min(m.ply+1,(active.history||[]).length))}><span>{m.ply+1}</span><b>{m.notation||m.move||'异常'}</b><small>{m.move?`${m.move} · `:''}{m.duration_ms} ms {m.attempt>1?'· 纠错':''}</small><MoveChain timeline={active.timeline} ply={m.ply}/>{m.note&&<i>{m.note}</i>}{(m.connect_ms!=null||m.first_byte_ms!=null||m.provider_request_id)&&<i className="request-meta">连接 {metric(m.connect_ms)} · 首字节 {metric(m.first_byte_ms)}{m.provider_request_id&&` · ID ${m.provider_request_id}`}</i>}{(m.total_input_tokens!=null||m.cache_read_tokens!=null)&&<i className="request-meta">缓存命中 {m.cache_read_tokens??'—'} / {m.total_input_tokens??'—'} token · {cacheRate(m.cache_read_tokens,m.total_input_tokens)}</i>}</button>)}</div>{active.timeline&&<AgentTrace timeline={active.timeline} sideNames={side=>side==='red'?'红':'黑'}/>}<div className="replay"><button onClick={()=>setReplay(Math.max(0,replay-1))}><ChevronLeft/></button><span>{replay} / {(active.history||[]).length}</span><button onClick={()=>setReplay(Math.min((active.history||[]).length,replay+1))}><ChevronRight/></button></div><details><summary>当前棋子位置表</summary><div className="piece-table">{[...boardPieces.entries()].sort().map(([square,piece])=><code key={square}>{square} {piece}</code>)}</div></details><details><summary>双方私有记忆</summary>{(['red','black'] as const).map(side=><section className="memory" key={side}><b>{side==='red'?'红方':'黑方'}</b>{(active.private_memory?.[side]||[]).map(x=><p key={x.ply}>{x.ply+1}. {x.notation||x.move} · {x.note}</p>)}{!(active.private_memory?.[side]||[]).length&&<p>暂无笔记</p>}</section>)}</details><details><summary>请求与归档</summary>{(['red','black'] as const).map(side=><section className="memory" key={side}><b>{side==='red'?'红方':'黑方'}</b><p>{active.context_messages?.[side]?.count||0} 条已归档消息</p><p>每次只发送当前全盘、最近 8 步与本方最新短笔记；归档消息不重复发送。</p></section>)}</details></>}</aside></main>}
 {tab==='bench'&&<Benchmarks report={report} benchmarks={benchmarks} presets={presets} onOpen={openReport} onGame={open}/>}
 {tab==='history'&&<main className="library"><div className="title"><p className="eyebrow">ARCHIVE</p><h1>棋谱库</h1></div><div className="game-list">{games.map(g=><button key={g.id} onClick={()=>open(g.id)}><span className={`status ${g.status}`}/><b>{name(g.red_preset,presets)}</b><i>对</i><b>{name(g.black_preset,presets)}</b><em>{resultText(g)}</em><small>{new Date(g.created_at).toLocaleString()}</small></button>)}</div></main>}
 {tab==='settings'&&<Settings presets={presets} connections={connections} providerCatalog={providerCatalog} onSaved={load} notify={setNotice}/>}<footer>规则裁判 <b>{judge}</b><span>坐标 a0—i9 · 红方视角</span></footer></div>
}
const GLYPH_NAME:Record<string,string>={R:'車 车',N:'馬 马',B:'相',A:'仕',K:'帥 帅',C:'炮',P:'兵',r:'車 车',n:'馬 马',b:'象',a:'士',k:'將 将',c:'砲 炮',p:'卒'};
function name(id:string,p:Preset[]){return p.find(x=>x.id===id)?.name||id}
function metric(value:number|null|undefined){return value==null?'—':`${value} ms`}
function cacheRate(read:number|null|undefined,total:number|null|undefined){return read==null||total==null||!total?'—':`${(read/total*100).toFixed(1)}%`}
function token(value:number|null|undefined){return value==null?'未知':String(value)}
function costOf(value:number|null|undefined){return value==null?'未知':value.toFixed(4)}
function agentMoveOf(timeline:AgentTimeline|undefined,ply:number){return timeline?.moves.find(move=>move.ply===ply)}
function MoveChain({timeline,ply}:{timeline?:AgentTimeline;ply:number}){const move=agentMoveOf(timeline,ply);const chain=move?chainOf(move):'';return chain?<i className="chain">{chain}</i>:null}
// 一步的工具调用摘要：读起来是「查询合法走法 → 试走变化 → 正式落子 a0a1」。
function chainOf(move:AgentMove){return move.actions.filter(a=>a.kind==='tool'||a.kind==='submit'||a.kind==='text_submit').map(a=>a.kind==='tool'?a.label:`${a.label} ${String(a.args?.move??'')}`.trim()).join(' → ')}
function briefOf(action:AgentAction){
 const result=(action.result||{}) as Record<string,unknown>;
 if(action.kind==='tool'){
  if(action.name==='get_legal_moves')return `${result.count??'?'} 个合法着法`;
  if(action.name==='check_move')return result.legal?'合法':`非法：${String(result.reason??'')}`;
  if(action.name==='simulate_line'){const steps=(result.steps||[]) as {index:number;legal:boolean;reason?:string}[];const bad=steps.find(s=>!s.legal);const tail=result.ended?`，终局：${String(result.reason??'')}`:'';return bad?`${steps.length} 步变化，第 ${bad.index+1} 步非法${tail}`:`${steps.length} 步变化全部合法${tail}`}
  if(action.name==='write_note')return '已替换为本手笔记';
  if(action.name==='read_note')return result.private_note?'读到本方笔记':'无笔记';
  if(action.name==='get_history')return `${((result.moves||[]) as string[]).length} 个半回合`;
  if(action.name==='get_position')return `手数 ${result.ply}，行棋方 ${result.side_to_move}`;
  if(action.name==='submit_move')return result.accepted?`接受 ${result.move}`:`拒绝：${String(result.reason??'')}`;
  return action.error||'';
 }
 return `接受 ${String(result.move??'')}`;
}
// 智能体游戏才有的行动回放：每个手数一条链条，展开能看每次工具调用的输入、返回、耗时与费用。
function AgentTrace({timeline,sideNames}:{timeline:AgentTimeline;sideNames:(side:string)=>string}){
 return <details className="agent-trace"><summary>智能体行动回放 · 请求 {timeline.total.requests} 次 · 工具 {timeline.total.tools} 次 · 输入 {token(timeline.total.input_tokens)} token（缓存读 {token(timeline.total.cache_read_tokens)} / 写 {token(timeline.total.cache_write_tokens)}）· 输出 {token(timeline.total.output_tokens)} token · 估算费用 {costOf(timeline.total.cost_estimate)}</summary>
  <div className="trace-list">{timeline.moves.map(move=><article key={`${move.ply}-${move.side}`} className={move.failed?'bad':''}>
   <header><b>{move.ply+1}</b><span>{sideNames(move.side)}方</span><em>{move.requests} 次请求 · {move.tools} 次工具{move.tool_errors?` · ${move.tool_errors} 次被拒`:''} · 输入 {token(move.input_tokens)}（缓存读 {token(move.cache_read_tokens)}）· 输出 {token(move.output_tokens)} · {(move.duration_ms/1000).toFixed(1)} s · 估算费用 {costOf(move.cost_estimate)}</em></header>
   {chainOf(move)&&<p className="chain">{chainOf(move)}</p>}
   <ul>{move.actions.map(action=><li key={action.sequence} className={action.error?'bad':''}>
    <span className="kind">{action.label}</span>
    {action.kind==='request'
     ?<span className="detail">第 {String(action.args?.round??'?')} 轮 · 可用工具 {((action.args?.tools||[]) as string[]).length} 个 · max_tokens {token(action.request?.max_tokens as number??action.args?.max_tokens as number)} · 时限 {token(action.request?.timeout_seconds as number??action.args?.timeout_seconds as number)} s · in {token(action.total_input_tokens??action.input_tokens)} / out {token(action.output_tokens)} · {metric(action.duration_ms)}{action.connect_ms!=null?` · 连接 ${metric(action.connect_ms)}`:''}{action.first_byte_ms!=null?` · 首字节 ${metric(action.first_byte_ms)}`:''}{action.finish_reason?` · 结束原因 ${action.finish_reason}`:''}{action.error?` · ${errorText(action.error)}`:''}</span>
     :<span className="detail">{action.args&&Object.keys(action.args).length?`${JSON.stringify(action.args)} → `:''}{briefOf(action)}</span>}
    {action.provider_request_id&&<i>ID {action.provider_request_id}</i>}
   </li>)}</ul>
  </article>)}</div>
 </details>;
}
function EffortSelect({label,value,onChange}:{label:string;value:string;onChange:(value:string)=>void}){
 return <label className="effort-select">{label}<select value={value} onChange={e=>onChange(e.target.value)}><option value="default">提供方默认</option><option value="none">关闭思考</option><option value="low">低</option><option value="medium">中</option><option value="high">高</option><option value="max">最高</option></select></label>
}
function reasonText(g:Game){return g.reason?REASON[g.reason]||g.reason:''}
function resultText(g:Game){if(g.status==='running'||g.status==='queued')return '对局进行中';if(g.winner)return `${g.winner==='red'?'红':'黑'}方胜`;if(g.reason&&REASON[g.reason]?.endsWith('和棋'))return REASON[g.reason];return ({truncated:'已截断',stopped:'已停止',interrupted:'已中断',aborted:'已中止'} as Record<string,string>)[g.status]||'和棋'}
function Empty(){return <div className="empty"><Activity/><b>尚无对局</b><span>选择两个模型，开始第一盘。</span></div>}

function Benchmarks({report,benchmarks,presets,onOpen,onGame}:{report:Benchmark|null;benchmarks:Benchmark[];presets:Preset[];onOpen:(id:string)=>void;onGame:(id:string)=>void}){
 if(!report)return <main className="library"><div className="title"><p className="eyebrow">BENCHMARK</p><h1>批量评测</h1><p>在对弈台选择两个模型与对数，评测会交换先后各下一局。</p></div><div className="game-list">{benchmarks.map(b=><button key={b.id} onClick={()=>onOpen(b.id)}><span className={`status ${b.status}`}/><b>{name(b.preset_a,presets)}</b><i>对</i><b>{name(b.preset_b,presets)}</b><em>{b.pairs} 对 · {b.status}</em><small>{new Date(b.created_at).toLocaleString()}</small></button>)}</div></main>;
 const stats=report.stats||{};
 return <main className="library"><div className="title"><p className="eyebrow">REPORT {report.id.slice(0,8).toUpperCase()}</p><h1>{name(report.preset_a,presets)} 对 {name(report.preset_b,presets)}</h1><p>{report.pairs} 对交换先后 · {report.status} · {report.settings?.mode==='agent'?`规则工具模式（每手最多 ${report.settings?.agent_max_rounds??6} 轮请求）`:'一次定一步'}</p></div>
  <div className="stats-grid">
   {[['总局数',stats.games],['计入胜负',stats.decided],['红胜',stats.red_wins],['黑胜',stats.black_wins],['和棋',stats.draws],['技术判负',stats.forfeit_wins],['接口故障',stats.api_failures],['截断',stats.truncated]].map(([label,value])=><div className="stat" key={String(label)}><b>{value??'—'}</b><span>{label}</span></div>)}
  </div>
 <div className="stats-grid">
   {[['首答合法率',rate(stats.first_answer_legal_rate)],['纠错成功率',rate(stats.correction_success_rate)],['接口故障率',rate(stats.api_failure_rate)],['缓存命中率',rate(stats.cache_hit_rate)],['平均每步',stats.average_move_ms?`${stats.average_move_ms} ms`:'—'],['平均每局',stats.average_game_ms?`${stats.average_game_ms} ms`:'—'],['输入 token',stats.input_tokens??'未知'],['缓存读取 token',stats.cache_read_tokens??'未知'],['输出 token',stats.output_tokens??'未知']].map(([label,value])=><div className="stat" key={String(label)}><b>{value??'—'}</b><span>{label}</span></div>)}
  </div>
  <div className="model-results">{[report.preset_a,report.preset_b].map(id=>{const r=report.model_results?.[id];return <article key={id}><b>{name(id,presets)}</b><span>{r?`${r.wins} 胜 ${r.losses} 负 ${r.draws} 和`:'—'}</span><strong>{rate(r?.score_rate)}</strong></article>})}</div>
  <table className="usage"><thead><tr><th>模型</th><th>走子</th><th>请求</th><th>输入 token</th><th>缓存命中</th><th>输出 token</th><th>耗时</th><th>估算费用</th></tr></thead><tbody>{(report.usage||[]).map(row=><tr key={row.preset_id}><td>{name(row.preset_id,presets)}</td><td>{row.moves}</td><td>{row.attempts}</td><td>{row.total_input_tokens??row.input_tokens??'未知'}</td><td>{row.cache_read_tokens==null?'未知':`${row.cache_read_tokens} · ${cacheRate(row.cache_read_tokens,row.total_input_tokens)}`}</td><td>{row.output_tokens??'未知'}</td><td>{(row.duration_ms/1000).toFixed(1)} s</td><td>{row.cost_estimate==null?'未知':row.cost_estimate.toFixed(4)}</td></tr>)}</tbody></table>
  <div className="game-list">{(report.games||[]).map(g=><button key={g.id} onClick={()=>onGame(g.id)}><span className={`status ${g.status}`}/><b>{name(g.red_preset,presets)}</b><i>对</i><b>{name(g.black_preset,presets)}</b><em>{resultText(g)}{reasonText(g)?` · ${reasonText(g)}`:''}</em><small>{new Date(g.created_at).toLocaleString()}</small></button>)}</div>
 </main>;
}
function rate(value:number|null|undefined){return value==null?'—':`${(value*100).toFixed(1)}%`}

function Settings({presets,connections,providerCatalog,onSaved,notify}:{presets:Preset[];connections:Connection[];providerCatalog:BuiltinProvider[];onSaved:()=>void;notify:(text:string)=>void}){
 const [mode,setMode]=useState<'builtin'|'custom'|null>(null);
 const supportedProviders=providerCatalog.filter(x=>x.supported&&x.protocol&&x.base_url);
 const [providerId,setProviderId]=useState('deepseek');
 const [providerKey,setProviderKey]=useState('');
 const [busyProvider,setBusyProvider]=useState(false);
 const [c,setC]=useState({id:'my-provider',name:'自定义提供方',protocol:'openai_chat',base_url:'',builtin_provider_id:'',api_key:'',api_key_env:'',extra_headers:{}});
 const [p,setP]=useState({id:'my-model',name:'我的模型',connection_id:connections[0]?.id||'',model:'',temperature:.2,max_tokens:4096,structured_output:false});
 useEffect(()=>{if(!supportedProviders.some(x=>x.id===providerId)&&supportedProviders[0])setProviderId(supportedProviders[0].id)},[providerCatalog]);
 const syncProvider=async(id:string,refresh=false)=>{const r=await api<{count:number;source?:string}>(`/api/connections/${id}/sync-models${refresh?'?refresh=true':''}`,{method:'POST'});await onSaved();notify(`${r.source==='catalog'?'已从内置模型目录':'已从提供方端点'}同步 ${r.count} 个模型`)};
 const addProvider=async()=>{const template=supportedProviders.find(x=>x.id===providerId);if(!template||!providerKey.trim()){notify('请选择提供方并填写 API Key');return}setBusyProvider(true);try{await api(`/api/connections/${template.id}`,{method:'PUT',body:JSON.stringify({id:template.id,name:template.name,protocol:template.protocol,base_url:template.base_url,builtin_provider_id:template.id,api_key:providerKey,api_key_env:template.api_key_env,extra_headers:{}})});await syncProvider(template.id);setProviderKey('');setMode(null)}catch(e){notify(`添加失败：${String(e)}`)}finally{setBusyProvider(false)}};
 const saveCustom=async()=>{try{await api(`/api/connections/${c.id}`,{method:'PUT',body:JSON.stringify({...c,builtin_provider_id:null})});await onSaved();notify('自定义提供方已保存，可点击“同步模型”自动识别模型')}catch(e){notify(String(e))}};
 const saveModel=async()=>{try{await api(`/api/presets/${p.id}`,{method:'PUT',body:JSON.stringify(p)});await onSaved();notify('模型已保存')}catch(e){notify(String(e))}};
 const editConnection=(x:Connection)=>{setC({id:x.id,name:x.name,protocol:x.protocol,base_url:x.base_url,builtin_provider_id:x.builtin_provider_id||'',api_key:'',api_key_env:x.api_key_env||'',extra_headers:{}});setMode('custom')};
 const selectedProvider=supportedProviders.find(x=>x.id===providerId);
 return <main className="settings provider-settings">
  <div className="title"><p className="eyebrow">PROVIDERS & MODELS</p><h1>模型</h1><p>提供方负责 API 地址和密钥，模型由内置兼容目录自动补充。思考强度在开始对局时分别设置。</p></div>
  <section className="provider-catalog"><div className="section-heading"><div><span>01</span><h2>提供方</h2></div><small>密钥只保存在本机后端</small></div>
   <div className="provider-list">{connections.map(x=>{const models=presets.filter(preset=>preset.connection_id===x.id);const ready=!!(x.api_key_masked||x.api_key_env||['mock','human'].includes(x.protocol));return <article className="provider-card" key={x.id}><div className="provider-mark">{x.name[0]}</div><div><b>{x.name} <i className={ready?'ready':''}/></b><small>{models.length} 个模型 · {protocolName(x.protocol)}</small></div><div className="provider-actions"><button onClick={()=>syncProvider(x.id).catch(e=>notify(`同步失败：${String(e)}`))}>同步模型</button><button onClick={()=>editConnection(x)}>编辑</button></div></article>})}</div>
   <div className="add-provider-row"><button onClick={()=>setMode(mode==='builtin'?null:'builtin')}>＋ 添加提供方</button><button onClick={()=>setMode(mode==='custom'?null:'custom')}>＋ 添加自定义提供方</button></div>
   {mode==='builtin'&&<div className="provider-wizard"><div><b>添加内置兼容提供方</b><small>只需选择提供方和 API Key；地址、协议、密钥环境变量和模型目录会自动填入。</small></div><label>提供方<select value={providerId} onChange={e=>setProviderId(e.target.value)}>{supportedProviders.map(x=><option key={x.id} value={x.id}>{x.name} · {protocolName(x.protocol)}</option>)}</select></label><label>API Key<input type="password" value={providerKey} onChange={e=>setProviderKey(e.target.value)} placeholder="输入提供方 API Key"/></label>{selectedProvider&&<div className="provider-hint"><span>默认地址：{selectedProvider.base_url}</span><span>环境变量：{selectedProvider.api_key_env}</span><span>目录示例：{selectedProvider.models.join(' · ')}</span></div>}<button className="primary" disabled={busyProvider||!selectedProvider} onClick={addProvider}>{busyProvider?'正在添加…':'添加并识别模型'}</button></div>}
   {mode==='custom'&&<div className="provider-wizard custom-provider"><div><b>自定义提供方</b><small>仅在兼容服务或私有网关时需要填写这些底层信息。</small></div><label>Provider ID<input value={c.id} onChange={e=>setC({...c,id:e.target.value})} placeholder="my-gateway"/></label><label>显示名称<input value={c.name} onChange={e=>setC({...c,name:e.target.value})}/></label><label className="wide">API 地址<input value={c.base_url} onChange={e=>setC({...c,base_url:e.target.value})} placeholder="https://gateway.example/v1"/></label><label>API 协议<select value={c.protocol} onChange={e=>setC({...c,protocol:e.target.value})}>{['openai_chat','openai_responses','anthropic_messages','mock','human'].map(x=><option key={x} value={x}>{protocolName(x)}</option>)}</select></label><label>API Key<input type="password" value={c.api_key} onChange={e=>setC({...c,api_key:e.target.value})} placeholder="留空保留已有密钥"/></label><label className="wide">密钥环境变量（可选）<input value={c.api_key_env} onChange={e=>setC({...c,api_key_env:e.target.value})} placeholder="MY_PROVIDER_API_KEY"/></label><button className="primary wide" onClick={saveCustom}>保存自定义提供方</button></div>}
  </section>
  <section className="model-catalog"><div className="section-heading"><div><span>02</span><h2>模型目录</h2></div><small>{presets.length} 个可选模型</small></div>
   <div className="model-table">{connections.map(provider=>{const models=presets.filter(model=>model.connection_id===provider.id);if(!models.length)return null;return <div className="model-group" key={provider.id}><b>{provider.name}</b><div>{models.map(model=><article key={model.id}><span>{model.name}</span><code>{model.model}</code></article>)}</div></div>})}</div>
   <details className="manual-model"><summary>模型未被识别？手动添加模型 ID</summary><div className="provider-wizard"><label>模型 ID<input value={p.model} onChange={e=>setP({...p,model:e.target.value,id:e.target.value.replace(/[^a-zA-Z0-9_-]+/g,'-').toLowerCase(),name:e.target.value})}/></label><label>所属提供方<select value={p.connection_id} onChange={e=>setP({...p,connection_id:e.target.value})}>{connections.map(x=><option key={x.id} value={x.id}>{x.name}</option>)}</select></label><label>显示名称<input value={p.name} onChange={e=>setP({...p,name:e.target.value})}/></label><label>最大输出 token<input type="number" value={p.max_tokens} onChange={e=>setP({...p,max_tokens:+e.target.value})}/></label><button className="primary" onClick={saveModel}>保存模型</button></div></details>
  </section>
 </main>
}

function protocolName(protocol:string){return ({openai_chat:'OpenAI Chat Completions',openai_responses:'OpenAI Responses',anthropic_messages:'Anthropic Messages',mock:'本地模拟',human:'网页落子'} as Record<string,string>)[protocol]||protocol}
