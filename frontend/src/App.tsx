import { useCallback, useEffect, useState } from "react";
import { api, newSessionId, PERSONAS, type AuditRecord, type Health } from "./api";
import { AuditTrail } from "./components/AuditTrail";
import { Chat, type Turn } from "./components/Chat";
import "./App.css";

export default function App() {
  const [sessionId, setSessionId] = useState(newSessionId);
  const [memberId, setMemberId] = useState(PERSONAS[0].id);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [records, setRecords] = useState<AuditRecord[]>([]);
  const [health, setHealth] = useState<Health | null>(null);

  useEffect(() => {
    api.health().then(setHealth).catch(() => setHealth(null));
  }, []);

  const refreshAudit = useCallback(() => {
    api
      .audit(sessionId)
      .then((r) => setRecords(r.records))
      .catch(() => setRecords([]));
  }, [sessionId]);

  function reset() {
    setSessionId(newSessionId());
    setTurns([]);
    setRecords([]);
  }

  // Switching cardmember mid-conversation would mix two people's turns into
  // one transcript and one audit session. Start a clean session instead.
  function switchMember(id: string) {
    setMemberId(id);
    reset();
  }

  const persona = PERSONAS.find((p) => p.id === memberId)!;

  return (
    <div className="app">
      <header>
        <div className="brand">
          <strong>American Express</strong> India
          <span className="sub">Card Servicing Agent</span>
        </div>

        <span className="spacer" />

        {health && (
          <span className="stack" title={health.target}>
            {health.engine} · sessions:{health.sessions} · splunk:
            {health.splunk ? "on" : "off"}
          </span>
        )}

        <select value={memberId} onChange={(e) => switchMember(e.target.value)}>
          {PERSONAS.map((p) => (
            <option key={p.id} value={p.id}>
              {p.label} — {p.note}
            </option>
          ))}
        </select>

        <button className="mini light" onClick={reset}>
          New session
        </button>
      </header>

      <p className="who">
        Speaking as <b>{persona.label}</b> <span className="note">{persona.note}</span>
      </p>

      <main>
        <Chat
          sessionId={sessionId}
          memberId={memberId}
          turns={turns}
          onTurns={setTurns}
          onAudit={refreshAudit}
        />
        <AuditTrail records={records} />
      </main>
    </div>
  );
}
