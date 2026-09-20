"use client";

import {useCallback, useEffect, useState} from "react";
import {AppShell} from "@/components/AppShell";
import {Empty, Notice} from "@/components/UI";
import {api} from "@/lib/api";

type Broker = {
  id: string;
  broker: "DHAN" | "FYERS" | "UPSTOX";
  status: string;
  broker_client_id: string | null;
  last_connected_at: string | null;
  last_checked_at: string | null;
};

const choices = ["DHAN", "FYERS", "UPSTOX"] as const;
const callbackErrors: Record<string, string> = {
  invalid_callback: "The broker did not return the required authorization data. Start the connection again.",
  invalid_state: "The broker connection request expired or was already used. Start the connection again.",
  authentication_failed: "The broker rejected the authorization response. Check the broker app credentials and try again.",
};

export default function Brokers() {
  const [rows, setRows] = useState<Broker[]>([]);
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const load = useCallback(() => api<Broker[]>("/brokers").then(setRows).catch(caught => setError(caught.message)), []);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const connected = params.get("connected");
    const callbackError = params.get("error");
    if (connected) setNotice(`${connected.toUpperCase()} connected successfully.`);
    if (callbackError) setError(callbackErrors[callbackError] ?? "The broker connection could not be completed.");
    if (connected || callbackError) window.history.replaceState({}, "", window.location.pathname);
    load();
  }, [load]);

  async function connect(name: string) {
    setError("");
    try {
      const result = await api<{authorization_url: string}>(`/brokers/${name}/connect`, {method: "POST"});
      window.location.href = result.authorization_url;
    } catch (caught) {
      setError((caught as Error).message);
    }
  }

  async function action(id: string, kind: "test" | "reconnect") {
    setError("");
    try {
      if (kind === "reconnect") {
        const result = await api<{authorization_url: string}>(`/brokers/${id}/reconnect`, {method: "POST"});
        window.location.href = result.authorization_url;
      } else {
        await api(`/brokers/${id}/test`, {method: "POST"});
        load();
      }
    } catch (caught) {
      setError((caught as Error).message);
    }
  }

  async function disconnect(id: string) {
    setError("");
    try {
      await api(`/brokers/${id}`, {method: "DELETE"});
      load();
    } catch (caught) {
      setError((caught as Error).message);
    }
  }

  return <AppShell title="Broker accounts" kicker="SETTINGS / CONNECTIONS">
    <div className="toolbar"><button className="btn" onClick={() => setAdding(true)}>+ Add broker</button></div>
    {notice && <Notice tone="success">{notice}</Notice>}
    {error && <Notice tone="error">{error}</Notice>}
    {!rows.length ? <Empty title="No broker connected" copy="Add Dhan, FYERS or Upstox. GnKAlgo credentials remain separate."/> :
      <div className="list">{rows.map(broker => <div className="row" key={broker.id}>
        <div className="broker-logo">{broker.broker[0]}</div>
        <div className="row-main" style={{marginRight: "auto"}}><strong>{broker.broker}</strong><span>{broker.broker_client_id ? `Client ${broker.broker_client_id} · ` : ""}Checked {broker.last_checked_at ? new Date(broker.last_checked_at).toLocaleString() : "never"}</span></div>
        <span className={`badge ${broker.status.toLowerCase()}`}>{broker.status}</span>
        <div className="actions"><button className="btn secondary small" onClick={() => action(broker.id, "test")}>Test</button><button className="btn secondary small" onClick={() => action(broker.id, "reconnect")}>Reconnect</button><button className="btn danger small" onClick={() => disconnect(broker.id)}>Disconnect</button></div>
      </div>)}</div>}
    {adding && <div className="modal" onClick={() => setAdding(false)}><div className="modal-card" onClick={event => event.stopPropagation()}>
      <h2>Connect a broker</h2><p className="muted">You’ll authenticate on the broker’s website. GnKAlgo never receives your broker password.</p>
      <div className="list">{choices.map(name => <button className="row" key={name} onClick={() => connect(name)}><span className="broker-logo">{name[0]}</span><span className="row-main"><strong>{name}</strong><span>Secure authorization flow</span></span><span>→</span></button>)}</div>
    </div></div>}
  </AppShell>;
}
