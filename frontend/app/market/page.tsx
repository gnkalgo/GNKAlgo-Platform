"use client";
import {FormEvent, useEffect, useRef, useState} from "react";
import {AppShell} from "@/components/AppShell";
import {Empty, Notice} from "@/components/UI";
import {api, marketWebSocketUrl} from "@/lib/api";

type Instrument={id:string;exchange:string;segment:string;trading_symbol:string;name:string|null;instrument_type:string};
type Quote={instrument_id:string;ltp:number;previous_close:number|null;bid:number|null;ask:number|null;source:string;stale:boolean};
type Ticket={ticket:string;expires_in:number};
type MarketStatus={provider:string;redis_connected:boolean;active_subscriptions:number;feed:{state:string;healthy:boolean;stale:boolean;quote_latency_ms:number|null;reconnect_count:number;decode_errors:number;last_tick_at:string|null;last_error:string|null}};

export default function MarketData(){
  const [query,setQuery]=useState(""); const [results,setResults]=useState<Instrument[]>([]);
  const [watch,setWatch]=useState<Instrument[]>([]); const [quotes,setQuotes]=useState<Record<string,Quote>>({});
  const [connected,setConnected]=useState(false); const [error,setError]=useState("");
  const [status,setStatus]=useState<MarketStatus|null>(null);
  const socket=useRef<WebSocket|null>(null); const watchRef=useRef<Instrument[]>([]);
  useEffect(()=>{watchRef.current=watch},[watch]);
  useEffect(()=>{
    let stopped=false,attempt=0; let heartbeat:ReturnType<typeof setInterval>|undefined,retry:ReturnType<typeof setTimeout>|undefined;
    const connect=async()=>{try{
      const {ticket}=await api<Ticket>("/market/ws-ticket",{method:"POST"}); if(stopped)return;
      const ws=new WebSocket(marketWebSocketUrl(ticket)); socket.current=ws;
      ws.onmessage=(event)=>{const message=JSON.parse(event.data);if(message.type==="ready"){attempt=0;setConnected(true);setError("");const ids=watchRef.current.map(x=>x.id);if(ids.length)ws.send(JSON.stringify({action:"subscribe",instrument_ids:ids,mode:"quote"}))}if(message.type==="quote"){const quote=message.data as Quote;setQuotes(old=>({...old,[quote.instrument_id]:quote}))}if(message.type==="error")setError(message.code)};
      ws.onerror=()=>setError("Market stream is unavailable.");
      ws.onclose=()=>{setConnected(false);if(heartbeat)clearInterval(heartbeat);if(!stopped){const delay=Math.min(30000,1000*2**attempt++);retry=setTimeout(connect,delay)}};
      heartbeat=setInterval(()=>{if(ws.readyState===WebSocket.OPEN)ws.send(JSON.stringify({action:"ping"}))},25000);
    }catch(e){setError((e as Error).message);if(!stopped){const delay=Math.min(30000,1000*2**attempt++);retry=setTimeout(connect,delay)}}};
    connect();return()=>{stopped=true;if(heartbeat)clearInterval(heartbeat);if(retry)clearTimeout(retry);socket.current?.close()};
  },[]);
  useEffect(()=>{let stopped=false;const refresh=async()=>{try{const value=await api<MarketStatus>("/market/status");if(!stopped)setStatus(value)}catch{/* WebSocket status remains authoritative for the browser connection. */}};refresh();const timer=setInterval(refresh,10000);return()=>{stopped=true;clearInterval(timer)}},[]);
  async function search(e:FormEvent){e.preventDefault();setError("");try{setResults(await api<Instrument[]>(`/market/instruments?query=${encodeURIComponent(query)}&limit=25`))}catch(e){setError((e as Error).message)}}
  function add(item:Instrument){if(watch.some(x=>x.id===item.id))return;setWatch(old=>[...old,item]);socket.current?.send(JSON.stringify({action:"subscribe",instrument_ids:[item.id],mode:"quote"}))}
  function remove(item:Instrument){setWatch(old=>old.filter(x=>x.id!==item.id));socket.current?.send(JSON.stringify({action:"unsubscribe",instrument_ids:[item.id]}))}
  function change(quote?:Quote){if(!quote?.previous_close)return "—";return `${(((quote.ltp-quote.previous_close)/quote.previous_close)*100).toFixed(2)}%`}
  const feedOk=connected&&(!status||status.feed.healthy);
  return <AppShell title="Market data" kicker="PHASE 6 / LIVE FEED">
    <div className="card"><div className="card-head"><div><span className="eyebrow">STREAM STATUS</span><h3>{status?.feed.stale?"Feed stale":connected?"Connected":"Connecting…"}</h3><span className="muted">{status?`${status.provider.toUpperCase()} · ${status.feed.state} · ${status.feed.quote_latency_ms===null?"no ticks":`${status.feed.quote_latency_ms.toFixed(0)} ms`} · ${status.feed.reconnect_count} reconnects`:"Loading feed health…"}</span></div><span className={`badge ${feedOk?"":"disconnected"}`}>{feedOk?"LIVE":"OFFLINE"}</span></div>
      <form className="market-search" onSubmit={search}><input aria-label="Search instruments" placeholder="Search symbol or company" value={query} onChange={e=>setQuery(e.target.value)}/><button className="btn">Search</button></form>
      {!!results.length&&<div className="market-results">{results.map(item=><button key={item.id} onClick={()=>add(item)}><strong>{item.trading_symbol}</strong><span>{item.exchange} · {item.segment} · {item.name??item.instrument_type}</span></button>)}</div>}
    </div>{error&&<Notice tone="error">{error}</Notice>}
    <h2 className="section-title">Watchlist</h2>
    {!watch.length?<Empty title="No instruments" copy="Search above and add instruments to begin streaming."/>:<div className="card market-table-wrap"><table className="table"><thead><tr><th>Instrument</th><th>LTP</th><th>Change</th><th>Bid / Ask</th><th>Source</th><th></th></tr></thead><tbody>{watch.map(item=>{const quote=quotes[item.id];return <tr key={item.id}><td><strong>{item.trading_symbol}</strong><br/><span className="muted">{item.exchange} · {item.segment}</span></td><td>{quote?quote.ltp.toLocaleString("en-IN",{minimumFractionDigits:2}):"Waiting…"}</td><td>{change(quote)}</td><td>{quote?.bid??"—"} / {quote?.ask??"—"}</td><td><span className={`badge ${quote?.stale?"error":""}`}>{quote?.source??"PENDING"}</span></td><td><button className="btn secondary small" onClick={()=>remove(item)}>Remove</button></td></tr>})}</tbody></table></div>}
  </AppShell>
}
