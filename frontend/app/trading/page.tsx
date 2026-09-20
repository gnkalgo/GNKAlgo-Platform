"use client";

import {FormEvent, useCallback, useEffect, useState} from "react";
import {AppShell} from "@/components/AppShell";
import {Empty, Notice} from "@/components/UI";
import {api} from "@/lib/api";

type Instrument={id:string;exchange:string;segment:string;trading_symbol:string;name:string|null};
type Order={id:string;client_order_id:string;instrument_id:string;mode:string;side:string;order_type:string;quantity:number;filled_quantity:number;limit_price:number|null;average_fill_price:number|null;status:string;created_at:string};
type Position={id:string;instrument_id:string;mode:string;quantity:number;average_price:number;realized_pnl:number;unrealized_pnl:number};
type TradingStatus={mode:string;live_ready:boolean;halted:boolean;halt_reason:string|null;limits:Record<string,number>};

export default function Trading(){
  const [status,setStatus]=useState<TradingStatus|null>(null); const [orders,setOrders]=useState<Order[]>([]);
  const [positions,setPositions]=useState<Position[]>([]); const [results,setResults]=useState<Instrument[]>([]);
  const [instrument,setInstrument]=useState<Instrument|null>(null); const [query,setQuery]=useState("");
  const [side,setSide]=useState("BUY"); const [orderType,setOrderType]=useState("MARKET");
  const [quantity,setQuantity]=useState("1"); const [limitPrice,setLimitPrice]=useState("");
  const [error,setError]=useState(""); const [message,setMessage]=useState(""); const [busy,setBusy]=useState(false);
  const refresh=useCallback(async()=>{try{const [nextStatus,nextOrders,nextPositions]=await Promise.all([api<TradingStatus>("/trading/status"),api<Order[]>("/trading/orders"),api<Position[]>("/trading/positions")]);setStatus(nextStatus);setOrders(nextOrders);setPositions(nextPositions)}catch(e){setError((e as Error).message)}},[]);
  useEffect(()=>{refresh();const timer=setInterval(refresh,5000);return()=>clearInterval(timer)},[refresh]);
  async function search(e:FormEvent){e.preventDefault();setError("");try{setResults(await api<Instrument[]>(`/market/instruments?query=${encodeURIComponent(query)}&limit=20`))}catch(e){setError((e as Error).message)}}
  async function submit(e:FormEvent){e.preventDefault();if(!instrument)return;setBusy(true);setError("");setMessage("");try{const body={instrument_id:instrument.id,client_order_id:`web-${Date.now()}`,side,order_type:orderType,product_type:"INTRADAY",validity:"DAY",quantity:Number(quantity),...(orderType==="LIMIT"?{limit_price:Number(limitPrice)}:{})};const result=await api<Order>("/trading/orders",{method:"POST",body:JSON.stringify(body)});setMessage(`${result.mode} order ${result.client_order_id}: ${result.status}`);await refresh()}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
  async function cancel(order:Order){setError("");try{await api(`/trading/orders/${order.id}/cancel`,{method:"POST"});await refresh()}catch(e){setError((e as Error).message)}}
  async function toggleHalt(){if(!status)return;setError("");try{await api("/trading/kill-switch",{method:"POST",body:JSON.stringify({halted:!status.halted,reason:status.halted?null:"User activated kill switch"})});await refresh()}catch(e){setError((e as Error).message)}}
  const names=new Map(results.map(item=>[item.id,item.trading_symbol]));
  return <AppShell title="Order management" kicker="PHASE 7 / EXECUTION">
    <div className="card"><div className="card-head"><div><span className="eyebrow">TRADING MODE</span><h3>{status?.mode.toUpperCase()??"LOADING"}</h3><span className="muted">Live-ready: {status?.live_ready?"yes":"no"} · Orders remain gated by server policy.</span></div><button className={`btn ${status?.halted?"":"secondary"}`} onClick={toggleHalt}>{status?.halted?"Resume trading":"Activate kill switch"}</button></div></div>
    {error&&<Notice tone="error">{error}</Notice>}{message&&<Notice>{message}</Notice>}
    <div className="card"><span className="eyebrow">NEW ORDER</span><form onSubmit={search} className="market-search"><input aria-label="Search instruments" placeholder="Search symbol" value={query} onChange={e=>setQuery(e.target.value)}/><button className="btn">Search</button></form>
      {!!results.length&&<div className="market-results">{results.map(item=><button key={item.id} type="button" onClick={()=>setInstrument(item)}><strong>{item.trading_symbol}</strong><span>{item.exchange} · {item.segment} · {item.name??"Instrument"}</span></button>)}</div>}
      <form onSubmit={submit} className="form-grid"><label>Instrument<input value={instrument?.trading_symbol??"Select from search"} readOnly/></label><label>Side<select value={side} onChange={e=>setSide(e.target.value)}><option>BUY</option><option>SELL</option></select></label><label>Order type<select value={orderType} onChange={e=>setOrderType(e.target.value)}><option>MARKET</option><option>LIMIT</option></select></label><label>Quantity<input type="number" min="1" value={quantity} onChange={e=>setQuantity(e.target.value)}/></label>{orderType==="LIMIT"&&<label>Limit price<input type="number" min="0.01" step="0.01" value={limitPrice} onChange={e=>setLimitPrice(e.target.value)} required/></label>}<button className="btn" disabled={!instrument||busy||status?.mode==="disabled"}>{busy?"Submitting…":"Submit order"}</button></form>
    </div>
    <h2 className="section-title">Orders</h2>{!orders.length?<Empty title="No orders" copy="Accepted paper and live orders appear here."/>:<div className="card market-table-wrap"><table className="table"><thead><tr><th>Created</th><th>Order</th><th>Side</th><th>Quantity</th><th>Price</th><th>Status</th><th></th></tr></thead><tbody>{orders.map(order=><tr key={order.id}><td>{new Date(order.created_at).toLocaleString()}</td><td><strong>{names.get(order.instrument_id)??order.client_order_id}</strong><br/><span className="muted">{order.mode} · {order.order_type}</span></td><td>{order.side}</td><td>{order.filled_quantity} / {order.quantity}</td><td>{order.average_fill_price??order.limit_price??"MARKET"}</td><td><span className="badge">{order.status}</span></td><td>{["PENDING","OPEN","PARTIALLY_FILLED","UNKNOWN"].includes(order.status)&&<button className="btn secondary small" onClick={()=>cancel(order)}>Cancel</button>}</td></tr>)}</tbody></table></div>}
    <h2 className="section-title">Positions</h2>{!positions.length?<Empty title="No positions" copy="Filled orders create positions."/>:<div className="card market-table-wrap"><table className="table"><thead><tr><th>Instrument</th><th>Mode</th><th>Quantity</th><th>Average</th><th>Realized P&amp;L</th><th>Unrealized P&amp;L</th></tr></thead><tbody>{positions.map(position=><tr key={position.id}><td>{names.get(position.instrument_id)??position.instrument_id}</td><td>{position.mode}</td><td>{position.quantity}</td><td>{position.average_price.toFixed(2)}</td><td>{position.realized_pnl.toFixed(2)}</td><td>{position.unrealized_pnl.toFixed(2)}</td></tr>)}</tbody></table></div>}
  </AppShell>;
}
