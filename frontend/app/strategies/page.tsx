"use client";

import {FormEvent, useCallback, useEffect, useState} from "react";
import {AppShell} from "@/components/AppShell";
import {Empty, Notice} from "@/components/UI";
import {api} from "@/lib/api";

type Instrument={id:string;trading_symbol:string;exchange:string;segment:string};
type Strategy={id:string;name:string;instrument_id:string;kind:string;status:string;execution_mode:string;version:number;fast_period:number;slow_period:number;quantity:number;last_error:string|null};
type Signal={id:string;action:string;status:string;reason:string|null;order_id:string|null;generated_at:string};
type Run={id:string;status:string;diagnostics_json:Record<string,string|number>;candle_start_at:string};
type TradingStatus={mode:string};

export default function Strategies(){
  const [strategies,setStrategies]=useState<Strategy[]>([]); const [mode,setMode]=useState("disabled");
  const [query,setQuery]=useState(""); const [results,setResults]=useState<Instrument[]>([]);
  const [instrument,setInstrument]=useState<Instrument|null>(null); const [name,setName]=useState("");
  const [fast,setFast]=useState("5"); const [slow,setSlow]=useState("20"); const [quantity,setQuantity]=useState("1");
  const [selected,setSelected]=useState<string|null>(null); const [signals,setSignals]=useState<Signal[]>([]);
  const [runs,setRuns]=useState<Run[]>([]); const [error,setError]=useState(""); const [notice,setNotice]=useState("");
  const refresh=useCallback(async()=>{try{const [items,status]=await Promise.all([api<Strategy[]>("/strategies"),api<TradingStatus>("/trading/status")]);setStrategies(items);setMode(status.mode)}catch(e){setError((e as Error).message)}},[]);
  const inspect=useCallback(async(id:string)=>{setSelected(id);try{const [nextSignals,nextRuns]=await Promise.all([api<Signal[]>(`/strategies/${id}/signals`),api<Run[]>(`/strategies/${id}/runs`)]);setSignals(nextSignals);setRuns(nextRuns)}catch(e){setError((e as Error).message)}},[]);
  useEffect(()=>{refresh();const timer=setInterval(refresh,10000);return()=>clearInterval(timer)},[refresh]);
  useEffect(()=>{if(!selected)return;const timer=setInterval(()=>inspect(selected),10000);return()=>clearInterval(timer)},[selected,inspect]);
  async function search(e:FormEvent){e.preventDefault();setError("");try{setResults(await api<Instrument[]>(`/market/instruments?query=${encodeURIComponent(query)}&limit=20`))}catch(e){setError((e as Error).message)}}
  async function create(e:FormEvent){e.preventDefault();if(!instrument)return;setError("");setNotice("");try{const strategy=await api<Strategy>("/strategies",{method:"POST",body:JSON.stringify({name,instrument_id:instrument.id,execution_mode:"PAPER",timeframe_seconds:60,fast_period:Number(fast),slow_period:Number(slow),quantity:Number(quantity),product_type:"INTRADAY",order_type:"MARKET"})});setNotice(`Created draft ${strategy.name}. Review before activating.`);await refresh()}catch(e){setError((e as Error).message)}}
  async function change(id:string,action:"activate"|"pause"){setError("");setNotice("");try{await api(`/strategies/${id}/${action}`,{method:"POST",...(action==="activate"?{body:JSON.stringify({})}:{})});await refresh();await inspect(id)}catch(e){setError((e as Error).message)}}
  return <AppShell title="Strategies" kicker="PHASE 8 / SIGNAL PIPELINE">
    <div className="card"><span className="eyebrow">ENGINE SAFETY</span><h3>Closed-candle SMA crossover</h3><p className="muted">Only versioned, built-in rules execute. Strategy automation has a separate server-side enable switch. New strategies remain drafts; this screen creates paper strategies only.</p><span className="badge">TRADING MODE {mode.toUpperCase()}</span></div>
    {error&&<Notice tone="error">{error}</Notice>}{notice&&<Notice>{notice}</Notice>}
    <div className="card"><span className="eyebrow">NEW PAPER STRATEGY</span><form className="market-search" onSubmit={search}><input aria-label="Search instruments" placeholder="Search instrument" value={query} onChange={e=>setQuery(e.target.value)}/><button className="btn">Search</button></form>
      {!!results.length&&<div className="market-results">{results.map(item=><button key={item.id} type="button" onClick={()=>setInstrument(item)}><strong>{item.trading_symbol}</strong><span>{item.exchange} · {item.segment}</span></button>)}</div>}
      <form className="form-grid" onSubmit={create}><label>Instrument<input readOnly value={instrument?.trading_symbol??"Select from search"}/></label><label>Name<input value={name} onChange={e=>setName(e.target.value)} maxLength={100} required/></label><label>Fast candles<input type="number" min="2" max="200" value={fast} onChange={e=>setFast(e.target.value)}/></label><label>Slow candles<input type="number" min="3" max="500" value={slow} onChange={e=>setSlow(e.target.value)}/></label><label>Quantity<input type="number" min="1" value={quantity} onChange={e=>setQuantity(e.target.value)}/></label><button className="btn" disabled={!instrument}>Create draft</button></form>
    </div>
    <h2 className="section-title">Definitions</h2>{!strategies.length?<Empty title="No strategies" copy="Create a paper strategy to evaluate signals on closed candles."/>:<div className="card market-table-wrap"><table className="table"><thead><tr><th>Strategy</th><th>Rule</th><th>Mode</th><th>Status</th><th>Actions</th></tr></thead><tbody>{strategies.map(item=><tr key={item.id}><td><strong>{item.name}</strong><br/><span className="muted">v{item.version} · {item.last_error??"No errors"}</span></td><td>SMA {item.fast_period}/{item.slow_period} · qty {item.quantity}</td><td>{item.execution_mode}</td><td><span className="badge">{item.status}</span></td><td><div className="actions"><button className="btn secondary small" onClick={()=>inspect(item.id)}>Inspect</button>{item.status==="ACTIVE"?<button className="btn secondary small" onClick={()=>change(item.id,"pause")}>Pause</button>:item.execution_mode==="PAPER"&&<button className="btn small" onClick={()=>change(item.id,"activate")}>Activate</button>}</div></td></tr>)}</tbody></table></div>}
    {selected&&<><h2 className="section-title">Recent signals</h2>{!signals.length?<Empty title="No signals" copy="A signal is recorded when a completed candle crosses the averages."/>:<div className="card market-table-wrap"><table className="table"><thead><tr><th>Time</th><th>Action</th><th>Status</th><th>Reason</th><th>Order</th></tr></thead><tbody>{signals.map(item=><tr key={item.id}><td>{new Date(item.generated_at).toLocaleString()}</td><td>{item.action}</td><td>{item.status}</td><td>{item.reason??"—"}</td><td>{item.order_id??"—"}</td></tr>)}</tbody></table></div>}
      <h2 className="section-title">Recent evaluations</h2>{!runs.length?<Empty title="No evaluations" copy="The worker records each completed-candle evaluation once."/>:<div className="card market-table-wrap"><table className="table"><thead><tr><th>Candle</th><th>Result</th><th>Details</th></tr></thead><tbody>{runs.map(item=><tr key={item.id}><td>{new Date(item.candle_start_at).toLocaleString()}</td><td>{item.status}</td><td>{JSON.stringify(item.diagnostics_json)}</td></tr>)}</tbody></table></div>}</>}
  </AppShell>;
}
